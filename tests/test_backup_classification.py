"""Synthetic sources only; no live credentials, accounts or exchange transports."""

import copy
import json
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_backup_roe import configure_roe

from aqla_agent.source_assistance import check_interpretation
from aqla_agent.source_disposition import issue_receipt, verify_disposition
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    Interpretation,
    SourceEvent,
    now_ms,
    require_first,
)
from aqla_hyperliquid_mcp.server import close_server, create_server

TEXT = "BTCUSDT.P SHORT\nSHORT-zone: 100+\nLeverage: 10x (cross)\nTargets: 50% / 100%\nMargin: <=2% of deposit"


def source_case(trading, text=TEXT):
    source = SourceEvent(chat_id="channel", message_id=9, sent_at=now_ms(), text=text)
    trading.ingest(source)
    draft = Interpretation(kind="ambiguous", source_refs=[source.source_id], reason_codes=["other"],
        reason="SHORT mit Zone, 10x cross und Margin <=2 %, aber ohne Stop-Loss und mit Zielen nur als 50 %/100 % ohne Preise",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=text)])
    return source, draft


@pytest.mark.parametrize("reason", [
    "ohne Stop-Loss und Ziele nur als 50%/100% ohne Preise",
    "Stop-Loss fehlt; kein vollstaendig ausfuehrbares Setup",
    "Without a source stop loss, risk sizing cannot proceed",
    "Missing SL and target prices; operator must specify a stop",
    "SL is not provided",
    "No source leverage; operator must specify leverage",
    "Стоп не указан; цели только 50% / 100%",
    "Без стопа невозможно рассчитать риск",
])
@pytest.mark.parametrize("kind", ["ambiguous", "incomplete"])
def test_missing_execution_fields_cannot_be_completed_as_semantic_ambiguity(setup, reason, kind):
    _, state, fake, _, trading = setup
    source, draft = source_case(trading)
    draft = draft.model_copy(update={"reason": reason, "kind": kind})
    before = state.snapshot()
    checked = check_interpretation(trading.context(source.event_id), draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "missing_execution_fields_are_not_ambiguity"
    assert checked["errors"][0]["field_paths"] == ["kind", "reason"]
    key = b"synthetic-source-receipt-key-32-bytes"
    with pytest.raises(AqlaError, match="Missing source SL/TP/leverage alone"):
        verify_disposition(before, source.event_id, draft, issue_receipt(before["events"], source.event_id, key), key)
    with pytest.raises(AqlaError, match="Missing source SL/TP/leverage alone"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions


@pytest.mark.parametrize("text,reason,codes", [
    ("BTC SHORT", "Missing SL; direction unknown", []),
    ("SHORT Targets: 50% / 100%", "Missing SL and symbol", ["missing_market"]),
    (TEXT, "Missing SL; conflicting source directions", ["other"]),
    ("BTC SHORT\nEntry MARKET 100 and LIMIT 101", "SL fehlt; mehrere Einstiege", ["other"]),
    ("BTC SHORT\nTargets: 1%-2%", "Stop fehlt; verbindlicher Zielbereich", ["other"]),
    ("BTC close half AND move stop to entry AND cancel entry", "Missing SL; mehrere Verwaltungsanweisungen", ["other"]),
    (TEXT, "Stop is ambiguous, two incompatible original values", ["contradictory_source"]),
    ("BTC LONG (2-5x)\nEntry: 100", "The leverage interval must not be treated as missing", ["other"]),
    ("BTC LONG (2-5x)\nEntry: 100", "Hebel als Intervall; darf nicht als fehlend behandelt werden", ["other"]),
    ("#BTC", "Kein Markt-Setup, keine Richtung oder Entry, SL/TP nicht belegbar", ["other"]),
])
def test_genuine_independent_uncertainty_never_becomes_execution_permission(setup, text, reason, codes):
    _, state, fake, _, trading = setup
    source, draft = source_case(trading, text)
    draft = draft.model_copy(update={"reason": reason, "reason_codes": codes})
    before = state.snapshot()
    assert check_interpretation(trading.context(source.event_id), draft)["valid"]
    trading.record_interpretation(source.event_id, draft)
    assert state.snapshot()["events"][source.event_id]["status"] == "ambiguous"
    assert not state.snapshot()["plans"] and not state.snapshot()["trades"] and not fake.actions
    assert state.snapshot()["policy"] == before["policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["interpret_source", "interpret_source_batch", "trade_preview"])
async def test_public_writes_recheck_policy_without_planning_or_ordering(setup, tool):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    try:
        source, draft = source_case(server.aqla_trading)
        receipt = server.aqla_trading.context(source.event_id)["source_read_receipt"]
        args = {"event_id": source.event_id, "source_read_receipt": receipt,
                "interpretation": draft.model_dump(mode="json"), "context_auth": server.aqla_authority.sign("source", "synthetic")}
        before = state.snapshot()
        if tool == "interpret_source_batch":
            item = {**draft.model_dump(mode="json", include={"kind", "reason", "reason_codes", "source_refs", "evidence"}),
                    "event_id": source.event_id, "source_read_receipt": receipt}
            response = await server.call_tool(tool, {"items": [item], "context_auth": args["context_auth"]})
            payload = response.structured_content or json.loads(require_first(c.text for c in response.content if c.type == "text"))
            assert payload["succeeded"] == 0 and not payload["items"][0]["ok"]
        else:
            with pytest.raises(ToolError, match="Missing source SL/TP/leverage alone"):
                await server.call_tool(tool, args)
        assert state.snapshot() == before and not fake.actions
    finally:
        await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_enabled", [False, True])
async def test_corrected_source_uses_only_backup_stop_and_preserves_both_source_roe_targets(setup, backup_enabled):
    _, state, fake, _, trading = setup
    if backup_enabled:
        configure_roe(state)
    state.update(lambda data: data["policy"].update(leverage_mode="signal", risk_percent="5"))
    source, _ = source_case(trading)
    draft = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="short",
        entry="limit", entry_prices=[100], leverage=10, take_profits=[50, 100], take_profit_unit="roe_percent",
        evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
            ("symbol", "BTCUSDT.P"), ("side", "SHORT"), ("entry_prices.0", "SHORT-zone: 100+"),
            ("leverage", "10x"), ("take_profits.0", "50%"), ("take_profits.1", "100%"))])
    checked = check_interpretation(trading.context(source.event_id), draft)
    assert checked["valid"] and not checked["execution_authorized"]
    if not backup_enabled:
        with pytest.raises(AqlaError, match="Backup SL percentage is not configured"):
            await trading.preview(source.event_id, draft)
        assert not state.snapshot()["plans"] and not fake.actions
        return
    plan = await trading.preview(source.event_id, draft)
    assert plan["backup_fields"] == ["stop_loss"] and plan["effective_leverage"] == 10
    assert Decimal(plan["stop_loss"]) == 104 and list(map(Decimal, plan["take_profits"])) == [95, 90]
    assert plan["source_tp"]["values"] == ["50", "100"]
    assert plan["policy"]["risk_percent"] == "5" and Decimal(plan["risk_usdc"]) <= 500
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "opening" and fake.position == 0
    fake.fill(trade["entry_cloid"], plan["quantity"], "100")
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "open" and Decimal(current["stop_price"]) == 104
    targets = [row for row in current["orders"] if row["kind"] == "tp" and not row.get("retired")]
    assert [Decimal(r["price"]) for r in targets] == [95, 90]
    assert all(fake.orders[row["cloid"]]["order"]["reduceOnly"] for row in current["orders"] if not row.get("retired"))
    sent = copy.deepcopy(fake.actions)
    await trading.recover()
    assert fake.actions == sent
