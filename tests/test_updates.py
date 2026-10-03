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
async def test_break_even_uses_reduced_replacement_target_budget(setup, signal):
    _, state, fake, _, trading = setup
    state.update(lambda row: row["policy"].update(break_even_after_tp=1))
    trade = await opened(setup, signal)
    assert Decimal(trade["filled_quantity"]) == Decimal("4.80")
    await trading.close_position(trade["trade_id"], Decimal(50), "reduction")
    await trading.refresh_trade(trade["trade_id"])
    source, interpretation = update(trading, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=["115", "125"])
    interpretation.evidence = [Evidence(field="take_profits.0", source_id=source.source_id, quote="115"), Evidence(field="take_profits.1", source_id=source.source_id, quote="125")]
    await trading.update_trade(source.event_id, interpretation)
    current = state.snapshot()["trades"][trade["trade_id"]]
    target = next(order for order in current["orders"] if order["kind"] == "tp" and order["index"] == 0 and not order.get("retired"))
    assert Decimal(target["quantity"]) == Decimal("1.20")
    fake.mid = "115"
    fake.fill(target["cloid"], target["quantity"], "115")
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["stop_price"]) == Decimal(current["entry_vwap"]) == 100
    assert Decimal(current["remaining_quantity"]) == Decimal("1.20")
    # A later replacement preserves the fulfilled first target instead of topping it up.
    source, interpretation = update(trading, 3, "replace_targets", "Neue Ziele 120 und 130", take_profits=["120", "130"])
    interpretation.evidence = [Evidence(field="take_profits.0", source_id=source.source_id, quote="120"), Evidence(field="take_profits.1", source_id=source.source_id, quote="130")]
    await trading.update_trade(source.event_id, interpretation)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["target_budgets"][0]) == Decimal(current["tp_filled"][0]) == Decimal("1.20")
    assert not any(order["kind"] == "tp" and order["index"] == 0 and not order.get("retired") for order in current["orders"])


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["0", None, "missing"])
async def test_missing_or_removed_target_budget_does_not_prove_break_even_fill(setup, signal, budget):
    _, state, _, _, trading = setup
    state.update(lambda row: row["policy"].update(break_even_after_tp=1))
    trade = await opened(setup, signal)

    def remove_budget(data):
        if budget == "missing":
            data["trades"][trade["trade_id"]].pop("target_budgets")
        else:
            data["trades"][trade["trade_id"]]["target_budgets"][0] = budget

    state.update(remove_budget)
    await trading.refresh_trade(trade["trade_id"])
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["stop_price"]) == 90


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


@pytest.mark.asyncio
async def test_completed_close_update_is_deduplicated_after_trade_disappears_from_candidates(setup, signal, monkeypatch):
    _, state, fake, _, trading = setup
    trade = await opened(setup, signal)
    source, interpretation = update(trading, 2, "close", "Rest schließen")
    await trading.update_trade(source.event_id, interpretation)
    await trading.recover()
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
    trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1, "text": "Neue Anweisung"}))
    before = len(fake.actions)

    def no_candidates(*args):
        raise AssertionError("A bound completed update must not search open trade candidates")

    monkeypatch.setattr(trading, "update_candidates", no_candidates)
    result = await trading.update_trade(source.event_id, copy.deepcopy(interpretation))
    assert result["duplicate"]
    assert len(fake.actions) == before
    with pytest.raises(AqlaError, match="already bound"):
        await trading.update_trade(source.event_id, interpretation.model_copy(update={"update_action": "reduce", "reduce_percent": Decimal(50)}))
    assert len(fake.actions) == before


@pytest.mark.asyncio
async def test_interrupted_close_update_keeps_its_binding_after_recovery_closes_trade(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    trade = await opened(setup, signal)
    source, interpretation = update(trading, 2, "close", "Rest schließen")
    original = trading.close_position

    async def interrupted(*args):
        await original(*args)
        raise RuntimeError("Crash after close dispatch before updating the event")

    monkeypatch.setattr(trading, "close_position", interrupted)
    with pytest.raises(RuntimeError):
        await trading.update_trade(source.event_id, interpretation)
    monkeypatch.setattr(trading, "close_position", original)
    await trading.recover()
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
    before = len(fake.actions)
    result = await trading.update_trade(source.event_id, interpretation)
    assert result["trade_id"] == trade["trade_id"]
    assert result["result"]["operation_id"] == "update:" + source.event_id
    assert state.snapshot()["events"][source.event_id]["status"] == "updated"
    assert len(fake.actions) == before
    assert "update:" + source.event_id in exchange.state.snapshot()["bindings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["long", "short"])
async def test_owned_close_limit_stays_inside_slippage_bound_on_each_side(setup, signal, side):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    if side == "short":
        source = source.model_copy(update={"edited_at": source.sent_at + 1, "text": "BTC verkaufen. SL 110. Ziele 90 und 80."})
        trading.ingest(source)
        interpretation = interpretation.model_copy(update={"side": "short", "stop_loss": Decimal(110), "take_profits": [Decimal(90), Decimal(80)], "evidence": [
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in [
                ("symbol", "BTC"), ("side", "verkaufen"), ("stop_loss", "SL 110"), ("take_profits.0", "Ziele 90"), ("take_profits.1", "80")]]})
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    fake.mid = "100.0001"
    await trading.close_position(trade["trade_id"], operation_id="rounded-close")
    order = state.snapshot()["operations"]["rounded-close"]["wire_action"]["orders"][0]
    bound = Decimal(fake.mid) * (Decimal("0.997") if side == "long" else Decimal("1.003"))
    assert Decimal(order["p"]) >= bound if side == "long" else Decimal(order["p"]) <= bound
