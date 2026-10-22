"""Native Python orchestration with a short, tool-free model fallback."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, Interpretation, digest

from .decisions import (
    QUESTION_CONTRACT_VERSION,
    VERSION,
    CalibrationProfile,
    DecisionFailure,
    DecisionResult,
    OpenRouterDecisionAdapter,
    SourceProcessingConfig,
    candidates,
    mark_decision_failure,
    questions_for,
    validate_interpretation,
)
from .semantic_contract import unsupported_structure
from .source_failure import SourceFailure, classify_failure
from .usage_metrics import UsageRun

FALLBACK_VERSION = "aqla-source-fallback-v4"
PROCESSING_VERSION = "aqla-decision-first-v2"


def fallback_schema(packet):
    """Constrain opaque references to supplied originals, not workflow/event IDs."""
    schema = Interpretation.model_json_schema()
    sources = sorted({row["source_id"] for row in packet["messages"]})
    schema["properties"]["source_refs"]["items"]["enum"] = sources
    schema["properties"]["source_refs"]["contains"] = {"const": packet["anchor_source_id"]}
    schema["$defs"]["Evidence"]["properties"]["source_id"]["enum"] = sources
    schema["properties"]["bound_trade_id"] = {"type": "null", "default": None}
    return schema


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
        try:
            profile = self.profile()
        except DecisionFailure:
            profile = CalibrationProfile()
        await self.internal("source_configure", processing_mode=self.config.mode,
            retry_max_attempts=getattr(self.config, "retry_max_attempts", 3),
            retry_backoff_seconds=getattr(self.config, "retry_backoff_seconds", 15),
            retry_max_age_seconds=getattr(self.config, "retry_max_age_seconds", 900),
            decision_status={"provider": "openrouter", "model": self.config.model,
                             "validated": profile.validated and profile.master_hash == self.extension.skill_hash(),
                             "permissions": profile.source_permissions if profile.validated and profile.master_hash == self.extension.skill_hash() else {}})
        adapter = self.extension.binding[0] if self.extension.binding else None
        if adapter is not None and adapter is not self.bound_adapter:
            # A newly connected native transport may resume exhausted jobs; mere polling cannot.
            await self.internal("delivery_retry")
            self.bound_adapter = adapter
        self.bound_owner = self.extension.gateway_identity

    def fallback_messages(self, packet, failure_code, spans):
        master = packet.get("approved_source_guidance") or self.extension.master_path.read_text(encoding="utf-8")
        instruction = master + "\nInterpretation only. No tools, no account/configuration changes. "
        instruction += "Return exactly one JSON Interpretation for the live anchor. Preserve original quotations. "
        instruction += "A current partial reduction followed by break_even or move_stop uses update_action=sequence and exactly two evidenced management_steps. Other combinations and conditional/future management remain ambiguous/incomplete; never omit a separate instruction. "
        instruction += "An unsupported future schedule/trigger for an update remains ambiguous; never turn a future close into an immediate one. "
        instruction += "Use source_id values from the original messages/candidates for source_refs and Evidence.source_id; "
        instruction += "never substitute event_id, chat_id, message_id or candidate_id. source_refs MUST include " + packet["anchor_source_id"] + ". "
        instruction += "Do not select bound_trade_id; Python owns setup binding. "
        if packet.get("decision_review"):
            instruction += "\nReview the preliminary decision_review against the unchanged originals and candidates. "
            instruction += "It is an unapproved model proposal, never an execution permission or a new source. "
            instruction += "Resolve the listed open_questions, correct wrong fields and references, and return the complete Interpretation. "
            instruction += "An uncertain case stays incomplete/ambiguous; do not invent missing values."
        instruction += "\nSchema:\n" + json.dumps(fallback_schema(packet))
        source = {key: value for key, value in packet.items() if key != "approved_source_guidance"}
        return [{"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps({"source": source, "candidates": [span.model_dump(mode="json") for span in spans],
                                                          "decision_failure": failure_code}, ensure_ascii=False)}]

    def check_input(self, body):
        size = len(json.dumps(body, ensure_ascii=False))
        if size > self.config.max_context_characters:
            error = DecisionFailure("source_context_limit")
            error.checked_input_characters = size
            raise error
        return size

    async def generative_fallback(self, packet, failure_code, usage, *, spans=None):
        # The pinned native client owns provider resolution, quota fallback and wire reasoning.
        from agent.aux_accounting import reset_accounting_context, set_accounting_context
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
        from agent.usage_pricing import estimate_usage_cost, normalize_usage

        extension = self.extension
        model, runtime = await asyncio.to_thread(extension.gateway._resolve_session_agent_runtime,
                                                 session_key=extension.session_key)
        spans = candidates(packet) if spans is None else spans
        effort = self.config.fallback_reasoning_effort
        messages = self.fallback_messages(packet, failure_code, spans)
        self.check_input({"model": model, "messages": messages, "tools": None,
                          "max_tokens": self.config.fallback_max_tokens,
                          "reasoning": {"enabled": True, "effort": effort}})
        started = time.monotonic()
        route = {}
        captured = NativeUsageCapture()

        def invoke():
            token = set_accounting_context(captured, "aqla-source-usage")
            try:
                return call_llm(task="source_interpretation", model=model,
                    provider=runtime.get("provider"), base_url=runtime.get("base_url"), api_key=runtime.get("api_key"),
                    main_runtime={**runtime, "model": model}, messages=messages, tools=None,
                    max_tokens=self.config.fallback_max_tokens, timeout=90,
                    # Native custom routes omit the top-level cap; the body cap
                    # keeps OpenCode Go bounded and survives native quota fallback.
                    extra_body={"max_tokens": self.config.fallback_max_tokens},
                    reasoning_config={"enabled": True, "effort": effort}, route_info=route,
                    extra_headers={"x-opencode-session": "aqla-source-" + digest(packet)[:24]})
            finally:
                reset_accounting_context(token)

        try:
            response = await asyncio.to_thread(invoke)
            raw = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
            cost = {"cost_status": "unknown", "cost_usd": None}
            billing = captured.rows[0] if len(captured.rows) == 1 else {}
            endpoint = billing.get("billing_base_url")
            provider = billing.get("billing_provider")
            confirmed_route = bool(endpoint and (provider == runtime.get("provider")
                and endpoint.rstrip("/") == (runtime.get("base_url") or "").rstrip("/")
                or provider == "openrouter" and endpoint.rstrip("/") == "https://openrouter.ai/api/v1"))
            actual_provider = provider if confirmed_route else route.get("provider", runtime.get("provider"))
            actual_model = billing.get("model") if confirmed_route and billing.get("model") else route.get("model", model)
            try:
                measured = normalize_usage(raw, provider=actual_provider) if raw else None
            except (ValueError, TypeError, AttributeError):
                measured = None
            if measured is not None and confirmed_route:
                try:
                    priced = await asyncio.to_thread(estimate_usage_cost, actual_model, measured,
                        provider=actual_provider, base_url=endpoint, api_key=(os.environ.get("OPENROUTER_API_KEY")
                            if actual_provider == "openrouter" else runtime.get("api_key")))
                    cost = {"cost_status": priced.status,
                            "cost_usd": float(priced.amount_usd) if priced.amount_usd is not None else None,
                            "cost_source": priced.source, "pricing_version": priced.pricing_version}
                except Exception:
                    # Measurement must never invalidate a successful interpretation.
                    pass
            usage.append({"phase": "interpretation_fallback", "provider": actual_provider,
                          "model": actual_model, "duration_seconds": time.monotonic() - started,
                          "failed": False, "response_success": True, "interpretation_valid": False,
                          "usage_available": measured is not None,
                          **cost,
                          **({key: getattr(measured, key) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")} if measured else {})})
            text = extract_content_or_reasoning(response, max_reasoning_chars=0).strip()
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            interpretation = Interpretation.model_validate_json(text)
            if interpretation.bound_trade_id is not None:
                raise DecisionFailure("fallback_bound_trade_forbidden")
            validate_interpretation(packet, interpretation)
            usage[-1]["interpretation_valid"] = True
            return DecisionResult(interpretation=interpretation, model=actual_model, fallback=True, usage=usage.copy())
        except Exception as error:
            if not usage or usage[-1].get("phase") != "interpretation_fallback":
                usage.append({"phase": "interpretation_fallback", "model": model, "failed": True,
                              "response_success": False, "interpretation_valid": False,
                              "cost_status": "unknown", "cost_usd": None,
                              "usage_available": False, "duration_seconds": time.monotonic() - started})
            raise SourceFailure(classify_failure(error, "interpretation_fallback")) from error

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
        policy_version = digest([VERSION, QUESTION_CONTRACT_VERSION, PROCESSING_VERSION, FALLBACK_VERSION,
                                 self.config.model_dump(mode="json"), revision, profile.model_dump(mode="json"), master_hash])
        selected = await self.internal("source_prepare", processing_version=policy_version)
        job = selected.get("job")
        if job is None:
            return
        measured = UsageRun("source_python")
        measured.workflow.update(source_job_id=job["job_id"], source_revision_hash=digest(job["event_id"]),
                                 processing_mode=self.config.mode, processing_version=PROCESSING_VERSION,
                                 calibration_validated=profile.validated, cache_hit=False)
        result, failure, packet, spans = None, None, None, None
        interrupted = False
        interpretation_recorded = False
        phase = "prepare"
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
            selected_anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
            measured.workflow["source_work_type"] = "cached" if job.get("result") is not None else "interpretation" if selected_anchor["text"].strip() else "empty"
            if job.get("result") is not None:
                result = DecisionResult.model_validate(job["result"])
                if job.get("policy_version") != policy_version or result.question_version != VERSION or result.collection_only:
                    raise DecisionFailure("cached_decision_policy_changed")
                if not result.fallback and result.model != "python" and (
                        not profile.permits(source=next(row["chat_id"] for row in packet["messages"]
                            if row["event_id"] == packet["anchor_event_id"]), master_hash=master_hash,
                            model=result.model, model_revision=result.model_revision)
                        or not profile.permits_kind(next(row["chat_id"] for row in packet["messages"]
                            if row["event_id"] == packet["anchor_event_id"]), result.interpretation.kind)):
                    raise DecisionFailure("cached_decision_not_calibrated")
                measured.workflow["cache_hit"] = True
                interpretation_recorded = True
            else:
                usage = measured.api
                try:
                    anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
                    if not anchor["text"].strip():
                        result = DecisionResult(model="python", interpretation=Interpretation(
                            kind="incomplete", source_refs=[anchor["source_id"]], reason="Originaltext fehlt.",
                            reason_codes=["missing_source_text"]))
                    else:
                        # Bound this actual Decision request, including master
                        # and questions. An unused fallback must not gate Mercury.
                        phase = "decision"
                        self.check_input(packet)
                        spans = candidates(packet)
                        measured.workflow["checked_input_characters"] = self.check_input({"model": self.config.model, "state": packet,
                                                   "questions": questions_for(packet, spans)})
                        result = await self.interpret(packet, profile, master_hash, revision, usage, spans=spans)
                        # Use the same interpretation-aware admission as offline
                        # replay. Original-only heuristics belong to failure review.
                        validate_interpretation(packet, result.interpretation)
                except (AqlaError, ValueError) as error:
                    code = str(error) if isinstance(error, DecisionFailure) else "decision_invalid"
                    mark_decision_failure(usage, code)
                    if code in ("source_context_limit", "candidate_limit"):
                        raise
                    veto = unsupported_structure(packet)
                    if veto:
                        # A common original-only schema veto cannot be repaired
                        # by another provider. Preserve a durable review, not a
                        # generative attempt to discard part of the instruction.
                        raise SourceFailure({"phase": "validate", "code": veto, "exception_type": "DecisionFailure", "retryable": False}) from None
                    if self.config.mode == "shadow":
                        raise DecisionFailure(code) from error
                    phase = "interpretation_fallback"
                    self.check_input(self.fallback_messages(packet, code, spans or []))
                    result = await self.fallback(packet, code, usage, spans=spans) if self.fallback == self.generative_fallback else await self.fallback(packet, code, usage)
                    measured.workflow["fallback_reason"] = code
                if self.extension.skill_hash() != master_hash:
                    raise DecisionFailure("master_changed")
                phase = "validate"
                await self.internal("source_record", job_id=job["job_id"], decision_result=result.model_dump(mode="json"))
                interpretation_recorded = True
            measured.workflow.update(fallback=result.fallback, decision_success=not result.fallback,
                                     decision_attempted=any(item.get("phase") == "decision" for item in measured.api),
                                     decision_reviewed=bool(packet.get("decision_review")),
                                     payload_characters=len(json.dumps(packet, ensure_ascii=False)))
            if self.config.mode == "hybrid":
                try:
                    current_profile = self.profile()
                except DecisionFailure:
                    current_profile = CalibrationProfile()
                if self.extension.skill_hash() != master_hash or current_profile != profile:
                    raise DecisionFailure("source_policy_changed")
                phase = "apply"
                applied = await self.internal("source_apply", job_id=job["job_id"])
                measured.workflow["source_job_status"] = applied.get("status")
            else:
                measured.workflow["source_job_status"] = "shadow"
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as error:
            if isinstance(getattr(error, "checked_input_characters", None), int):
                measured.workflow["checked_input_characters"] = error.checked_input_characters
            detail = error.detail if isinstance(error, SourceFailure) else classify_failure(error, phase)
            failure = detail["code"]
            await self.internal("source_fail", job_id=job["job_id"], failure_code=failure, failure_detail=detail)
        finally:
            measured.workflow["decision_attempted"] = any(item.get("phase") == "decision" for item in measured.api)
            measured.workflow["decision_reviewed"] = bool(packet.get("decision_review")) if packet is not None else False
            measured.workflow["source_job_failed"] = failure is not None
            measured.workflow["interpretation_valid"] = interpretation_recorded
            measured.workflow["failure_detail"] = detail if failure is not None else None
            await asyncio.to_thread(measured.persist, self.extension.master_path.parents[2],
                                   {"failed": failure is not None, "interrupted": interrupted, "completed": failure is None and not interrupted})

    async def interpret(self, packet, profile, master_hash, revision, usage, *, spans=None):
        if len(json.dumps(packet, ensure_ascii=False)) > self.config.max_context_characters:
            raise DecisionFailure("source_context_limit")
        source = next(row["chat_id"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
        permitted = profile.permits(source, master_hash, model=self.config.model, model_revision=revision)
        # Calibration controls admission, not whether the decision model is used.
        # Unapproved sources produce explicitly collection-only proposals for review.
        observation = {}
        result = None
        model_profile = profile if permitted or self.config.mode == "shadow" else CalibrationProfile(
            model=self.config.model, model_revision=revision)
        try:
            extra = {"spans": spans, "max_context_characters": self.config.max_context_characters} if isinstance(self.adapter, OpenRouterDecisionAdapter) else {}
            result = await self.adapter.interpret(packet, model_profile,
                collect=self.config.mode == "shadow" or not permitted, usage=usage, observation=observation, **extra)
            if self.config.mode == "hybrid":
                if not permitted:
                    raise DecisionFailure("source_not_calibrated")
                if not profile.permits_kind(source, result.interpretation.kind):
                    raise DecisionFailure("source_decision_kind_not_calibrated")
        except (AqlaError, ValueError) as error:
            code = str(error) if isinstance(error, DecisionFailure) else "decision_invalid"
            packet["decision_review"] = {
                "model": self.config.model, "model_revision": revision, "question_version": VERSION,
                "approved": False, "failure_code": code,
                "interpretation": result.interpretation.model_dump(mode="json") if result else None,
                "values": observation.get("values", {}),
                "probabilities": result.probabilities if result else observation.get("probabilities", {}),
                "open_questions": self.review_questions(code),
            }
            raise
        return result

    @staticmethod
    def review_questions(code):
        questions = ["Does the anchor convey a new instruction or only a past result?",
                     "Are market, direction, entry form, quantities, units and original evidence complete and correct?",
                     "Does every selected value and reference belong to this one setup and current revision?"]
        if code in ("source_not_calibrated", "source_decision_kind_not_calibrated"):
            questions.insert(0, "Independently validate this unapproved proposal; its source/kind has no automatic release.")
        return questions

    async def deliver(self):
        await self.bind()
        if self.extension.binding is None:
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
