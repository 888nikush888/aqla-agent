import copy
import json
import time
from datetime import datetime

import pytest
from jsonschema import Draft202012Validator

from aqla_agent.tool_queries import BYTE_CAP, compact_view, source_attribution, source_lookup
from aqla_agent.tool_schema import nullable_schema
from aqla_hyperliquid_mcp.models import AqlaError, SourceEvent
from aqla_hyperliquid_mcp.server import close_server, create_server


def retained():
    stamp = int(datetime.fromisoformat("2026-10-11T17:39:00+02:00").timestamp() * 1000)
    source = SourceEvent(chat_id="-123", message_id=1, sent_at=stamp, received_at=stamp, text="BTC source " + "😺" * 1600)
    edited = source.model_copy(update={"edited_at": stamp + 1000, "text": "later edit"})
    data = {"events": {row.event_id: {"source": row.model_dump(mode="json"), "event_id": row.event_id,
        "source_id": row.source_id, "status": "executed", "interpretation": {"symbol": "BTC"}} for row in (source, edited)},
        "source_names": {"-123": "Named source"}, "event_tombstones": {}, "trades": {}, "operations": {}}
    for index, (network, account, status) in enumerate((("mainnet", "main", "open"), ("testnet", "test", "closed"), ("mainnet", "other", "unknown"))):
        identity = "trade-" + str(index)
        data["trades"][identity] = {"trade_id": identity, "status": status, "chat_id": "-123", "fill_events": {},
            "plan": {"plan_id": "plan-" + str(index), "source_refs": [source.source_id], "source_revisions": {source.source_id: source.event_id},
                     "network": network, "account": account, "exchange": "aster" if index == 2 else "hyperliquid", "market": {"symbol": "BTC"}}}
    data["trades"]["trade-0"]["fill_events"] = {"entry": {"kind": "entry", "sz": "2", "px": "100", "fee": ".1"}}
    data["operations"] = {"pending": {"operation_id": "entry:trade-2", "phase": "unknown"}}
    return data, source, edited


def lookup(data, **kwargs):
    return source_lookup(data, "2026-10-11T17:39:00+02:00", "2026-10-11T17:40:00+02:00", **kwargs)


def test_exact_sources_revisions_accounts_and_fill_proofs_with_lossless_pages():
    data, source, edited = retained()
    before = copy.deepcopy(data)
    result = lookup(data, source="named SOURCE", limit=1)
    combined = list(result["rows"])
    while result["next_cursor"]:
        assert len(json.dumps(result, ensure_ascii=True).encode()) <= BYTE_CAP
        result = lookup(data, source="named SOURCE", limit=1, cursor=result["next_cursor"])
        combined += result["rows"]
    assert len(combined) == 6 and {row["event_id"] for row in combined} == {source.event_id, edited.event_id}
    assert {row["account"] for row in combined} == {"main", "test", "other"}
    assert all(row["sent_at"] == "2026-10-11T17:39:00+02:00" for row in combined)
    assert all(row["event_status_is_fill_proof"] is False for row in combined)
    assert {row["trade_id"] for row in combined if row["confirmed_own_entry_fill_count"]} == {"trade-0"}
    assert all(row["plan_source_revision"] == source.event_id for row in combined)
    assert lookup(data, status="open")["total"] == 2
    assert lookup(data, account="test", network="testnet")["total"] == 2
    assert lookup(data, account="test", network="mainnet")["total"] == 0
    assert data == before


def test_pruned_original_is_unknown_in_interval_but_saved_diagnostics_remain_reachable():
    data, source, edited = retained()
    data["events"].pop(source.event_id)
    data["event_tombstones"][source.event_id] = source.source_id
    attribution = source_attribution(data, data["trades"]["trade-0"]["plan"])
    assert attribution["original_sources"] == {} and attribution["missing_original_sources"][0]["event_id"] == source.event_id
    assert attribution["later_source_edits"] == []  # Cannot date an edit relative to a missing original.
    assert attribution["retained_revisions_with_missing_original"][0]["event_id"] == edited.event_id
    result = lookup(data)
    assert result["historical_complete"] is False and result["absence_proves_never_received"] is False
    assert result["undated_saved_source_bindings"] == 3
    unknown = [row for row in result["rows"] if row.get("kind") == "missing_original_source_revision"]
    assert unknown and all(row["source_time"] is None and row["trade_details"]["tool"] == "trade_diagnostics" for row in unknown)


@pytest.mark.parametrize("original", ["retained", "missing", "opaque"])
def test_source_lookup_and_attribution_preserve_opaque_history_without_claiming_originals(original):
    data, source, edited = retained()
    if original == "missing":
        data["events"].pop(source.event_id)
    elif original == "opaque":
        data["events"][source.event_id] = {"event_id": source.event_id, "source_id": source.source_id, "status": "executed"}
    data["events"].update({"old-report": {"event_id": "old-report", "classification": "report"},
        "old-pending": {"event_id": "old-pending", "status": "pending"},
        "partial-source": {"event_id": "partial-source", "source_id": source.source_id, "source": {"text": "old"}}})
    before = copy.deepcopy(data)
    result = lookup(data)
    rows = list(result["rows"])
    while result["next_cursor"]:
        result = lookup(data, cursor=result["next_cursor"])
        rows += result["rows"]
    assert {row["event_id"] for row in rows if row.get("kind") != "missing_original_source_revision"} == (
        {source.event_id, edited.event_id} if original == "retained" else {edited.event_id})
    attribution = source_attribution(data, data["trades"]["trade-0"]["plan"])
    if original == "retained":
        assert attribution["original_sources"][source.source_id]["source"] == source.model_dump(mode="json")
        assert attribution["later_source_edits"][0]["event_id"] == edited.event_id
    else:
        assert attribution["original_sources"] == {} and attribution["later_source_edits"] == []
        assert attribution["retained_revisions_with_missing_original"][0]["event_id"] == edited.event_id
        assert result["undated_saved_source_bindings"] == 3
        assert attribution["missing_original_sources"][0]["reason"] == (
            "original_revision_unavailable" if original == "opaque" else "original_revision_not_retained")
    assert data == before


def test_source_lookup_keeps_valid_original_rows_without_processing_metadata():
    data, source, _ = retained()
    data.update(trades={}, operations={})
    data["events"] = {source.event_id: {"event_id": source.event_id, "source_id": source.source_id,
                                     "source": source.model_dump(mode="json")}}
    before = copy.deepcopy(data)
    result = lookup(data)
    assert result["rows"][0]["event_id"] == source.event_id and result["rows"][0]["event_status"] is None
    assert data == before


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["full", "compact"])
@pytest.mark.parametrize("sections", [None, ["sources"]])
async def test_public_trade_diagnostics_ignore_opaque_source_rows(setup, signal, view, sections):
    settings, state, fake, exchange, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    opaque = {"old-report": {"event_id": "old-report", "classification": "report"},
              "partial-source": {"event_id": "partial-source", "source_id": source.source_id,
                                 "source": {"text": "old"}, "status": "pending"}}
    state.update(lambda data: data["events"].update(opaque))
    server = create_server(settings, state=state, exchange=exchange)
    try:
        before = state.snapshot()
        actions = copy.deepcopy(fake.actions)
        result = await server.call_tool("trade_diagnostics", {"trade_id": trade["trade_id"], "view": view, "sections": sections})
        assert not result.is_error and state.snapshot() == before and fake.actions == actions
        result = result.structured_content or json.loads(result.content[0].text)
        if view == "full":
            if sections:
                assert set(result["source_messages"]) == {source.event_id}
            else:
                assert result["sources"] == [source.model_dump(mode="json")]
            assert result["original_sources"][source.source_id]["event_id"] == source.event_id
    finally:
        await close_server(server)


def test_changed_filters_and_snapshot_reject_cursors_without_reset():
    data, *_ = retained()
    cursor = lookup(data, limit=1)["next_cursor"]
    with pytest.raises(AqlaError, match="cursor"):
        lookup(data, cursor=cursor, account="main")
    data["trades"]["trade-0"]["fill_events"]["entry"]["sz"] = "3"
    with pytest.raises(AqlaError, match="cursor"):
        lookup(data, cursor=cursor)
    with pytest.raises(AqlaError, match="aware"):
        source_lookup(data, "2026-10-11T17:39:00", "2026-10-11T17:40:00")


def test_berlin_dst_has_explicit_offsets_without_mixing_two_0230_times():
    data, source, _ = retained()
    data.update(trades={}, operations={})
    events = []
    for index, instant in enumerate(("2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00")):
        stamp = int(datetime.fromisoformat(instant).timestamp() * 1000)
        event = source.model_copy(update={"message_id": index + 2, "sent_at": stamp, "received_at": stamp})
        events.append({"event_id": event.event_id, "source_id": event.source_id, "source": event.model_dump(mode="json"), "status": "pending"})
    data["events"] = {row["event_id"]: row for row in events}
    result = source_lookup(data, "2026-10-25T02:00:00+02:00", "2026-10-25T03:00:00+01:00")
    assert {row["sent_at"] for row in result["rows"]} == {"2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00"}


def test_single_oversized_journal_row_is_truthful_compact_data():
    raw = {"rows": [{"ID": "stable", "Text": "😺\n" * 100000, "_verified": True}], "next_offset": 1}
    before = copy.deepcopy(raw)
    result = compact_view(raw, "journal_read", {"table": "Signale", "offset": 0, "limit": 1})
    row = next(item["value"] for item in result["rows"] if item["section"] == "rows")
    assert row["_verified"] is False and row["full_record_fingerprint_verified"] is True and row["body_complete"] is False
    assert len(json.dumps(result, ensure_ascii=True).encode()) <= BYTE_CAP
    assert result["full_details"]["arguments"]["view"] == "full" and raw == before


@pytest.mark.parametrize("schema", [
    {"type": "string", "enum": ["long", "short"], "nullable": True},
    {"type": "string", "const": "long", "nullable": True},
    {"$ref": "#/$defs/Side", "nullable": True, "$defs": {"Side": {"enum": ["long"]}}},
    {"allOf": [{"type": "string"}, {"enum": ["long"]}], "nullable": True},
])
def test_nullable_constraints_allow_null_but_reject_invalid_nonnull(schema):
    normalized = nullable_schema(schema)
    validator = Draft202012Validator(normalized)
    assert validator.is_valid(None) and validator.is_valid("long")
    assert not validator.is_valid("illegal") and not validator.is_valid(123)


def test_nullable_metadata_and_instance_literals_remain_at_correct_levels():
    literal = {"nullable": True, "const": {"nullable": True}}
    schema = {"$id": "urn:aqla:test", "$schema": "https://json-schema.org/draft/2020-12/schema", "$anchor": "root", "nullable": True,
              "type": "object", "examples": [literal], "default": literal, "properties": {"side": {"$ref": "#/$defs/Side"}},
              "$defs": {"Side": {"type": "string", "enum": ["long"], "nullable": True}}}
    normalized = nullable_schema(schema)
    assert all(normalized[name] == schema[name] for name in ("$id", "$schema", "$anchor"))
    assert normalized["anyOf"][0]["default"] == literal and normalized["anyOf"][0]["examples"] == [literal]
    assert Draft202012Validator(normalized).is_valid({"side": None})
    assert not Draft202012Validator(normalized).is_valid({"side": "invalid"})


@pytest.mark.asyncio
async def test_more_than_twenty_diagnostic_candidates_have_reachable_continuation(setup, signal):
    settings, state, _, exchange, trading = setup
    plan = await trading.preview(signal[0].event_id, signal[1])
    for index in range(25):
        identity = "historical-" + str(index)
        state.update(lambda data, identity=identity, index=index: data["trades"].update({identity: {
            "trade_id": identity, "status": "closed", "plan": {**plan, "created_at": index}, "orders": []}}))
    server = create_server(settings, state=state, exchange=exchange)
    first = await server.call_tool("trade_diagnostics", {"symbol": "BTC"})
    value = first.structured_content
    if value is None:
        value = json.loads(first.content[0].text)
    assert value["total"] == 25 and value["next_offset"] == 20 and len(value["candidates"]) == 20
    second = await server.call_tool("trade_diagnostics", {"symbol": "BTC", "offset": 20})
    result = second.structured_content or json.loads(second.content[0].text)
    assert len(result["candidates"]) == 5 and result["next_offset"] is None
    await close_server(server)


def test_empty_supported_status_and_unrelated_arrival_do_not_invalidate_pages():
    data, source, _ = retained()
    assert lookup(data, status="waiting_entry")["total"] == 0
    cursor = lookup(data, limit=1)["next_cursor"]
    unrelated = source.model_copy(update={"message_id": 99, "sent_at": source.sent_at + 86400000})
    data["events"][unrelated.event_id] = {"event_id": unrelated.event_id, "source_id": unrelated.source_id,
        "source": unrelated.model_dump(mode="json"), "status": "pending"}
    assert lookup(data, limit=1, cursor=cursor)["offset"] == 1


def test_large_retained_lookup_indexes_collections_and_never_mutates():
    class Counted(dict):
        calls = 0
        def values(self):
            self.calls += 1
            return super().values()
    data, source, _ = retained()
    events, trades, operations = {}, Counted(), Counted()
    for index in range(4000):
        row = source.model_copy(update={"message_id": index + 1, "text": "retained body " * 20})
        events[row.event_id] = {"event_id": row.event_id, "source_id": row.source_id, "source": row.model_dump(mode="json"), "status": "executed"}
        identity = "trade-" + str(index)
        trades[identity] = {"trade_id": identity, "chat_id": "-123", "status": "open", "fill_events": {},
            "plan": {"plan_id": "plan-" + str(index), "source_refs": [row.source_id], "source_revisions": {row.source_id: row.event_id},
                     "market": {"symbol": "BTC"}, "network": "mainnet", "account": "main"}}
        operations[identity] = {"operation_id": "entry:" + identity, "phase": "unknown"}
    data.update(events=events, trades=trades, operations=operations)
    before = copy.deepcopy(data)
    start = time.perf_counter()
    result = lookup(data, limit=20)
    elapsed = time.perf_counter() - start
    assert result["total"] == 4000 and all(row["uncertain_operation_count"] == 1 for row in result["rows"])
    assert trades.calls <= 4 and operations.calls == 1
    assert data == before
    print(f"4000-source/trade/operation lookup: {elapsed:.3f}s")


def test_live_compact_continuation_rejects_changed_version_and_has_frozen_fallback():
    value = {"messages": list(range(30))}
    first = compact_view(value, "source_context", {"event_id": "source"}, limit=1)
    assert first["paging_basis"] == "live_snapshot" and first["next_page"]["arguments"]["view_version"] == first["view_version"]
    with pytest.raises(AqlaError, match="view_version"):
        compact_view(value, "source_context", {"event_id": "source"}, offset=1, limit=1)
    value["messages"].insert(0, "new")
    with pytest.raises(AqlaError, match="view_version"):
        compact_view(value, "source_context", {"event_id": "source"}, offset=1, limit=1, view_version=first["view_version"])
    assert first["full_details"]["arguments"]["view"] == "full"
