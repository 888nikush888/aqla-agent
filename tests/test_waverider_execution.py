import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
from conftest import FakeExchange

from aqla_agent import waverider
from aqla_agent.managed_entries import position_basis
from aqla_agent.waverider import execute, pools, resume_exit
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, Market, SourceEvent, digest, now_ms


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


async def wave_trade(setup, signal, monkeypatch, lifetime="signal_deadline"):
    _, state, old, exchange, trading = setup
    boundary = WaveExchange(old.signer)
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(boundary.handle))
    monkeypatch.setattr(waverider, "qualification_valid", lambda *_: True)
    async def facts(*_):
        from aqla_hyperliquid_mcp.models import now_ms
        return {"atr": "1", "fee_rate": ".0004", "observed_at": now_ms()}
    monkeypatch.setattr(waverider, "market_facts", facts)
    state.update(lambda data: data["policy"].update(management_mode="waverider", waverider_lifetime=lifetime))
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    return state, boundary, exchange, trading, plan, trade


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["source", "edit", "break_even", "direct"])
async def test_stop_change_preserves_waves_and_reentry_after_restart(setup, signal, monkeypatch, origin):
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch, "until_target")
    identity, initial = trade["trade_id"], Decimal(trade["remaining_quantity"])
    boundary.mid = "104"
    before = list(boundary.fills)
    if origin == "direct":
        await trading.replace_stop(identity, Decimal(95), "operator:synthetic-stop")
    else:
        source = SourceEvent(chat_id="channel", message_id=signal[0].message_id if origin == "edit" else 2,
            reply_to_id=None if origin == "edit" else signal[0].message_id,
            edited_at=now_ms() if origin == "edit" else None, sent_at=signal[0].sent_at if origin == "edit" else now_ms(),
            text="BTC Stop auf 95" if origin != "break_even" else "BTC Stop auf Einstand")
        trading.ingest(source)
        change = Interpretation(kind="trade_update", source_refs=[source.source_id], symbol="BTC",
            update_action="break_even" if origin == "break_even" else "move_stop",
            stop_loss=None if origin == "break_even" else 95,
            evidence=[Evidence(field="symbol", source_id=source.source_id, quote="BTC"),
                Evidence(field="update_action" if origin == "break_even" else "stop_loss", source_id=source.source_id,
                    quote="Stop auf Einstand" if origin == "break_even" else "95")])
        await trading.update_trade(source.event_id, change)
        assert state.snapshot()["trades"][identity]["pending_stop_update"]["phase"] == "confirmed"
    current = state.snapshot()["trades"][identity]
    assert not current["waverider"]["finished"] and not current["entries_disabled"]
    assert boundary.fills == before and current["plan"] == plan
    trading = type(trading)(exchange, state)
    await trading.recover()
    market = Market.model_validate(plan["market"])
    await execute(trading, identity, current["waverider"], "reduce", initial / 2,
        await exchange.price_snapshot(market), Decimal(".2"))
    boundary.mid = "101"
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", initial / 2,
        await exchange.price_snapshot(market), Decimal(".2"))
    await trading.recover()
    current = state.snapshot()["trades"][identity]
    assert initial / 2 < Decimal(current["remaining_quantity"]) <= initial
    assert any(row.get("entry_role") == "waverider" for row in current["fill_events"].values())
    assert current["plan"] == plan and Decimal(current["reserved_risk"]) <= Decimal(trade["reserved_risk"])
    fills = list(boundary.fills)
    await trading.recover()
    assert boundary.fills == fills
    await trading.cancel_entry(identity)
    assert state.snapshot()["trades"][identity]["waverider"]["finished"]
    await trading.replace_stop(identity, Decimal(current["stop_price"]), "operator:still-finished")
    assert state.snapshot()["trades"][identity]["waverider"]["finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["waiting_fill", "replacing", "unknown"])
async def test_pending_stop_blocks_optional_wave_without_finishing_it(setup, signal, monkeypatch, phase):
    from aqla_hyperliquid_mcp.models import AqlaError
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch)
    identity = trade["trade_id"]
    state.update(lambda data: data["trades"][identity].update(pending_stop_update={"phase":phase}))
    boundary.mid = "104"
    quote = await exchange.price_snapshot(Market.model_validate(plan["market"]))
    before = list(boundary.actions)
    await waverider.tick(trading, identity, quote)
    with pytest.raises(AqlaError, match="pending stop"):
        await execute(trading, identity, trade["waverider"], "reduce", Decimal(trade["remaining_quantity"]) / 2, quote, Decimal(".2"))
    assert boundary.actions == before
    assert not state.snapshot()["trades"][identity]["waverider"]["finished"]


@pytest.mark.asyncio
async def test_unaccepted_edit_still_blocks_wave_reentry(setup, signal, monkeypatch):
    from aqla_hyperliquid_mcp.models import AqlaError
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch)
    identity = trade["trade_id"]
    source = signal[0].model_copy(update={"edited_at":now_ms(), "text":"BTC Stop auf 95"})
    trading.ingest(source)
    boundary.mid = "104"
    market = Market.model_validate(plan["market"])
    await execute(trading, identity, trade["waverider"], "reduce", Decimal(trade["remaining_quantity"]) / 2,
        await exchange.price_snapshot(market), Decimal(".2"))
    before = list(boundary.fills)
    boundary.mid = "101"
    with pytest.raises(AqlaError, match="Source changed"):
        await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", Decimal(trade["remaining_quantity"]) / 2,
            await exchange.price_snapshot(market), Decimal(".2"))
    assert boundary.fills == before


@pytest.mark.asyncio
async def test_lost_stop_reply_recovers_once_and_keeps_wave_active(setup, signal, monkeypatch):
    from test_updates import stop_update
    state, boundary, exchange, trading, _, trade = await wave_trade(setup, signal, monkeypatch)
    source, change = stop_update(trading)
    cloid = exchange.cloid("update:" + source.event_id)
    visible = False
    original = boundary.handle
    def hidden_stop(request):
        payload = json.loads(request.content)
        if request.url.path == "/info" and payload.get("type") == "orderStatus" and payload.get("oid") == cloid and not visible:
            return httpx.Response(200, json={"status":"unknownOid"})
        response = original(request)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order" and any(row.get("c") == cloid for row in payload["action"]["orders"]):
            raise httpx.ReadTimeout("Stop accepted, response lost")
        return response
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(hidden_stop))
    await trading.update_trade(source.event_id, change)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "unknown"
    assert not current["waverider"]["finished"]
    visible = True
    trading = type(trading)(exchange, state)
    await trading.recover()
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert not current["waverider"]["finished"] and not current["entries_disabled"]
    submitted = [row for action in boundary.actions if action["type"] == "order" for row in action["orders"] if row.get("c") == cloid]
    assert len(submitted) == 1


@pytest.mark.asyncio
async def test_price_failure_after_target_cancel_restores_native_targets_without_wave_send(setup, signal, monkeypatch):
    from aqla_agent.observations import TemporaryObservation
    state, boundary, exchange, _, plan, trade = await wave_trade(setup, signal, monkeypatch)
    boundary.mid = "104"
    original = exchange.price_snapshot
    reads = 0
    async def interrupted(market):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise TemporaryObservation("read_unavailable", "price", "transport")
        return await original(market)
    quote = await original(Market.model_validate(plan["market"]))
    monkeypatch.setattr(exchange, "price_snapshot", interrupted)
    with pytest.raises(TemporaryObservation):
        await execute(setup[-1], trade["trade_id"], trade["waverider"], "reduce", Decimal(trade["remaining_quantity"]) / 2, quote, Decimal(".2"))
    current = state.read("trades")["trades"][trade["trade_id"]]
    assert not current["suspended_wave_targets"] and current["protection_repair_pending"] is None
    assert not any(row["kind"] == "waverider" for row in current["fill_events"].values())
    assert not any(key.startswith("waverider:") for key in state.read("operations")["operations"])
    targets = [row for row in current["orders"] if row["kind"] == "tp" and not row.get("retired")]
    assert targets and all(boundary.orders[row["cloid"]]["status"] == "open" for row in targets)
    assert sum(Decimal(row["quantity"]) for row in targets) == Decimal(current["remaining_quantity"])


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", ["signal_deadline", "until_target"])
async def test_lifetime_controls_waves_but_never_revives_finished_setups(setup, signal, monkeypatch, lifetime):
    from aqla_hyperliquid_mcp import models
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch, lifetime)
    identity, original = trade["trade_id"], Decimal(trade["remaining_quantity"])
    monkeypatch.setattr(models.time, "time_ns", lambda: (plan["deadline_at"] + 1000) * 1000000)
    await trading.refresh_trade(identity)
    current = state.snapshot()["trades"][identity]
    assert bool(current["waverider"].get("finished")) == (lifetime == "signal_deadline")
    assert not current.get("entries_disabled") if lifetime == "until_target" else current["entries_disabled"]
    boundary.mid = "104"
    market = Market.model_validate(plan["market"])
    await execute(trading, identity, current["waverider"], "reduce", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    if lifetime == "signal_deadline":
        assert boundary.position == original
        return
    assert boundary.position == original / 2
    boundary.mid = "101"
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    await trading.refresh_trade(identity)
    current = state.snapshot()["trades"][identity]
    assert original / 2 < boundary.position <= original
    assert current["plan"] == plan and Decimal(current["stop_price"]) == 90
    assert pools(current, Decimal(50))["spent"] > 0
    state.update(lambda data: data["trades"][identity]["waverider"].update(finished=True, phase="finished"))
    actions = list(boundary.actions)
    boundary.mid = "104"
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reduce", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    assert actions == boundary.actions


@pytest.mark.asyncio
async def test_until_target_never_admits_a_new_entry_after_its_original_deadline(setup, signal, monkeypatch):
    from aqla_hyperliquid_mcp import models
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(management_mode="waverider", waverider_lifetime="until_target"))
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    monkeypatch.setattr(models.time, "time_ns", lambda: plan["deadline_at"] * 1000000)
    with pytest.raises(models.AqlaError, match="deadline"):
        await trading.execute(plan["plan_id"])
    assert not fake.actions


@pytest.mark.asyncio
async def test_until_target_read_budget_discards_only_a_proved_unsent_wave(setup, signal, monkeypatch):
    from aqla_agent.managed_entries import retain_retry
    from aqla_agent.observations import TemporaryObservation
    from aqla_hyperliquid_mcp import models
    state, boundary, exchange, trading, plan, trade = await wave_trade(setup, signal, monkeypatch, "until_target")
    identity, original = trade["trade_id"], Decimal(trade["remaining_quantity"])
    boundary.mid = "104"
    market = Market.model_validate(plan["market"])
    await execute(trading, identity, trade["waverider"], "reduce", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    boundary.mid = "101"
    snapshot = exchange.account_snapshot
    async def unavailable(*_):
        raise TemporaryObservation("read_unavailable", "inventory", "transport")
    monkeypatch.setattr(exchange, "account_snapshot", unavailable)
    monkeypatch.setattr(models.time, "time_ns", lambda: (plan["deadline_at"] + 1000) * 1000000)
    await execute(trading, identity, state.snapshot()["trades"][identity]["waverider"], "reenter", original / 2,
                  await exchange.price_snapshot(market), Decimal(".2"))
    marker = state.snapshot()["trades"][identity]["managed_entry_retry"]
    assert marker["attempts"] == 1 and not marker["exhausted"]
    for attempt in range(2, 6):
        current = await retain_retry(trading, identity, TemporaryObservation("read_unavailable", "inventory", "transport"))
        assert current["managed_entry_retry"]["attempts"] == attempt
    assert not current.get("entries_disabled") and not current["waverider"].get("finished")
    assert current["waverider"]["phase"] == "waiting_rebound" and current["waverider"]["cycle"] == 1
    assert all(row["phase"] == "terminal" and row.get("not_sent") for row in current["entry_orders"] if row["kind"] == "waverider")
    assert boundary.position == original / 2
    monkeypatch.setattr(exchange, "account_snapshot", snapshot)
    await trading.refresh_trade(identity)
    assert not state.snapshot()["trades"][identity].get("execution_incident")


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_tick_refreshes_book_aged_by_ordinary_protection_before_optional_wave(setup, signal, monkeypatch, available):
    from aqla_agent.observations import TemporaryObservation
    from aqla_agent.waverider import tick
    from aqla_hyperliquid_mcp.models import now_ms
    state, boundary, exchange, trading, raw, trade = await wave_trade(setup, signal, monkeypatch)
    original = exchange.price_snapshot
    market = Market.model_validate(raw["market"])
    quote = await original(market)
    quote = quote.model_copy(update={"exchange_time": now_ms() - 3000})
    reads = 0
    async def fresh(market):
        nonlocal reads
        reads += 1
        if not available:
            raise TemporaryObservation("read_unavailable", "price", "transport")
        return await original(market)
    monkeypatch.setattr(exchange, "price_snapshot", fresh)
    sent = len(boundary.actions)
    await tick(trading, trade["trade_id"], quote)
    observed = trading._waverider_observations[trade["trade_id"]][1]
    assert observed["phase"] == ("waiting_rise" if available else "paused")
    assert reads == 1 and len(boundary.actions) == sent
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["remaining_quantity"]) == boundary.position


@pytest.mark.asyncio
async def test_operator_authorization_uses_guarded_orders_and_revocation_stops_new_waves(setup, signal, monkeypatch):
    from aqla_agent.waverider import VERSION, live_authority, profile_hash, software_hash
    from aqla_hyperliquid_mcp.models import AqlaError, TradePlan
    state, boundary, exchange, trading, raw, trade = await wave_trade(setup, signal, monkeypatch)
    monkeypatch.setattr(waverider, "qualification_valid", lambda *_: False)
    plan = TradePlan.model_validate(raw)
    identity = profile_hash(plan.policy, plan.base_parameters)
    payload = {"kind": "waverider_operator_live_v1", "version": VERSION, "operator": "test-owner",
        "historical_quality_waived": True, "consent_sha256": hashlib.sha256(b"explicit test owner waiver").hexdigest(),
        "issued_at": plan.created_at, "software_hash": software_hash(), "profile_hash": identity,
        "allowed_accounts": [f"{plan.exchange}:{plan.network}:{plan.account.lower()}"],
        "allowed_cohorts": [f"{plan.exchange}:{plan.network}:{plan.market.quote_token}"]}
    key = exchange.settings.integration_key_file.read_bytes().strip()
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = exchange.settings.state_path.parent / f"waverider-operator-live-{identity}.json"
    path.write_text(json.dumps({**payload, "signature": hmac.new(key, body, hashlib.sha256).hexdigest()}))
    assert live_authority(trading, plan) == "operator"
    initial = Decimal(trade["remaining_quantity"])
    boundary.mid = "104"
    quote = await exchange.price_snapshot(plan.market)
    wave = {**state.snapshot()["trades"][plan.trade_id]["waverider"], "shadow": False, "live_authority": "operator"}
    await execute(trading, plan.trade_id, wave, "reduce", initial / 2, quote, Decimal(".2"))
    current = state.snapshot()["trades"][plan.trade_id]
    assert boundary.position == initial / 2 and current["waverider"]["live_authority"] == "operator"
    assert any(row["status"] == "open" and row["order"]["isTrigger"] and row["order"]["reduceOnly"] for row in boundary.orders.values())
    path.unlink()
    before = list(boundary.actions)
    with pytest.raises(AqlaError, match="not authorized"):
        await execute(trading, plan.trade_id, current["waverider"], "reduce", initial / 2, quote, Decimal(".2"))
    assert boundary.actions == before and boundary.position == initial / 2


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
