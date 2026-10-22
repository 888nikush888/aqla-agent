import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from conftest import FakeExchange
from test_adaptive_tps import setup_signal
from test_deferred_entries import delayed_source
from test_updates import update

from aqla_agent.entry import PriceSnapshot, passive_entry_price, validate_entry_prices
from aqla_agent.execution_roles import native_role
from aqla_agent.messages import trade_block
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    Evidence,
    Interpretation,
    Market,
    Policy,
    TradePlan,
    digest,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import make_plan

pytest_plugins = []

ORACLES = Path(__file__).parent / "oracles"
STANDARD = json.loads((ORACLES / "maker_standard_4253019.json").read_text())
MAKER = json.loads((ORACLES / "maker_modes.json").read_text())


@pytest.mark.parametrize("case", STANDARD)
def test_full_standard_plan_matches_release_oracle(case):
    expected = case["plan"]
    plan = make_plan(Interpretation.model_validate(case["signal"]), Market.model_validate(case["market"]),
                     AccountSnapshot.model_validate(case["account"]), Policy.model_validate(case["policy"]),
                     7, 1234567890000 - 1, ("testnet", "account"), reserved_risk=Decimal(5), stamp=1234567890000)
    assert plan.model_dump(mode="json") == expected
    assert TradePlan.model_validate(expected).model_dump(mode="json") == expected
    explicit_defaults = {**expected, "entry_execution": "standard", "take_profit_execution": "standard",
                         "policy": {**expected["policy"], "entry_execution": "standard", "take_profit_execution": "standard"}}
    assert TradePlan.model_validate(explicit_defaults).model_dump(mode="json") == expected


@pytest.mark.parametrize("case", MAKER)
def test_full_maker_plan_decimal_risk_ids_and_json_match_frozen_oracle(case):
    plan = make_plan(Interpretation.model_validate(case["signal"]), Market.model_validate(case["market"]),
                     AccountSnapshot.model_validate(case["account"]), Policy.model_validate(case["policy"]),
                     7, 1234567889999, ("testnet", "account"), reserved_risk=Decimal(5), stamp=1234567890000,
                     maker_price=Decimal(case["maker_price"]))
    assert plan.model_dump(mode="json") == case["plan"]
    assert TradePlan.model_validate(case["plan"]).model_dump(mode="json") == case["plan"]


@pytest.mark.parametrize("value", ["maker", "taker", "Gtc", ""])
def test_execution_modes_are_closed_literals(value):
    with pytest.raises(ValueError):
        Policy(entry_execution=value)


def test_saved_execution_modes_must_match_frozen_policy():
    expected = STANDARD[0]["plan"]
    with pytest.raises(ValueError, match="immutable policy"):
        TradePlan.model_validate({**expected, "entry_execution": "maker_only"})


class MakerBoundary(FakeExchange):
    def __init__(self, signer):
        super().__init__(signer)
        self.bid, self.ask = "99.9", "100.1"
        self.partial = Decimal(0)
        self.reject_maker_entry = False

    def handle(self, request):
        payload = json.loads(request.content)
        if request.url.path == "/info" and payload.get("type") == "l2Book":
            return httpx.Response(200, json={"coin": payload["coin"], "time": now_ms(),
                "levels": [[{"px": self.bid, "sz": "1000000"}], [{"px": self.ask, "sz": "1000000"}]]})
        response = super().handle(request)
        if request.url.path != "/exchange" or payload["action"]["type"] != "order":
            return response
        result = response.json()
        for i, order in enumerate(payload["action"]["orders"]):
            if order["t"].get("limit", {}).get("tif") != "Alo":
                continue
            crosses = Decimal(order["p"]) >= Decimal(self.ask) if order["b"] else Decimal(order["p"]) <= Decimal(self.bid)
            if crosses or not order["r"] and self.reject_maker_entry:
                self.orders[order["c"]]["status"] = "rejected"
                result["response"]["data"]["statuses"][i] = {"error": "Post only order would cross"}
            elif not order["r"] and self.partial:
                self.fill(order["c"], Decimal(order["s"]) * self.partial, order["p"])
                self.fills[-1]["crossed"] = False
        return httpx.Response(200, json=result)


def maker_setup(setup, monkeypatch, *, entry="maker_only", tp="maker_only"):
    _, state, old, exchange, trading = setup
    state.update(lambda data: data["policy"].update(entry_execution=entry, take_profit_execution=tp))
    fake = MakerBoundary(old.signer)
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(fake.handle))
    return state, fake, exchange, trading


async def open_maker(setup, signal, monkeypatch, **modes):
    state, fake, exchange, trading = maker_setup(setup, monkeypatch, **modes)
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    return state, fake, exchange, trading, plan, trade


@pytest.mark.asyncio
async def test_maker_market_reference_sizing_and_native_resting_wire(setup, signal, monkeypatch):
    state, fake, _, trading, plan, trade = await open_maker(setup, signal, monkeypatch)
    assert plan["entry_price"] == plan["entry_limit"] == "99.900"
    assert plan["quantity"] == "5.00"
    assert Decimal(plan["risk_usdc"]) == Decimal("49.999500")
    assert trade["status"] == "opening" and not fake.position
    action = state.snapshot()["operations"][trade["entry_operation_id"]]["wire_action"]
    assert action["orders"][0]["t"] == {"limit": {"tif": "Alo"}}
    assert action["orders"][0]["p"] == "99.9"
    assert "trigger" in action["orders"][1]["t"]
    assert action["orders"][1]["r"] is True
    count = len(fake.actions)
    fake.bid, fake.ask = "100.1", "100.3"
    await trading.recover()
    assert len(fake.actions) == count
    assert state.snapshot()["trades"][trade["trade_id"]]["plan"] == plan


@pytest.mark.asyncio
async def test_native_cross_rejection_and_restart_never_resend(setup, signal, monkeypatch):
    state, fake, exchange, trading = maker_setup(setup, monkeypatch)
    fake.reject_maker_entry = True
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "rejected" and not fake.position
    record = copy.deepcopy(state.snapshot()["operations"][trade["entry_operation_id"]])
    sends = len(fake.actions)
    await Trading(exchange, state).recover()
    await trading.execute(plan["plan_id"])
    assert len(fake.actions) == sends
    assert state.snapshot()["operations"][trade["entry_operation_id"]] == record


@pytest.mark.asyncio
async def test_saved_maker_price_crossed_before_send_fails_guard(setup, signal, monkeypatch):
    _, fake, _, trading = maker_setup(setup, monkeypatch)
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    fake.bid, fake.ask = "99.7", "99.8"
    with pytest.raises(AqlaError, match="cross"):
        await trading.execute(plan["plan_id"])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_maker_partial_fill_cancel_protects_only_owned_size(setup, signal, monkeypatch):
    state, fake, _, trading = maker_setup(setup, monkeypatch)
    fake.partial = Decimal(".5")
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "open"
    assert Decimal(trade["filled_quantity"]) == Decimal(trade["remaining_quantity"]) == Decimal("2.5")
    assert fake.orders[trade["entry_cloid"]]["status"] == "canceled"
    stops = [order for order in trade["orders"] if order["kind"] == "sl"]
    assert any(Decimal(order["quantity"]) == Decimal("2.5") and fake.orders[order["cloid"]]["status"] == "open" for order in stops)
    tp = state.snapshot()["operations"]["targets:" + trade["trade_id"] + ":0"]["wire_action"]["orders"]
    assert all(order["t"] == {"limit": {"tif": "Alo"}} and order["r"] for order in tp)
    assert sum(Decimal(order["s"]) for order in tp) == Decimal("2.5")
    assert set(trade["fill_execution_roles"].values()) == {"maker"}
    await trading.close_position(trade["trade_id"], Decimal(100), "emergency:" + trade["trade_id"])
    close = [action for action in fake.actions if action["type"] == "order"][-1]
    assert close["orders"][0]["t"] == {"limit": {"tif": "Ioc"}}
    assert close["orders"][0]["r"] and not fake.position


@pytest.mark.asyncio
async def test_maker_target_reject_keeps_stop_and_unfilled_budget(setup, signal, monkeypatch):
    state, fake, _, trading, _, trade = await open_maker(setup, signal, monkeypatch)
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], trade["plan"]["entry_price"])
    fake.bid, fake.ask, fake.mid = "111", "111.2", "111.1"
    trade = await trading.refresh_trade(trade["trade_id"])
    assert trade["status"] == "protecting" and "maker_target_rejection" in trade
    assert trade["tp_filled"] == ["0", "0"] and fake.position > 0
    assert any(fake.orders[order["cloid"]]["status"] == "open" for order in trade["orders"] if order["kind"] == "sl")
    targets = state.snapshot()["operations"]["targets:" + trade["trade_id"] + ":0"]
    assert targets["legs"][0]["disposition"] == "rejected"
    assert targets["wire_action"]["orders"][0]["p"] == "110"
    sends = len(fake.actions)
    await trading.recover()
    assert len(fake.actions) == sends
    rejection = copy.deepcopy(state.snapshot()["operations"][targets["operation_id"]])
    await trading.close_position(trade["trade_id"], Decimal(100), "emergency:" + trade["trade_id"])
    closed = await trading.refresh_trade(trade["trade_id"])
    assert closed["status"] == "closed" and closed["remaining_quantity"] == "0"
    rendered = trade_block(state.snapshot(), closed, [{"change": {"status": "closed"}}])
    assert "**TP abgelehnt:**" not in rendered and "vorhandener SL bleibt aktiv" not in rendered
    emergency = state.snapshot()["operations"]["emergency:" + trade["trade_id"]]
    assert emergency["wire_action"]["orders"][0]["t"] == {"limit": {"tif": "Ioc"}}
    assert state.snapshot()["operations"][targets["operation_id"]] == rejection


@pytest.mark.asyncio
@pytest.mark.parametrize("unconfirmed", ["unknown", "foreign_cloid", "foreign_market", "wrong_side", "wrong_size", "missing_size", "not_reduce_only"])
async def test_source_maker_replacement_resolves_only_after_own_confirmation_and_stays_resolved(setup, signal, monkeypatch, unconfirmed):
    state, fake, exchange, trading, _, trade = await open_maker(setup, signal, monkeypatch)
    identity = trade["trade_id"]
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], trade["plan"]["entry_price"])
    fake.bid, fake.ask, fake.mid = "111", "111.2", "111.1"
    rejected = await trading.refresh_trade(identity)
    initial_operation = rejected["initial_targets"]["operation_id"]
    initial = copy.deepcopy(state.snapshot()["operations"][initial_operation])
    financial = {key: copy.deepcopy(rejected.get(key)) for key in
                 ("fill_events", "entry_accounting", "entry_validated_fingerprint", "initial_sl_risk", "lifecycle_events", "fill_execution_roles")}

    async def source_targets(message_id, prices):
        source, interpretation = update(trading, message_id, "replace_targets", "Neue Ziele " + " und ".join(prices), take_profits=prices)
        interpretation.evidence = [Evidence(field="take_profits." + str(index), source_id=source.source_id, quote=price)
                                   for index, price in enumerate(prices)]
        result = await trading.update_trade(source.event_id, interpretation)
        assert state.snapshot()["bindings"]["update:" + source.event_id]["trade_id"] == identity
        return source, result

    # A source-authorized replacement may itself cross and reject. Both rejected
    # generations remain immutable history after a second authorized replacement.
    fake.bid, fake.ask, fake.mid = "116", "116.2", "116.1"
    first_source, first_result = await source_targets(2, ["115", "125"])
    assert first_result["result"]["legs"][0]["disposition"] == "rejected"
    first_operation = "update:" + first_source.event_id
    first = copy.deepcopy(state.snapshot()["operations"][first_operation])
    await trading.refresh_trade(identity)
    incident = copy.deepcopy(state.snapshot()["trades"][identity]["maker_target_rejection"])
    assert incident["operation_id"] == first_operation
    second_source, second_result = await source_targets(3, ["120", "130"])
    assert second_result["result"]["phase"] == "acknowledged"
    current = state.snapshot()["trades"][identity]
    assert current["maker_target_rejection"] == incident  # ACK alone is insufficient.
    replacement_cloids = {order["cloid"] for order in current["orders"] if order["kind"] == "tp" and not order.get("retired")}
    assert replacement_cloids.isdisjoint(incident["target_cloids"])
    original_handle = fake.handle
    visible = False

    def delayed_confirmation(request):
        payload = json.loads(request.content)
        response = original_handle(request)
        if visible or request.url.path != "/info" or payload.get("type") != "orderStatus" or payload.get("oid") not in replacement_cloids:
            return response
        if unconfirmed == "unknown":
            return httpx.Response(200, json={"status": "unknownOid"})
        row = response.json()
        field, value = {"foreign_cloid": ("cloid", "another-order"), "foreign_market": ("coin", "ETH"),
                        "wrong_side": ("side", "B"), "wrong_size": ("origSz", "999"), "missing_size": ("origSz", None),
                        "not_reduce_only": ("reduceOnly", False)}[unconfirmed]
        row["order"]["order"][field] = value
        return httpx.Response(200, json=row)

    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(delayed_confirmation))
    await trading.refresh_trade(identity)
    assert state.snapshot()["trades"][identity]["maker_target_rejection"] == incident
    assert state.snapshot()["trades"][identity]["status"] == "protecting"
    visible = True
    resolved = await trading.refresh_trade(identity)
    assert resolved["status"] == "open" and not resolved.get("maker_target_rejection")
    assert "**TP abgelehnt:**" not in trade_block(state.snapshot(), resolved, [{"change": {"status": "open"}}])
    stable = state.snapshot()
    sends = len(fake.actions)
    incident_writes = []
    original_update = state.update

    def track_incident_writes(mutator, *args, **kwargs):
        before = state.snapshot()["trades"][identity].get("maker_target_rejection")
        result = original_update(mutator, *args, **kwargs)
        after = state.snapshot()["trades"][identity].get("maker_target_rejection")
        if before != after:
            incident_writes.append((before, after))
        return result

    monkeypatch.setattr(state, "update", track_incident_writes)
    for _ in range(2):
        await Trading(exchange, state).recover()
        after = state.snapshot()
        assert after["trades"][identity]["maker_target_rejection"] is None
        assert after["notifications"] == stable["notifications"]
        assert after["operations"][initial_operation] == initial
        assert after["operations"][first_operation] == first
        assert {key: after["trades"][identity].get(key) for key in financial} == financial
        assert after["trades"][identity]["target_updates"] == stable["trades"][identity]["target_updates"]
        assert len(fake.actions) == sends
    assert incident_writes == []
    second_operation = state.snapshot()["operations"]["update:" + second_source.event_id]
    assert all(order["r"] and order["t"] == {"limit": {"tif": "Alo"}} for order in second_operation["wire_action"]["orders"])
    rejection_notices = [row["change"]["maker_target_rejection"] for row in stable["notifications"].values()
                         if row.get("change", {}).get("maker_target_rejection")]
    assert {row["operation_id"] for row in rejection_notices} == {initial_operation, first_operation}


@pytest.mark.asyncio
async def test_later_fill_role_enrichment_preserves_all_financial_proofs(setup, signal, monkeypatch):
    state, fake, _, trading, _, trade = await open_maker(setup, signal, monkeypatch)
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], trade["plan"]["entry_price"])
    trade = await trading.refresh_trade(trade["trade_id"])
    identity = next(iter(trade["fill_events"]))
    before = copy.deepcopy(trade)
    financial_keys = ("fill_events", "entry_accounting", "entry_validated_fingerprint", "initial_sl_risk", "lifecycle_events")
    state.update(lambda data: data["trades"][trade["trade_id"]].update(funding_coverage={"fill_fingerprint": digest(trade["fill_events"])}))
    fake.fills[0]["crossed"] = False
    await trading.capture_fills(trade["trade_id"])
    after = state.snapshot()["trades"][trade["trade_id"]]
    assert after["fill_execution_roles"][identity] == "maker"
    assert {key: before.get(key) for key in financial_keys} == {key: after.get(key) for key in financial_keys}
    assert after["funding_coverage"]["fill_fingerprint"] == digest(after["fill_events"])
    del fake.fills[0]["crossed"]
    await trading.capture_fills(trade["trade_id"])
    assert state.snapshot()["trades"][trade["trade_id"]]["fill_execution_roles"][identity] == "maker"
    fake.fills[0]["crossed"] = True
    await trading.capture_fills(trade["trade_id"])
    after = state.snapshot()["trades"][trade["trade_id"]]
    assert after["fill_execution_roles"][identity] == "unknown"
    assert after["fill_execution_role_conflicts"][identity] == ["maker", "taker"]
    fake.fills[0]["crossed"] = False
    await trading.capture_fills(trade["trade_id"])
    assert state.snapshot()["trades"][trade["trade_id"]]["fill_execution_roles"][identity] == "unknown"
    assert after["fill_events"] == before["fill_events"]


@pytest.mark.asyncio
async def test_maker_resting_deadline_cancels_exact_order(setup, signal, monkeypatch):
    state, fake, _, trading, plan, trade = await open_maker(setup, signal, monkeypatch)
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: plan["deadline_at"] + 1)
    trade = await trading.refresh_trade(trade["trade_id"])
    assert trade["status"] == "rejected" and trade["reserved_risk"] == "0"
    assert fake.orders[trade["entry_cloid"]]["status"] == "canceled"
    cancel = state.snapshot()["operations"]["cancel-entry:" + trade["trade_id"]]["wire_action"]
    assert cancel == {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": trade["entry_cloid"]}]}


@pytest.mark.asyncio
async def test_maker_cancel_without_terminal_proof_preserves_stop_and_send_identity(setup, signal, monkeypatch):
    state, fake, exchange, trading, _, trade = await open_maker(setup, signal, monkeypatch)
    original = fake.handle
    def ignore_cancel(request):
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"]["type"] == "cancelByCloid":
            fake.actions.append(payload["action"])
            return httpx.Response(200, json={"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}})
        return original(request)
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(ignore_cancel))
    fake.fill(trade["entry_cloid"], Decimal(trade["plan"]["quantity"]) / 2, trade["plan"]["entry_price"])
    with pytest.raises(AqlaError, match="termination is not proved"):
        await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["filled_quantity"]) == Decimal("2.5")
    assert all(fake.orders[order["cloid"]]["status"] == "open" for order in current["orders"] if order["kind"] == "sl")
    assert not any(order["kind"] == "tp" for order in current["orders"])
    record = copy.deepcopy(state.snapshot()["operations"]["cancel-entry:" + trade["trade_id"]])
    sends = len(fake.actions)
    await Trading(exchange, state).recover()
    assert len(fake.actions) == sends
    assert state.snapshot()["operations"]["cancel-entry:" + trade["trade_id"]] == record


@pytest.mark.asyncio
async def test_maker_lost_ack_restart_preserves_nonce_request_and_single_send(setup, signal, monkeypatch):
    state, fake, exchange, trading = maker_setup(setup, monkeypatch)
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    fake.lose_entry_response = True
    trade = await trading.execute(plan["plan_id"])
    before = state.snapshot()["operations"][trade["entry_operation_id"]]
    assert before["phase"] == "unknown"
    sends = len(fake.actions)
    await Trading(exchange, state).recover()
    after = state.snapshot()["operations"][trade["entry_operation_id"]]
    assert len(fake.actions) == sends and after["phase"] == "reconciled"
    for key in ("nonce", "request", "wire_action", "request_hash", "expires_after", "wire_vault_address"):
        assert after[key] == before[key]
    assert after["wire_action"]["orders"][0]["t"] == {"limit": {"tif": "Alo"}}


@pytest.mark.asyncio
async def test_maker_crossing_race_after_signing_is_not_sent(setup, signal, monkeypatch):
    state, fake, exchange, trading = maker_setup(setup, monkeypatch)
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original, reads = exchange.price_snapshot, []
    async def crossing_after_signing(market):
        reads.append(market)
        if "entry:" + plan["trade_id"] in state.snapshot()["operations"]:
            fake.bid, fake.ask = "99.7", "99.8"
        return await original(market)
    monkeypatch.setattr(exchange, "price_snapshot", crossing_after_signing)
    trade = await trading.execute(plan["plan_id"])
    record = state.snapshot()["operations"][trade["entry_operation_id"]]
    assert record["outcome"] == "not_sent" and record["phase"] == "rejected"
    assert record["wire_action"]["orders"][0]["t"] == {"limit": {"tif": "Alo"}}
    assert type(record["nonce"]) is int
    assert not any(action["type"] == "order" for action in fake.actions)


@pytest.mark.asyncio
async def test_active_maker_modes_are_frozen_after_profile_change(setup, signal, monkeypatch):
    state, fake, _, trading, plan, trade = await open_maker(setup, signal, monkeypatch)
    state.update(lambda data: data["policy"].update(entry_execution="standard", take_profit_execution="standard"))
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], trade["plan"]["entry_price"])
    trade = await trading.refresh_trade(trade["trade_id"])
    assert trade["plan"] == plan
    assert all(order["t"] == {"limit": {"tif": "Alo"}} for order in state.snapshot()["operations"]["targets:" + trade["trade_id"] + ":0"]["wire_action"]["orders"])


@pytest.mark.asyncio
async def test_missing_venue_maker_capability_rejects_before_send(setup, signal, monkeypatch):
    _, fake, exchange, trading = maker_setup(setup, monkeypatch)
    monkeypatch.setattr(exchange, "supports_maker_orders", False)
    with pytest.raises(AqlaError, match="does not support"):
        await trading.preview(signal[0].event_id, signal[1])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_deferred_source_maker_gate_preserves_source_price_and_alo(setup, monkeypatch):
    state, fake, _, trading = maker_setup(setup, monkeypatch)
    fake.mid, fake.bid, fake.ask = "105", "104.9", "105.1"
    source, interpretation = delayed_source(trading, side="long", stop="90", targets=("110", "120"))
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "waiting_entry" and not fake.actions
    fake.mid, fake.bid, fake.ask = "100", "99.9", "100.1"
    await trading.recover()
    trade = state.snapshot()["trades"][trade["trade_id"]]
    assert trade["status"] == "opening"
    action = state.snapshot()["operations"][trade["entry_operation_id"]]["wire_action"]
    assert action["orders"][0]["p"] == "100" and action["orders"][0]["t"] == {"limit": {"tif": "Alo"}}
    assert trade["plan"]["entry_limit"] == plan["entry_limit"]


@pytest.mark.asyncio
async def test_maker_adaptive_ladder_uses_own_full_tp_fills_and_trigger_stop(setup, monkeypatch):
    state, fake, _, trading = maker_setup(setup, monkeypatch)
    state.update(lambda data: data["policy"].update(take_profit_allocation="adaptive", stop_management="tp_ladder"))
    source, interpretation = setup_signal(trading, count=3)
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    fake.fill(trade["entry_cloid"], plan["quantity"], plan["entry_price"])
    trade = await trading.refresh_trade(trade["trade_id"])
    target = next(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.fill(target["cloid"], Decimal(target["quantity"]) / 2, target["price"])
    trade = await trading.refresh_trade(trade["trade_id"])
    assert Decimal(trade["stop_price"]) == Decimal("90")
    fake.fill(target["cloid"], Decimal(target["quantity"]) / 2, target["price"])
    trade = await trading.refresh_trade(trade["trade_id"])
    assert Decimal(trade["stop_price"]) == Decimal(plan["entry_price"])
    orders = [order for action in fake.actions if action["type"] == "order" for order in action["orders"]]
    assert all("trigger" in order["t"] for order in orders if order["t"].get("trigger", {}).get("tpsl") == "sl")
    assert all(order["t"]["limit"]["tif"] == "Alo" for order in orders if order["r"] and "limit" in order["t"])


def quote(bid="99.93", ask="100.07", **values):
    stamp = now_ms()
    return PriceSnapshot(market="BTC", bid=bid, ask=ask, mid=(Decimal(bid) + Decimal(ask)) / 2,
                         stop_reference="100", stop_reference_kind="mark", exchange_time=stamp,
                         started_at=stamp, observed_at=stamp, **values)


@pytest.mark.parametrize("side,expected", [("long", "99.9"), ("short", "100.1")])
def test_passive_side_and_tick_rounding(side, expected):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", price_tick=".1", max_leverage=20)
    assert passive_entry_price(market, side, quote()) == Decimal(expected)


def test_passive_quote_crossed_stale_and_market_identity_fail_closed():
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", price_tick=".1", max_leverage=20)
    with pytest.raises(AqlaError, match="cross"):
        passive_entry_price(market, "long", quote(bid="100", ask="100"))
    with pytest.raises(AqlaError, match="stale"):
        passive_entry_price(market, "long", quote().model_copy(update={"observed_at": now_ms() - 2001}))
    with pytest.raises(AqlaError, match="another market"):
        passive_entry_price(market, "long", quote().model_copy(update={"market": "ETH"}))


@pytest.mark.parametrize("entry,prices", [("limit", ["99.8"]), ("range", ["99.7", "99.9"])])
def test_maker_source_limit_and_range_remain_binding(signal, entry, prices):
    signal = signal[1].model_copy(update={"entry": entry, "entry_prices": [Decimal(value) for value in prices]})
    stamp = now_ms()
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", price_tick=".1", max_leverage=20)
    plan = make_plan(signal, market, AccountSnapshot(equity="10000", available_margin="10000"),
                     Policy(entry_execution="maker_only"), 1, stamp, ("testnet", "account"), stamp=stamp)
    assert plan.entry_price == plan.entry_limit == Decimal("99.8")
    assert validate_entry_prices(plan, quote())["marketable"] is False
    with pytest.raises(AqlaError, match="cross"):
        validate_entry_prices(plan, quote(bid="99.6", ask="99.7"))


@pytest.mark.parametrize("price", ["99.6", "100.4"])
def test_maker_market_price_keeps_existing_slippage_bounds(signal, price):
    stamp = now_ms()
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", price_tick=".1", max_leverage=20)
    with pytest.raises(AqlaError, match="slippage tolerance"):
        make_plan(signal[1], market, AccountSnapshot(equity="10000", available_margin="10000"),
                  Policy(entry_execution="maker_only"), 1, stamp, ("testnet", "account"), stamp=stamp, maker_price=Decimal(price))


def test_maker_range_without_valid_tick_is_rejected(signal):
    stamp = now_ms()
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", price_tick=".1", max_leverage=20)
    signal = signal[1].model_copy(update={"entry": "range", "entry_prices": [Decimal("99.91"), Decimal("99.99")]})
    with pytest.raises(AqlaError, match="source entry range"):
        make_plan(signal, market, AccountSnapshot(equity="10000", available_margin="10000"),
                  Policy(entry_execution="maker_only"), 1, stamp, ("testnet", "account"), stamp=stamp)


@pytest.mark.parametrize("crossed,role", [(True, "taker"), (False, "maker"), (None, "unknown"), (0, "unknown")])
def test_hl_role_comes_only_from_strict_native_boolean(crossed, role):
    assert native_role("hyperliquid", {"crossed": crossed, "fee": "0", "execution_role": "maker"}) == role


def test_role_enrichment_and_conflict_preserve_complete_financial_report_fingerprint():
    from test_monthly import data, trade

    from aqla_agent.execution_roles import merge_role
    from aqla_agent.monthly import monthly_report

    row = trade()
    before = monthly_report(data(row), "2026-10")
    assert before["rows"][0]["complete"] and before["rows"][0]["net_pnl"] == "15.1"
    facts, entry, coverage = copy.deepcopy(row["fill_events"]), copy.deepcopy(row["entry_accounting"]), copy.deepcopy(row["funding_coverage"])
    row["fill_execution_roles"], row["fill_execution_role_conflicts"] = {}, {}
    for identity in row["fill_events"]:
        merge_role(row["fill_execution_roles"], row["fill_execution_role_conflicts"], identity, "maker")
    assert monthly_report(data(row), "2026-10") == before
    identity = next(iter(row["fill_events"]))
    merge_role(row["fill_execution_roles"], row["fill_execution_role_conflicts"], identity, "taker")
    assert monthly_report(data(row), "2026-10") == before
    assert row["fill_events"] == facts and row["entry_accounting"] == entry and row["funding_coverage"] == coverage












