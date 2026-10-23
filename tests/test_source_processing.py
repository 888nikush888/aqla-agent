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
@pytest.mark.parametrize("mode", ["legacy", "shadow", "hybrid"])
async def test_source_notices_keep_internal_auth_without_requiring_a_legacy_worker(setup, mode):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    try:
        if mode != "legacy":
            await rpc(server, "source_configure", processing_mode=mode)
        args = {"action": "source_notice", "token": server.aqla_authority.key.decode(),
                "failure_code": "reader_unavailable", "processing_version": "outage"}
        before = state.snapshot()
        with pytest.raises(Exception, match="Internal integration access denied"):
            await server.call_tool("integration", {**args, "token": "untrusted"})
        if mode != "legacy":
            for owner in (None, "stale-worker"):
                with pytest.raises(Exception, match="ownership changed"):
                    await server.call_tool("integration", {**args, "processing_owner": owner})
            args["processing_owner"] = "native-worker"
        assert state.snapshot() == before
        await server.call_tool("integration", args)
        after = state.snapshot()
        assert any(row.get("source_notice", {}).get("code") == "reader_unavailable" for row in after["notifications"].values())
        assert all(after[key] == before[key] for key in ("trades", "plans", "operations", "events"))
        assert not fake.actions
    finally:
        await close_server(server)


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
    server.aqla_trading.ingest(source.model_copy(update={"edited_at":source.sent_at+1,"text":"Edited original report."}))
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
    _source, interpretation = signal
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
    assert processor.prepare("same-gateway", "p")["job"]["status"] == "interpreted"
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
    server.aqla_trading.ingest(source.model_copy(update={"edited_at":source.sent_at+1,"text":text+"Changed original."}))
    with pytest.raises(Exception, match="changed during native read"):
        await rpc(server, "source_packet", job_id=identity, offset=0)
    assert fake.actions == []


@pytest.mark.parametrize("scope", ["bindings", "account_bindings", "venue_bindings"])
def test_management_reply_binding_is_resolved_across_account_and_venue_scopes(scope):
    from types import SimpleNamespace
    accepted = SourceEvent(chat_id="channel", message_id=10, sent_at=1000, text="BTC: stop to entry.")
    reply = SourceEvent(chat_id="channel", message_id=11, sent_at=1001, reply_to_id=10, text="BTC: close now.")
    rows = [{"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
            for source in (accepted, reply)]
    snapshot = {"events": {row["event_id"]: row for row in rows},
        "trades": {"owned": {"chat_id": "channel", "plan": {"source_refs": []}}}}
    binding = {"update:" + accepted.event_id: {"trade_id": "owned"}}
    snapshot[scope] = binding if scope == "bindings" else {"configured-scope": binding}
    trading = SimpleNamespace(context=lambda *args, **kwargs: {"event": rows[-1], "messages": rows.copy(), "trades": []})
    processor = SourceProcessor(SimpleNamespace(snapshot=lambda: snapshot), trading)
    context, packet, _ = processor.context(reply.event_id)
    assert context["bound_source_refs"] == [accepted.source_id]
    assert packet["anchor_setup_bound"]
    snapshot["trades"]["owned"]["chat_id"] = "another-source"
    assert not processor.context(reply.event_id)[1]["anchor_setup_bound"]


@pytest.mark.parametrize("scope", ["bindings", "account_bindings", "venue_bindings"])
@pytest.mark.parametrize("opaque", [{"event_id": "old-update", "status": "pending"},
                                   {"event_id": "old-update", "source_id": "old", "source": {"text": "old"}, "status": "pending"}])
def test_opaque_bound_update_history_cannot_supply_packet_context(scope, opaque):
    from types import SimpleNamespace

    source = SourceEvent(chat_id="channel", message_id=11, sent_at=1001, reply_to_id=10, text="BTC close now")
    row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
    snapshot = {"events": {"old-update": opaque, source.event_id: row},
                "trades": {"owned": {"chat_id": "channel", "plan": {"source_refs": []}}}}
    binding = {"update:old-update": {"trade_id": "owned"}}
    snapshot[scope] = binding if scope == "bindings" else {"configured-scope": binding}
    before = json.loads(json.dumps(snapshot))
    trading = SimpleNamespace(context=lambda *args, **kwargs: {"event": row, "messages": [row], "trades": []})
    processor = SourceProcessor(SimpleNamespace(snapshot=lambda: snapshot), trading)
    context, packet, _ = processor.context(source.event_id)
    assert context["bound_source_refs"] == [] and not packet["anchor_setup_bound"]
    assert [message["event_id"] for message in packet["messages"]] == [source.event_id]
    assert snapshot == before


@pytest.mark.asyncio
async def test_unrelated_channel_post_does_not_invalidate_cached_semantics(native_processor):
    server,state,fake=native_processor
    source=SourceEvent(chat_id="channel",message_id=20,sent_at=now_ms(),text="Result report.")
    server.aqla_trading.ingest(source)
    selected=await rpc(server,"source_prepare",processing_version="stable")
    server.aqla_trading.ingest(SourceEvent(chat_id="channel",message_id=21,sent_at=source.sent_at+1,text="Unrelated greeting."))
    await rpc(server,"source_record",job_id=selected["job"]["job_id"],decision_result=classification(source).model_dump(mode="json"))
    await rpc(server,"source_apply",job_id=selected["job"]["job_id"])
    assert state.snapshot()["events"][source.event_id]["status"]=="report" and not fake.actions


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
async def test_raw_collection_is_rejected_at_hybrid_record_boundary(native_processor, signal):
    server, state, fake = native_processor
    _, interpretation = signal
    selected = await rpc(server, "source_prepare", processing_version="collection-boundary")
    raw = DecisionResult(model="inception/mercury-decide:free", interpretation=interpretation,
                         verified=True, collection_only=True)
    with pytest.raises(Exception, match="Collection-only"):
        await rpc(server, "source_record", job_id=selected["job"]["job_id"], decision_result=raw.model_dump(mode="json"))
    assert not fake.actions and not state.snapshot()["plans"]




@pytest.mark.asyncio
async def test_current_policy_is_required_for_cached_unsubmitted_results(native_processor, signal):
    server, state, fake = native_processor
    _, interpretation = signal
    job = (await rpc(server, "source_prepare", processing_version="p1"))["job"]
    await rpc(server, "source_record", job_id=job["job_id"], decision_result=DecisionResult(
        model="inception/mercury-decide:free", interpretation=interpretation, verified=True).model_dump(mode="json"))
    await rpc(server, "source_prepare", processing_version="p2")
    with pytest.raises(Exception, match="policy changed"):
        await rpc(server, "source_apply", job_id=job["job_id"])
    assert not fake.actions and not state.snapshot()["plans"]


@pytest.mark.asyncio
async def test_paused_entry_rejection_completes_without_retry_or_failure_notification(native_processor, signal):
    server, state, fake = native_processor
    source, value = signal
    state.update(lambda data: data.update(paused=True))
    job = (await rpc(server, "source_prepare", processing_version="paused"))["job"]
    await rpc(server, "source_record", job_id=job["job_id"], decision_result=DecisionResult(
        model="fixture", interpretation=value, verified=True).model_dump(mode="json"))
    applied = await rpc(server, "source_apply", job_id=job["job_id"])
    assert applied["status"] == "done"
    assert state.snapshot()["events"][source.event_id]["status"] == "rejected"
    assert not state.snapshot()["source_jobs"][job["job_id"]]["failure_history"]
    assert "source-job:" + job["job_id"] not in state.snapshot()["notifications"]
    assert (await rpc(server, "source_prepare", processing_version="paused"))["job"] is None
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.asyncio
async def test_update_without_own_position_finishes_rejected_without_loop(native_processor):
    server, state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=55, sent_at=now_ms(), text="BTC close now.")
    server.aqla_trading.ingest(source)
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="close", source_refs=[source.source_id],
        evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
            ("classification", source.text), ("symbol", "BTC"), ("update_action", "close now"))])
    job = (await rpc(server, "source_prepare", processing_version="unbound"))["job"]
    await rpc(server, "source_record", job_id=job["job_id"], decision_result=DecisionResult(
        model="fixture", interpretation=value, verified=True).model_dump(mode="json"))
    applied = await rpc(server, "source_apply", job_id=job["job_id"])
    assert applied["status"] == "done"
    assert (await rpc(server, "source_prepare", processing_version="unbound"))["job"] is None
    snapshot = state.snapshot()
    assert snapshot["events"][source.event_id]["status"] == "rejected"
    assert snapshot["events"][source.event_id]["update_disposition"] == "no_matching_own_position"
    assert not snapshot["source_jobs"][job["job_id"]]["failure_detail"]
    assert not fake.actions and not snapshot["operations"]


@pytest.mark.asyncio
async def test_old_policy_terminal_applying_reconciles_and_keeps_failure_history(native_processor):
    server, state, fake = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker")
    source = SourceEvent(chat_id="channel", message_id=57, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = processor.prepare("worker", "old")["job"]
    processor.record("worker", job["job_id"], classification(source))
    state.update_metadata(("source_jobs",), lambda data: data["source_jobs"][job["job_id"]].update(status="applying"))
    processor.fail("worker", job["job_id"], "prior_failure")
    server.aqla_trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1, "text": "Changed report."}))
    assert state.snapshot()["events"][source.event_id]["status"] == "superseded"
    processor.prepare("worker", "new")
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "done" and saved["outcome"] == "superseded"
    assert saved["failure_history"][0]["code"] == "prior_failure"
    assert not fake.actions


@pytest.mark.asyncio
async def test_unknown_financial_send_is_engine_owned_and_never_resubmitted(native_processor, signal):
    server, state, fake = native_processor
    source, value = signal
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker")
    job = processor.prepare("worker", "p")["job"]
    processor.record("worker", job["job_id"], DecisionResult(model="fixture", interpretation=value, verified=True))
    fake.lose_entry_response = True
    await processor.apply("worker", job["job_id"])
    operations = state.snapshot()["operations"]
    assert any(row["phase"] == "unknown" for row in operations.values())
    state.update_metadata(("source_jobs",), lambda data: data["source_jobs"][job["job_id"]].update(status="applying"))
    counts = len(fake.actions)
    assert processor.prepare("worker", "p")["job"] is None
    assert state.snapshot()["source_jobs"][job["job_id"]]["status"] == "recovering"
    restarted = SourceProcessor(state, server.aqla_trading)
    restarted.configure("hybrid", "worker")
    assert restarted.prepare("worker", "p")["job"] is None
    assert state.snapshot()["operations"] == operations and len(fake.actions) == counts
    assert state.snapshot()["events"][source.event_id]["status"] in ("executed", "needs_reconciliation")


@pytest.mark.asyncio
async def test_active_application_is_never_reconciled_by_polling(native_processor):
    server, state, fake = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker")
    source = SourceEvent(chat_id="channel", message_id=58, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = processor.prepare("worker", "p")["job"]
    processor.record("worker", job["job_id"], classification(source))
    state.update_metadata(("source_jobs",), lambda data: data["source_jobs"][job["job_id"]].update(status="applying"))
    processor.active_jobs.add(job["job_id"])
    assert processor.prepare("worker", "p")["job"] is None
    assert state.snapshot()["source_jobs"][job["job_id"]]["status"] == "applying"
    assert not fake.actions


@pytest.mark.asyncio
async def test_temporary_provider_retry_is_persisted_bounded_and_original_deadline_owned(native_processor, monkeypatch):
    server, state, fake = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker", retry_max_attempts=2, retry_backoff_seconds=1, retry_max_age_seconds=30)
    source = SourceEvent(chat_id="channel", message_id=59, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = processor.prepare("worker", "p")["job"]
    detail = {"phase": "interpretation_fallback", "code": "provider_timeout", "exception_type": "TimeoutError", "retryable": True}
    processor.fail("worker", job["job_id"], "provider_timeout", detail)
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "retry_wait" and not state.snapshot()["notifications"]
    assert processor.prepare("worker", "p")["job"] is None
    monkeypatch.setattr("aqla_agent.source_processing.now_ms", lambda: saved["next_retry_at"])
    restarted = SourceProcessor(state, server.aqla_trading)
    restarted.configure("hybrid", "worker", retry_max_attempts=2, retry_backoff_seconds=1, retry_max_age_seconds=30)
    resumed = restarted.prepare("worker", "p")["job"]
    assert resumed["job_id"] == job["job_id"] and resumed["attempt_count"] == 2
    assert resumed["retry_deadline_at"] == saved["retry_deadline_at"]
    restarted.fail("worker", job["job_id"], "provider_timeout", detail)
    assert state.snapshot()["source_jobs"][job["job_id"]]["status"] == "review"
    assert restarted.prepare("worker", "p")["job"] is None
    assert not fake.actions


@pytest.mark.asyncio
async def test_semantic_failure_is_not_a_provider_retry_and_diagnostics_remove_payloads(native_processor):
    server, state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=60, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_fail", job_id=job["job_id"], failure_code="management_actions_not_representable",
        failure_detail={"phase": "validate", "retryable": True, "exception_type": "DecisionFailure",
                        "field_paths": ["symbol", "api_key.secret"], "response": "SYNTHETIC_SECRET"})
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "review" and not saved["failure_detail"]["retryable"]
    assert saved["failure_detail"]["field_paths"] == ["symbol"]
    assert "SYNTHETIC_SECRET" not in json.dumps(saved)
    assert (await rpc(server, "source_prepare", processing_version="p"))["job"] is None
    assert not fake.actions


@pytest.mark.asyncio
async def test_identical_later_edit_reuses_nonactionable_interpretation_but_true_change_invalidates(native_processor):
    server, _state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=61, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    original = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_record", job_id=original["job_id"], decision_result=classification(source).model_dump(mode="json"))
    await rpc(server, "source_apply", job_id=original["job_id"])
    edited = source.model_copy(update={"edited_at": source.sent_at + 1})
    server.aqla_trading.ingest(edited)
    reused = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    assert reused["job_id"] != original["job_id"] and reused["cache_reused_from"] == original["job_id"]
    assert reused["intent_revisions"] == {source.source_id: edited.event_id}
    await rpc(server, "source_apply", job_id=reused["job_id"])
    server.aqla_trading.ingest(edited.model_copy(update={"edited_at": source.sent_at + 2, "text": "Different result report."}))
    changed = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    assert changed["result"] is None and changed["cache_reused_from"] is None
    assert not fake.actions


@pytest.mark.asyncio
async def test_identical_unsubmitted_signal_edit_rebinds_evidence_and_executes_once(native_processor, signal):
    server, _state, fake = native_processor
    source, interpretation = signal
    original = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_record", job_id=original["job_id"], decision_result=DecisionResult(
        model="fixture", interpretation=interpretation, verified=True).model_dump(mode="json"))
    edited = source.model_copy(update={"edited_at": source.sent_at + 1})
    server.aqla_trading.ingest(edited)
    reused = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    assert reused["cache_reused_from"] == original["job_id"]
    assert reused["intent_revisions"] == {source.source_id: edited.event_id}
    assert reused["result"]["interpretation"]["evidence"] == interpretation.model_dump(mode="json")["evidence"]
    await rpc(server, "source_apply", job_id=reused["job_id"])
    count = len(fake.actions)
    assert count > 0 and fake.position > 0
    with pytest.raises(Exception, match="changed"):
        await rpc(server, "source_apply", job_id=original["job_id"])
    assert len(fake.actions) == count
    server.aqla_trading.ingest(edited.model_copy(update={"edited_at": source.sent_at + 2}))
    later = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    if later is not None:
        assert later["result"] is None and later["cache_reused_from"] is None
    assert len(fake.actions) == count


@pytest.mark.asyncio
async def test_configuration_change_invalidates_equivalent_edit_cache(native_processor):
    server, state, fake = native_processor
    source = SourceEvent(chat_id="channel", message_id=62, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    original = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    await rpc(server, "source_record", job_id=original["job_id"], decision_result=classification(source).model_dump(mode="json"))
    await rpc(server, "source_apply", job_id=original["job_id"])
    state.update(lambda data: data.update(configuration_revision=data["configuration_revision"] + 1))
    server.aqla_trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1}))
    changed = (await rpc(server, "source_prepare", processing_version="p"))["job"]
    assert changed["result"] is None and changed["cache_reused_from"] is None
    assert not fake.actions


@pytest.mark.asyncio
async def test_provider_retry_expiry_never_refreshes_original_deadline(native_processor, monkeypatch):
    server, state, fake = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker", retry_max_age_seconds=30)
    source = SourceEvent(chat_id="channel", message_id=63, sent_at=now_ms(), text="Result report.")
    server.aqla_trading.ingest(source)
    job = processor.prepare("worker", "p")["job"]
    processor.fail("worker", job["job_id"], "provider_timeout", {"phase": "decision", "retryable": True, "exception_type": "ReadTimeout"})
    monkeypatch.setattr("aqla_agent.source_processing.now_ms", lambda: job["retry_deadline_at"])
    assert processor.prepare("worker", "p")["job"] is None
    saved = state.snapshot()["source_jobs"][job["job_id"]]
    assert saved["status"] == "review" and saved["outcome"] == "provider_retry_exhausted"
    assert saved["retry_deadline_at"] == job["retry_deadline_at"] and saved["attempt_count"] == 1
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.parametrize("invalid", [{"retry_max_attempts": True}, {"retry_max_attempts": 0},
                                   {"retry_backoff_seconds": 301}, {"retry_max_age_seconds": 29}])
def test_invalid_retry_configuration_cannot_change_admission_owner_or_policy(native_processor, invalid):
    server, state, _ = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "original", retry_backoff_seconds=20,
                        decision_status={"model": "fixture/model", "validated": False})
    processor.policy_version = "approved-policy"
    previous = (processor.mode, processor.owner, processor.policy_version, processor.retry_max_attempts,
                processor.retry_backoff_seconds, processor.retry_max_age_seconds, processor.decision_status)
    with pytest.raises(Exception, match="Invalid bounded"):
        processor.configure("shadow", "replacement", decision_status={"model": "different/model"}, **invalid)
    assert (processor.mode, processor.owner, processor.policy_version, processor.retry_max_attempts,
            processor.retry_backoff_seconds, processor.retry_max_age_seconds, processor.decision_status) == previous
    processor.require_owner("original")


def test_entry_retry_deadline_does_not_apply_signal_ttl_to_updates_or_historical_qa(native_processor):
    server, state, _ = native_processor
    processor = SourceProcessor(state, server.aqla_trading)
    processor.configure("hybrid", "worker", retry_max_age_seconds=900)
    context = {"event": {"source": {"sent_at": 1000, "historical": False}},
               "policy": {"max_signal_age_seconds": 5}, "trades": []}
    packet = {"anchor_setup_bound": False}
    assert processor.retry_deadline(context, packet, 5000) == 6000
    assert processor.retry_deadline(context, packet, 5000, kind="trade_update") == 905000
    assert processor.retry_deadline(context, {"anchor_setup_bound": True}, 5000) == 905000
    assert processor.retry_deadline({**context, "trades": [{"trade_id": "own"}]}, packet, 5000) == 905000
    # A confirmed entry still uses its original deadline alongside other trades.
    assert processor.retry_deadline({**context, "trades": [{"trade_id": "own"}]}, packet, 5000, kind="new_signal") == 6000
    context["event"]["source"]["historical"] = True
    assert processor.retry_deadline(context, packet, 5000, kind="new_signal") == 905000
    context["event"]["source"]["historical"] = False
    processor.configure("shadow", "worker")
    assert processor.retry_deadline(context, packet, 5000, kind="new_signal") == 905000
