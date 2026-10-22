import copy
import json

import httpx
import pytest

from aqla_hyperliquid_mcp.models import now_ms


async def lost_target(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    event, value = signal
    plan = await trading.preview(event.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    staged = trade["initial_targets"]
    cloids = {row["c"] for row in staged["action"]["orders"]}
    for cloid in cloids:
        fake.orders.pop(cloid)

    def interrupted(data):
        op = data["operations"][staged["operation_id"]]
        op.update(phase="unknown", result=None)
        for cloid in cloids:
            data["trades"][trade["trade_id"]]["order_observations"].pop(cloid, None)

    state.update(interrupted)
    handle = fake.handle
    extra = {"frontendOpenOrders": [], "historicalOrders": [], "userFillsByTime": fake.fills}

    def transport(request):
        payload = json.loads(request.content)
        if request.url.path == "/info" and payload["type"] in extra:
            return httpx.Response(200, json=extra[payload["type"]])
        return handle(request)

    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(transport))
    monkeypatch.setattr("aqla_agent.target_recovery.now_ms", lambda: now_ms() + 60_000)
    return state, fake, exchange, trading, trade, staged, extra


@pytest.mark.asyncio
async def test_expired_target_absence_retires_exact_generation_then_restores_once(setup, signal, monkeypatch):
    state, fake, exchange, trading, trade, staged, _ = await lost_target(setup, signal, monkeypatch)
    before = copy.deepcopy(fake.actions)
    result = await exchange.reconcile(staged["operation_id"])
    assert result["resolved"] and result["absence_is_proof"] and not result["resubmitted"]
    assert fake.actions == before
    assert result["operation"]["outcome"] == "absent_expired"
    repeated = await exchange.reconcile(staged["operation_id"])
    assert repeated["resolved"] and repeated["absence_is_proof"] and fake.actions == before
    stop = next(row for row in trade["orders"] if row["kind"] == "sl")
    assert fake.orders[stop["cloid"]]["status"] == "open"
    await trading.refresh_trade(trade["trade_id"])
    after = len(fake.actions)
    assert after > len(before)
    await trading.refresh_trade(trade["trade_id"])
    assert len(fake.actions) == after
    current = state.read("trades")["trades"][trade["trade_id"]]
    assert current["status"] == "open" and all(
        row.get("retired")
        for row in current["orders"]
        if row["cloid"] in {r["c"] for r in staged["action"]["orders"]}
    )
    assert all(order.get("r") for action in fake.actions[len(before) :] for order in action.get("orders", []))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked",
    [
        "unexpired",
        "entry",
        "trigger",
        "observed",
        "open",
        "history",
        "fill",
        "truncated_history",
        "truncated_fills",
    ],
)
async def test_absence_never_allows_ambiguous_or_exposure_increasing_replacement(
    setup, signal, monkeypatch, blocked
):
    state, fake, exchange, _, trade, staged, extra = await lost_target(setup, signal, monkeypatch)
    cloid = staged["action"]["orders"][0]["c"]
    if blocked == "unexpired":
        monkeypatch.setattr("aqla_agent.target_recovery.now_ms", now_ms)
    elif blocked in ("entry", "trigger"):

        def change(data):
            row = data["operations"][staged["operation_id"]]["wire_action"]["orders"][0]
            if blocked == "entry":
                row["r"] = False
            else:
                row["t"] = {"trigger": {"triggerPx": "90", "tpsl": "sl", "isMarket": True}}

        state.update(change)
    elif blocked == "observed":
        state.update(
            lambda data: data["trades"][trade["trade_id"]]["order_observations"].update({cloid: {"oid": 999}})
        )
    elif blocked == "open":
        extra["frontendOpenOrders"] = [{"cloid": cloid}]
    elif blocked == "history":
        extra["historicalOrders"] = [{"order": {"cloid": cloid}, "status": "filled"}]
    elif blocked == "fill":
        extra["userFillsByTime"] = [
            {"coin": "BTC", "oid": 999, "tid": 999, "time": now_ms(), "sz": "1", "px": "110"}
        ]
    elif blocked == "truncated_history":
        extra["historicalOrders"] = [{"order": {"cloid": "other"}}] * 2000
    elif blocked == "truncated_fills":
        extra["userFillsByTime"] = [{"coin": "OTHER"}] * 2000
    before = copy.deepcopy(fake.actions)
    result = await exchange.reconcile(staged["operation_id"])
    assert not result["resolved"] and not result["absence_is_proof"] and fake.actions == before
    assert state.read("operations")["operations"][staged["operation_id"]]["phase"] == "unknown"
