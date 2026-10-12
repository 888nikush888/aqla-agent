"""Native Python orchestration with a short, tool-free model fallback."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, Interpretation, digest

from .decisions import (
    VERSION,
    CalibrationProfile,
    DecisionFailure,
    DecisionResult,
    OpenRouterDecisionAdapter,
    SourceProcessingConfig,
    candidates,
    validate_interpretation,
)
from .usage_metrics import UsageRun


class NativeUsageCapture:
    """Request-scoped native accounting, including the observed billing endpoint."""
    def __init__(self):
        self.rows = []

    def record_auxiliary_usage(self, session_id, task, **fields):
        self.rows.append(fields)


class SourceWorker:
    def __init__(self, extension, config, *, adapter=None, fallback=None):
        self.extension = extension
        self.config = SourceProcessingConfig.model_validate(config or {})
        self.adapter = adapter or OpenRouterDecisionAdapter(os.environ.get(self.config.key_env, ""), timeout=self.config.timeout_seconds,
            model=self.config.model, model_revision=self.config.model_revision)
        self.fallback = fallback or self.generative_fallback
        self.bound_owner = None
        self.bound_adapter = None

    def profile(self):
        path = self.config.calibration_file
        if path is None:
            return CalibrationProfile()
        path = Path(path)
        if not path.is_absolute():
            path = self.extension.master_path.parents[2] / path
        if path.is_symlink():
            raise DecisionFailure("calibration_link_forbidden")
        try:
            return CalibrationProfile.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DecisionFailure("calibration_unavailable") from error

    async def internal(self, action, **kwargs):
        return await self.extension.mcp_call(self.extension.server, "integration", {
            "action": action, "token": self.extension.authority.key.decode(),
            "processing_owner": self.extension.gateway_identity, **kwargs})

    async def bind(self):
        # The MCP can restart independently of the gateway, losing its in-memory owner.
        # Same-generation binding is idempotent and never recovers live sending parts.
        await self.internal("source_configure", processing_mode=self.config.mode)
        adapter = self.extension.binding[0] if self.extension.binding else None
        if self.config.mode == "hybrid" and adapter is not None and adapter is not self.bound_adapter:
            # A newly connected native transport may resume exhausted jobs; mere polling cannot.
            await self.internal("delivery_retry")
            self.bound_adapter = adapter
        self.bound_owner = self.extension.gateway_identity

    async def generative_fallback(self, packet, failure_code, usage):
        # The pinned native client owns provider resolution, quota fallback and wire reasoning.
        from agent.aux_accounting import reset_accounting_context, set_accounting_context
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
        from agent.usage_pricing import estimate_usage_cost, normalize_usage
        from hermes_cli.config import load_config_readonly

        extension = self.extension
        model, runtime = await asyncio.to_thread(extension.gateway._resolve_session_agent_runtime,
                                                 session_key=extension.session_key)
        master = extension.master_path.read_text(encoding="utf-8")
        instruction = master + "\nInterpretation only. No tools, no account/configuration changes. "
        instruction += "Return exactly one JSON Interpretation for the live anchor. Preserve original quotations. "
        instruction += "Do not select bound_trade_id; Python owns setup binding. Schema:\n" + json.dumps(Interpretation.model_json_schema())
        try:
            spans = [span.model_dump(mode="json") for span in candidates(packet)]
        except DecisionFailure:
            spans = []
        source = {key: value for key, value in packet.items() if key != "approved_source_guidance"}
        effort = load_config_readonly().get("agent", {}).get("reasoning_effort", "ultra")
        messages = [{"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps({"source": source, "candidates": spans,
                                                             "decision_failure": failure_code}, ensure_ascii=False)}]
        started = time.monotonic()
        route = {}
        captured = NativeUsageCapture()

        def invoke():
            token = set_accounting_context(captured, "aqla-source-usage")
            try:
                return call_llm(task="source_interpretation", model=model,
                    provider=runtime.get("provider"), base_url=runtime.get("base_url"), api_key=runtime.get("api_key"),
                    main_runtime={**runtime, "model": model}, messages=messages, tools=None, max_tokens=32768, timeout=90,
                    reasoning_config={"enabled": True, "effort": effort}, route_info=route,
                    extra_headers={"x-opencode-session": "aqla-source-" + digest(packet)[:24]})
            finally:
                reset_accounting_context(token)

        try:
            response = await asyncio.to_thread(invoke)
            raw = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
            measured = normalize_usage(raw, provider=route.get("provider", runtime.get("provider"))) if raw else None
            cost = {"cost_status": "unknown", "cost_usd": None}
            billing = captured.rows[0] if len(captured.rows) == 1 else {}
            endpoint = billing.get("billing_base_url")
            confirmed_route = (billing.get("billing_provider") == runtime.get("provider") and endpoint
                               and endpoint.rstrip("/") == (runtime.get("base_url") or "").rstrip("/"))
            if measured is not None and confirmed_route:
                try:
                    priced = await asyncio.to_thread(estimate_usage_cost, route.get("model", model), measured,
                        provider=runtime.get("provider"), base_url=runtime.get("base_url"), api_key=runtime.get("api_key"))
                    cost = {"cost_status": priced.status,
                            "cost_usd": float(priced.amount_usd) if priced.amount_usd is not None else None,
                            "cost_source": priced.source, "pricing_version": priced.pricing_version}
                except Exception:
                    # Measurement must never invalidate a successful interpretation.
                    pass
            usage.append({"phase": "interpretation_fallback", "provider": route.get("provider", runtime.get("provider")),
                          "model": route.get("model", model), "duration_seconds": time.monotonic() - started,
                          "failed": False, "usage_available": measured is not None,
                          **cost,
                          **({key: getattr(measured, key) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")} if measured else {})})
            text = extract_content_or_reasoning(response, max_reasoning_chars=0).strip()
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            interpretation = Interpretation.model_validate_json(text)
            validate_interpretation(packet, interpretation)
            return DecisionResult(interpretation=interpretation, model=route.get("model", model), fallback=True, usage=usage.copy())
        except Exception as error:
            if not usage or usage[-1].get("phase") != "interpretation_fallback":
                usage.append({"phase": "interpretation_fallback", "model": model, "failed": True,
                              "cost_status": "unknown", "cost_usd": None,
                              "usage_available": False, "duration_seconds": time.monotonic() - started})
            raise DecisionFailure("fallback_unresolved") from error

    async def tick(self):
        await self.bind()
        try:
            profile = self.profile()
        except DecisionFailure:
            # An invalid/missing activation profile cannot stop the configured model fallback.
            profile = CalibrationProfile()
        master_hash = self.extension.skill_hash()
        revision = self.config.model_revision or self.config.model
        if hasattr(self.adapter, "identity"):
            try:
                revision = await self.adapter.identity()
            except DecisionFailure:
                pass
        policy_version = digest([VERSION, self.config.model, revision, profile.model_dump(mode="json"), master_hash])
        selected = await self.internal("source_prepare", processing_version=policy_version)
        job = selected.get("job")
        if job is None:
            return
        measured = UsageRun("source_python")
        measured.workflow.update(source_job_id=job["job_id"], source_revision_hash=digest(job["event_id"]),
                                 processing_mode=self.config.mode, cache_hit=False)
        result, failure = None, None
        try:
            packet = selected.get("packet")
            if packet is None:
                parts, offset = [], 0
                for _ in range(500):
                    page = await self.internal("source_packet", job_id=job["job_id"], offset=offset)
                    if page.get("fingerprint") != job["fingerprint"] or not isinstance(page.get("chunk"), str):
                        raise DecisionFailure("source_packet_changed")
                    parts.append(page["chunk"])
                    if page.get("eof"):
                        packet = json.loads("".join(parts))
                        break
                    next_offset = page.get("next_offset")
                    if type(next_offset) is not int or next_offset != offset + len(page["chunk"]):
                        raise DecisionFailure("source_packet_cursor")
                    offset = next_offset
                if packet is None:
                    raise DecisionFailure("source_packet_limit")
            packet["approved_source_guidance"] = self.extension.master_path.read_text(encoding="utf-8")
            if job.get("result") is not None:
                result = DecisionResult.model_validate(job["result"])
                measured.workflow["cache_hit"] = True
            else:
                usage = measured.api
                try:
                    anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
                    if not anchor["text"].strip():
                        result = DecisionResult(model="python", interpretation=Interpretation(
                            kind="incomplete", source_refs=[anchor["source_id"]], reason="Originaltext fehlt.",
                            reason_codes=["missing_source_text"]))
                    else:
                        result = await self.interpret(packet, profile, master_hash, revision, usage)
                except (AqlaError, ValueError) as error:
                    code = str(error) if isinstance(error, DecisionFailure) else "decision_invalid"
                    if self.config.mode == "shadow":
                        raise DecisionFailure(code) from error
                    result = await self.fallback(packet, code, usage)
                if self.extension.skill_hash() != master_hash:
                    raise DecisionFailure("master_changed")
                await self.internal("source_record", job_id=job["job_id"], decision_result=result.model_dump(mode="json"))
            measured.workflow.update(fallback=result.fallback, decision_success=not result.fallback,
                                     payload_characters=len(json.dumps(packet, ensure_ascii=False)))
            if self.config.mode == "hybrid":
                applied = await self.internal("source_apply", job_id=job["job_id"])
                measured.workflow["source_job_status"] = applied.get("status")
            else:
                measured.workflow["source_job_status"] = "shadow"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = str(error) if isinstance(error, DecisionFailure) else "source_processing_failed"
            await self.internal("source_fail", job_id=job["job_id"], failure_code=failure)
        finally:
            measured.workflow["source_job_failed"] = failure is not None
            await asyncio.to_thread(measured.persist, self.extension.master_path.parents[2],
                                   {"failed": failure is not None, "completed": failure is None})

    async def interpret(self, packet, profile, master_hash, revision, usage):
        if len(json.dumps(packet, ensure_ascii=False)) > self.config.max_context_characters:
            raise DecisionFailure("source_context_limit")
        source = next(row["chat_id"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
        if self.config.mode == "hybrid" and not profile.permits(source, master_hash, model=self.config.model, model_revision=revision):
            raise DecisionFailure("source_not_calibrated")
        return await self.adapter.interpret(packet, profile, collect=self.config.mode == "shadow", usage=usage)

    async def deliver(self):
        await self.bind()
        if self.config.mode != "hybrid" or self.extension.binding is None:
            return
        selected = await self.internal("delivery_next")
        if selected.get("delivery_id") is None:
            return
        success = False
        try:
            # No final-response ledger: this outbox is the ONE owner of delivery retries.
            result = await asyncio.wait_for(self.extension.binding[0].send(self.extension.owner, selected["text"]), 20)
            success = result.success is True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await self.internal("delivery_complete", delivery_id=selected["delivery_id"],
                            delivery_part=selected["part"], delivery_success=success)
