"""TP2-to-TP1 uses real synthetic fills and the existing durable stop replacement."""
from decimal import ROUND_DOWN, Decimal

import httpx
import pytest
from test_adaptive_tps import setup_signal
from test_waverider_execution import WaveExchange

from aqla_agent import waverider
from aqla_agent.pipelines import AccountLimits, Parameters
from aqla_agent.waverider_replay import ReplaySimulation
from aqla_hyperliquid_mcp.models import AccountSnapshot, Interpretation, Market, Policy, now_ms, require_first
from aqla_hyperliquid_mcp.rules import make_plan
from aqla_hyperliquid_mcp.state import State


async def open_synthetic_trade(setup, monkeypatch, side, lifecycle="waverider", count=4):
    _, state, old, exchange, trading = setup
    boundary = WaveExchange(old.signer)
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(boundary.handle))
    async def facts(*_):
        return {"atr": "1", "fee_rate": ".0004", "observed_at": now_ms()}
    monkeypatch.setattr(waverider, "market_facts", facts)
    state.update(lambda data: data["policy"].update(stop_management="tp2_to_tp1", take_profit_allocation="adaptive",
        entry_range_choice="midpoint" if lifecycle == "legacy" else "split",
        management_mode="waverider" if lifecycle == "waverider" else "standard",
        waverider_lifetime="until_target", waverider_until_tp=2))
    source, signal = setup_signal(trading, side, count=count)
    plan = await trading.preview(source.event_id, signal)
    trade = await trading.execute(plan["plan_id"])
    return boundary, plan, trade


def target(trade, index):
    return require_first(row for row in trade["orders"] if row["kind"] == "tp" and row["index"] == index and not row.get("retired"))


async def fill_target(trading, state, boundary, identity, index, *, partial=False):
    current = state.snapshot()["trades"][identity]
    order = target(current, index)
    remaining = Decimal(boundary.orders[order["cloid"]]["order"]["sz"])
    amount = (remaining / 2).quantize(Decimal(".01"), rounding=ROUND_DOWN) if partial else remaining
    boundary.mid = str(Decimal(order["price"]) + (1 if current["plan"]["side"] == "long" else -1))
    boundary.fill(order["cloid"], amount, order["price"])
    await trading.refresh_trade(identity)
    return state.snapshot()["trades"][identity]


@pytest.mark.parametrize("model", [Policy, Parameters])
def test_new_mode_roundtrips_without_changing_existing_defaults(model):
    assert model().stop_management == "break_even"
    chosen = model(stop_management="tp2_to_tp1")
    assert model.model_validate(chosen.model_dump(mode="json")).stop_management == "tp2_to_tp1"
    assert "tp2_to_tp1" in model.model_json_schema()["properties"]["stop_management"]["enum"]
    assert Parameters(stop_management="tp2_to_tp1").policy(AccountLimits(max_open_trades=2, max_total_risk_percent=2)).stop_management == "tp2_to_tp1"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("lifecycle", ["legacy", "managed", "waverider"])
@pytest.mark.parametrize("count", [2, 4])
async def test_only_full_own_tp2_moves_the_remaining_stop(setup, monkeypatch, side, lifecycle, count):
    _, state, _, _, trading = setup
    boundary, plan, trade = await open_synthetic_trade(setup, monkeypatch, side, lifecycle, count)
    identity = trade["trade_id"]
    original = trade["stop_price"]
    first = await fill_target(trading, state, boundary, identity, 0)
    assert first["stop_price"] == original
    partial = await fill_target(trading, state, boundary, identity, 1, partial=True)
    assert partial["stop_price"] == original and "2" not in partial.get("stop_ladder_intents", {})
    finished = await fill_target(trading, state, boundary, identity, 1)
    if count == 2:
        assert finished["status"] == "closed" and Decimal(finished["remaining_quantity"]) == 0
        assert not finished.get("stop_ladder_intents")
    else:
        assert Decimal(finished["stop_price"]) == Decimal(plan["take_profits"][0])
        assert finished["stop_ladder_stage"] == 2 and set(finished["stop_ladder_intents"]) == {"2"}
        cloids = {row["cloid"] for row in finished["orders"] if row["kind"] == "sl" and not row.get("retired")}
        native = [boundary.orders[cloid]["order"] for cloid in cloids if boundary.orders[cloid]["status"] == "open"]
        assert native and all(row["reduceOnly"] and Decimal(row["triggerPx"]) == Decimal(finished["stop_price"])
                              and Decimal(row["sz"]) >= Decimal(finished["remaining_quantity"]) for row in native)
    assert finished["plan"] == plan and Decimal(finished["reserved_risk"]) <= Decimal(trade["reserved_risk"])


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["long", "short"])
async def test_tp1_reference_is_frozen_and_a_tighter_stop_is_preserved(setup, monkeypatch, side):
    _, state, _, _, trading = setup
    boundary, plan, trade = await open_synthetic_trade(setup, monkeypatch, side)
    identity = trade["trade_id"]
    await fill_target(trading, state, boundary, identity, 0)
    sign = 1 if side == "long" else -1
    replacements = [Decimal(value) + 2 * sign for value in plan["take_profits"]]
    await trading.replace_targets(identity, replacements, "synthetic-new-targets")
    boundary.mid = str(100 + 25 * sign)
    tighter = Decimal(100 + 15 * sign)
    await trading.replace_stop(identity, tighter, "synthetic-tighter-stop")
    current = await fill_target(trading, state, boundary, identity, 1)
    assert Decimal(current["stop_price"]) == tighter
    assert Decimal(current["stop_ladder_intents"]["2"]) == Decimal(plan["take_profits"][0])
    assert Decimal(current["completed_target_prices"]["0"]) == Decimal(plan["take_profits"][0])


@pytest.mark.asyncio
async def test_lost_tp2_stop_response_recovers_after_real_state_reopen_without_double_order(setup, monkeypatch):
    settings, state, _, exchange, trading = setup
    boundary, plan, trade = await open_synthetic_trade(setup, monkeypatch, "long")
    identity = trade["trade_id"]
    await fill_target(trading, state, boundary, identity, 0)
    operation = "tp-ladder:" + identity + ":2"
    mutate, status = exchange.mutate, exchange.order_status
    hidden = True
    async def lose_response(name, action, **kwargs):
        if name == operation:
            boundary.lost_response = True
        return await mutate(name, action, **kwargs)
    async def delayed_status(name, user=None):
        if name == exchange.cloid(operation) and hidden:
            return {"status": "unknownOid"}
        return await status(name, user)
    monkeypatch.setattr(exchange, "mutate", lose_response)
    monkeypatch.setattr(exchange, "order_status", delayed_status)
    current = await fill_target(trading, state, boundary, identity, 1)
    assert Decimal(current["stop_price"]) == Decimal(plan["stop_loss"])
    order_count = sum(row["type"] == "order" for row in boundary.actions)
    original_intents = dict(current["stop_ladder_intents"])
    state.close()
    reopened = State(settings)
    try:
        exchange.state = reopened
        recovered = type(trading)(exchange, reopened)
        hidden = False
        await recovered.recover()
        await recovered.recover()
        current = reopened.snapshot()["trades"][identity]
        assert Decimal(current["stop_price"]) == Decimal(plan["take_profits"][0])
        assert current["stop_ladder_intents"] == original_intents and current["stop_ladder_stage"] == 2
        assert sum(row["type"] == "order" for row in boundary.actions) == order_count
    finally:
        reopened.close()


@pytest.mark.parametrize("side", ["long", "short"])
def test_causal_replay_uses_the_same_tp2_rule(side):
    sign = 1 if side == "long" else -1
    stamp = 1000000
    source = Interpretation(kind="new_signal", source_refs=["synthetic"], symbol="BTC", side=side,
        entry="market", stop_loss=100 - 10 * sign, take_profits=[100 + distance * sign for distance in (10, 20, 30)])
    plan = make_plan(source, Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20),
        AccountSnapshot(equity=10000, available_margin=10000, observed_at=stamp),
        Policy(stop_management="tp2_to_tp1", take_profit_allocation="adaptive", waverider_until_tp=2),
        1, stamp, ("testnet", "account"), stamp=stamp)
    prices = [100, 100, 100, 111, 111, 121, 121, 109, 109, 109]
    rows = [{"quote": {"market": "BTC", "bid": str(100 + (price - 100) * sign), "ask": str(100 + (price - 100) * sign),
        "mid": str(100 + (price - 100) * sign), "stop_reference": str(100 + (price - 100) * sign), "stop_reference_kind": "mark",
        "started_at": stamp + i * 1000, "exchange_time": stamp + i * 1000, "observed_at": stamp + i * 1000,
        "bids": [[str(100 + (price - 100) * sign), "10000"]], "asks": [[str(100 + (price - 100) * sign), "10000"]]},
        "fee_rate": ".0004", "atr": "1"} for i, price in enumerate(prices)]
    case = {"plan": plan.model_dump(mode="json"), "latency_ms": 1000, "observations": rows,
        "original_entry_events": [{"kind": "entry", "entry_role": "original", "entry_leg": 0, "time": stamp,
            "sz": str(plan.quantity), "px": "100", "fee": ".01", "feeToken": "USDC", "oid": 1}]}
    first = ReplaySimulation({**case, "observations": rows[:5]}, wave=True)
    first.run()
    assert first.stop == plan.stop_loss
    second = ReplaySimulation({**case, "observations": rows[:7]}, wave=True)
    second.run()
    assert second.stop == plan.take_profits[0]
    complete = ReplaySimulation(case, wave=True)
    complete.run()
    assert complete.stopped and complete.stop == plan.take_profits[0]
