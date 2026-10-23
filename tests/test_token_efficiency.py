
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from aqla_agent.efficiency import WakeSchedule, decision_batch
from aqla_agent.source_disposition import issue_receipt, verify_disposition
from aqla_agent.tool_outputs import PAGE_BYTES, OutputStore, reader_text
from aqla_agent.usage_metrics import UsageRun
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    Interpretation,
    SourceClassification,
    SourceEvent,
    require_first,
)
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.fixture
async def efficiency_server(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    yield server
    await close_server(server)


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    return result.structured_content or json.loads(require_first(block.text for block in result.content if block.type == "text"))


def report_item(source, receipt):
    return {"event_id": source.event_id, "kind": "report", "source_refs": [source.source_id],
            "reason": "Original channel result report, no entry intent", "source_read_receipt": receipt,
            "evidence": [{"field": "classification", "source_id": source.source_id, "quote": source.text[:1000]}]}


def test_wake_backoff_coalescing_progress_and_lost_scheduling():
    schedule = WakeSchedule()
    work = {"ready": ["a"], "notifications": "v1"}
    assert schedule.due(work, 0, 0)
    schedule.accepted(0)
    assert not schedule.due(work, 299, 0)
    assert not schedule.due(work, 301, 0, busy=True)
    schedule.consumed()
    assert schedule.due(work, 30, 0)
    schedule.accepted(30)
    assert schedule.interval == 60
    schedule.consumed()
    assert not schedule.due(work, 89, 30)
    assert schedule.due(work, 90, 30)
    schedule.accepted(90)
    assert schedule.interval == 120
    schedule.consumed()
    assert schedule.due({"ready": ["b"], "notifications": "v1"}, 91, 90)
    assert schedule.interval == 30
    schedule.accepted(91)
    # New arrivals update the authoritative observed signature, but never add
    # another waiting wake behind the accepted one.
    assert not schedule.due({"ready": ["b", "c"]}, 92, 91)
    assert schedule.due({"ready": ["b", "c"]}, 392, 91)
    schedule.accepted(392)
    schedule.consumed()
    for stamp in (422, 482, 602, 842):
        assert schedule.due({"ready": ["b", "c"]}, stamp, stamp - schedule.interval)
        schedule.accepted(stamp)
        schedule.consumed()
    assert schedule.interval == 300


@pytest.mark.asyncio
async def test_summary_is_fresh_bounded_and_avoids_duplicate_notifications(efficiency_server, setup, signal):
    _, state, _, _, _ = setup
    server = efficiency_server
    state.update(lambda data: data["notifications"].update({"notice": {"error": "protection check"}}))
    summary = await call(server, "status", view="summary")
    assert summary["source_queue"]["ready_total"] == 1
    assert summary["notification_pending"] == 1
    assert not {"profiles", "notifications", "telegram_view", "trades"} & summary.keys()
    assert summary["inventory_refreshed"] is False
    state.update(lambda data: data.update(paused=True))
    assert (await call(server, "status", view="summary"))["paused"] is True


@pytest.mark.asyncio
async def test_summary_never_requests_a_full_state_snapshot(efficiency_server, setup, monkeypatch):
    state = setup[1]
    monkeypatch.setattr(state, "snapshot", lambda: pytest.fail("Summary copied the whole financial and source body"))
    summary = await call(efficiency_server, "status", view="summary")
    assert summary["view"] == "summary" and "account_recovery" in summary


# Sequential safety assertions; control flow (including test doubles): 13.
# skipcq: PY-R1000
@pytest.mark.asyncio
async def test_decision_context_and_batch_keep_originals_quotes_replies_and_receipts(efficiency_server, setup):
    _, state, _, _, trading = setup
    sources = [SourceEvent(chat_id="channel", message_id=i + 1, sent_at=1000 + i,
                          reply_to_id=1 if i else None, text=("Результат 😺 50% ROE " + str(i)) * 100) for i in range(3)]
    for source in sources:
        trading.ingest(source)
    full = await call(efficiency_server, "source_context", event_id=sources[0].event_id)
    one = await call(efficiency_server, "source_context", event_id=sources[0].event_id, view="decision")
    assert one["anchor_event_id"] == sources[0].event_id
    assert one["messages"][sources[0].event_id]["source"] == full["event"]["source"]
    assert one["source_read_receipt"].startswith("sr2.")
    batch = await call(efficiency_server, "source_context_batch", event_ids=[source.event_id for source in sources])
    assert len(batch["messages"]) == 3
    assert list(batch["messages"]) == [source.event_id for source in sources]
    assert len(batch["configurations"]) == 1
    assert batch["stored_trade_details_omitted"]
    for contract in batch["interpretation_contracts"].values():
        assert contract["workflow"] and contract["version"] and contract["candidate_limit"] == 64
        assert contract["vetoes_are_not_classification_or_permission"] and contract["candidate_roles_are_not_inferred"]
    assert {row["anchor_event_id"] for row in batch["items"]} == {source.event_id for source in sources}
    for source, item in zip(sources, batch["items"], strict=True):
        assert batch["messages"][source.event_id]["source"]["text"] == source.text
        for identity in item["interpretation_assistance"]["candidate_ids"]:
            span = batch["interpretation_candidates"][identity]
            original = require_first(row["source"]["text"] for row in batch["messages"].values() if row["source_id"] == span["source_id"])
            assert original[span["start"]:span["end"]] == span["quote"]
        interpretation = Interpretation.model_validate({key: value for key, value in report_item(source, item["source_read_receipt"]).items()
                                                        if key not in ("event_id", "source_read_receipt")})
        verify_disposition(state.snapshot(), source.event_id, interpretation, item["source_read_receipt"], efficiency_server.aqla_authority.key)
    separate = [len(json.dumps(await call(efficiency_server, "source_context", event_id=source.event_id, view="decision"), ensure_ascii=False)) for source in sources]
    assert len(json.dumps(batch, ensure_ascii=False)) < sum(separate)


@pytest.mark.asyncio
async def test_batch_mutations_are_per_item_and_reject_trade_fields(efficiency_server, setup):
    _, state, _, _, trading = setup
    sources = [SourceEvent(chat_id="channel", message_id=i + 1, sent_at=1000 + i, text=f"Ergebnisbericht {i}") for i in range(2)]
    for source in sources:
        trading.ingest(source)
    batch = await call(efficiency_server, "source_context_batch", event_ids=[source.event_id for source in sources])
    items = [report_item(source, item["source_read_receipt"]) for source, item in zip(sources, batch["items"], strict=True)]
    with pytest.raises(ValidationError):
        SourceClassification.model_validate({**items[0], "kind": "new_signal", "symbol": "BTC"})
    with pytest.raises(ValidationError):
        SourceClassification.model_validate({**items[0], "stop_loss": "90"})
    state.update(lambda data: data["events"][sources[0].event_id].update(status="ambiguous"))
    result = await call(efficiency_server, "interpret_source_batch", items=items,
                        context_auth=efficiency_server.aqla_authority.sign("source", "batch-test"))
    assert result["succeeded"] == 1 and result["partial_success"]
    assert not result["items"][0]["ok"] and result["items"][1]["ok"]
    assert result["items"][1]["status"] == "report"
    assert "source" not in result["items"][1] and "interpretation" not in result["items"][1]
    assert state.snapshot()["plans"] == {} and state.snapshot()["trades"] == {}
    with pytest.raises(ToolError):
        await call(efficiency_server, "interpret_source_batch", items=items, context_auth="forged")
    with pytest.raises(ToolError):
        await call(efficiency_server, "source_context_batch", event_ids=[sources[0].event_id] * 2)


@pytest.mark.parametrize("compact", [False, True])
def test_receipt_encoding_preserves_expiry_tampering_and_content_binding(compact):
    from test_source_queue import row, snapshot
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="Resultatbericht")
    data = snapshot([row(source)])
    interpretation = Interpretation(kind="report", source_refs=[source.source_id], reason="result",
                                   evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    key = b"synthetic-key-not-a-user-credential"
    receipt = issue_receipt(data["events"], source.event_id, key, stamp=1000, compact=compact)
    verify_disposition(data, source.event_id, interpretation, receipt, key, stamp=1001)
    with pytest.raises(AqlaError):
        verify_disposition(data, source.event_id, interpretation, receipt, key, stamp=901000)
    changed = copy.deepcopy(data)
    changed["events"][source.event_id]["source"]["text"] += "changed"
    with pytest.raises(AqlaError):
        verify_disposition(changed, source.event_id, interpretation, receipt, key, stamp=1001)
    with pytest.raises(AqlaError):
        verify_disposition(data, source.event_id, interpretation, receipt[:-3] + "bad", key, stamp=1001)


def test_semantic_output_selection_stays_owned_lossless_and_bounded(tmp_path):
    store = OutputStore(tmp_path)
    text = json.dumps({"result": json.dumps({"messages": {"a": {"text": "ä😺" * 6000}, "b": {"text": "不要读"}},
                                           "irrelevant": "X" * 90000})})
    identity = store.save(text, "own", "call")
    parts, offset = [], 0
    while True:
        page = store.read(identity, "own", offset, sections=["messages"], record_ids=["a"])
        assert len(json.dumps(reader_text(page), ensure_ascii=True).encode()) <= PAGE_BYTES
        assert page["selection"]["unselected_content_omitted"] and page["selection"]["missing_ids"] == []
        parts.append(page["content"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert json.loads("".join(parts)) == {"messages": {"a": {"text": "ä😺" * 6000}}}
    with pytest.raises(ValueError):
        store.read(identity, "another", sections=["messages"])
    with pytest.raises(ValueError):
        store.read(identity, "own", sections=["/etc/passwd"])


def test_aggregate_metrics_have_no_prompt_arguments_and_count_reasoning_once(tmp_path):
    run = UsageRun("source")
    usage = SimpleNamespace(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=0, reasoning_tokens=15)
    run.response(usage, "same-model", "fallback", 1.5)
    marker = "SECRET_MARKER_MUST_NOT_BE_STORED"
    run.tool("mcp__aqla__status", marker, False)
    run.tool("mcp__aqla__status", marker, False)
    run.persist(tmp_path, {"completed": True})
    with sqlite3.connect(tmp_path / "cache" / "aqla-usage.sqlite3") as con:
        serialized = con.execute("SELECT metrics FROM runs").fetchone()[0]
    assert marker not in serialized
    data = json.loads(serialized)
    assert data["api_responses"][0]["output_tokens"] == 20
    assert data["api_responses"][0]["reasoning_tokens"] == 15
    assert data["tools"]["mcp__aqla__status"]["repeated_results"] == 1


def test_conflicting_batch_projection_does_not_hide_revisions():
    context = {"event": {"event_id": "anchor", "source_id": "source", "source": {"text": "original"}}, "messages": [], "trades": []}
    changed = copy.deepcopy(context)
    changed["event"]["source"]["text"] = "changed"
    with pytest.raises(AqlaError, match="changed during batch"):
        decision_batch([context, changed])


@pytest.mark.asyncio
async def test_notification_views_preserve_versioned_rendering_and_ack(efficiency_server, setup):
    from test_notifications import lifecycle_fixture
    _, state, _, _, _ = setup
    state.update(lambda data: lifecycle_fixture(data, setup[0], count=2, fills=2))
    original = state.snapshot()["notifications"]
    full = await call(efficiency_server, "notifications_read")
    rendered = await call(efficiency_server, "notifications_read", view="rendered")
    facts = await call(efficiency_server, "notifications_read", view="facts")
    assert "notifications" not in rendered and "telegram_view" not in facts
    assert rendered["telegram_view"] == full["telegram_view"]
    assert facts["notifications"] == full["notifications"] == original
    assert rendered["notification_versions"] == full["notification_versions"]
    assert len(json.dumps(rendered)) < len(json.dumps(full))
    identity = require_first(iter(rendered["notification_versions"]))
    version = rendered["notification_versions"][identity]
    result = await call(efficiency_server, "notifications_ack", notification_ids=[identity], versions={identity: version},
                        context_auth=efficiency_server.aqla_authority.sign("source", "notice"))
    assert result["processed"] == [identity]


@pytest.mark.asyncio
async def test_diagnostic_sections_deduplicate_source_revisions(efficiency_server, setup):
    from test_notifications import lifecycle_fixture
    _, state, *_ = setup
    state.update(lambda data: lifecycle_fixture(data, setup[0], count=1, fills=2))
    data = state.snapshot()
    identity = require_first(iter(data["trades"]))
    legacy = await call(efficiency_server, "trade_diagnostics", trade_id=identity, sections=["sources"])
    assert legacy["missing_original_sources"][0]["reason"] == "legacy_plan_has_no_revision_binding"
    assert legacy["retained_revisions_with_missing_original"][0]["relative_to_missing_original"] == "unknown"
    source = require_first(iter(data["events"].values()))
    state.update(lambda data: data["trades"][identity]["plan"].update(source_revisions={source["source_id"]: source["event_id"]}))
    result = await call(efficiency_server, "trade_diagnostics", trade_id=identity, sections=["sources", "fills"])
    assert result["scope"]["network"] == "testnet"
    assert "plan" not in result and "orders" not in result
    assert result["fill_events"] == data["trades"][identity].get("fill_events", {})
    for ref, original in result["original_sources"].items():
        assert result["source_messages"][original["event_id"]]["source_id"] == ref
    assert "source" not in require_first(iter(result["original_sources"].values()))


@pytest.mark.asyncio
async def test_filtered_diagnostic_compact_continuations_preserve_sections_and_version(efficiency_server, setup):
    from test_notifications import lifecycle_fixture
    _, state, *_ = setup
    state.update(lambda data: lifecycle_fixture(data, setup[0], count=1, fills=2))
    identity = require_first(iter(state.snapshot()["trades"]))
    first = await call(efficiency_server, "trade_diagnostics", trade_id=identity, sections=["fills"], view="compact", limit=1)
    result, rows = first, list(first["rows"])
    while result.get("next_page"):
        arguments = result["next_page"]["arguments"]
        assert arguments["sections"] == ["fills"]
        result = await call(efficiency_server, "trade_diagnostics", **arguments)
        rows.extend(result["rows"])
    assert result["retained_page_complete"] and result["next_offset"] is None
    assert {row["section"] for row in rows}.issuperset({"fill_events", "entry_vwap"})
    assert not {row["section"] for row in rows}.intersection({"plan", "orders", "sources"})
    state.update(lambda data: data["trades"][identity].update(entry_vwap="101"))
    with pytest.raises(ToolError, match="view_version"):
        await call(efficiency_server, "trade_diagnostics", **first["next_page"]["arguments"])
