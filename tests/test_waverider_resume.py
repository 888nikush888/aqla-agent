import json
from decimal import Decimal

import httpx
import pytest
from test_waverider_execution import wave_trade

from aqla_agent import waverider
from aqla_agent.managed_entries import position_basis
from aqla_hyperliquid_mcp import models
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, Market, SourceEvent, now_ms
from aqla_hyperliquid_mcp.precision import quantity_grid


async def reduced_trade(setup, signal, monkeypatch, side):
    if side == "short":
        source = SourceEvent(chat_id="channel", message_id=2, sent_at=now_ms(),
            text="BTC SHORT Entry 100 SL 110 TP 90 80")
        setup[-1].ingest(source)
        interpretation = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC",
            side="short", entry="market", stop_loss=110, take_profits=[90, 80],
            evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "BTC"), ("side", "SHORT"), ("stop_loss", "110"),
                ("take_profits.0", "90"), ("take_profits.1", "80"))])
        signal = source, interpretation
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch, "until_target")
    identity = trade["trade_id"]
    quantity = Decimal(trade["remaining_quantity"])
    market = Market.model_validate(plan["market"])
    boundary.mid = "104" if side == "long" else "96"
    await waverider.execute(trading, identity, trade["waverider"], "reduce", quantity / 2,
                           await exchange.price_snapshot(market), Decimal(".2"))
    assert waverider.pools(state.snapshot()["trades"][identity], Decimal(50))["flex"] == 0
    return state, boundary, exchange, trading, plan, identity, quantity


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("legacy_paused", [False, True])
async def test_cost_blocked_reentry_keeps_rights_and_resumes_once_after_restart(setup, signal, monkeypatch, side, legacy_paused):
    state, boundary, exchange, trading, plan, identity, quantity = await reduced_trade(setup, signal, monkeypatch, side)
    stamp = now_ms()
    clock = [stamp]
    monkeypatch.setattr(models.time, "time_ns", lambda: clock[0] * 1_000_000)
    market = Market.model_validate(plan["market"])
    def seed(data):
        wave = data["trades"][identity]["waverider"]
        wave.update(phase="paused" if legacy_paused else "waiting_rebound", wave_atr=".5",
            extreme="100.5" if side == "long" else "-99.5", confirmation_since=stamp-2000,
            confirmation_count=2, last_observed_at=stamp-1000,
            market_facts={"fee_rate": ".0004", "atr": ".5", "observed_at": stamp})
        if legacy_paused:
            wave.update(resume_phase="pending_reenter", operation_id=f"waverider:{identity}:0:reduce")
    state.update(seed)
    boundary.mid = "103.1" if side == "long" else "96.9"
    before = list(boundary.fills)
    await waverider.tick(trading, identity, await exchange.price_snapshot(market))
    current = state.snapshot()["trades"][identity]
    assert current["waverider"]["phase"] == "waiting_rebound"
    assert not current["waverider"].get("finished") and not current.get("entries_disabled")
    assert current["waverider"].get("cycle", 0) == 0 and boundary.fills == before
    assert waverider.pools(current, Decimal(50))["rights"] == quantity / 2
    trading = type(trading)(exchange, state)
    await trading.recover()
    assert boundary.fills == before
    for price in ("102.5", "102.8", "102.8", "102.8", "102.8"):
        boundary.mid = price if side == "long" else str(200 - Decimal(price))
        clock[0] += 1000
        await waverider.tick(trading, identity, await exchange.price_snapshot(market))
    await trading.recover()
    current = state.snapshot()["trades"][identity]
    reentries = [row for row in current["fill_events"].values() if row.get("entry_role") == "waverider"]
    assert len(reentries) == 1 and reentries[0]["oid"] != before[-1]["oid"]
    assert quantity / 2 < position_basis(current)[0] <= quantity
    assert current["waverider"]["cycle"] == 1 and current["plan"] == plan
    fills = list(boundary.fills)
    await trading.recover()
    assert boundary.fills == fills


@pytest.mark.parametrize("side", ["long", "short"])
async def test_pending_reentry_never_counts_previous_exit_fills(setup, signal, monkeypatch, side):
    state, boundary, exchange, trading, _, identity, quantity = await reduced_trade(setup, signal, monkeypatch, side)
    trade = state.snapshot()["trades"][identity]
    phase = {**trade["waverider"], "phase": "pending_reenter", "operation_id": f"waverider:{identity}:0:reduce"}
    result = await waverider.resume_cycle(trading, identity, phase)
    assert result["phase"] == "waiting_rebound" and result.get("cycle", 0) == 0
    assert "operation_id" not in result
    assert position_basis(state.snapshot()["trades"][identity])[0] == quantity / 2
    assert len(boundary.fills) == 2


@pytest.mark.parametrize("side", ["long", "short"])
async def test_lost_reentry_response_reconciles_its_own_fill_without_resending(setup, signal, monkeypatch, side):
    state, boundary, exchange, trading, plan, identity, quantity = await reduced_trade(setup, signal, monkeypatch, side)
    operation = f"waverider:{identity}:0:reenter"
    cloid = exchange.cloid(operation)
    handle = boundary.handle
    submissions = []
    def lost_reply(request):
        payload = json.loads(request.content)
        response = handle(request)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order":
            if any(row.get("c") == cloid for row in payload["action"]["orders"]):
                submissions.append(cloid)
                raise httpx.ReadTimeout("Synthetic accepted reentry with a lost response")
        return response
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(lost_reply))
    boundary.mid = "101" if side == "long" else "99"
    await waverider.execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", quantity / 2,
                           await exchange.price_snapshot(Market.model_validate(plan["market"])), Decimal(".2"))
    current = state.snapshot()["trades"][identity]
    assert current["waverider"]["operation_id"] == operation
    assert state.snapshot()["operations"][operation]["phase"] == "unknown"
    trading = type(trading)(exchange, state)
    await trading.recover()
    await trading.recover()
    current = state.snapshot()["trades"][identity]
    assert submissions == [cloid]
    assert len([row for row in current["fill_events"].values() if row.get("entry_role") == "waverider"]) == 1
    assert current["waverider"]["cycle"] == 1 and current["waverider"]["phase"] == "waiting_rise"
    assert quantity / 2 < position_basis(current)[0] <= quantity
    assert current["plan"] == plan


async def test_one_native_tick_of_noise_cannot_reduce_but_two_confirmed_ticks_can(setup, signal, monkeypatch):
    state, boundary, exchange, trading, raw, trade = await wave_trade(setup, signal, monkeypatch, "until_target")
    identity = trade["trade_id"]
    initial = Decimal(trade["remaining_quantity"])
    market = Market.model_validate(raw["market"])
    clock = [now_ms() + 1000]
    monkeypatch.setattr(models.time, "time_ns", lambda: clock[0] * 1_000_000)
    state.update(lambda data: data["trades"][identity].update(waverider={"phase": "waiting_rise", "cycle": 0,
        "finished": False, "checkpoint": clock[0],
        "market_facts": {"fee_rate": ".0004", "atr": ".001", "observed_at": clock[0]}}))
    before = list(boundary.fills)
    for price in ("104", "103.99", "103.99", "103.99"):
        clock[0] += 1000
        boundary.mid = price
        await waverider.tick(trading, identity, await exchange.price_snapshot(market))
    assert boundary.fills == before and boundary.position == initial
    for _ in range(3):
        clock[0] += 1000
        boundary.mid = "103.98"
        await waverider.tick(trading, identity, await exchange.price_snapshot(market))
    assert boundary.position == initial - quantity_grid(initial / 2, market)
    current = state.snapshot()["trades"][identity]
    assert current["waverider"]["phase"] == "waiting_rebound" and current["waverider"]["cycle"] == 0
