import json
from decimal import Decimal

import httpx
import pytest
from conftest import FakeExchange

from aqla_agent import waverider
from aqla_agent.managed_entries import position_basis
from aqla_agent.waverider import execute, pools, resume_exit
from aqla_hyperliquid_mcp.models import Market, digest


class WaveExchange(FakeExchange):
    """Native IOC limits, fee currency and decimal positions at the HTTP boundary."""

    def update_position(self):
        orders = {row["order"]["oid"]: row["order"] for row in self.orders.values()}
        self.position = sum((Decimal(row["sz"]) * (1 if orders[row["oid"]]["side"] == "B" else -1) for row in self.fills), Decimal(0))

    def handle(self, request):
        self.update_position()
        before = len(self.fills)
        payload = json.loads(request.content)
        response = super().handle(request)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order":
            data = response.json()
            by_oid = {row["order"]["oid"]: row for row in self.orders.values()}
            for fill in list(self.fills[before:]):
                order = by_oid[fill["oid"]]["order"]
                price = Decimal(self.mid)
                marketable = price <= Decimal(order["limitPx"]) if order["side"] == "B" else price >= Decimal(order["limitPx"])
                if not marketable:
                    self.fills.remove(fill)
                    by_oid[fill["oid"]]["status"] = "canceled"
                else:
                    fill["px"] = self.mid
                    for status in data["response"]["data"]["statuses"]:
                        if status.get("filled", {}).get("oid") == fill["oid"]:
                            status["filled"]["avgPx"] = self.mid
            response = httpx.Response(response.status_code, json=data)
        for fill in self.fills:
            fill["feeToken"] = "USDC"
        self.update_position()
        if request.url.path == "/info" and payload.get("type") == "userFillsByTime":
            response = httpx.Response(200, json=self.fills)
        return response


async def wave_trade(setup, signal, monkeypatch):
    _, state, old, exchange, trading = setup
    boundary = WaveExchange(old.signer)
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(boundary.handle))
    monkeypatch.setattr(waverider, "qualification_valid", lambda *_: True)
    async def facts(*_):
        from aqla_hyperliquid_mcp.models import now_ms
        return {"atr": "1", "fee_rate": ".0004", "observed_at": now_ms()}
    monkeypatch.setattr(waverider, "market_facts", facts)
    state.update(lambda data: data["policy"].update(management_mode="waverider"))
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    return state, boundary, exchange, trading, plan, trade


@pytest.mark.asyncio
async def test_profit_exit_keeps_core_and_restores_source_targets(setup, signal, monkeypatch):
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch)
    initial = Decimal(trade["remaining_quantity"])
    boundary.mid = "104"
    quote = await exchange.price_snapshot(Market.model_validate(plan["market"]))
    wave = {**state.snapshot()["trades"][trade["trade_id"]]["waverider"], "shadow": False}
    await execute(trading, trade["trade_id"], wave, "reduce", initial / 2, quote, Decimal(".2"))
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert boundary.position == initial / 2
    assert not current.get("suspended_wave_targets")
    assert pools(current, Decimal(50))["core"] == initial / 2
    active = [row for row in boundary.orders.values() if row["status"] == "open" and row["order"]["reduceOnly"] and not row["order"]["isTrigger"]]
    assert sum(Decimal(row["order"]["sz"]) for row in active) == initial / 2
    assert current["waverider"]["phase"] == "waiting_rebound"
    boundary.mid = "101"
    quote = await exchange.price_snapshot(Market.model_validate(plan["market"]))
    await execute(trading, trade["trade_id"], current["waverider"], "reenter", initial / 2, quote, Decimal(".2"))
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert initial / 2 < Decimal(current["remaining_quantity"]) <= initial
    assert Decimal(current["source_entry_vwap"]) == 100
    assert position_basis(current)[0] == Decimal(current["remaining_quantity"])
    assert pools(current, Decimal(50))["core"] == initial / 2


@pytest.mark.asyncio
async def test_restart_before_optional_exit_send_aborts_without_an_order(setup, signal, monkeypatch):
    state, boundary, _, _, _, trade = await wave_trade(setup, signal, monkeypatch)
    identity = "waverider:" + trade["trade_id"] + ":0:reduce"
    def stage(data):
        row = data["trades"][trade["trade_id"]]
        row["suspended_wave_targets"] = identity
        row["waverider"].update(phase="preparing_reduce", operation_id=identity)
    state.update(stage)
    actions = len(boundary.actions)
    assert await resume_exit(setup[-1], trade["trade_id"])
    assert len(boundary.actions) == actions
    assert state.snapshot()["trades"][trade["trade_id"]]["suspended_wave_targets"] is None


@pytest.mark.asyncio
async def test_closed_cycle_has_one_pnl_and_original_margin_not_turnover(setup, signal, monkeypatch):
    from aqla_agent.conflict_review import observe_conflict
    from aqla_agent.monthly import report_row
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch)
    identity = trade["trade_id"]
    market = Market.model_validate(plan["market"])
    original = Decimal(trade["remaining_quantity"])
    boundary.mid = "104"
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reduce", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    boundary.mid = "101"
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    await trading.refresh_trade(identity)
    boundary.mid = "108"
    await trading.close_position(identity, Decimal(100), "operator:fake-close")
    await trading.refresh_trade(identity)
    closed = state.snapshot()["trades"][identity]
    assert closed["status"] == "closed"
    assert Decimal(closed["entry_accounting"]["initial_margin"]) == original * 100 / plan["effective_leverage"]
    assert Decimal(closed["entry_accounting"]["entry_turnover"]) > original * 100
    def funding(data):
        row = data["trades"][identity]
        row["funding_events"] = {}
        row["funding_coverage"] = {"complete": True, "start": row["entry_accounting"]["entry_at"], "end": row["closed_at"],
            "fill_fingerprint": digest(row["fill_events"]), "payments_fingerprint": digest({})}
    state.update(funding)
    snapshot = state.snapshot()
    row = report_row(snapshot, snapshot["trades"][identity])
    expected = sum((Decimal(fill["sz"]) * Decimal(fill["px"]) * (1 if fill["kind"] != "entry" else -1)
                    - Decimal(fill["fee"]) for fill in snapshot["trades"][identity]["fill_events"].values()), Decimal(0))
    assert row["complete"], row["issues"]
    assert abs(Decimal(row["net_pnl"]) - expected) < Decimal("1e-18")
    state.update(lambda data: data["trades"][identity].update(status="conflicted"))
    review, _ = await observe_conflict(trading, identity)
    assert review["resolvable"], review
