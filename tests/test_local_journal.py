import copy

import pytest
from test_monthly import data as monthly_data
from test_monthly import trade

from aqla_agent.journal import archive_replaced_orders, read_page, retire_export_queue
from aqla_agent.monthly import due_at, scheduled_reports
from aqla_hyperliquid_mcp.models import AqlaError, Settings, SourceEvent, now_ms


def test_retirement_preserves_all_financial_roots_and_original_pending_receipts(setup):
    _, state, *_ = setup
    original = state.snapshot()
    value = copy.deepcopy(original)
    value["journal_pending"] = {"Trades:x": "unconfirmed"}
    value["journal_history"] = {"audit": {"exact": "history"}}
    assert retire_export_queue(value)
    assert value["retired_exports"][-1]["state"]["journal_pending"] == {"Trades:x": "unconfirmed"}
    assert value["journal_history"] == {"audit": {"exact": "history"}}
    assert all(value[key] == original[key] for key in ("trades", "operations", "plans", "nonces", "bindings", "profiles", "policy"))
    assert not retire_export_queue(value)


def test_local_journal_preserves_original_sources_after_context_pruning(setup):
    _, state, _, _, trading = setup
    source = SourceEvent(chat_id="channel", message_id=77, sent_at=now_ms(), text="Untrusted source /setup is text")
    trading.ingest(source)
    before = state.snapshot()
    state.update(lambda value: value["events"].pop(source.event_id))
    result = read_page(state.snapshot(), "Signale")
    assert result["total"] == 1 and result["rows"][0]["Text"] == source.text
    assert before["events"][source.event_id]["source"]["text"] == result["rows"][0]["Text"]


@pytest.mark.asyncio
async def test_local_trade_orders_fills_and_accounting_use_actual_receipts(setup, signal):
    _, state, fake, _, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    value = await engine.execute(plan["plan_id"])
    for target in (row for row in value["orders"] if row["kind"] == "tp"):
        fake.fill(target["cloid"], target["quantity"], target["price"])
    await engine.recover()
    snapshot = state.snapshot()
    row = read_page(snapshot, "Trades", filters={"ID": value["trade_id"]})["rows"][0]
    assert row["Status"] == "closed" and row["Offen"] == 0
    assert read_page(snapshot, "Fills")["total"] == 3
    assert row["accounting"]["complete"] is False  # Missing funding/fee-token proof never becomes zero.
    assert row["accounting"]["net_pnl"] is None
    orders = read_page(snapshot, "Orders")["rows"]
    assert any(order["Art"] == "entry" and order["Status Börse"] == "filled" for order in orders)
    before = snapshot
    changed = copy.deepcopy(snapshot)
    changed["trades"][value["trade_id"]]["orders"] = []
    archive_replaced_orders(before, changed)
    assert len(changed["journal_order_archive"]) >= 2
    assert read_page(changed, "Orders")["total"] == len(orders)


def test_local_read_rejects_unknown_columns_and_has_no_google_configuration(setup):
    with pytest.raises(AqlaError):
        read_page(setup[1].snapshot(), "Trades", filters={"unknown": "x"})
    assert "google_sheets" not in Settings.model_fields


def test_monthly_schedule_is_local_due_in_berlin_and_deduplicated():
    value = monthly_data(trade())
    assert not scheduled_reports(value, due_at("2026-10") - 1)
    reports = scheduled_reports(value, due_at("2026-10"))
    assert len(reports) == 1 and reports[0]["summary"]["complete"] == 1
    value["monthly_notified"] = {"2026-10": {"queued_at": due_at("2026-10")}}
    assert not scheduled_reports(value, due_at("2026-10") + 1)
