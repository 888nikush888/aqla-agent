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
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
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
        source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5, "accepted_accuracy": 1, "critical_errors": 0}},
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
@pytest.mark.parametrize("endpoint_changed,price_failed,bound_trade", [(False, False, False), (True, False, False),
                                                                    (False, True, False), (False, False, True)])
async def test_fallback_cost_requires_observed_endpoint_and_cannot_block_interpretation(
        worker_boundary, monkeypatch, endpoint_changed, price_failed, bound_trade):
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
        kwargs["route_info"].update(provider="custom", model="fixture")
        capture[-1].record_auxiliary_usage("fixture", "source_interpretation", billing_provider="custom",
            billing_base_url="https://other.example/v1" if endpoint_changed else runtime["base_url"])
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
        with pytest.raises(DecisionFailure, match="fallback_unresolved"):
            await SourceWorker(extension, {"mode": "hybrid"}).generative_fallback(packet, "synthetic_unclear", measured)
        assert measured and fake.actions == []
        return
    result = await SourceWorker(extension, {"mode": "hybrid"}).generative_fallback(packet, "synthetic_unclear", measured)
    assert result.interpretation.kind == "report" and result.fallback
    assert len(prices) == int(not endpoint_changed)
    if endpoint_changed or price_failed:
        assert measured[0]["cost_status"] == "unknown" and measured[0]["cost_usd"] is None
    else:
        assert measured[0]["cost_usd"] == 0.007 and measured[0]["pricing_version"] == "fixture-v1"
    assert runtime["api_key"] not in json.dumps(measured)
