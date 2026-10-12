import asyncio
import json

import pytest
from test_pipelines import routed  # noqa: F401 -- independent exchange boundaries

from aqla_agent.decisions import DecisionResult
from aqla_agent.observations import TemporaryObservation
from aqla_agent.source_processing import SourceProcessor
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server


async def rpc(server, action, **kwargs):
    response = await server.call_tool("integration", {"action": action,
        "token": server.aqla_authority.key.decode(), "processing_owner": "native-worker", **kwargs})
    return response.structured_content or json.loads(response.content[0].text)


@pytest.fixture
async def native_processor(setup):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    await rpc(server, "source_configure", processing_mode="hybrid")
    yield server, state, fake
    await close_server(server)


def classification(source):
    return DecisionResult(model="inception/mercury-decide:free", interpretation=Interpretation(
        kind="report", source_refs=[source.source_id], reason="Result report",
        reason_codes=["result_report"], evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)]))


@pytest.mark.asyncio
async def test_persisted_classification_resumes_and_does_not_repeat(native_processor):
    server, state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="BTC result report, no new trade.")
    server.aqla_trading.ingest(source)
    first = await rpc(server, "source_prepare", processing_version="policy-v1")
    identity = first["job"]["job_id"]
    await rpc(server, "source_record", job_id=identity, decision_result=classification(source).model_dump(mode="json"))
    await rpc(server, "source_configure", processing_mode="hybrid")
    resumed = await rpc(server, "source_prepare", processing_version="policy-v1")
    assert resumed["job"]["result"]["interpretation"]["kind"] == "report"
    await rpc(server, "source_apply", job_id=identity)
    assert state.snapshot()["events"][source.event_id]["status"] == "report"
    assert (await rpc(server, "source_apply", job_id=identity))["already_processed"]
    assert (await rpc(server, "source_prepare", processing_version="policy-v1"))["job"] is None
    assert fake.actions == []


@pytest.mark.asyncio
async def test_shadow_cannot_mutate_source_or_trade_state(native_processor):
    server, state, fake = native_processor
    await rpc(server, "source_configure", processing_mode="shadow")
    source = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_record", job_id=job["job_id"], decision_result=classification(source).model_dump(mode="json"))
    with pytest.raises(Exception, match="Shadow"):
        await rpc(server, "source_apply", job_id=job["job_id"])
    assert state.snapshot()["events"][source.event_id]["status"] == "pending"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_changed_context_and_native_owner_reject_old_result(native_processor):
    server, _, _ = native_processor
    source = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    server.aqla_trading.ingest(SourceEvent(chat_id="channel", message_id=4, sent_at=now_ms() + 1, text="New context."))
    with pytest.raises(Exception, match="context changed"):
        await rpc(server, "source_record", job_id=job["job_id"], decision_result=classification(source).model_dump(mode="json"))
    await rpc(server, "source_configure", processing_mode="hybrid", processing_owner="replacement-worker")
    with pytest.raises(Exception, match="ownership changed"):
        await rpc(server, "source_prepare", processing_version="p")


@pytest.mark.asyncio
async def test_failed_unchanged_job_does_not_restart_model_work(native_processor):
    server, state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_fail", job_id=job["job_id"], failure_code="fallback_unresolved")
    assert (await rpc(server, "source_prepare", processing_version="p"))["job"] is None
    assert state.snapshot()["events"][source.event_id]["status"] == "pending"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_unverified_trade_cannot_enter_executor(native_processor, signal):
    server, _, fake = native_processor
    source, interpretation = signal
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    result = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation)
    with pytest.raises(Exception, match="second verification"):
        await rpc(server, "source_record", job_id=job["job_id"], decision_result=result.model_dump(mode="json"))
    assert fake.actions == []


@pytest.mark.asyncio
async def test_hybrid_blocks_legacy_source_tools_but_preserves_owner_control(native_processor, signal):
    server, _, fake = native_processor
    source, interpretation = signal
    with pytest.raises(Exception, match="Python processor"):
        await server.call_tool("interpret_source", {"event_id": source.event_id,
            "interpretation": interpretation.model_dump(mode="json"),
            "context_auth": server.aqla_authority.sign("source", "legacy-turn")})
    await server.call_tool("control", {"action": "pause", "context_auth": server.aqla_authority.sign("operator", "owner")})
    assert fake.actions == []


@pytest.mark.asyncio
async def test_actual_entry_uses_existing_engine_and_saved_plan_only_once(native_processor, signal):
    server, state, fake = native_processor
    _, interpretation = signal
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    result = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True)
    await rpc(server, "source_record", job_id=job["job_id"], decision_result=result.model_dump(mode="json"))
    applied = await rpc(server, "source_apply", job_id=job["job_id"])
    assert len(applied["plan_ids"]) == 1
    assert fake.position > 0
    count = len(fake.actions)
    await rpc(server, "source_apply", job_id=job["job_id"])
    assert len(fake.actions) == count
    assert state.snapshot()["source_jobs"][job["job_id"]]["status"] == "done"


@pytest.mark.asyncio
async def test_mcp_incarnation_change_resumes_proved_unsubmitted_job(native_processor):
    server, state, fake = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "same-gateway")
    source = SourceEvent(chat_id="channel", message_id=9, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = processor.prepare("same-gateway", "p")["job"]
    processor.record("same-gateway", job["job_id"], classification(source))
    state.update_metadata(("source_jobs",), lambda data: data["source_jobs"][job["job_id"]].update(status="applying"))
    assert processor.prepare("same-gateway", "p")["job"] is None
    restarted = SourceProcessor(state, server.aqla_trading)
    restarted.configure("hybrid", "same-gateway")
    resumed = restarted.prepare("same-gateway", "p")["job"]
    assert resumed["status"] == "interpreted"
    await restarted.apply("same-gateway", resumed["job_id"])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_identical_status_recurs_after_recovery_as_a_new_transition(native_processor):
    server, state, _ = native_processor
    await rpc(server, "source_notice", failure_code="reader_failed", processing_version="a")
    first = state.snapshot()["notifications"]
    await rpc(server, "source_notice", failure_code="reader_failed", processing_version="a")
    assert state.snapshot()["notifications"] == first
    await rpc(server, "source_notice", failure_code="reader_recovered", processing_version="clear")
    await rpc(server, "source_notice", failure_code="reader_failed", processing_version="a")
    assert len(state.snapshot()["notifications"]) == 3


@pytest.mark.asyncio
async def test_large_originals_use_complete_native_pages_and_stale_pages_fail(native_processor):
    server, _, fake = native_processor
    text = "Оригинальный текст 100. " * 400
    source = SourceEvent(chat_id="channel", message_id=9, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    response = await rpc(server, "source_prepare", processing_version="p")
    assert response["packet_required"] and "packet" not in response
    identity, chunks, offset = response["job"]["job_id"], [], 0
    while True:
        page = await rpc(server, "source_packet", job_id=identity, offset=offset)
        chunks.append(page["chunk"])
        assert len(page["chunk"]) <= 4096
        if page["eof"]:
            break
        offset = page["next_offset"]
    packet = json.loads("".join(chunks))
    assert packet["messages"][0]["text"] == text
    server.aqla_trading.ingest(SourceEvent(chat_id="channel", message_id=10, sent_at=now_ms() + 1, text="New reply context"))
    with pytest.raises(Exception, match="changed during native read"):
        await rpc(server, "source_packet", job_id=identity, offset=0)
    assert fake.actions == []


@pytest.mark.asyncio
async def test_temporary_preview_resumes_same_interpretation_without_new_model(native_processor, signal, monkeypatch):
    server, state, fake = native_processor
    source, interpretation = signal
    engine = server.aqla_trading.engine_for("hyperliquid", "testnet")
    original = engine.preview
    calls = []

    async def temporary(event_id, value):
        calls.append(value.model_dump(mode="json"))
        if len(calls) == 1:
            raise TemporaryObservation("read_unavailable", "preview", "transport")
        return await original(event_id, value)

    monkeypatch.setattr(engine, "preview", temporary)
    selected = await rpc(server, "source_prepare", processing_version="p")
    identity = selected["job"]["job_id"]
    result = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True)
    await rpc(server, "source_record", job_id=identity, decision_result=result.model_dump(mode="json"))
    first = await rpc(server, "source_apply", job_id=identity)
    assert first["status"] == "waiting_preview" and first["plan_ids"] == []
    assert fake.actions == []
    assert (await rpc(server, "source_prepare", processing_version="p"))["job"] is None
    def make_due(data):
        for target in data["events"][source.event_id]["targets"].values():
            target["observation_retry"]["next_retry_at"] = 0
    state.update(make_due)
    resumed = await rpc(server, "source_prepare", processing_version="p")
    assert resumed["job"]["job_id"] == identity and resumed["job"]["result"] == result.model_dump(mode="json")
    final = await rpc(server, "source_apply", job_id=identity)
    assert final["status"] == "done" and fake.position > 0, state.snapshot()["events"][source.event_id].get("targets")
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.asyncio
async def test_changed_accepted_source_cancels_pending_preview(native_processor, signal, monkeypatch):
    server, state, fake = native_processor
    source, interpretation = signal
    engine = server.aqla_trading.engine_for("hyperliquid", "testnet")

    async def unavailable(*args):
        raise TemporaryObservation("read_unavailable", "preview", "transport")

    monkeypatch.setattr(engine, "preview", unavailable)
    identity = (await rpc(server, "source_prepare", processing_version="p"))["job"]["job_id"]
    result = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True)
    await rpc(server, "source_record", job_id=identity, decision_result=result.model_dump(mode="json"))
    await rpc(server, "source_apply", job_id=identity)
    edited = source.model_copy(update={"edited_at": source.sent_at + 1, "text": source.text.replace("90", "95")})
    server.aqla_trading.ingest(edited)
    def make_due(data):
        for target in data["events"][source.event_id]["targets"].values():
            target["observation_retry"]["next_retry_at"] = 0
    state.update(make_due)
    await rpc(server, "source_prepare", processing_version="p")
    assert state.snapshot()["source_jobs"][identity]["status"] == "failed"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_partial_execution_resumes_only_pending_preview_leg_after_restart(routed, setup, signal, monkeypatch):  # noqa: F811
    router, boundaries = routed
    state = setup[1]
    _, interpretation = signal
    processor = SourceProcessor(state, router)
    processor.configure("hybrid", "worker")
    aster = router.engines["aster"]
    original, attempts = aster.preview, []

    async def temporary(event_id, value):
        attempts.append(event_id)
        if len(attempts) == 1:
            raise TemporaryObservation("read_unavailable", "preview", "transport")
        return await original(event_id, value)

    monkeypatch.setattr(aster, "preview", temporary)
    job = processor.prepare("worker", "p")["job"]
    result = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True)
    processor.record("worker", job["job_id"], result)
    first = await processor.apply("worker", job["job_id"])
    assert first["status"] == "waiting_preview"
    assert boundaries["hyperliquid"].position > 0 and boundaries["lighter"].position > 0
    assert boundaries["aster"].position == 0
    healthy_calls = {name: len(boundaries[name].actions) for name in ("hyperliquid", "lighter")}
    def make_due(data):
        for target in data["events"][job["event_id"]]["targets"].values():
            if target["status"] == "pending":
                target["observation_retry"]["next_retry_at"] = 0
    state.update(make_due)
    restarted = SourceProcessor(state, router)
    restarted.configure("hybrid", "worker")
    assert restarted.prepare("worker", "p")["job"]["job_id"] == job["job_id"]
    final = await restarted.apply("worker", job["job_id"])
    assert final["status"] == "done" and boundaries["aster"].position > 0
    assert all(len(boundaries[name].actions) == count for name, count in healthy_calls.items())
    await router.close()


@pytest.mark.asyncio
async def test_restart_before_first_dispatch_retains_healthy_saved_plans(routed, setup, signal, monkeypatch):  # noqa: F811
    router, boundaries = routed
    processor = SourceProcessor(setup[1], router)
    processor.configure("hybrid", "worker")
    _, interpretation = signal

    async def unavailable(*args):
        raise TemporaryObservation("read_unavailable", "preview", "transport")

    async def interrupted(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(router.engines["aster"], "preview", unavailable)
    execute = router.execute_many
    monkeypatch.setattr(router, "execute_many", interrupted)
    job = processor.prepare("worker", "p")["job"]
    processor.record("worker", job["job_id"], DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True))
    with pytest.raises(asyncio.CancelledError):
        await processor.apply("worker", job["job_id"])
    assert all(not boundary.actions for boundary in boundaries.values())
    monkeypatch.setattr(router, "execute_many", execute)
    restarted = SourceProcessor(setup[1], router)
    restarted.configure("hybrid", "worker")
    assert restarted.prepare("worker", "p")["job"]["job_id"] == job["job_id"]
    await restarted.apply("worker", job["job_id"])
    assert boundaries["hyperliquid"].position > 0 and boundaries["lighter"].position > 0
    assert boundaries["aster"].position == 0
    await router.close()


@pytest.mark.asyncio
async def test_restart_during_resumed_preview_retains_pending_leg(routed, setup, signal, monkeypatch):  # noqa: F811
    router, boundaries = routed
    state = setup[1]
    processor = SourceProcessor(state, router)
    processor.configure("hybrid", "worker")
    _, interpretation = signal
    aster = router.engines["aster"]
    original, attempts = aster.preview, []

    async def interrupted_preview(event_id, value):
        attempts.append(event_id)
        if len(attempts) == 1:
            raise TemporaryObservation("read_unavailable", "preview", "transport")
        if len(attempts) == 2:
            raise asyncio.CancelledError
        return await original(event_id, value)

    monkeypatch.setattr(aster, "preview", interrupted_preview)
    job = processor.prepare("worker", "p")["job"]
    processor.record("worker", job["job_id"], DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True))
    await processor.apply("worker", job["job_id"])
    healthy_calls = {name: len(boundaries[name].actions) for name in ("hyperliquid", "lighter")}
    assert all(boundaries[name].position > 0 for name in healthy_calls)

    def make_due(data):
        for target in data["events"][job["event_id"]]["targets"].values():
            if target["status"] == "pending":
                target["observation_retry"]["next_retry_at"] = 0
    state.update(make_due)
    assert processor.prepare("worker", "p")["job"]["job_id"] == job["job_id"]
    with pytest.raises(asyncio.CancelledError):
        await processor.apply("worker", job["job_id"])
    assert state.snapshot()["source_jobs"][job["job_id"]]["status"] == "waiting_preview"
    assert any(target.get("preview_inflight_until", 0) > now_ms()
               for target in state.snapshot()["events"][job["event_id"]]["targets"].values())

    restarted = SourceProcessor(state, router)
    restarted.configure("hybrid", "worker")
    recovered = restarted.prepare("worker", "p")["job"]
    assert recovered["job_id"] == job["job_id"] and recovered["result"] == state.snapshot()["source_jobs"][job["job_id"]]["result"]
    assert (await restarted.apply("worker", job["job_id"]))["status"] == "done"
    assert boundaries["aster"].position > 0 and len(attempts) == 3
    assert all(len(boundaries[name].actions) == count for name, count in healthy_calls.items())
    await router.close()


@pytest.mark.asyncio
async def test_restart_after_final_preview_submits_only_saved_missing_plan(routed, setup, signal, monkeypatch):  # noqa: F811
    router, boundaries = routed
    state = setup[1]
    processor = SourceProcessor(state, router)
    processor.configure("hybrid", "worker")
    _, interpretation = signal
    aster = router.engines["aster"]
    original, attempts = aster.preview, []

    async def temporary(event_id, value):
        attempts.append(event_id)
        if len(attempts) == 1:
            raise TemporaryObservation("read_unavailable", "preview", "transport")
        return await original(event_id, value)

    monkeypatch.setattr(aster, "preview", temporary)
    job = processor.prepare("worker", "p")["job"]
    processor.record("worker", job["job_id"], DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation, verified=True))
    await processor.apply("worker", job["job_id"])
    healthy_calls = {name: len(boundaries[name].actions) for name in ("hyperliquid", "lighter")}

    def make_due(data):
        for target in data["events"][job["event_id"]]["targets"].values():
            if target["status"] == "pending":
                target["observation_retry"]["next_retry_at"] = 0
    state.update(make_due)
    execute = router.execute_many

    async def interrupted(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr(router, "execute_many", interrupted)
    assert processor.prepare("worker", "p")["job"]["job_id"] == job["job_id"]
    with pytest.raises(asyncio.CancelledError):
        await processor.apply("worker", job["job_id"])
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "waiting_preview" and len(saved["plan_ids"]) == 3
    assert not processor.pending_previews(state.snapshot(), processor.read_job("worker", job["job_id"]))
    assert boundaries["aster"].position == 0

    monkeypatch.setattr(router, "execute_many", execute)
    restarted = SourceProcessor(state, router)
    restarted.configure("hybrid", "worker")
    assert restarted.prepare("worker", "p")["job"]["job_id"] == job["job_id"]
    assert (await restarted.apply("worker", job["job_id"]))["status"] == "done"
    assert boundaries["aster"].position > 0 and len(attempts) == 2
    assert all(len(boundaries[name].actions) == count for name, count in healthy_calls.items())
    await router.close()
