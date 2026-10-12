"""Durable internal workflow, using the existing trading engine as sole executor."""
from __future__ import annotations

import json
import secrets
from typing import Literal

from pydantic import Field

from aqla_hyperliquid_mcp.models import AqlaError, Model, digest, now_ms
from aqla_hyperliquid_mcp.rules import latest_sources, verify_evidence

from .decisions import VERSION, DecisionResult, build_packet, validate_interpretation
from .observations import retry_due
from .source_queue import queue_overview


class SourceJob(Model):
    job_id: str
    event_id: str
    fingerprint: str
    version: str = VERSION
    status: Literal["prepared", "interpreted", "applying", "waiting_preview", "done", "failed", "shadow"] = "prepared"
    result: DecisionResult | None = None
    plan_ids: list[str] = Field(default_factory=list)
    created_at: int
    owner: str = ""
    processor_instance: str = ""
    policy_version: str = ""
    processing_mode: str = ""
    intent_revisions: dict[str, str] = Field(default_factory=dict)
    failure_code: str | None = None


class SourceProcessor:
    def __init__(self, state, trading):
        self.state, self.trading = state, trading
        self.mode, self.owner = "legacy", None
        self.instance = secrets.token_hex(16)
        self.active_jobs = set()

    def configure(self, mode, owner):
        if mode not in ("legacy", "shadow", "hybrid") or not owner or len(owner) > 100:
            raise AqlaError("Invalid native source processing configuration")
        self.mode, self.owner = mode, owner
        return {"mode": mode}

    def require_owner(self, owner):
        if self.mode == "legacy" or owner != self.owner:
            raise AqlaError("Source processor ownership changed")

    def context(self, event_id):
        context = self.trading.context(event_id, decision=True)
        packet = build_packet(context)
        binding = {"packet": packet, "configuration_revision": context.get("configuration_revision"),
                   "policy_revision": context.get("policy_revision")}
        return context, packet, digest(binding)

    def prepare(self, owner, version):
        self.require_owner(owner)
        if not isinstance(version, str) or not 1 <= len(version) <= 100:
            raise AqlaError("A versioned native interpretation policy is required")
        snapshot = self.state.snapshot()
        if self.mode == "hybrid":
            for record in snapshot.get("source_jobs", {}).values():
                if record["status"] != "waiting_preview" or record["job_id"] in self.active_jobs:
                    continue
                job = SourceJob.model_validate(record)
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
                    context = self.trading.context(job.event_id, decision=True)
                    included = {row["event_id"] for row in context["messages"]}
                    context["messages"] += [row for row in latest_sources(snapshot["events"], job.result.interpretation.source_refs).values()
                                            if row["event_id"] not in included]
                    packet = build_packet(context)
                    job.fingerprint = digest({"packet": packet, "configuration_revision": context.get("configuration_revision"),
                                              "policy_revision": context.get("policy_revision")})
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
            if any(row["status"] in ("done", "shadow", "waiting_preview") for row in prior):
                continue
            # Unrelated new context must not reopen an indefinitely failing anchor.
            # A new source revision or explicitly changed policy owns a fresh budget.
            if sum(row["status"] == "failed" for row in prior) >= 3:
                continue
            context, packet, fingerprint = self.context(event_id)
            job_id = digest([event_id, fingerprint, version, self.mode])
            previous = jobs.get(job_id)
            if previous and previous["status"] in ("done", "failed", "shadow"):
                continue
            if previous and previous["status"] == "applying":
                if previous.get("owner") == owner and previous.get("processor_instance") == self.instance:
                    continue
                # Ready excludes every financially bound/recovery event. A new native owner
                # can resume a proved unsubmitted saved result, never an unknown operation.
                previous = {**previous, "status": "interpreted", "owner": owner, "processor_instance": self.instance}
                self.state.update_metadata(("source_jobs",), lambda data, identity=job_id, value=previous:
                                           data["source_jobs"].__setitem__(identity, value))
            job = SourceJob.model_validate(previous) if previous else SourceJob(
                job_id=job_id, event_id=event_id, fingerprint=fingerprint, created_at=now_ms(), owner=owner,
                processor_instance=self.instance, policy_version=version, processing_mode=self.mode)
            if not previous:
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
            context = self.trading.context(job.event_id, decision=True)
            included = {row["event_id"] for row in context["messages"]}
            context["messages"] += [row for row in latest_sources(snapshot["events"], job.result.interpretation.source_refs).values()
                                    if row["event_id"] not in included]
            packet = build_packet(context)
            fingerprint = digest({"packet": packet, "configuration_revision": context.get("configuration_revision"),
                                  "policy_revision": context.get("policy_revision")})
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
        _, packet, fingerprint = self.context(job.event_id)
        if job.fingerprint != fingerprint:
            raise AqlaError("Source job context changed; prepare a current job")
        validate_interpretation(packet, result.interpretation)
        if result.question_version != VERSION:
            raise AqlaError("Decision questions changed; a current interpretation is required")
        if result.model == "python":
            anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
            if anchor["text"].strip() or result.interpretation.kind != "incomplete":
                raise AqlaError("Python-only interpretation is restricted to missing original text")
        if not result.fallback and result.interpretation.kind in ("new_signal", "trade_update") and not result.verified:
            raise AqlaError("Actionable decision needs its second verification")
        if job.result is not None and job.result != result:
            raise AqlaError("An already recorded interpretation cannot be replaced")
        job.result, job.status = result, "shadow" if self.mode == "shadow" else "interpreted"
        job.intent_revisions = {ref: row["event_id"] for ref, row in latest_sources(
            self.state.snapshot()["events"], result.interpretation.source_refs).items()}
        self.save(job)
        return {"job_id": job_id, "status": job.status}

    def fail(self, owner, job_id, code):
        job = self.read_job(owner, job_id)
        if job.status in ("done", "shadow"):
            return {"status": job.status}
        # A possibly dispatched financial operation is never reinterpreted/retried here.
        if job.status not in ("applying", "waiting_preview"):
            job.status = "failed"
        job.failure_code = code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() and len(code) <= 80 else "processing_failed"
        if self.mode == "shadow":
            self.save(job)
            return {"status": job.status, "failure_code": job.failure_code}
        def record(data):
            data["source_jobs"][job_id] = job.model_dump(mode="json")
            data["notifications"].setdefault("source-job:" + job_id, {
                "event_id": job.event_id, "error": "Quellenverarbeitung benötigt Prüfung: " + job.failure_code})
        self.state.update_metadata(("source_jobs", "notifications"), record)
        return {"status": job.status, "failure_code": job.failure_code}

    async def apply(self, owner, job_id):
        if job_id in self.active_jobs:
            raise AqlaError("This source job is already applying")
        self.active_jobs.add(job_id)
        try:
            return await self.apply_saved(owner, job_id)
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
        rows = self.state.read("source_jobs").get("source_jobs", {}).values()
        counts = {status: 0 for status in ("prepared", "interpreted", "applying", "waiting_preview", "done", "failed", "shadow")}
        for row in rows:
            counts[row["status"]] += 1
        return {"mode": self.mode, "jobs": counts}
