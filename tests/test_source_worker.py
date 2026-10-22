import asyncio
import json
import sqlite3
import sys
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_decisions import response_for

from aqla_agent.decisions import (
    CalibrationProfile,
    DecisionFailure,
    DecisionResult,
    OpenRouterDecisionAdapter,
)
from aqla_agent.source_worker import SourceWorker
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server


def report(packet):
    anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    return Interpretation(kind="report", source_refs=[anchor["source_id"]], reason="Result report",
        reason_codes=["result_report"], evidence=[Evidence(field="classification", source_id=anchor["source_id"], quote=anchor["text"])])


@pytest.fixture
async def worker_boundary(setup, tmp_path):
    settings, state, fake, exchange, _ = setup
    settings.telegram_owner_id = "42"
    server = create_server(settings, state=state, exchange=exchange)
    calls, sent = [], []

    async def mcp_call(_, tool, args):
        calls.append((tool, args["action"]))
        value = await server.call_tool(tool, args)
        return value.structured_content or json.loads(value.content[0].text)

    async def send(owner, text):
        sent.append((owner, text))
        return SimpleNamespace(success=True)

    master = tmp_path / "home/skills/aqla-master/SKILL.md"
    master.parent.mkdir(parents=True)
    master.write_text("Synthetic master")
    extension = SimpleNamespace(authority=server.aqla_authority, server="aqla", mcp_call=mcp_call,
        gateway_identity="native-worker", owner="42", master_path=master, skill_hash=lambda: "master-v1",
        binding=(SimpleNamespace(send=send), None, None))
    yield extension, server, state, fake, calls, sent
    await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "shadow", "hybrid"])
async def test_frozen_notifications_use_native_transport_without_any_model_in_every_mode(worker_boundary, mode):
    extension, _, state, fake, calls, sent = worker_boundary
    async def forbidden(*_, **__):
        raise AssertionError("A ready notification must not call a model")
    worker = SourceWorker(extension, {"mode": mode}, adapter=SimpleNamespace(decide=forbidden), fallback=forbidden)
    state.update_metadata(("notifications",), lambda data: data["notifications"].__setitem__("protection:test", {"error": "Native protection observation unavailable"}))
    await worker.deliver()
    await worker.deliver()
    assert len(sent) == 1 and sent[0][0] == "42" and "protection observation" in sent[0][1]
    assert state.read("notifications")["notifications"] == {} and not fake.actions
    assert any(action == "delivery_complete" for _, action in calls)


@pytest.mark.asyncio
async def test_short_fallback_runs_once_and_empty_queue_uses_no_model(worker_boundary):
    extension, server, state, fake, calls, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="BTC result report.")
    server.aqla_trading.ingest(source)
    interpretations = []
    decision_requests = []

    def upstream(request):
        body = json.loads(request.content)
        decision_requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"], kind="report"))

    async def fallback(packet, code, usage):
        interpretations.append(packet)
        assert code == "source_not_calibrated"
        assert packet["decision_review"]["interpretation"]["kind"] == "report"
        assert packet["decision_review"]["approved"] is False
        assert packet["decision_review"]["open_questions"]
        usage.append({"phase": "interpretation_fallback", "model": "synthetic", "usage_available": True,
                      "input_tokens": 100, "output_tokens": 20, "failed": False})
        return DecisionResult(interpretation=report(packet), model="synthetic", fallback=True, usage=usage.copy())

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=fallback,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"))
        await worker.tick()
        await worker.tick()
    assert len(decision_requests) == 1
    assert len(interpretations) == 1
    assert state.snapshot()["events"][source.event_id]["status"] == "report"
    assert all(tool == "integration" for tool, _ in calls)
    with sqlite3.connect(extension.master_path.parents[2] / "cache/aqla-usage.sqlite3") as connection:
        metrics = json.loads(connection.execute("SELECT metrics FROM runs").fetchone()[0])
    assert metrics["workflow"]["fallback"] is True
    assert metrics["workflow"]["source_job_id"] in state.snapshot()["source_jobs"]
    assert metrics["workflow"]["source_job_status"] == "done"
    assert metrics["workflow"]["cache_hit"] is False
    assert metrics["workflow"]["decision_attempted"] and metrics["workflow"]["decision_reviewed"]
    assert not metrics["workflow"]["calibration_validated"]
    assert len(metrics["api_responses"]) == 2
    assert "source_read_receipt" not in json.dumps(interpretations)
    assert fake.actions == []


@pytest.mark.asyncio
async def test_fallback_domain_rejection_persists_safe_code_without_secret_or_retry(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=901, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    attempts = {"decision": 0, "fallback": 0}

    async def decision(*args, **kwargs):
        attempts["decision"] += 1
        raise DecisionFailure("synthetic_unclear")

    async def fallback(*args, **kwargs):
        attempts["fallback"] += 1
        raise AqlaError("geheimer_API_key_and_private_provider_payload")

    worker = SourceWorker(extension, {"mode": "hybrid"}, adapter=SimpleNamespace(interpret=decision), fallback=fallback)
    await worker.tick()
    await worker.tick()
    saved = next(iter(state.snapshot()["source_jobs"].values()))
    assert saved["status"] == "failed" and saved["failure_code"] == "fallback_domain_rejected"
    assert saved["failure_detail"] == {"phase": "interpretation_fallback", "code": "fallback_domain_rejected",
        "exception_type": "AqlaError", "retryable": False, "field_paths": []}
    assert saved["next_retry_at"] is None and saved["attempt_count"] == 1
    assert len(saved["failure_history"]) == 1 and attempts == {"decision": 1, "fallback": 1}
    assert "geheimer" not in json.dumps(state.snapshot())
    with sqlite3.connect(extension.master_path.parents[2] / "cache/aqla-usage.sqlite3") as connection:
        metrics = json.loads(connection.execute("SELECT metrics FROM runs").fetchone()[0])
    assert metrics["failed"] is True and metrics["workflow"]["source_job_failed"] is True
    assert metrics["workflow"]["failure_detail"] == saved["failure_detail"]
    assert "geheimer" not in json.dumps(metrics)
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.asyncio
async def test_uncalibrated_decision_is_reviewed_and_can_be_corrected_without_admission(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="BTC result report.")
    server.aqla_trading.ingest(source)
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"], kind="unrelated"))

    async def correct(packet, code, usage):
        assert code == "source_not_calibrated"
        assert packet["decision_review"]["interpretation"]["kind"] == "unrelated"
        return DecisionResult(interpretation=report(packet), model="synthetic-review", fallback=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=correct,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"))
        await worker.tick()
    assert len(requests) == 1
    assert state.snapshot()["events"][source.event_id]["interpretation"]["kind"] == "report"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_actionable_uncalibrated_decision_gets_two_calls_and_never_executes_without_review(worker_boundary, signal):
    extension, server, state, fake, _, _ = worker_boundary
    source, _ = signal
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"]))

    async def unresolved(packet, code, usage):
        assert code == "source_not_calibrated"
        assert packet["decision_review"]["interpretation"]["kind"] == "new_signal"
        raise DecisionFailure("fallback_unresolved")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=unresolved,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"))
        await worker.tick()
        await worker.tick()
    assert len(requests) == 2
    assert state.snapshot()["events"][source.event_id]["status"] == "pending"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_unavailable_decision_provider_still_uses_bounded_review(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="BTC result report.")
    server.aqla_trading.ingest(source)
    requests, reviews = [], []

    def unavailable(request):
        requests.append(request)
        return httpx.Response(503)

    async def fallback(packet, code, usage):
        reviews.append(packet["decision_review"])
        assert code == "decision_api_unavailable"
        assert packet["decision_review"]["interpretation"] is None
        return DecisionResult(interpretation=report(packet), model="synthetic", fallback=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=fallback,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"))
        await worker.tick()
    assert len(requests) == len(reviews) == 1
    assert state.snapshot()["events"][source.event_id]["status"] == "report"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_notification_only_never_calls_any_model(worker_boundary):
    extension, _, state, fake, _, sent = worker_boundary
    state.update_metadata(("notifications",), lambda data: data["notifications"].__setitem__("n", {"error": "Saved result"}))

    async def forbidden(*args):
        raise AssertionError("A model must not be used")

    worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=forbidden)
    await worker.tick()
    await worker.deliver()
    await worker.deliver()
    assert len(sent) == 1 and sent[0][0] == "42"
    assert state.snapshot()["notifications"] == {}
    assert fake.actions == []


@pytest.mark.asyncio
async def test_shadow_failure_leaves_legacy_source_ready(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="BTC result report.")
    server.aqla_trading.ingest(source)

    async def fail(*args, **kwargs):
        raise DecisionFailure("synthetic_unavailable")

    worker = SourceWorker(extension, {"mode": "shadow"}, adapter=SimpleNamespace(interpret=fail))
    await worker.tick()
    await worker.tick()
    assert state.snapshot()["events"][source.event_id]["status"] == "pending"
    assert state.snapshot()["notifications"] == {}
    assert fake.actions == []


@pytest.mark.asyncio
async def test_rebinding_occurs_even_when_gateway_identity_did_not_change(worker_boundary):
    extension, _, _, _, calls, _ = worker_boundary
    worker = SourceWorker(extension, {"mode": "hybrid"})
    await worker.bind()
    await worker.bind()
    assert sum(action == "source_configure" for _, action in calls) == 2


@pytest.mark.asyncio
async def test_calibrated_source_uses_decision_and_no_generative_model(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="BTC result report.")
    server.aqla_trading.ingest(source)
    profile = CalibrationProfile(validated=True, master_hash="master-v1", sources=["channel"],
        model_revision="inception/mercury-decide:free", dataset_hash="calibration", holdout_hash="holdout", holdout_cases=30, thresholds={"kind": 0.99},
        source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5, "accepted_accuracy": 1, "critical_errors": 0,
            "kinds": {"report": {"calibration_correct_groups": 1, "holdout_correct_accepted_groups": 2}}}},
        source_permissions={"channel": ["report"]})
    profile_path = extension.master_path.parents[2] / "profile.json"
    profile_path.write_text(profile.model_dump_json())
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"], kind="report"))

    async def forbidden(*args):
        raise AssertionError("Calibrated report must not invoke a generative model")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "calibration_file": str(profile_path)},
                              adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"), fallback=forbidden)
        await worker.tick()
    assert len(requests) == 1
    assert state.snapshot()["events"][source.event_id]["status"] == "report"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_missing_text_is_deterministic_and_uses_no_model(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="", media="photo")
    server.aqla_trading.ingest(source)

    async def forbidden(*args):
        raise AssertionError("Missing text must not use a model")

    worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=forbidden)
    await worker.tick()
    assert state.snapshot()["events"][source.event_id]["status"] == "incomplete"
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_changed,price_failed,bound_trade,route_switched", [(False, False, False, False), (True, False, False, False),
                                                                    (False, True, False, False), (False, False, True, False), (False, False, False, True)])
async def test_fallback_cost_requires_observed_endpoint_and_cannot_block_interpretation(
        worker_boundary, monkeypatch, endpoint_changed, price_failed, bound_trade, route_switched):
    extension, server, _, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text="Result report, no new orders.")
    server.aqla_trading.ingest(source)
    from aqla_agent.decisions import build_packet
    packet = build_packet(server.aqla_trading.context(source.event_id, decision=True))
    runtime = {"provider": "custom", "base_url": "https://original.example/v1", "api_key": "synthetic"}
    extension.gateway = SimpleNamespace(_resolve_session_agent_runtime=lambda **_: ("fixture", runtime))
    extension.session_key = "fixture"
    capture, prices = [], []

    def set_context(collector, session_id):
        capture.append(collector)
        return "token"

    def call_llm(**kwargs):
        assert kwargs["max_tokens"] == 8192
        assert kwargs["extra_body"]["max_tokens"] == 8192
        assert kwargs["reasoning_config"] == {"enabled":True,"effort":"medium"}
        kwargs["route_info"].update(provider="custom", model="fixture")
        capture[-1].record_auxiliary_usage("fixture", "source_interpretation", billing_provider="openrouter" if route_switched else "custom",
            model="deepseek/fixture" if route_switched else "fixture",
            billing_base_url="https://openrouter.ai/api/v1" if route_switched else "https://other.example/v1" if endpoint_changed else runtime["base_url"])
        interpretation = report(packet)
        if bound_trade:
            interpretation.bound_trade_id = "model-selected-binding"
        return {"usage": {"prompt_tokens": 100}, "content": interpretation.model_dump_json()}

    def price(*args, **kwargs):
        prices.append(kwargs)
        if price_failed:
            raise OSError("Price service unavailable")
        return SimpleNamespace(status="estimated", amount_usd=Decimal("0.007"), source="fixture", pricing_version="fixture-v1")

    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "agent.aux_accounting", SimpleNamespace(
        set_accounting_context=set_context, reset_accounting_context=lambda _: None))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(call_llm=call_llm,
        extract_content_or_reasoning=lambda response, **_: response["content"]))
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", SimpleNamespace(
        normalize_usage=lambda *args, **kwargs: usage, estimate_usage_cost=price))
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "hermes_cli.config", SimpleNamespace(load_config_readonly=lambda: {}))
    measured = []
    if bound_trade:
        with pytest.raises(DecisionFailure, match="fallback_bound_trade_forbidden"):
            await SourceWorker(extension, {"mode": "hybrid"}).generative_fallback(packet, "synthetic_unclear", measured)
        assert measured and fake.actions == []
        assert measured[0]["response_success"] and not measured[0]["interpretation_valid"]
        return
    result = await SourceWorker(extension, {"mode": "hybrid"}).generative_fallback(packet, "synthetic_unclear", measured)
    assert result.interpretation.kind == "report" and result.fallback
    assert len(prices) == int(not endpoint_changed)
    if endpoint_changed or price_failed:
        assert measured[0]["cost_status"] == "unknown" and measured[0]["cost_usd"] is None
    else:
        assert measured[0]["cost_usd"] == 0.007 and measured[0]["pricing_version"] == "fixture-v1"
    if route_switched:
        assert prices[0]["provider"] == measured[0]["provider"] == "openrouter"
        assert prices[0]["base_url"] == "https://openrouter.ai/api/v1"
        assert measured[0]["model"] == "deepseek/fixture"
    assert runtime["api_key"] not in json.dumps(measured)


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["original", "master", "questions"])
async def test_full_outgoing_input_limit_blocks_both_models_without_truncating_evidence(worker_boundary, oversized):
    extension, server, state, fake, _, _ = worker_boundary
    text = "Original unchanged text. " * 500 if oversized == "original" else (
        "BTC  " + " ".join(str(1000 + index) for index in range(45)) if oversized == "questions" else "Result report.")
    if oversized == "master":
        extension.master_path.write_text("Complete master instruction. " * 3500, encoding="utf-8")
    source = SourceEvent(chat_id="channel", message_id=101, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    requests, fallbacks = [], []

    def upstream(request):
        requests.append(request)
        raise AssertionError("Oversized input must never reach the decision API")

    async def forbidden(*args):
        fallbacks.append(args)
        raise AssertionError("Oversized input must never reach generative fallback")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "max_context_characters": 2000 if oversized == "original" else 60000}, fallback=forbidden,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="fixture/revision"))
        await worker.tick()
        await worker.tick()
    job = next(iter(state.snapshot()["source_jobs"].values()))
    assert job["status"] == "review" and job["failure_code"] == "source_context_limit"
    assert not job["failure_detail"]["retryable"]
    assert state.snapshot()["events"][source.event_id]["source"]["text"] == text
    assert not requests and not fallbacks and not fake.actions


def test_source_efficiency_excludes_cache_empty_status_and_counts_retries_as_one_revision():
    from aqla_agent.usage_metrics import source_efficiency
    def row(key, *, valid=False, fallback=False, cache=False, responses=True):
        return {"origin": "source_python", "workflow": {"source_revision_hash": key, "interpretation_valid": valid, "cache_hit": cache},
                "api_responses": [{"phase": "interpretation_fallback" if fallback else "decision", "response_success": valid,
                                   "interpretation_valid": valid, "failed": not valid}] if responses else []}
    measured = source_efficiency([row("a"), row("a", valid=True), row("b", valid=True, fallback=True),
        row("cache", valid=True, cache=True), row("empty", valid=True, responses=False), row("status", responses=False)])
    assert measured == {"fresh_source_revisions": 2, "valid_interpretations": 2, "fully_without_fallback": 1,
        "without_fallback_share": 0.5, "fallback_used": 1, "api_response_failures": 1, "invalid_model_responses": 0}


def test_received_invalid_model_response_is_not_a_provider_failure():
    from aqla_agent.usage_metrics import source_efficiency
    measured = source_efficiency([{"origin": "source_python", "workflow": {"source_revision_hash": "invalid", "interpretation_valid": False},
        "api_responses": [{"phase": "interpretation_fallback", "response_success": True,
                           "interpretation_valid": False, "failed": True}]}])
    assert measured["api_response_failures"] == 0 and measured["invalid_model_responses"] == 1
    assert measured["fresh_source_revisions"] == 1 and measured["valid_interpretations"] == 0


def test_rejected_oversized_fresh_source_is_not_removed_from_the_efficiency_denominator():
    from aqla_agent.usage_metrics import source_efficiency
    measured = source_efficiency([{"origin": "source_python", "workflow": {"source_revision_hash": "oversized",
        "source_work_type": "interpretation", "interpretation_valid": False}, "api_responses": []},
        {"origin": "source_python", "workflow": {"source_revision_hash": "empty", "source_work_type": "empty"}, "api_responses": []}])
    assert measured["fresh_source_revisions"] == 1 and measured["without_fallback_share"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_down", [False, True])
@pytest.mark.parametrize("text,code", [
    ("BTC entry half market 100 and half limit 95. Long. SL 90.", "entry_legs_not_representable"),
    ("BTC close half position and move stop to entry and cancel remaining entry orders.", "management_actions_not_representable"),
    ("BTC close tomorrow.", "scheduled_management_not_representable"),
    ("BTC long entry market 100. Stop after 1h candle close below 90.", "conditional_stop_not_representable"),
    ("BTC LONG. An expected correction will provide an opportunity to open a long position with potential growth 49.33%.", "future_entry_opportunity_requires_review"),
    ("BTC LONG. Ожидаемая коррекция предоставит возможность для открытия лонг позиции с потенциалом роста 49.33%.", "future_entry_opportunity_requires_review"),
    ("BTC LONG. Entry 100. Target: 120 (25% additional entry at", "source_financial_instruction_incomplete"),
    ("BTC LONG. Вход 100. Цель: 120 (25% Также есть точка в", "source_financial_instruction_incomplete"),
])
async def test_proven_unsupported_original_uses_mercury_first_then_review_without_generative_retry(worker_boundary, provider_down, text, code):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=102, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    requests, fallbacks = [], []

    def upstream(request):
        requests.append(request)
        return httpx.Response(503) if provider_down else httpx.Response(200, json=response_for(json.loads(request.content)["questions"], kind="report"))

    async def forbidden(*args):
        fallbacks.append(args)
        raise AssertionError("Generative model cannot override an original structural veto")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=forbidden,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="fixture/revision"))
        await worker.tick()
        await worker.tick()
    saved = next(iter(state.snapshot()["source_jobs"].values()))
    assert saved["status"] == "review" and saved["failure_code"] == code
    assert saved["review_sources"] == {source.source_id: source.event_id}
    assert saved["failure_detail"]["phase"] == "validate" and not saved["failure_detail"]["retryable"]
    assert len(requests) == 1 and not fallbacks and not fake.actions and not state.snapshot()["operations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,code", [
    ("BTC LONG. An expected correction will provide an opportunity to open a long position with growth 49.33%.", "future_entry_opportunity_requires_review"),
    ("BTC LONG. Entry 100. Target: 120 (25% additional entry at", "source_financial_instruction_incomplete"),
])
async def test_falsely_actionable_original_is_identically_vetoed_offline_and_by_native_worker(worker_boundary, text, code):
    from aqla_agent.decisions import DEFAULT_DECISION_MODEL, replay_decision
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=110, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    requests, responses, fallbacks = [], [], []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        overrides = {key: "target_1" for key, question in body["questions"].items() if key.startswith("role.")
            and json.loads(question["instructions"])["occurrence"]["quote"] in ("120", "49.33%")}
        response = response_for(body["questions"], overrides=overrides)
        responses.append(response)
        return httpx.Response(200, json=response)

    async def forbidden(*args):
        fallbacks.append(True)
        raise AssertionError("A proved original veto cannot be repaired by a generative retry")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=forbidden,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL))
        await worker.tick()
        await worker.tick()
    with pytest.raises(DecisionFailure, match=code):
        replay_decision(requests[0]["state"], CalibrationProfile(), responses,
            model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL, collect=True)
    saved = next(iter(state.snapshot()["source_jobs"].values()))
    assert saved["status"] == "review" and saved["failure_code"] == code
    assert len(requests) == 1 and not fallbacks and not fake.actions
    assert not state.snapshot()["operations"] and not state.snapshot()["plans"]


@pytest.mark.asyncio
async def test_recovered_model_provider_resumes_same_durable_source_job(worker_boundary, monkeypatch):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=103, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    requests, attempts = [], []

    def upstream(request):
        requests.append(request)
        return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json=response_for(json.loads(request.content)["questions"], kind="report"))

    async def fallback(packet, code, usage):
        attempts.append(code)
        failed = len(attempts) == 1
        usage.append({"phase": "interpretation_fallback", "response_success": not failed,
                      "interpretation_valid": not failed, "failed": failed, "usage_available": False})
        if failed:
            raise httpx.ReadTimeout("SYNTHETIC_PROVIDER_SECRET")
        return DecisionResult(interpretation=report(packet), model="fixture", fallback=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "retry_backoff_seconds": 1}, fallback=fallback,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="fixture/revision"))
        await worker.tick()
        saved = next(iter(state.snapshot()["source_jobs"].values()))
        assert saved["status"] == "retry_wait" and saved["failure_detail"]["code"] == "provider_timeout"
        assert "SYNTHETIC_PROVIDER_SECRET" not in json.dumps(saved)
        await worker.tick()
        assert len(requests) == len(attempts) == 1
        monkeypatch.setattr("aqla_agent.source_processing.now_ms", lambda: saved["next_retry_at"])
        await worker.tick()
    jobs = state.snapshot()["source_jobs"]
    assert len(jobs) == 1 and jobs[saved["job_id"]]["status"] == "done"
    assert jobs[saved["job_id"]]["attempt_count"] == 2
    assert jobs[saved["job_id"]]["retry_deadline_at"] == saved["retry_deadline_at"]
    assert state.snapshot()["events"][source.event_id]["status"] == "report"
    assert len(requests) == len(attempts) == 2 and not fake.actions


@pytest.mark.asyncio
async def test_interrupted_model_work_is_never_measured_as_completed_interpretation(worker_boundary):
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=104, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)

    async def interrupted(packet, profile, **kwargs):
        kwargs["usage"].append({"phase": "decision", "failed": False, "response_success": True})
        raise asyncio.CancelledError

    worker = SourceWorker(extension, {"mode": "hybrid"}, adapter=SimpleNamespace(interpret=interrupted))
    with pytest.raises(asyncio.CancelledError):
        await worker.tick()
    with sqlite3.connect(extension.master_path.parents[2] / "cache/aqla-usage.sqlite3") as connection:
        metrics = json.loads(connection.execute("SELECT metrics FROM runs").fetchone()[0])
    assert metrics["interrupted"] is True and metrics["completed"] is False
    assert not metrics["workflow"]["interpretation_valid"]
    assert next(iter(state.snapshot()["source_jobs"].values()))["status"] == "prepared"
    assert not fake.actions


@pytest.mark.asyncio
async def test_original_signal_expiry_stops_retry_before_any_further_model_or_dispatch(worker_boundary, monkeypatch):
    extension, server, state, fake, _, _ = worker_boundary
    clock = now_ms()
    monkeypatch.setattr("aqla_agent.source_processing.now_ms", lambda: clock)
    state.update(lambda data: data["policy"].update(max_signal_age_seconds=5))
    source = SourceEvent(chat_id="channel", message_id=105, sent_at=clock - 4000,
                        text="BTC long entry market 100. SL 90.")
    server.aqla_trading.ingest(source)
    requests, fallbacks = [], []

    def upstream(request):
        requests.append(request)
        return httpx.Response(503)

    async def failed_fallback(*args):
        fallbacks.append(True)
        raise httpx.ReadTimeout("Synthetic transient provider failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "retry_max_age_seconds": 900}, fallback=failed_fallback,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="fixture/revision"))
        await worker.tick()
        job = next(iter(state.snapshot()["source_jobs"].values()))
        assert job["status"] == "retry_wait" and job["retry_deadline_at"] == source.sent_at + 5000
        assert job["retry_deadline_at"] < job["created_at"] + 900000
        clock = job["retry_deadline_at"]
        await worker.tick()
        await worker.tick()
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "review" and saved["outcome"] == "provider_retry_exhausted"
    assert saved["attempt_count"] == 1 and len(requests) == len(fallbacks) == 1
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("calibrated", [True, False])
async def test_only_the_actual_model_route_must_fit_its_input_budget(worker_boundary, calibrated, monkeypatch):
    from aqla_agent.decisions import build_packet, candidates, questions_for
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=106, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    profile = CalibrationProfile(validated=True, master_hash="master-v1", sources=["channel"],
        model_revision="inception/mercury-decide:free", dataset_hash="calibration", holdout_hash="holdout", holdout_cases=30,
        thresholds={"kind": 0.99}, source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5,
            "accepted_accuracy": 1, "critical_errors": 0, "kinds": {"report": {
                "calibration_correct_groups": 1, "holdout_correct_accepted_groups": 2}}}}, source_permissions={"channel": ["report"]})
    # Exercise a deliberately larger review without depending on production
    # prompt lengths or assuming the generative schema is always larger.
    original_review = SourceWorker.review_questions
    monkeypatch.setattr(SourceWorker, "review_questions", staticmethod(
        lambda code: [*original_review(code), "Detailed synthetic review context. " * 100]))
    path = extension.master_path.parents[2] / "route-profile.json"
    path.write_text(profile.model_dump_json())
    probe = SourceWorker(extension, {"mode": "hybrid"})
    packet = build_packet(server.aqla_trading.context(source.event_id, decision=True))
    packet["approved_source_guidance"] = extension.master_path.read_text(encoding="utf-8")
    spans = candidates(packet)
    decision_size = len(json.dumps({"model": probe.config.model, "state": packet, "questions": questions_for(packet, spans)}, ensure_ascii=False))
    reviewed = {**packet, "decision_review": {"failure_code": "source_not_calibrated",
                "open_questions": probe.review_questions("source_not_calibrated")}}
    fallback_size = len(json.dumps(probe.fallback_messages(reviewed, "decision_review_required", spans), ensure_ascii=False))
    limit = decision_size + 10
    assert decision_size < limit < fallback_size
    requests, fallbacks = [], []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json=response_for(json.loads(request.content)["questions"], kind="report"))

    async def forbidden(*args):
        fallbacks.append(True)
        raise AssertionError("The oversized fallback route must never be invoked")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "max_context_characters": limit,
            "calibration_file": str(path) if calibrated else None}, fallback=forbidden,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision="inception/mercury-decide:free"))
        await worker.tick()
    job = next(iter(state.snapshot()["source_jobs"].values()))
    if calibrated:
        assert job["status"] == "done" and state.snapshot()["events"][source.event_id]["status"] == "report"
    else:
        assert job["status"] == "review" and job["failure_code"] == "source_context_limit"
    assert len(requests) == 1 and not fallbacks and not fake.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("text,kind,failure", [
    ("Trading example only: BTC close half and move stop to95", "report", None),
    ("Trading example only: BTC close half and move stop to95", "unrelated", None),
    ("Earlier we agreed to close half and move stop to95 and cancel entry", "report", None),
    ("BTC close half and move stop to95", "ambiguous", None),
    ("BTC close half and move stop to95", "incomplete", None),
    ("BTC close half and move stop to95 and cancel entry", "report", "management_actions_not_representable"),
    ("Earlier we agreed to close half and now move stop to95 and cancel entry", "unrelated", "management_actions_not_representable"),
])
async def test_common_nonaction_contract_matches_native_worker_and_offline_replay(worker_boundary, text, kind, failure):
    from aqla_agent.decisions import DEFAULT_DECISION_MODEL, replay_decision
    extension, server, state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=107, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    profile = CalibrationProfile(validated=True, master_hash="master-v1", sources=["channel"],
        model_revision=DEFAULT_DECISION_MODEL, dataset_hash="calibration", holdout_hash="holdout", holdout_cases=30,
        thresholds={"kind": 0.99}, source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5,
            "accepted_accuracy": 1, "critical_errors": 0, "kinds": {kind: {
                "calibration_correct_groups": 1, "holdout_correct_accepted_groups": 2}}}}, source_permissions={"channel": [kind]})
    path = extension.master_path.parents[2] / "nonaction-profile.json"
    path.write_text(profile.model_dump_json())
    requests, responses, fallbacks = [], [], []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        value = response_for(body["questions"], kind=kind)
        responses.append(value)
        return httpx.Response(200, json=value)

    async def forbidden(*args):
        fallbacks.append(True)
        raise AssertionError("Correct nonaction or a hard original veto never needs generative override")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        worker = SourceWorker(extension, {"mode": "hybrid", "calibration_file": str(path)}, fallback=forbidden,
            adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL))
        await worker.tick()
    job = next(iter(state.snapshot()["source_jobs"].values()))
    if failure:
        with pytest.raises(DecisionFailure, match=failure):
            replay_decision(requests[0]["state"], profile, responses, model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
        assert job["status"] == "review" and job["failure_code"] == failure
    else:
        offline = replay_decision(requests[0]["state"], profile, responses, model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
        assert job["status"] == "done" and state.snapshot()["events"][source.event_id]["status"] == kind
        assert job["result"]["interpretation"] == offline.interpretation.model_dump(mode="json")
    assert len(requests) == 1 and not fallbacks and not fake.actions and not state.snapshot()["operations"]
