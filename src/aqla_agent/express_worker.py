"""Event-driven template admission, sharing the durable source executor."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets

from aqla_hyperliquid_mcp.models import AqlaError, SourceEvent, digest, now_ms

from .decisions import VERSION as DECISION_VERSION
from .decisions import DecisionResult
from .express import ACTIVE, CLAIM, QUALIFIED_MASTER, VERSION, compile_template
from .observations import retry_due
from .source_assistance import VERSION as ASSISTANCE_VERSION
from .source_assistance import check_interpretation, precise_numeric_evidence
from .source_processing import SourceJob

LOG = logging.getLogger(__name__)


class ExpressWorker:
    def __init__(self, processor, settings):
        self.processor, self.settings = processor, settings
        self.state, self.trading = processor.state, processor.trading
        self.owner = secrets.token_hex(32)
        self.signal = asyncio.Event()
        self.lock = asyncio.Lock()
        self.policy_version = digest([VERSION, DECISION_VERSION, ASSISTANCE_VERSION, QUALIFIED_MASTER])
        processor.express = self
        self.trading.source.express_ingress = self.admit
        for engine in self.trading.engines.values():
            engine.express_admission = self.check_admission

    def qualified(self):
        path = self.settings.master_path
        try:
            return path is not None and hashlib.sha256(path.read_bytes()).hexdigest() == QUALIFIED_MASTER
        except OSError:
            return False

    def owns(self, owner, job=None):
        current = CLAIM.get()
        return owner == self.owner and current is not None and current[0] == self.owner and (
            job is None or job.execution_path == "express" and current[1] == job.job_id)

    def admit(self, data, row):
        """Called inside source insertion's state transaction, before visibility."""
        if (self.settings.mode == "off" or self.processor.mode != "legacy" or not self.qualified()
                or row.get("status") != "pending" or row.get("source_quarantine")
                or row.get("native_forward") or row.get("interpretation") or row.get("targets")):
            return
        source = SourceEvent.model_validate(row["source"])
        if source.historical:
            return
        try:
            draft = compile_template(source)
        except (AqlaError, ValueError):
            return
        if draft is None:
            return
        identity = "express:" + digest([row["event_id"], self.policy_version, self.settings.mode])[:32]
        jobs = data.setdefault("source_jobs", {})
        if identity in jobs:
            return
        job = SourceJob(job_id=identity, event_id=row["event_id"], fingerprint="unread", created_at=now_ms(),
            owner=self.owner, processor_instance=self.processor.instance, policy_version=self.policy_version,
            processing_mode="express_" + self.settings.mode, execution_path="express",
            express_timings={"received_at": source.received_at, "claimed_at": now_ms(), "model_calls": 0})
        jobs[identity] = job.model_dump(mode="json")
        if self.settings.mode == "live":
            row["express_job_id"] = identity
        self.signal.set()

    def release(self, job, outcome):
        operations = self.processor.event_operations(self.state.snapshot(), job)
        job.status = "recovering" if any(op.get("phase") in ("prepared", "sending", "unknown") for op in operations) else "done" if operations else "failed"
        job.outcome = outcome
        self.processor.save(job)

    def check_admission(self, snapshot, plan):
        for event_id in plan.source_revisions.values():
            row = snapshot.get("events", {}).get(event_id, {})
            identity = row.get("express_job_id")
            record = snapshot.get("source_jobs", {}).get(identity, {})
            if record.get("execution_path") != "express":
                continue
            if (self.settings.mode != "live" or self.processor.mode != "legacy" or not self.qualified()
                    or record.get("policy_version") != self.policy_version):
                raise AqlaError("Express admission was revoked; no new exposure")
            draft = compile_template(SourceEvent.model_validate(row["source"]))
            saved = record.get("result", {}).get("interpretation") if record.get("result") else None
            if draft is None or saved is None:
                raise AqlaError("Express source no longer matches its validated template")
            # Existing own entry bindings are expected after entry preparation;
            # repeat original proof checks, not new-source admission semantics.
            from aqla_hyperliquid_mcp.rules import verify_evidence
            packet = {"messages": [{"source_id": row["source_id"], "text": row["source"]["text"]}]}
            normalized = precise_numeric_evidence(packet, draft)
            verify_evidence(normalized, snapshot["events"])
            if normalized.model_dump(mode="json") != saved:
                raise AqlaError("Express original proof changed before send")

    def recover(self, job, snapshot):
        event = snapshot["events"].get(job.event_id, {})
        operations = self.processor.event_operations(snapshot, job)
        if any(op.get("phase") in ("prepared", "sending", "unknown") for op in operations):
            job.status, job.outcome = "recovering", "engine_recovery"
        elif event.get("status") in ("rejected", "cancelled", "superseded", "unrouted"):
            job.status, job.outcome = "done", event["status"]
        elif self.processor.pending_previews(snapshot, job) or self.processor.unsubmitted_plans(snapshot, job):
            job.status = "waiting_preview"
        elif operations or event.get("status") in ("executed", "updated", "needs_reconciliation"):
            job.status, job.outcome = "done", "engine_owned"
        elif job.result is not None:
            job.status = "interpreted"
        else:
            job.status = "prepared"
        self.processor.save(job)

    async def process(self, job):
        snapshot = self.state.snapshot()
        if job.status in ("applying", "recovering"):
            self.recover(job, snapshot)
            if job.status not in ("interpreted", "waiting_preview", "prepared"):
                return
        row = snapshot["events"].get(job.event_id)
        if row is None:
            self.release(job, "source_missing")
            return
        from aqla_hyperliquid_mcp.rules import latest_sources
        try:
            current = latest_sources(snapshot["events"], [row["source_id"]])[row["source_id"]]
        except AqlaError:
            self.release(job, "source_revision_conflict")
            return
        if current["event_id"] != job.event_id:
            self.release(job, "source_revision_changed")
            return
        shadow = job.processing_mode == "express_shadow"
        if (not self.qualified() or job.policy_version != self.policy_version or self.processor.mode != "legacy"
                or self.settings.mode == "off" or not shadow and self.settings.mode != "live"):
            self.release(job, "express_disabled_or_policy_changed")
            return
        if job.status == "waiting_preview":
            pending = self.processor.pending_previews(snapshot, job)
            if pending and not self.processor.unsubmitted_plans(snapshot, job) and not any(
                    retry_due(target["observation_retry"]) or now_ms() >= target["observation_retry"]["deadline_at"]
                    for target in pending):
                return
        token = CLAIM.set((self.owner, job.job_id))
        try:
            source = SourceEvent.model_validate(row["source"])
            draft = compile_template(source)
            if source.historical or draft is None:
                self.release(job, "template_changed")
                return
            context, packet, fingerprint = self.processor.context(job.event_id)
            if job.status == "waiting_preview":
                try:
                    self.processor.check_intent(job, snapshot)
                except AqlaError:
                    self.release(job, "source_intent_changed")
                    return
                interpretation = precise_numeric_evidence(packet, draft)
            else:
                checked = check_interpretation(context, draft)
                if not checked["valid"]:
                    self.release(job, "shared_validation_deferred")
                    return
                from aqla_hyperliquid_mcp.models import Interpretation
                interpretation = Interpretation.model_validate(checked["interpretation"])
            # A fresh full parse plus the common checker is the internal proof;
            # never impersonate a Decision Model's second-verification flag.
            if job.result and job.result.interpretation != interpretation:
                self.release(job, "interpretation_changed")
                return
            job.express_timings["checked_at"] = now_ms()
            if shadow:
                job.status, job.outcome = "shadow", "template_valid"
                job.result = DecisionResult(model=VERSION, interpretation=interpretation)
                self.processor.save(job)
                return
            if row.get("express_job_id") != job.job_id:
                self.release(job, "claim_changed")
                return
            if job.status == "prepared":
                job.fingerprint = fingerprint
                job.result = DecisionResult(model=VERSION, interpretation=interpretation)
                job.intent_revisions = {source.source_id: source.event_id}
                job.status = "interpreted"
                self.processor.save(job)
            await self.processor.apply_saved(self.owner, job.job_id)
            updated = SourceJob.model_validate(self.state.read("source_jobs", entries={"source_jobs": [job.job_id]})["source_jobs"][job.job_id])
            updated.express_timings["application_reply_at"] = now_ms()
            self.processor.save(updated)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            current = SourceJob.model_validate(self.state.read("source_jobs", entries={"source_jobs": [job.job_id]})["source_jobs"][job.job_id])
            observed = self.state.snapshot()
            if self.processor.event_operations(observed, current) or self.processor.pending_previews(observed, current):
                self.recover(current, observed)
            else:
                self.release(current, "express_processing_deferred")
            LOG.warning("Express source remains durable (%s)", type(error).__name__)
        finally:
            CLAIM.reset(token)

    async def tick(self):
        async with self.lock:
            records = self.state.read("source_jobs").get("source_jobs", {})
            for record in records.values():
                if record.get("execution_path") == "express" and record["status"] in ACTIVE:
                    await self.process(SourceJob.model_validate(record))

    async def run(self):
        # Scan durable jobs at startup; ingress is the normal immediate wakeup.
        while True:
            self.signal.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning("Express worker unavailable (%s); sources remain durable", type(error).__name__)
            try:
                await asyncio.wait_for(self.signal.wait(), timeout=1)
            except TimeoutError:
                pass

    def overview(self):
        counts = {}
        for job in self.state.read("source_jobs").get("source_jobs", {}).values():
            if job.get("execution_path") == "express":
                counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {"mode": self.settings.mode, "qualified_master": self.qualified(), "template_version": VERSION, "jobs": counts}
