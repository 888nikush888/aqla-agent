import copy
from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms


async def opened(setup, signal):
    *_, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    return trade


def update(trading, message_id, action, text, **fields):
    source = SourceEvent(chat_id="channel", message_id=message_id, sent_at=now_ms(), reply_to_id=1, text=text)
    trading.ingest(source)
    evidence = fields.pop("evidence", [])
    interpretation = Interpretation(kind="trade_update", source_refs=[source.source_id], update_action=action, evidence=evidence, **fields)
    return source, interpretation


@pytest.mark.asyncio
async def test_partial_entry_is_frozen_before_targets_are_sized(setup, signal):
    _, _, fake, _, trading = setup
    source, interpretation = signal
    interpretation.entry = "limit"
    interpretation.entry_prices = [Decimal("100")]
    fake.partial_limit_fraction = 0.5
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    assert fake.orders[trade["entry_cloid"]]["status"] == "canceled"
    assert trade["status"] == "open"
    assert Decimal(trade["filled_quantity"]) == Decimal(plan["quantity"]) / 2
    assert sum(Decimal(order["quantity"]) for order in trade["orders"] if order["kind"] == "tp") == Decimal(trade["filled_quantity"])


@pytest.mark.asyncio
async def test_tp_partial_fill_is_not_reallocated_when_targets_change(setup, signal):
    _, state, fake, _, trading = setup
    trade = await opened(setup, signal)
    target = next(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.mid = "115"
    consumed = Decimal(target["quantity"]) / 2
    fake.fill(target["cloid"], consumed, "110")
    await trading.refresh_trade(trade["trade_id"])
    source, interpretation = update(trading, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=["115", "125"])
    interpretation.evidence = [Evidence(field="take_profits.0", source_id=source.source_id, quote="115"), Evidence(field="take_profits.1", source_id=source.source_id, quote="125")]
    await trading.update_trade(source.event_id, interpretation)
    current = state.snapshot()["trades"][trade["trade_id"]]
    active = [order for order in current["orders"] if order["kind"] == "tp" and not order.get("retired")]
    first = next(order for order in active if order["index"] == 0)
    assert Decimal(first["quantity"]) == Decimal(target["quantity"]) - consumed
    assert sum(Decimal(order["quantity"]) for order in active) == Decimal(str(fake.position))
    assert all(order["cloid"] != target["cloid"] for order in active)


@pytest.mark.asyncio
async def test_completed_tp_budget_moves_stop_only_from_own_exchange_fills(setup, signal):
    _, state, fake, _, trading = setup
    state.update(lambda row: row["policy"].update(break_even_after_tp=1))
    trade = await opened(setup, signal)
    fake.mid = "115"
    target = next(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.fill(target["cloid"], Decimal(target["quantity"]) / 2, "110")
    await trading.refresh_trade(trade["trade_id"])
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["stop_price"]) == 90
    fake.fill(target["cloid"], Decimal(target["quantity"]) / 2, "110")
    await trading.refresh_trade(trade["trade_id"])
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["stop_price"]) == 100


@pytest.mark.asyncio
async def test_rejected_initial_stop_triggers_owned_emergency_reduction(setup, signal):
    _, state, fake, _, trading = setup
    fake.reject_stop = True
    trade = await opened(setup, signal)
    assert fake.position == 0
    assert trade["status"] == "closing"
    await trading.refresh_trade(trade["trade_id"])
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"


@pytest.mark.asyncio
async def test_closed_old_trade_cannot_close_a_later_foreign_position(setup, signal):
    _, state, fake, _, trading = setup
    trade = await opened(setup, signal)
    await trading.close_position(trade["trade_id"])
    await trading.refresh_trade(trade["trade_id"])
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
    fake.position = 1.0
    before = len(fake.actions)
    result = await trading.close_position(trade["trade_id"])
    assert result["already_closed"]
    assert len(fake.actions) == before
    assert fake.position == 1.0


@pytest.mark.asyncio
async def test_same_update_and_close_identity_do_not_dispatch_twice(setup, signal):
    _, _, fake, _, trading = setup
    trade = await opened(setup, signal)
    source, interpretation = update(trading, 2, "reduce", "Halbe Position schließen", reduce_percent="50")
    await trading.update_trade(source.event_id, interpretation)
    before = len(fake.actions)
    await trading.update_trade(source.event_id, interpretation)
    assert len(fake.actions) == before
    assert Decimal(str(fake.position)) == Decimal(trade["filled_quantity"]) / 2


@pytest.mark.asyncio
async def test_invalid_update_is_rejected_before_any_cancel_or_binding(setup, signal):
    _, state, fake, _, trading = setup
    trade = await opened(setup, signal)
    source, interpretation = update(trading, 2, "move_stop", "SL auf 80", stop_loss="80")
    interpretation.evidence = [Evidence(field="stop_loss", source_id=source.source_id, quote="80")]
    before = len(fake.actions)
    with pytest.raises(AqlaError, match="widening"):
        await trading.update_trade(source.event_id, interpretation)
    assert len(fake.actions) == before
    assert "update:" + source.event_id not in state.snapshot()["bindings"]
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["stop_price"]) == 90


@pytest.mark.asyncio
async def test_source_edit_invalidates_unsent_plan_and_historical_update_never_trades(setup, signal):
    _, _, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    edited = source.model_copy(update={"edited_at": now_ms() + 1, "text": source.text.replace("90", "95")})
    trading.ingest(edited)
    with pytest.raises(AqlaError, match="Source changed"):
        await trading.execute(plan["plan_id"])
    historic = SourceEvent(chat_id="channel", message_id=5, sent_at=now_ms(), text="Rest schließen", historical=True)
    trading.ingest(historic)
    with pytest.raises(AqlaError, match="Historical"):
        trading.record_interpretation(historic.event_id, Interpretation(kind="trade_update", source_refs=[historic.source_id], update_action="close"))
    assert fake.actions == []


@pytest.mark.asyncio
async def test_repeated_interpretation_keeps_executed_source_state(setup, signal):
    _, state, _, _, trading = setup
    await opened(setup, signal)
    source, interpretation = signal
    trading.record_interpretation(source.event_id, copy.deepcopy(interpretation))
    assert state.snapshot()["events"][source.event_id]["status"] == "executed"
