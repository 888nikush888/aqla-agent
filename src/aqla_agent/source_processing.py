"""Durable internal workflow, using the existing trading engine as sole executor."""
from __future__ import annotations

import json
import secrets
from typing import Literal

from pydantic import Field

from aqla_hyperliquid_mcp.models import AqlaError, Model, digest, now_ms
from aqla_hyperliquid_mcp.rules import latest_sources, verify_evidence

from .decisions import KINDS, VERSION, DecisionResult, build_packet, validate_interpretation
from .observations import retry_due
from .source_failure import classify_failure, safe_code, sanitize_detail
from .source_queue import queue_overview


class SourceJob(Model):
    job_id: str
    event_id: str
    fingerprint: str
    version: str = VERSION
    status: Literal["prepared", "interpreted", "applying", "waiting_preview", "retry_wait", "recovering", "review", "done", "failed", "shadow"] = "prepared"
    result: DecisionResult | None = None
    plan_ids: list[str] = Field(default_factory=list)
    created_at: int
    owner: str = ""
    processor_instance: str = ""
    policy_version: str = ""
    processing_mode: str = ""
    intent_revisions: dict[str, str] = Field(default_factory=dict)
    failure_code: str | None = None
    failure_detail: dict | None = None
    failure_history: list[dict] = Field(default_factory=list)
    attempt_count: int = Field(default=1, ge=1)
    retry_deadline_at: int | None = None
    next_retry_at: int | None = None
    content_fingerprint: str = ""
    cache_reused_from: str | None = None
    outcome: str | None = None
    review_sources: dict[str, str] = Field(default_factory=dict)


class SourceProcessor:
    def __init__(self, state, trading):
        self.state, self.trading = state, trading
        self.mode, self.owner = "legacy", None
        self.policy_version = None
        self.instance = secrets.token_hex(16)
        self.active_jobs = set()
        self.retry_max_attempts, self.retry_backoff_seconds, self.retry_max_age_seconds = 3, 15, 900
        self.decision_status = {}

    def configure(self, mode, owner, *, retry_max_attempts=3, retry_backoff_seconds=15, retry_max_age_seconds=900, decision_status=None):
        if mode not in ("legacy", "shadow", "hybrid") or not isinstance(owner, str) or not owner or len(owner) > 100:
            raise AqlaError("Invalid native source processing configuration")
        if (type(retry_max_attempts) is not int or not 1 <= retry_max_attempts <= 5
                or type(retry_backoff_seconds) is not int or not 1 <= retry_backoff_seconds <= 300
                or type(retry_max_age_seconds) is not int or not 30 <= retry_max_age_seconds <= 3600):
            raise AqlaError("Invalid bounded source retry configuration")
        status = self.decision_status
        if isinstance(decision_status, dict):
            permissions = decision_status.get("permissions", {})
            status = {"provider": "openrouter", "model": str(decision_status.get("model", ""))[:200],
                "validated": decision_status.get("validated") is True,
                "permissions": {str(source)[:100]: [kind for kind in kinds if kind in KINDS]
                    for source, kinds in list(permissions.items())[:100] if isinstance(kinds, list)} if isinstance(permissions, dict) else {}}
        # Admission ownership changes only after every proposed field is valid.
        if (mode, owner) != (self.mode, self.owner):
            self.policy_version = None
        self.mode, self.owner = mode, owner
        self.retry_max_attempts, self.retry_backoff_seconds, self.retry_max_age_seconds = retry_max_attempts, retry_backoff_seconds, retry_max_age_seconds
        self.decision_status = status
        return {"mode": mode}

    def require_owner(self, owner):
        if self.mode == "legacy" or owner != self.owner:
            raise AqlaError("Source processor ownership changed")

    def context(self, event_id, *, source_refs=()):
        context = self.trading.context(event_id, decision=True)
        snapshot = self.state.snapshot()
        chat_id = context["event"]["source"]["chat_id"]
        trade_ids = {identity for identity, trade in snapshot["trades"].items() if trade.get("chat_id") == chat_id}
        bound_refs = {ref for identity in trade_ids for ref in snapshot["trades"][identity]["plan"].get("source_refs", [])}
        bindings = [snapshot.get("bindings", {}), *snapshot.get("account_bindings", {}).values(),
                    *snapshot.get("venue_bindings", {}).values()]
        for table in bindings:
            for key, value in table.items():
                if not key.startswith("update:") or not isinstance(value, dict) or value.get("trade_id") not in trade_ids:
                    continue
                row = snapshot["events"].get(key.removeprefix("update:"))
                if row is not None and row["source"]["chat_id"] == chat_id:
                    bound_refs.add(row["source_id"])
        context["bound_source_refs"] = sorted(bound_refs)
        own_refs = {ref for trade in context.get("trades", []) for ref in trade["plan"].get("source_refs", [])} | set(source_refs)
        included = {row["event_id"] for row in context["messages"]}
        context["messages"] += [row for row in latest_sources(snapshot["events"], own_refs).values()
                                if row["event_id"] not in included]
        packet = build_packet(context)
        binding = {"packet": packet, "configuration_revision": context.get("configuration_revision"),
                   "policy_revision": context.get("policy_revision")}
        return context, packet, digest(binding)

    @staticmethod
    def content_identity(context, packet):
        """Only the anchor revision changes. Causal original revisions stay exact."""
        packet = json.loads(json.dumps(packet))
        anchor_event_id = packet["anchor_event_id"]
        packet["messages"] = [row for row in packet["messages"] if row["source_id"] != packet["anchor_source_id"]
                              or row["event_id"] == anchor_event_id]
        packet["anchor_event_id"] = "equivalent-anchor"
        for row in packet["messages"]:
            if row["source_id"] == packet["anchor_source_id"]:
                row["event_id"] = "equivalent-anchor"
                row.pop("edited_at", None)
                row.pop("received_at", None)
        return digest([packet, context.get("configuration_revision"), context.get("policy_revision")])

    def retry_deadline(self, context, packet, created_at, *, kind=None):
        deadline = created_at + self.retry_max_age_seconds * 1000
        source = context["event"]["source"]
        kind = kind or (context["event"].get("interpretation") or {}).get("kind")
        # Entry freshness is not an update TTL or an offline semantic QA TTL.
        # Until interpreted, Python-owned setup context may represent management.
        if (self.mode != "hybrid" or source.get("historical") or kind is not None and kind != "new_signal"
                or kind is None and (packet.get("anchor_setup_bound") or context.get("trades"))):
            return deadline
        age = (context.get("policy") or {}).get("max_signal_age_seconds")
        if type(age) is int and age > 0:
            deadline = min(deadline, source["sent_at"] + age * 1000)
        return deadline

    @staticmethod
    def event_operations(snapshot, job):
        trade_ids = {row.get("trade_id") for row in snapshot["events"].get(job.event_id, {}).get("targets", {}).values()}
        for identity in job.plan_ids:
            trade_ids.add(snapshot.get("plans", {}).get(identity, {}).get("plan", {}).get("trade_id"))
        return [operation for identity, operation in snapshot.get("operations", {}).items()
                if identity == "update:" + job.event_id or operation.get("request", {}).get("event_id") == job.event_id
                or job.event_id in identity.split(":") or set(identity.split(":")) & (trade_ids - {None})
                or job.event_id in operation.get("request", {}).get("source_revisions", {}).values()
                or operation.get("trade_id") is not None and operation.get("trade_id") in trade_ids]

    def reconcile_jobs(self, owner, version, snapshot):
        """Finish durable outcomes before queue selection; never dispatch/recover orders."""
        for record in snapshot.get("source_jobs", {}).values():
            if record["status"] not in ("applying", "recovering", "retry_wait") or record["job_id"] in self.active_jobs:
                continue
            job = SourceJob.model_validate(record)
            event = snapshot["events"].get(job.event_id, {})
            operations = self.event_operations(snapshot, job)
            uncertain = any(row.get("phase") in ("prepared", "sending", "unknown") for row in operations)
            if uncertain or event.get("status") == "needs_reconciliation":
                job.status, job.outcome = "recovering", "engine_recovery"
                self.save(job)
                continue
            if event.get("status") in ("rejected", "cancelled", "superseded", "unrouted", "report", "unrelated", "ambiguous", "incomplete", "updated", "executed"):
                if self.pending_previews(snapshot, job) or self.unsubmitted_plans(snapshot, job):
                    if job.policy_version == version and job.version == VERSION and job.result and job.result.question_version == VERSION:
                        job.status = "waiting_preview"
                    else:
                        job.status, job.outcome = "review", "source_policy_changed"
                else:
                    job.status, job.outcome = "done", event["status"]
                self.save(job)
                continue
            if job.status == "retry_wait":
                try:
                    context, packet, _ = self.context(job.event_id)
                    job.retry_deadline_at = min(job.retry_deadline_at or 0, self.retry_deadline(context, packet, job.created_at))
                    self.save(job)
                except AqlaError:
                    job.status, job.outcome = "review", "retry_source_changed"
                    self.save(job)
                    continue
                if (job.policy_version != version or job.version != VERSION or now_ms() >= (job.retry_deadline_at or 0)
                        or job.attempt_count >= self.retry_max_attempts):
                    job.status, job.outcome = "review", "provider_retry_exhausted"
                    self.save(job)
                continue
            # Financially known work belongs exclusively to engine recovery. A
            # partially dispatched entry can resume only independently unsent legs.
            started = any(self.trading.target_started(snapshot, row) for row in event.get("targets", {}).values())
            if operations or started:
                job.status, job.outcome = ("waiting_preview", "partial_application") if (
                    self.pending_previews(snapshot, job) or self.unsubmitted_plans(snapshot, job)) else ("done", "engine_owned")
                self.save(job)
                continue
            if job.result is None or job.policy_version != version or job.version != VERSION or job.result.question_version != VERSION:
                job.status, job.outcome = "review", "source_policy_changed"
                self.save(job)
                continue
            if job.result.interpretation.kind == "trade_update" and event.get("interpretation"):
                candidate_groups = [engine.update_candidates(job.event_id, job.result.interpretation) for engine in self.trading.engines.values()]
                candidates = [trade for rows in candidate_groups for trade in rows]
                families = {tuple(sorted(row["plan"]["source_refs"])) for row in candidates}
                if len(families) != 1 or any(len(rows) > 1 for rows in candidate_groups) or any(row.get("status") in ("conflicted", "cleanup") for row in candidates):
                    job.status, job.outcome = "review", "update_binding_review"
                    self.save(job)
                    continue
            if job.event_id in queue_overview(snapshot, limit=len(snapshot["events"]) + 1)["ready"]:
                job.status, job.owner, job.processor_instance = "interpreted", owner, self.instance
                self.save(job)
            else:
                job.status, job.outcome = "review", "application_not_ready"
                self.save(job)

    def prepare(self, owner, version):
        self.require_owner(owner)
        if not isinstance(version, str) or not 1 <= len(version) <= 100:
            raise AqlaError("A versioned native interpretation policy is required")
        self.policy_version = version
        snapshot = self.state.snapshot()
        if self.mode == "hybrid":
            self.reconcile_jobs(owner, version, snapshot)
            snapshot = self.state.snapshot()
            for record in snapshot.get("source_jobs", {}).values():
                if record["status"] != "waiting_preview" or record["job_id"] in self.active_jobs:
                    continue
                job = SourceJob.model_validate(record)
                if job.policy_version != version or job.version != VERSION or job.result is None or job.result.question_version != VERSION:
                    # A policy change revokes ONLY the right to submit remaining
                    # legs. Already sent operations stay owned by engine recovery.
                    job.status, job.failure_code = "failed", "source_policy_changed"
                    self.save(job)
                    self.fail(owner, job.job_id, job.failure_code)
                    continue
                targets = self.pending_previews(snapshot, job)
                if not targets and not self.unsubmitted_plans(snapshot, job):
                    job.status = "done"
                    self.save(job)
                    continue
                if not self.unsubmitted_plans(snapshot, job) and not any(retry_due(row["observation_retry"]) or now_ms() >= row["observation_retry"]["deadline_at"]
                           or row["observation_retry"].get("configuration_revision") != snapshot["configuration_revision"]
                           or row["observation_retry"].get("ranking_revision") != snapshot["ranking_revision"] for row in targets):
                    continue
                try:
                    self.check_intent(job, snapshot)
                    if job.processor_instance != self.instance:
                        # Only a read-only preview lease is reclaimed. Financial
                        # operations retain the trading engine's recovery guards.
                        def reclaim_preview(data, selected_job=job):
                            for target in self.pending_previews(data, selected_job):
                                if not self.trading.target_started(data, target):
                                    target.pop("preview_inflight_until", None)
                        self.state.update(reclaim_preview)
                    _, packet, job.fingerprint = self.context(job.event_id, source_refs=job.result.interpretation.source_refs)
                    job.owner, job.processor_instance = owner, self.instance
                    self.save(job)
                    return self.response(job, packet)
                except AqlaError:
                    job.status = "failed"
                    job.failure_code = "accepted_source_changed"
                    self.save(job)
        ready = queue_overview(snapshot, limit=len(snapshot["events"]) + 1)["ready"]
        ready.sort(key=lambda identity: (snapshot["events"][identity]["source"].get("received_at", 0), identity))
        jobs = snapshot.get("source_jobs", {})
        for event_id in ready:
            prior = [row for row in jobs.values() if row["event_id"] == event_id
                     and row.get("policy_version") == version and row.get("processing_mode") == self.mode]
            if any(row["status"] in ("done", "shadow", "waiting_preview", "review", "recovering") for row in prior):
                continue
            # Unrelated new context must not reopen an indefinitely failing anchor.
            # A new source revision or explicitly changed policy owns a fresh budget.
            if sum(row["status"] == "failed" for row in prior) >= 3:
                continue
            context, packet, fingerprint = self.context(event_id)
            job_id = digest([event_id, fingerprint, version, self.mode])
            previous = jobs.get(job_id)
            pending_retry = next((row for row in prior if row["status"] == "retry_wait"), None)
            if pending_retry and pending_retry["job_id"] != job_id:
                changed = SourceJob.model_validate(pending_retry)
                changed.status, changed.outcome = "review", "retry_context_changed"
                self.save(changed)
                continue
            if previous and previous["status"] in ("done", "failed", "shadow"):
                continue
            if previous and previous["status"] == "retry_wait":
                if now_ms() < (previous.get("next_retry_at") or 0):
                    continue
                previous = {**previous, "status": "prepared", "attempt_count": previous.get("attempt_count", 1) + 1,
                            "owner": owner, "processor_instance": self.instance}
                self.state.update_metadata(("source_jobs",), lambda data, identity=job_id, value=previous:
                                           data["source_jobs"].__setitem__(identity, value))
            if previous and previous["status"] == "applying":
                continue
            job = SourceJob.model_validate(previous) if previous else SourceJob(
                job_id=job_id, event_id=event_id, fingerprint=fingerprint, created_at=now_ms(), owner=owner,
                processor_instance=self.instance, policy_version=version, processing_mode=self.mode,
                content_fingerprint=self.content_identity(context, packet), retry_deadline_at=now_ms() + self.retry_max_age_seconds * 1000)
            if not previous:
                job.retry_deadline_at = self.retry_deadline(context, packet, job.created_at)
                if self.mode == "hybrid" and now_ms() >= job.retry_deadline_at:
                    job.status, job.outcome = "review", "source_retry_deadline_expired"
                    job.review_sources = {packet["anchor_source_id"]: event_id}
                    self.save(job)
                    continue
                # Reuse only exact semantics under the same complete admission
                # policy. Binding changes invalidate identity; entries already
                # consumed by any owned setup must never become a second trade.
                cached = next((row for row in reversed(list(jobs.values())) if row.get("result")
                    and row.get("event_id") != event_id and row.get("content_fingerprint") == job.content_fingerprint
                    and row.get("policy_version") == version and row.get("processing_mode") == self.mode
                    and row.get("version") == VERSION and row.get("status") in ("done", "interpreted")
                    and row.get("job_id") not in self.active_jobs
                    and (row["result"]["interpretation"]["kind"] not in ("new_signal", "trade_update") or row["status"] == "interpreted")
                    and not row["result"].get("collection_only")), None)
                if cached and not (cached["result"]["interpretation"]["kind"] == "new_signal" and (
                        packet["anchor_setup_bound"] or any(packet["anchor_source_id"] in saved.get("plan", {}).get("source_refs", [])
                                                          for saved in [*snapshot["trades"].values(), *snapshot["plans"].values()]))):
                    reused = DecisionResult.model_validate(cached["result"])
                    validate_interpretation(packet, reused.interpretation)
                    job.result, job.status, job.cache_reused_from = reused, "interpreted", cached["job_id"]
                    job.intent_revisions = {ref: row["event_id"] for ref, row in latest_sources(
                        snapshot["events"], reused.interpretation.source_refs).items()}
                record = job.model_dump(mode="json")
                self.state.update_metadata(("source_jobs",), lambda data, identity=job_id, value=record:
                                           data["source_jobs"].__setitem__(identity, value))
            return self.response(job, packet)
        return {"job": None}

    def response(self, job, packet):
        response = {"job": job.model_dump(mode="json")}
        if len(json.dumps(packet, ensure_ascii=True)) <= 16000:
            response["packet"] = packet
        else:
            response["packet_required"] = True
        return response

    def pending_previews(self, snapshot, job):
        targets = snapshot["events"].get(job.event_id, {}).get("targets", {})
        return [row for row in targets.values() if row.get("status") == "pending"
                and row.get("observation_retry", {}).get("authority") == "preview"]

    def unsubmitted_plans(self, snapshot, job):
        targets = snapshot["events"].get(job.event_id, {}).get("targets", {})
        return [row["plan_id"] for row in targets.values() if row.get("status") == "planned"
                and row.get("plan_id") in job.plan_ids and not self.trading.target_started(snapshot, row)]

    def check_intent(self, job, snapshot):
        if job.result is None:
            raise AqlaError("Saved interpretation is missing")
        current = latest_sources(snapshot["events"], job.result.interpretation.source_refs)
        if {ref: row["event_id"] for ref, row in current.items()} != job.intent_revisions:
            raise AqlaError("Accepted original sources changed; no new submission")
        verify_evidence(job.result.interpretation, snapshot["events"])

    def packet_page(self, owner, job_id, offset):
        job = self.read_job(owner, job_id)
        if type(offset) is not int or offset < 0:
            raise AqlaError("Packet offset must be nonnegative")
        _, packet, fingerprint = self.context(job.event_id)
        if job.status == "waiting_preview":
            snapshot = self.state.snapshot()
            self.check_intent(job, snapshot)
            _, packet, fingerprint = self.context(job.event_id, source_refs=job.result.interpretation.source_refs)
        if fingerprint != job.fingerprint:
            raise AqlaError("Source packet changed during native read")
        body = json.dumps(packet, ensure_ascii=True, separators=(",", ":"))
        if offset > len(body):
            raise AqlaError("Packet offset exceeds the selected original")
        end = min(len(body), offset + 4096)
        return {"job_id": job_id, "fingerprint": fingerprint, "chunk": body[offset:end],
                "next_offset": end if end < len(body) else None, "eof": end == len(body)}

    def read_job(self, owner, job_id):
        self.require_owner(owner)
        record = self.state.read("source_jobs", entries={"source_jobs": [job_id]}).get("source_jobs", {}).get(job_id)
        if record is None:
            raise AqlaError("Unknown native source job")
        return SourceJob.model_validate(record)

    def save(self, job):
        self.state.update_metadata(("source_jobs",), lambda data: data["source_jobs"].__setitem__(job.job_id, job.model_dump(mode="json")))

    def record(self, owner, job_id, result):
        job = self.read_job(owner, job_id)
        if job.status not in ("prepared", "interpreted"):
            raise AqlaError("Source job result is immutable")
        context, packet, fingerprint = self.context(job.event_id)
        if job.fingerprint != fingerprint:
            raise AqlaError("Source job context changed; prepare a current job")
        validate_interpretation(packet, result.interpretation)
        if result.question_version != VERSION:
            raise AqlaError("Decision questions changed; a current interpretation is required")
        if result.model == "python":
            anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
            if anchor["text"].strip() or result.interpretation.kind != "incomplete":
                raise AqlaError("Python-only interpretation is restricted to missing original text")
        if result.collection_only and self.mode != "shadow":
            raise AqlaError("Collection-only decisions cannot authorize production processing")
        if not result.collection_only and not result.fallback and result.interpretation.kind in ("new_signal", "trade_update") and not result.verified:
            raise AqlaError("Actionable decision needs its second verification")
        if job.result is not None and job.result != result:
            raise AqlaError("An already recorded interpretation cannot be replaced")
        job.result, job.status = result, "shadow" if self.mode == "shadow" else "interpreted"
        job.retry_deadline_at = min(job.retry_deadline_at or job.created_at + self.retry_max_age_seconds * 1000,
                                   self.retry_deadline(context, packet, job.created_at, kind=result.interpretation.kind))
        job.intent_revisions = {ref: row["event_id"] for ref, row in latest_sources(
            self.state.snapshot()["events"], result.interpretation.source_refs).items()}
        self.save(job)
        return {"job_id": job_id, "status": job.status}

    def fail(self, owner, job_id, code, detail=None):
        job = self.read_job(owner, job_id)
        if job.status in ("done", "shadow"):
            return {"status": job.status}
        # A possibly dispatched financial operation is never reinterpreted/retried here.
        if job.status not in ("applying", "waiting_preview"):
            job.status = "failed"
        job.failure_code = safe_code(code)
        job.failure_detail = sanitize_detail(detail, job.failure_code)
        job.failure_history.append({"at": now_ms(), "attempt": job.attempt_count, **job.failure_detail})
        job.failure_history = job.failure_history[-15:]
        if job.status == "failed" and job.failure_code in ("source_context_limit", "candidate_limit", "entry_legs_not_representable",
                "signal_contains_management_instruction", "management_actions_not_representable", "scheduled_management_not_representable",
                "conditional_stop_not_representable", "additional_entry_legs_not_representable"):
            job.status, job.outcome = "review", job.failure_code
            event = self.state.snapshot()["events"][job.event_id]
            job.review_sources = {event["source_id"]: job.event_id}
        if (job.status == "failed" and job.failure_detail["retryable"] and job.result is None
                and job.attempt_count < self.retry_max_attempts and now_ms() < (job.retry_deadline_at or 0)):
            job.status = "retry_wait"
            job.next_retry_at = min(job.retry_deadline_at, now_ms() + self.retry_backoff_seconds * 1000 * 2 ** (job.attempt_count - 1))
        elif job.status == "failed" and job.failure_detail["retryable"]:
            job.status, job.outcome = "review", "provider_retry_exhausted"
        if self.mode == "shadow":
            self.save(job)
            return {"status": job.status, "failure_code": job.failure_code, "failure_detail": job.failure_detail}
        def record(data):
            data["source_jobs"][job_id] = job.model_dump(mode="json")
            if job.status != "retry_wait":
                data["notifications"].setdefault("source-job:" + job_id, {
                    "event_id": job.event_id, "error": "Quellenverarbeitung benötigt Prüfung: " + job.failure_code})
        self.state.update_metadata(("source_jobs", "notifications"), record)
        return {"status": job.status, "failure_code": job.failure_code, "failure_detail": job.failure_detail}

    async def apply(self, owner, job_id):
        if job_id in self.active_jobs:
            raise AqlaError("This source job is already applying")
        self.active_jobs.add(job_id)
        try:
            return await self.apply_saved(owner, job_id)
        except Exception as error:
            # The legacy single-target router raises AFTER a durable rejection.
            # That rejection is a normal completed application, not a failed API.
            job = self.read_job(owner, job_id)
            snapshot = self.state.snapshot()
            event = snapshot["events"].get(job.event_id, {})
            terminal = event.get("status") in ("rejected", "cancelled", "superseded", "unrouted") and not any(
                row.get("phase") in ("prepared", "sending", "unknown") for row in self.event_operations(snapshot, job))
            if not terminal:
                self.fail(owner, job_id, "application_review", classify_failure(error, "apply"))
            self.active_jobs.discard(job_id)
            self.reconcile_jobs(owner, self.policy_version, self.state.snapshot())
            job = self.read_job(owner, job_id)
            if job.status in ("done", "review", "recovering"):
                return {"status": job.status, "job_id": job_id, "plan_ids": job.plan_ids, "outcome": job.outcome}
            raise
        finally:
            self.active_jobs.discard(job_id)

    async def apply_saved(self, owner, job_id):
        job = self.read_job(owner, job_id)
        if self.mode != "hybrid":
            raise AqlaError("Shadow source jobs cannot execute")
        if job.status == "done":
            return {"status": "done", "already_processed": True}
        resume_preview = job.status == "waiting_preview"
        if job.status not in ("interpreted", "waiting_preview") or job.result is None:
            raise AqlaError("Source job is not a validated unsubmitted interpretation")
        if job.result.collection_only:
            raise AqlaError("Collection-only decisions cannot execute")
        if job.policy_version != self.policy_version or job.version != VERSION or job.result.question_version != VERSION:
            raise AqlaError("Source processing policy changed; no new submission")
        # This read happens immediately before the existing transactional source/entry guards.
        context, packet, fingerprint = self.context(job.event_id)
        if not resume_preview and fingerprint != job.fingerprint:
            raise AqlaError("Source context changed before application")
        if resume_preview:
            snapshot = self.state.snapshot()
            self.check_intent(job, snapshot)
            if not self.pending_previews(snapshot, job) and not self.unsubmitted_plans(snapshot, job):
                raise AqlaError("No proven unsubmitted preview target or saved plan remains")
        else:
            validate_interpretation(packet, job.result.interpretation)
        if not resume_preview and job.event_id not in queue_overview(self.state.snapshot(), limit=100000)["ready"]:
            raise AqlaError("Source job is no longer ready; recover existing operations")
        # Keep pending preview work discoverable if this read is interrupted
        # after another venue already opened its position.
        job.status = "waiting_preview" if resume_preview else "applying"
        self.save(job)
        interpretation = job.result.interpretation
        if interpretation.kind not in ("new_signal", "trade_update"):
            self.trading.source.record_interpretation(job.event_id, interpretation,
                read_receipt=context.get("source_read_receipt", ""), result_view="ack")
        elif interpretation.kind == "trade_update":
            await self.trading.update_trade(job.event_id, interpretation)
        else:
            execute_ids = self.unsubmitted_plans(self.state.snapshot(), job) if resume_preview else job.plan_ids.copy()
            if not job.plan_ids or resume_preview and self.pending_previews(self.state.snapshot(), job):
                preview = await self.trading.preview(job.event_id, interpretation)
                self.require_owner(owner)
                targets = preview.get("targets", {})
                targets = list(targets.values()) if isinstance(targets, dict) else targets
                # A successful retry can retain a historical error on its target row.
                # Only a freshly returned, validated plan proves new admission.
                execute_ids += [row["plan_id"] for row in targets if row.get("plan_id") and isinstance(row.get("plan"), dict)
                                and row.get("status") == "planned" and row["plan_id"] not in job.plan_ids]
                execute_ids = list(dict.fromkeys(execute_ids))
                if not targets and preview.get("plan_id"):
                    execute_ids = [preview["plan_id"]]
                job.plan_ids = list(dict.fromkeys([*job.plan_ids, *execute_ids]))
                if self.pending_previews(self.state.snapshot(), job):
                    # Persist remaining preview work BEFORE a healthy leg can be sent.
                    job.status = "waiting_preview"
                self.save(job)
            self.require_owner(owner)
            if execute_ids:
                await self.trading.execute_many(execute_ids)
        job.status = "waiting_preview" if self.pending_previews(self.state.snapshot(), job) else "done"
        self.save(job)
        return {"status": job.status, "job_id": job_id, "plan_ids": job.plan_ids}

    def overview(self):
        rows = list(self.state.read("source_jobs").get("source_jobs", {}).values())
        counts = {status: 0 for status in ("prepared", "interpreted", "applying", "waiting_preview", "retry_wait", "recovering", "review", "done", "failed", "shadow")}
        for row in rows:
            counts[row["status"]] += 1
        diagnostics = [{"job_id": row["job_id"], "status": row["status"], "outcome": row.get("outcome"),
                        "failure": row.get("failure_detail"), "attempt_count": row.get("attempt_count", 1),
                        "next_retry_at": row.get("next_retry_at"), "retry_deadline_at": row.get("retry_deadline_at")}
                       for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)
                       if row.get("failure_detail") or row["status"] in ("review", "recovering", "retry_wait")][:8]
        return {"mode": self.mode, "jobs": counts, "decision": self.decision_status, "diagnostics": diagnostics}
