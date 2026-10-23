import copy
import json
from decimal import Decimal

import httpx
import pytest
from test_adaptive_tps import setup_signal

from aqla_hyperliquid_mcp.models import AqlaError, require_first


def active_targets(fake):
    return [row["order"] for row in fake.orders.values()
            if row["status"] == "open" and not row["order"]["isTrigger"] and row["order"]["reduceOnly"]]


def assert_resize_preserved(state, fake, initial, before, remaining):
    trade = initial["trades"][before["trade_id"]]
    current = state.snapshot()["trades"][trade["trade_id"]]
    budgets = [max(Decimal(0), Decimal(budget) - Decimal(spent))
               for budget, spent in zip(before["target_budgets"], before["tp_filled"], strict=True)]
    assert Decimal(current["remaining_quantity"]) == remaining
    assert sum(Decimal(row["sz"]) for row in active_targets(fake)) == remaining
    assert current["tp_filled"] == before["tp_filled"]
    assert [Decimal(row["quantity"]) for row in current["orders"] if row["kind"] == "tp" and not row.get("retired")] == [size * remaining / sum(budgets) for size in budgets if size]
    assert current["plan"] == trade["plan"] and current["targets"] == trade["targets"]
    assert Decimal(current["reserved_risk"]) <= Decimal(trade["reserved_risk"])
    assert all(initial.get(key) == state.snapshot().get(key) for key in ("policy", "profiles", "account_limits"))
    assert any(row["status"] == "open" and row["order"]["isTrigger"] and row["order"]["reduceOnly"]
               and Decimal(row["order"]["sz"]) >= remaining for row in fake.orders.values())


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("filled_fraction", ["0", ".25", "1"])
async def test_legacy_reduction_resizes_only_unfilled_targets_and_is_idempotent(setup, side, filled_fraction):
    _, state, fake, _, trading = setup
    source, signal = setup_signal(trading, side, count=2)
    plan = await trading.preview(source.event_id, signal)
    trade = await trading.execute(plan["plan_id"])
    assert trade["lifecycle_version"] == 1
    original = copy.deepcopy(state.snapshot())
    first = require_first(row for row in trade["orders"] if row["kind"] == "tp" and row["index"] == 0)
    filled = Decimal(first["quantity"]) * Decimal(filled_fraction)
    if filled:
        fake.fill(first["cloid"], filled, first["price"])
        await trading.refresh_trade(trade["trade_id"])
    before = state.snapshot()["trades"][trade["trade_id"]]
    remaining = Decimal(before["remaining_quantity"]) / 2
    await trading.close_position(trade["trade_id"], Decimal(50), "operator:synthetic-reduce")
    await trading.refresh_trade(trade["trade_id"])
    assert_resize_preserved(state, fake, original, before, remaining)
    sent = copy.deepcopy(fake.actions)
    await trading.recover()
    await trading.recover()
    assert fake.actions == sent


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_legacy_target_resize_recovers_its_original_identity(setup, signal, monkeypatch, lost_reply):
    _, state, fake, exchange, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    await trading.close_position(trade["trade_id"], Decimal(50), "operator:synthetic-reduce")
    original = fake.handle
    mutate = exchange.mutate
    replacements = []
    interrupted = False

    def interrupt(request):
        nonlocal interrupted
        payload = json.loads(request.content)
        action = payload.get("action", {})
        replacement = action.get("type") == "order" and any(row["r"] and not row["t"].get("trigger") for row in action["orders"])
        if replacement and not interrupted:
            interrupted = True
        response = original(request)
        if replacement:
            replacements.extend(row["c"] for row in action["orders"])
            if lost_reply and len(replacements) == 2:
                raise httpx.ReadTimeout("Synthetic accepted TP replacement response lost")
        return response

    async def before_dispatch(identity, *args, **kwargs):
        nonlocal interrupted
        if identity.startswith("resize-targets:") and not lost_reply and not interrupted:
            interrupted = True
            raise RuntimeError("Synthetic crash before target dispatch")
        return await mutate(identity, *args, **kwargs)

    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(interrupt))
    monkeypatch.setattr(exchange, "mutate", before_dispatch)
    try:
        await trading.refresh_trade(trade["trade_id"])
    except RuntimeError as error:
        assert "Synthetic crash" in str(error)
    trading = type(trading)(exchange, state)
    await trading.recover()
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert len(replacements) == len(set(replacements)) == 2
    assert sum(Decimal(row["sz"]) for row in active_targets(fake)) == Decimal(current["remaining_quantity"])
    assert Decimal(current["remaining_quantity"]) == Decimal(trade["remaining_quantity"]) / 2


async def test_legacy_resize_keeps_the_last_native_quantity_tick(setup, signal):
    _, state, fake, _, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    await trading.close_position(trade["trade_id"], Decimal("99.8"), "operator:synthetic-small-remainder")
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["remaining_quantity"]) == Decimal(".01")
    assert sum(Decimal(row["sz"]) for row in active_targets(fake)) == Decimal(".01")
    assert current["tp_filled"] == ["0", "0"]
    assert not Decimal(current["target_budgets"][0])


@pytest.mark.parametrize("amount", [".2", "2.4"])
async def test_legacy_resize_recaptures_a_target_fill_racing_with_cancel(setup, signal, monkeypatch, amount):
    _, state, fake, exchange, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    first = require_first(row for row in trade["orders"] if row["kind"] == "tp" and row["index"] == 0)
    await trading.close_position(trade["trade_id"], Decimal(50), "operator:synthetic-reduce")
    original = fake.handle
    injected = False

    def fill_before_cancel(request):
        nonlocal injected
        action = json.loads(request.content).get("action", {})
        if not injected and action.get("type") == "cancelByCloid" and any(row["cloid"] == first["cloid"] for row in action["cancels"]):
            fake.fill(first["cloid"], amount, first["price"])
            injected = True
        return original(request)

    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(fill_before_cancel))
    sent = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    remaining = Decimal("2.4") - Decimal(amount)
    assert injected and Decimal(current["remaining_quantity"]) == remaining
    assert Decimal(current["tp_filled"][0]) == Decimal(amount)
    assert sum(Decimal(row["sz"]) for row in active_targets(fake)) == remaining
    if not remaining:
        assert current["status"] == "closed"
        assert not any(action["type"] == "order" for action in fake.actions[sent:])


async def test_legacy_resize_cannot_adopt_an_extra_foreign_position(setup, signal):
    _, _, fake, _, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    await trading.close_position(trade["trade_id"], Decimal(50), "operator:synthetic-reduce")
    fake.position += 1
    sent = copy.deepcopy(fake.actions)
    with pytest.raises(AqlaError, match="differs from remaining owned fills"):
        await trading.refresh_trade(trade["trade_id"])
    assert fake.actions == sent
