import copy

import pytest

from aqla_agent.source_disposition import issue_receipt
from aqla_agent.source_queue import queue_overview, repair_queue
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, digest
from aqla_hyperliquid_mcp.projections import reader_overview
from aqla_hyperliquid_mcp.rules import latest_sources


def row(source, status="pending"):
    return {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json"),
            "status": status, "interpretation": None}


def snapshot(events):
    return {"events": {item["event_id"]: item for item in events}, "trades": {}, "plans": {}, "operations": {},
        "bindings": {}, "sources": ["channel"], "cursors": {}, "source_aliases": {}, "notifications": {}}


@pytest.mark.parametrize("change", [{"edited_at": 1000}, {"media": "caption"}])
def test_representational_duplicates_are_not_financial_conflicts(change):
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="BTC SL 90 TP 110")
    equivalent = source.model_copy(update=change)
    events = snapshot([row(source), row(equivalent)])["events"]
    assert latest_sources(events)[source.source_id]["source"]["text"] == source.text


def test_true_current_conflict_blocks_but_unique_newer_edit_is_processable():
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="BTC SL 90")
    conflict = source.model_copy(update={"text": "BTC SL 95"})
    events = snapshot([row(source), row(conflict)])["events"]
    with pytest.raises(AqlaError, match="conflicting content"):
        latest_sources(events)
    fresh = source.model_copy(update={"edited_at": 1001, "text": "BTC SL 96"})
    events[fresh.event_id] = row(fresh)
    assert latest_sources(events)[source.source_id]["event_id"] == fresh.event_id


def test_fifty_older_versions_do_not_hide_five_current_groups_or_notifications():
    events, current = [], []
    for identity in range(1, 6):
        source = SourceEvent(chat_id="channel", message_id=identity, sent_at=1000, text="initial")
        for version in range(11):
            edited = source.model_copy(update={"edited_at": 1000 + version, "text": f"source {identity} version {version}"})
            events.append(row(edited))
        current.append(edited.event_id)
    data = snapshot(events)
    before = copy.deepcopy(data)
    overview = reader_overview(data, 20)
    assert set(overview["pending"]) == set(current)
    assert overview["source_queue"]["ready_total"] == 5 and overview["source_queue"]["raw_unfinished_total"] == 55
    assert overview["notification_pending"] == 0 and data == before


def test_atomic_repair_preserves_financial_recovery_and_true_conflict_has_no_winner():
    original = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="old")
    current = original.model_copy(update={"edited_at": 1001, "text": "current"})
    conflict = current.model_copy(update={"text": "different"})
    data = snapshot([row(original), row(current), row(conflict)])
    repair_queue(data)
    old = data["events"][original.event_id]
    assert old["status"] == "superseded" and "successor_event_id" not in old
    assert set(old["successor_candidates"]) == {current.event_id, conflict.event_id}
    assert queue_overview(data)["blocked_total"] == 1 and queue_overview(data)["ready_total"] == 0
    before = copy.deepcopy(data)
    repair_queue(data)
    assert data == before
    fresh = current.model_copy(update={"edited_at": 1002, "text": "unique"})
    data["events"][fresh.event_id] = row(fresh)
    repair_queue(data)
    assert queue_overview(data)["ready"] == [fresh.event_id]
    assert not any(item.get("source_quarantine") for item in data["events"].values())
    for phase in ("prepared", "sending", "unknown"):
        data = snapshot([row(original), row(current)])
        data["plans"]["p"] = {"event_id": original.event_id, "plan": {"trade_id": "trade123", "source_revisions": {original.source_id: original.event_id}}}
        data["operations"]["entry:trade123"] = {"phase": phase, "request": {"action": {}}}
        saved = copy.deepcopy(data["events"][original.event_id])
        repair_queue(data)
        assert data["events"][original.event_id] == saved
        assert queue_overview(data)["recovery"] == [original.event_id]


@pytest.mark.parametrize("metadata", [{"reply_to_id": 2}, {"thread_id": 2}, {"forward_from": "other"}, {"album_id": "other"}])
def test_contextual_difference_has_distinct_identity_and_current_conflict(metadata):
    original = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="body")
    different = original.model_copy(update=metadata)
    assert original.event_id != different.event_id
    with pytest.raises(AqlaError, match="conflicting content"):
        latest_sources(snapshot([row(original), row(different)])["events"])


@pytest.mark.asyncio
async def test_equivalent_alias_after_preview_and_sorted_sqlite_restart_keep_plan_identity(setup, signal):
    from aqla_hyperliquid_mcp.state import State
    settings, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    alias = source.model_copy(update={"edited_at": source.sent_at, "media": "caption"})
    trading.ingest(alias)
    saved = copy.deepcopy(state.snapshot()["plans"])
    assert trading.source_revisions(state.snapshot(), [source.source_id]) == plan["source_revisions"]
    assert state.snapshot()["events"][alias.event_id]["status"] == "superseded"
    state.close()
    with State(settings) as restarted:
        assert trading.source_revisions(restarted.snapshot(), [source.source_id]) == plan["source_revisions"]
        assert restarted.snapshot()["plans"] == saved
        sequence = restarted.snapshot()["sequence"]
        restarted.update_if_changed(repair_queue)
        assert restarted.snapshot()["sequence"] == sequence
    assert fake.actions == []


def test_legacy_contextual_tombstone_is_not_replayed(setup, signal):
    _, state, fake, _, trading = setup
    source = signal[0].model_copy(update={"message_id": 20, "reply_to_id": 1})
    old_id = digest([source.source_id, source.edited_at, source.text, source.media])[:32]
    state.update(lambda data: data["event_tombstones"].__setitem__(old_id, source.source_id))
    assert trading.ingest(source)["status"] == "already_processed"
    assert source.event_id not in state.snapshot()["events"] and not fake.actions


def test_retained_legacy_contextual_identity_is_preserved_on_alias_ingest(setup, signal):
    _, state, fake, _, trading = setup
    source = signal[0].model_copy(update={"message_id": 20, "reply_to_id": 1})
    legacy_id = digest([source.source_id, source.edited_at, source.text, source.media])[:32]
    legacy = {**row(source, "executed"), "event_id": legacy_id}
    state.update(lambda data: data["events"].__setitem__(legacy_id, legacy))
    trading.ingest(source)
    assert state.snapshot()["events"][legacy_id] == legacy
    assert state.snapshot()["events"][source.event_id]["status"] == "superseded"
    assert latest_sources(state.snapshot()["events"], [source.source_id])[source.source_id]["event_id"] == legacy_id
    assert not fake.actions


def test_pending_stop_update_without_exchange_operation_retains_source_for_recovery():
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="SL 90")
    edited = source.model_copy(update={"edited_at": 1001, "text": "SL 95"})
    data = snapshot([row(source, "interpreted"), row(edited)])
    data["trades"]["owned"] = {"status": "open", "plan": {}, "pending_stop_update": {
        "event_id": source.event_id, "source_revisions": {source.source_id: source.event_id}, "phase": "waiting_fill"}}
    saved = copy.deepcopy(data["events"][source.event_id])
    repair_queue(data)
    assert data["events"][source.event_id] == saved and source.event_id in queue_overview(data)["recovery"]


def test_actionable_interpretation_cannot_reset_recovery_status(setup, signal):
    _, state, _, _, trading = setup
    source, value = signal
    trading.record_interpretation(source.event_id, value)
    state.update(lambda data: data["events"][source.event_id].update(status="needs_reconciliation"))
    before = state.snapshot()
    assert trading.record_interpretation(source.event_id, value)["status"] == "needs_reconciliation"
    with pytest.raises(AqlaError, match="recovery revision is immutable"):
        trading.record_interpretation(source.event_id, value.model_copy(update={"reason": "different decision"}))
    assert state.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "tamper", "expiry", "wrong_anchor", "edit", "quote", "reason", "recovery"])
async def test_public_classification_receipt_failures_are_atomic(setup, signal, failure):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source = signal[0]
    receipt = server.aqla_trading.context(source.event_id)["source_read_receipt"]
    value = Interpretation(kind="report", source_refs=[source.source_id], reason="Result report", evidence=[Evidence(field="classification", source_id=source.source_id, quote="BTC")])
    if failure == "missing":
        receipt = ""
    elif failure == "tamper":
        receipt = receipt.replace("source_read", "forged")
    elif failure == "expiry":
        receipt = issue_receipt(state.snapshot()["events"], source.event_id, server.aqla_authority.key, stamp=0)
    elif failure == "wrong_anchor":
        other = source.model_copy(update={"message_id": 2})
        server.aqla_trading.ingest(other)
        receipt = server.aqla_trading.context(other.event_id)["source_read_receipt"]
    elif failure == "edit":
        server.aqla_trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1, "text": source.text + " Edited"}))
    elif failure == "quote":
        value.evidence = []
    elif failure == "reason":
        value.reason = ""
    else:
        state.update(lambda data: data["events"][source.event_id].update(status="needs_reconciliation"))
    before = state.snapshot()
    for tool in ("interpret_source", "trade_preview"):
        with pytest.raises(ToolError):
            await server.call_tool(tool, {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                "source_read_receipt": receipt, "context_auth": server.aqla_authority.sign("source", "fixture")})
        assert state.snapshot() == before
    assert not fake.actions
    await close_server(server)


@pytest.mark.asyncio
async def test_public_receipt_context_is_readonly_complete_compact_and_valid_commit(setup, signal):
    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source = signal[0]
    before = state.snapshot()
    context = server.aqla_trading.context(source.event_id)
    from aqla_agent.tool_queries import compact_view
    compact = compact_view(context, "source_context", {"event_id": source.event_id}, limit=1)
    assert compact["source_read_receipt"] == context["source_read_receipt"] and state.snapshot() == before
    value = Interpretation(kind="report", source_refs=[source.source_id], reason="Result report", evidence=[Evidence(field="classification", source_id=source.source_id, quote="BTC")])
    result = await server.call_tool("interpret_source", {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
        "source_read_receipt": context["source_read_receipt"], "context_auth": server.aqla_authority.sign("source", "fixture")})
    assert not result.is_error and state.snapshot()["events"][source.event_id]["status"] == "report"
    assert not fake.actions
    await close_server(server)


def test_receipt_concurrent_classification_cannot_overwrite_and_unrelated_arrival_is_not_stale(setup, signal):
    from concurrent.futures import ThreadPoolExecutor
    _, state, _, _, trading = setup
    source = signal[0]
    trading.disposition_key = b"synthetic-receipt-key-at-least-32-bytes"
    receipt = trading.context(source.event_id)["source_read_receipt"]
    trading.ingest(source.model_copy(update={"message_id": 8, "text": "Unrelated new message"}))
    values = [Interpretation(kind=kind, source_refs=[source.source_id], reason="contextual decision",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote="BTC")]) for kind in ("report", "unrelated")]
    def classify(value):
        try:
            return trading.record_interpretation(source.event_id, value, read_receipt=receipt)["status"]
        except AqlaError:
            return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(classify, values))
    assert outcomes.count("stale") == 1
    assert state.snapshot()["events"][source.event_id]["status"] == next(value for value in outcomes if value != "stale")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["incomplete", "report", "unrelated", "ambiguous"])
async def test_empty_photo_requires_receipt_and_only_incomplete_can_close_without_invented_quote(setup, kind):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.models import now_ms
    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source = SourceEvent(chat_id="channel", message_id=20, sent_at=now_ms(), media="photo", text="")
    server.aqla_trading.ingest(source)
    context = server.aqla_trading.context(source.event_id)
    assert context["classification_constraints"]["missing_text"] and context["classification_constraints"]["media"] == "photo"
    arguments = {"event_id": source.event_id, "context_auth": server.aqla_authority.sign("source", "fixture"),
        "source_read_receipt": context["source_read_receipt"], "interpretation": {
            "kind": kind, "source_refs": [source.source_id], "reason": "Photo has no textual source evidence"}}
    before = state.snapshot()
    if kind == "incomplete":
        result = await server.call_tool("interpret_source", arguments)
        assert not result.is_error and state.snapshot()["events"][source.event_id]["status"] == "incomplete"
    else:
        with pytest.raises(ToolError, match="classification evidence quote"):
            await server.call_tool("interpret_source", arguments)
        assert state.snapshot() == before
    assert not fake.actions
    await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["new_signal", "trade_update"])
async def test_empty_photo_cannot_use_parent_only_quotes_to_authorize_action(setup, signal, kind):
    from aqla_hyperliquid_mcp.models import now_ms
    _, state, fake, _, trading = setup
    parent, value = signal
    source = SourceEvent(chat_id="channel", message_id=20, reply_to_id=parent.message_id, sent_at=now_ms(), media="photo")
    trading.ingest(source)
    value = value.model_copy(update={"kind": kind, "source_refs": [source.source_id, parent.source_id],
        "update_action": "replace_targets" if kind == "trade_update" else None})
    before = state.snapshot()
    with pytest.raises(AqlaError, match="no textual source intent"):
        await trading.preview(source.event_id, value)
    assert state.snapshot() == before and not fake.actions
