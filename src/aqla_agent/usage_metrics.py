"""Private aggregate usage; never stores messages, arguments or credentials."""
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

RUN = ContextVar("aqla_usage_run", default=None)
LOG = logging.getLogger(__name__)


def _fresh_source_calls(row):
    workflow, responses = row.get("workflow", {}), row.get("api_responses", [])
    if row.get("origin") != "source_python" or workflow.get("cache_hit") or workflow.get("source_work_type") in ("cached", "empty", "status"):
        return None
    calls = [call for call in responses if call.get("phase") in ("decision", "decision_verification", "interpretation_fallback")]
    if not calls and workflow.get("source_work_type") != "interpretation":
        return None
    key = workflow.get("source_revision_hash") or workflow.get("source_job_id")
    if not key:
        return None
    return workflow, calls, key


def _api_response_failed(call):
    return call.get("response_success") is False or "response_success" not in call and call.get("failed") is True


def _model_response_invalid(call):
    return call.get("response_success") is True and (
        call.get("contract_valid") is False or call.get("interpretation_valid") is False
        or call.get("failed") is True and call.get("review_deferred") is not True)


def source_efficiency(rows):
    """Group fresh original revisions; retries/cache/status cannot inflate shares."""
    grouped, api_failures, invalid_responses = {}, 0, 0
    for row in rows:
        fresh = _fresh_source_calls(row)
        if fresh is None:
            continue
        workflow, calls, key = fresh
        selected = grouped.setdefault(key, {"valid": False, "fallback": False})
        selected["valid"] |= workflow.get("interpretation_valid") is True
        selected["fallback"] |= any(call.get("phase") == "interpretation_fallback" for call in calls)
        api_failures += sum(_api_response_failed(call) for call in calls)
        invalid_responses += sum(_model_response_invalid(call) for call in calls)
    valid = sum(item["valid"] for item in grouped.values())
    decision_only = sum(item["valid"] and not item["fallback"] for item in grouped.values())
    count = len(grouped)
    return {"fresh_source_revisions": count, "valid_interpretations": valid,
            "fully_without_fallback": decision_only,
            "without_fallback_share": decision_only / count if count else None,
            "fallback_used": sum(item["fallback"] for item in grouped.values()),
            "api_response_failures": api_failures, "invalid_model_responses": invalid_responses}


def _tool_payload(content):
    value = json.loads(content)
    for _ in range(3):
        if isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
            value = json.loads(value) if isinstance(value, str) else value
    return value


def _source_timing_key(identity):
    if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
        return None
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass
class UsageRun:
    origin: str
    started: float = field(default_factory=time.time)
    elapsed_started: float = field(default_factory=time.monotonic)
    api: list = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    result_hashes: set = field(default_factory=set)
    classified_sources: set = field(default_factory=set)
    workflow: dict = field(default_factory=dict)
    source_timings: dict = field(default_factory=dict)
    trade_sources: dict = field(default_factory=dict, repr=False)

    def response(self, usage, model, provider, duration, *, phase="main", failed=False):
        self.api.append({"model": model, "provider": provider, "usage_available": usage is not None,
                         "duration_seconds": duration, "phase": phase, "failed": failed, **({key: getattr(usage, key) for key in
                         ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")} if usage else {})})

    def tool(self, name, content, error, duration=None):
        from .efficiency import signature
        key = signature([name, content])
        row = self.tools.setdefault(name, {"calls": 0, "output_characters": 0, "errors": 0, "repeated_results": 0})
        row["calls"] += 1
        row["output_characters"] += len(content) if isinstance(content, str) else 0
        row["errors"] += int(bool(error))
        row["repeated_results"] += int(key in self.result_hashes)
        self.result_hashes.add(key)
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            samples = row.setdefault("duration_seconds", [])
            if len(samples) < 256:
                samples.append(duration)
        if not error and isinstance(content, str):
            self.observe_source(name, content)
        if isinstance(content, str) and not error and name.endswith(("interpret_source", "interpret_source_batch")):
            try:
                value = _tool_payload(content)
                for item in value.get("items", [value]):
                    if item.get("status") in ("report", "unrelated", "incomplete", "ambiguous") and item.get("event_id"):
                        self.classified_sources.add(item["event_id"])
            except (ValueError, TypeError, AttributeError):
                pass

    def observe_source(self, name, content):
        """Timing facts only. A tool reply is never proof of an exchange send/fill."""
        if not name.endswith(("source_context", "source_context_batch", "interpret_source", "interpret_source_batch", "trade_preview", "trade_execute")):
            return
        try:
            value = _tool_payload(content)
            if not isinstance(value, dict):
                return
            stamp = int(time.time()*1000)
            if name.endswith(("source_context", "source_context_batch")):
                self.observe_context(value, stamp)
            elif name.endswith(("interpret_source", "interpret_source_batch")):
                self.observe_interpretation(value, stamp)
            elif name.endswith(("trade_preview", "trade_execute")):
                self.observe_trade(value, name, stamp)
        except (ValueError, TypeError, AttributeError):
            pass

    def observe_context(self, value, stamp):
        messages = value.get("messages", {})
        if not isinstance(messages, dict):
            messages = {}
        anchors = [value.get("event", {})] if "event" in value else [messages.get(item.get("anchor_event_id"), {}) for item in value.get("items", [value]) if isinstance(item, dict)]
        if not anchors or not any(anchors):
            anchors = [messages.get(value.get("anchor_event_id"), {})]
        for event in anchors[:8]:
            source = event.get("source", {})
            identity = _source_timing_key(event.get("event_id"))
            received = source.get("received_at")
            if (identity and source.get("historical") is not True and type(received) is int and 0 <= received <= stamp
                    and (len(self.source_timings) < 256 or identity in self.source_timings)):
                self.source_timings.setdefault(identity, {"received_at": received, "turn_started_at": int(self.started*1000)})["context_reply_at"] = stamp

    def observe_interpretation(self, value, stamp):
        for item in value.get("items", [value])[:8]:
            if (isinstance(item, dict) and item.get("status") in ("report", "unrelated", "incomplete", "ambiguous", "interpreted", "updated", "executed")
                    and (identity := _source_timing_key(item.get("event_id"))) in self.source_timings):
                self.source_timings[identity]["interpretation_reply_at"] = stamp

    def observe_trade(self, value, name, stamp):
        for item in value.get("targets", [value])[:8]:
            if not isinstance(item, dict):
                continue
            plan = item.get("plan", item)
            if not isinstance(plan, dict):
                continue
            identities = [identity for event in plan.get("source_revisions", {}).values() if (identity := _source_timing_key(event)) in self.source_timings]
            trade = item.get("trade_id", plan.get("trade_id"))
            if identities and isinstance(trade, str) and len(self.trade_sources) < 256:
                self.trade_sources[trade] = identities
            for identity in identities or self.trade_sources.get(trade, []):
                self.source_timings[identity]["preview_reply_at" if name.endswith("trade_preview") else "execution_reply_at"] = stamp

    def persist(self, home, result):
        try:
            path = Path(home) / "cache" / "aqla-usage.sqlite3"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink():
                raise OSError("Usage storage link forbidden")
            if not path.exists():
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            os.chmod(path, 0o600)
            row = {"origin": self.origin, "started_at": self.started,
                   "duration_seconds": time.monotonic() - self.elapsed_started,
                   "api_responses": self.api, "tools": self.tools,
                   "classified_source_count": len(self.classified_sources),
                   "api_calls_reported": result.get("api_calls"),
                   "failed": bool(result.get("failed", False)), "interrupted": bool(result.get("interrupted", False)),
                   "completed": result.get("completed"), "compression_exhausted": bool(result.get("compression_exhausted", False))}
            row["workflow"] = self.workflow
            row["source_timings"] = self.source_timings
            with sqlite3.connect(path, timeout=1) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, origin TEXT NOT NULL, metrics TEXT NOT NULL)")
                connection.execute("INSERT INTO runs(started_at,origin,metrics) VALUES(?,?,?)", (self.started, self.origin, json.dumps(row, separators=(",", ":"))))
                connection.execute("DELETE FROM runs WHERE started_at < ?", (self.started - 90 * 86400,))
        except (OSError, sqlite3.Error, TypeError, ValueError):
            LOG.warning("aqla aggregate usage unavailable; trading continues")


def install(replace):
    from agent import context_compressor, turn_response_check, turn_usage
    from agent.usage_pricing import normalize_usage
    original = turn_usage.record_response_usage

    def record_response_usage(agent, response, *, messages, api_call_count, api_duration, compression_attempts, max_compression_attempts):
        result = original(agent, response, messages=messages, api_call_count=api_call_count, api_duration=api_duration,
                          compression_attempts=compression_attempts, max_compression_attempts=max_compression_attempts)
        if run := RUN.get():
            try:
                usage = normalize_usage(response.usage, provider=agent.provider, api_mode=agent.api_mode) if getattr(response, "usage", None) else None
                run.response(usage, agent.model, agent.provider, api_duration)
            except (AttributeError, TypeError, ValueError):
                run.response(None, agent.model, agent.provider, api_duration)
        return result

    replace(turn_usage, "record_response_usage", record_response_usage)
    replace(turn_response_check, "record_response_usage", record_response_usage)
    auxiliary = context_compressor.call_llm

    def summary_call(*args, **kwargs):
        started = time.monotonic()
        result, failed = None, True
        try:
            result = auxiliary(*args, **kwargs)
            failed = False
            return result
        finally:
            if run := RUN.get():
                route = kwargs.get("route_info") or kwargs.get("main_runtime") or {}
                raw = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)
                try:
                    usage = normalize_usage(raw, provider=route.get("provider"), api_mode=route.get("api_mode", "chat_completions")) if raw else None
                    run.response(usage, route.get("model"), route.get("provider"), time.monotonic() - started, phase="compression", failed=failed)
                except (AttributeError, TypeError, ValueError):
                    run.response(None, None, None, time.monotonic() - started, phase="compression", failed=failed)

    replace(context_compressor, "call_llm", summary_call)
