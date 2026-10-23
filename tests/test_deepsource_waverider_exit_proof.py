"""Exact native exit quantities must precede Waverider target restoration."""
import copy
import json
import sqlite3
from decimal import Decimal
from types import MethodType, SimpleNamespace

import httpx
import pytest

from aqla_agent import waverider
from aqla_agent.observations import TemporaryObservation
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import ExternalInterference, Market


def native_wave_status(cloid, status, remaining, reentering=False):
    return {"status": "order", "order": {"status": status, "order": {
        "oid": 7, "cloid": cloid, "coin": "BTC", "origSz": "1", "sz": remaining,
        "limitPx": "103", "side": "B" if reentering else "A", "reduceOnly": not reentering, "isTrigger": False}}}


def exit_proof_engine(status, remaining, *, rejected=False, reentering=False):
    identity = "waverider:trade:0:" + ("reenter" if reentering else "reduce")
    cloid = "owned-wave-cloid"
    original = {"oid": 1, "kind": "entry", "sz": "4", "px": "100", "time": 1}
    payload = {
        "trades": {"trade": {
            "suspended_wave_targets": identity,
            "waverider": {"phase": "pending_reduce", "cycle": 0, "operation_id": identity, "action": "reduce"},
            "fill_events": {"entry": original},
            "plan": {"side": "long", "market": {"asset_id": 0, "symbol": "BTC"}},
            "orders": [{"kind": "sl", "cloid": "original-stop"}],
            "exit_orders": [{"kind": "waverider", "operation_id": identity, "cloid": cloid, "quantity": "1", "price": "103"}],
        }},
        "operations": {identity: {"operation_id": identity, "phase": "rejected" if rejected else "unknown",
            "outcome": "rejected" if rejected else "unknown", "result": {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Native order price rejected"}]}}} if rejected else None,
            "wire_action": {"type": "order", "orders": [{"a": 0, "b": reentering, "r": not reentering, "c": cloid, "s": "1", "p": "103"}]}}},
    }
    if reentering:
        row = {"kind": "waverider", "index": 0, "quantity": "1", "limit": "103", "price": "103",
               "phase": "dispatching", "operation_id": identity, "cloid": cloid, "cycle": 0}
        payload["trades"]["trade"].update(entry_orders=[row], exit_orders=[], suspended_wave_targets=None,
            waverider={"phase": "pending_reenter", "cycle": 0, "operation_id": identity})
    calls = []
    available = []

    class State:
        @staticmethod
        def read(*_args, **_kwargs):
            return copy.deepcopy(payload)

        @staticmethod
        def update(callback):
            return callback(payload)

    async def order_status(selected):
        calls.append(("status", selected))
        if rejected:
            return {"status": "unknownOid"}
        return native_wave_status(cloid, status, remaining, reentering)

    async def capture_fills(selected):
        calls.append(("fills", selected))
        for index, size in enumerate(available):
            payload["trades"][selected]["fill_events"][f"exit-{index}"] = {
                "oid": 7, "tid": index + 10, "kind": "entry" if reentering else "waverider",
                **({"entry_role": "waverider"} if reentering else {}), "sz": size, "px": "104", "time": index + 2}

    engine = SimpleNamespace(
        state=State(), exchange=SimpleNamespace(cloid=lambda selected: cloid), order_status=order_status,
        capture_fills=capture_fills, patch_trade=lambda selected, **values: payload["trades"][selected].update(values))
    return engine, payload, calls, available, identity, cloid


@pytest.mark.parametrize("status,remaining,initial,complete", [
    ("filled", "0", [], ["1"]),
    ("canceled", ".6", [".2"], [".2", ".2"]),
])
@pytest.mark.asyncio
async def test_exit_waits_for_full_native_quantity_before_restoring_targets(status, remaining, initial, complete):
    engine, payload, calls, available, identity, cloid = exit_proof_engine(status, remaining)
    available.extend(initial)
    stops = copy.deepcopy(payload["trades"]["trade"]["orders"])
    operation = copy.deepcopy(payload["operations"][identity])

    assert await waverider.resume_exit(engine, "trade") is False
    row = payload["trades"]["trade"]
    assert row["suspended_wave_targets"] == identity and row["waverider"]["phase"] == "pending_reduce"
    assert row["orders"] == stops and payload["operations"][identity] == operation

    available[:] = complete
    assert await waverider.resume_exit(engine, "trade") is True
    assert row["suspended_wave_targets"] is None and row["waverider"]["phase"] == "waiting_rebound"
    assert Decimal(row["waverider"]["sold_quantity"]) == 1 - Decimal(remaining)
    assert row["waverider"]["sale_price"] == "104" and row["orders"] == stops
    assert await waverider.resume_exit(engine, "trade") is True
    assert calls == [("status", cloid), ("fills", "trade"), ("status", cloid), ("fills", "trade")]


@pytest.mark.asyncio
async def test_definitively_rejected_exit_releases_targets_without_a_resend():
    engine, payload, calls, _available, identity, _cloid = exit_proof_engine("rejected", "1", rejected=True)
    receipt = copy.deepcopy(payload["operations"][identity])

    assert await waverider.resume_exit(engine, "trade") is True
    row = payload["trades"]["trade"]
    assert row["suspended_wave_targets"] is None and row["waverider"]["phase"] == "waiting_rise"
    assert row["exit_orders"][0]["retired"] and not row["exit_orders"][0].get("not_sent")
    assert payload["operations"][identity] == receipt
    assert calls == [("status", "owned-wave-cloid"), ("fills", "trade")]


@pytest.mark.parametrize("mutation", ["unknown_send", "wrong_identity", "two_orders", "wrong_asset", "missing_receipt", "known_native_order", "global_error", "global_nonce_error"])
@pytest.mark.asyncio
async def test_unknown_native_exit_needs_a_matching_definitive_rejection(mutation):
    engine, payload, _calls, _available, identity, cloid = exit_proof_engine("rejected", "1", rejected=True)
    operation = payload["operations"][identity]
    if mutation == "unknown_send":
        operation.update(phase="unknown", outcome="unknown")
    elif mutation == "wrong_identity":
        operation["operation_id"] = "different-request"
    elif mutation == "two_orders":
        operation["wire_action"]["orders"] *= 2
    elif mutation == "wrong_asset":
        operation["wire_action"]["orders"][0]["a"] = 99
    elif mutation == "missing_receipt":
        operation["result"] = None
    elif mutation.startswith("global_"):
        operation["result"] = {"status": "err", "response": "Invalid nonce: repeated" if mutation == "global_nonce_error" else "Native request rejected"}
    else:
        payload["trades"]["trade"]["order_observations"] = {cloid: {"oid": 7}}

    assert await waverider.resume_exit(engine, "trade") is False
    assert payload["trades"]["trade"]["suspended_wave_targets"] == identity
    assert payload["trades"]["trade"]["waverider"]["phase"] == "pending_reduce"


@pytest.mark.asyncio
async def test_exit_cancel_race_keeps_targets_suspended_until_all_fills_arrive(monkeypatch):
    monkeypatch.setattr(waverider.TradePlan, "model_validate", lambda raw: SimpleNamespace(market=SimpleNamespace(asset_id=raw["market"]["asset_id"])))
    engine, payload, calls, available, identity, cloid = exit_proof_engine("canceled", ".4")
    terminal_status = engine.order_status
    canceled = []

    async def order_status(selected):
        if canceled:
            return await terminal_status(selected)
        calls.append(("status", selected))
        return native_wave_status(selected, "open", "1")

    async def cancel(operation, action):
        canceled.append((operation, action))
        return {"phase": "acknowledged"}

    engine.order_status, engine.exchange.mutate = order_status, cancel
    available.append(".2")
    assert await waverider.resume_exit(engine, "trade") is False
    assert payload["trades"]["trade"]["suspended_wave_targets"] == identity
    available.append(".4")
    assert await waverider.resume_exit(engine, "trade") is True
    assert payload["trades"]["trade"]["waverider"]["sold_quantity"] == "0.6"
    assert canceled == [("cancel-wave-exit:" + identity, {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": cloid}]})]
    assert payload["trades"]["trade"]["orders"] == [{"kind": "sl", "cloid": "original-stop"}]


@pytest.mark.asyncio
async def test_positive_native_fills_take_precedence_over_a_rejected_request_receipt():
    engine, payload, _calls, available, _identity, cloid = exit_proof_engine("filled", "0", rejected=True)

    async def filled(selected):
        assert selected == cloid
        return native_wave_status(selected, "filled", "0")

    engine.order_status = filled
    available.append("1")
    assert await waverider.resume_exit(engine, "trade") is True
    row = payload["trades"]["trade"]
    assert row["waverider"]["phase"] == "waiting_rebound" and row["waverider"]["sold_quantity"] == "1"
    assert not row["exit_orders"][0].get("not_sent")


@pytest.mark.parametrize("status", ["canceled", "marginCanceled", "reduceOnlyCanceled", "iocCancelRejected"])
def test_terminal_original_entries_release_only_their_unused_risk_reservation(status):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, max_leverage=10, mid=100)
    plan = SimpleNamespace(side="long", market=market, risk_usdc=Decimal(100), quantity=Decimal(10),
        policy=SimpleNamespace(cost_reserve_bps=Decimal(0), max_notional_usdc=Decimal(10000)))
    trade = {"stop_price": "90", "entry_orders": [{"quantity": "10", "limit": "100", "phase": "terminal",
        "cloid": "entry", "confirmed_filled": "8"}], "order_observations": {"entry": {"status": status}}}
    context = SimpleNamespace(trade=trade, plan=plan, own=Decimal(4), average=Decimal(100), inventory={
        "spent": Decimal(0), "core": Decimal(4), "core_basis": Decimal(100), "flex": Decimal(0), "flex_basis": Decimal(0)})

    quantity = waverider.reentry_size(context, SimpleNamespace(ask=Decimal(100)), Decimal(100), Decimal(4))

    assert quantity == 4
    assert context.own + quantity <= plan.quantity
    assert (context.own + quantity) * 10 <= plan.risk_usdc


async def run_cycle_recovery(engine, payload, reentering):
    if reentering:
        return await waverider.resume_cycle(engine, "trade", dict(payload["trades"]["trade"]["waverider"]))
    return await waverider.resume_exit(engine, "trade")


@pytest.mark.parametrize("reentering", [False, True])
@pytest.mark.parametrize("status,remaining,initial,complete", [
    ("filled", "0", [], ["1"]), ("canceled", ".6", [".2"], [".2", ".2"]),
])
@pytest.mark.asyncio
async def test_both_cycle_callers_require_complete_matching_native_fills(reentering, status, remaining, initial, complete):
    engine, payload, _calls, available, _identity, _cloid = exit_proof_engine(status, remaining, reentering=reentering)
    before = copy.deepcopy(payload["trades"]["trade"]["waverider"])
    available.extend(initial)
    await run_cycle_recovery(engine, payload, reentering)
    assert payload["trades"]["trade"]["waverider"] == before
    available[:] = complete
    await run_cycle_recovery(engine, payload, reentering)
    saved = payload["trades"]["trade"]["waverider"]
    assert saved["phase"] == ("waiting_rise" if reentering else "waiting_rebound")
    assert saved.get("cycle", 0) == int(reentering)


def corrupt_native_proof(mutation, native, payload, identity, cloid, reentering):
    changes = {"cloid": "previous-wave-cloid", "coin": "ETH", "side": "A" if reentering else "B",
        "origSz": "2", "limitPx": "102", "isTrigger": True, "reduceOnly": reentering}
    if mutation in changes:
        native[mutation] = changes[mutation]
    elif mutation == "asset":
        payload["operations"][identity]["wire_action"]["orders"][0]["a"] = 99
    elif mutation == "known_oid":
        payload["trades"]["trade"]["order_observations"] = {cloid: {"oid": 8, "status": "open"}}
    else:
        payload["trades"]["trade"]["order_observations"] = {"previous-wave-cloid": {"oid": 7, "status": "filled"}}


@pytest.mark.parametrize("reentering", [False, True])
@pytest.mark.parametrize("mutation", ["cloid", "coin", "side", "origSz", "limitPx", "isTrigger", "reduceOnly", "asset", "known_oid", "previous_oid"])
@pytest.mark.asyncio
async def test_both_cycle_callers_preserve_state_on_a_different_native_order(reentering, mutation):
    engine, payload, calls, available, identity, cloid = exit_proof_engine("filled", "0", reentering=reentering)
    observed = native_wave_status(cloid, "filled", "0", reentering)
    corrupt_native_proof(mutation, observed["order"]["order"], payload, identity, cloid, reentering)
    before = copy.deepcopy(payload["trades"]["trade"])
    available.append("1")

    async def native_status(selected):
        calls.append(("status", selected))
        return observed

    engine.order_status = native_status
    await run_cycle_recovery(engine, payload, reentering)
    assert payload["trades"]["trade"] == before
    assert calls == [("status", cloid)]


@pytest.mark.parametrize("reentering", [False, True])
@pytest.mark.parametrize("field", ["cloid", "coin", "side", "oid", "origSz", "sz", "limitPx", "isTrigger", "reduceOnly"])
@pytest.mark.asyncio
async def test_both_cycle_callers_fail_closed_on_missing_native_fields(reentering, field):
    engine, payload, _calls, available, _identity, cloid = exit_proof_engine("filled", "0", reentering=reentering)
    observed = native_wave_status(cloid, "filled", "0", reentering)
    observed["order"]["order"].pop(field)
    before = copy.deepcopy(payload["trades"]["trade"])
    available.append("1")

    async def native_status(_selected):
        return observed

    engine.order_status = native_status
    await run_cycle_recovery(engine, payload, reentering)
    assert payload["trades"]["trade"] == before


@pytest.mark.parametrize("evidence", ["known_open_oid", "retained_partial_fill", "native_ack_oid"])
@pytest.mark.asyncio
async def test_rejection_cannot_erase_positive_evidence_when_a_later_read_is_unknown(evidence):
    engine, payload, _calls, _available, identity, cloid = exit_proof_engine("rejected", "1", rejected=True)
    row = payload["trades"]["trade"]
    if evidence == "known_open_oid":
        row["order_observations"] = {cloid: {"oid": 7, "status": "open"}}
    elif evidence == "retained_partial_fill":
        row["fill_events"]["partial"] = {"oid": 7, "kind": "waverider", "sz": ".4", "px": "104", "time": 2}
    else:
        payload["operations"][identity]["legs"] = [{"index": 0, "status": {"resting": {"oid": 7}}}]
    before = copy.deepcopy(row)
    captures = []

    async def unknown_capture(selected):
        captures.append(selected)
        Trading.observe_orders(engine, selected, {cloid: {"status": "unknownOid"}})

    engine.capture_fills = unknown_capture
    assert await waverider.resume_exit(engine, "trade") is False
    assert row == before and captures == []


@pytest.mark.asyncio
async def test_rejection_preserves_positive_fill_evidence_discovered_during_capture():
    engine, payload, _calls, _available, identity, cloid = exit_proof_engine("rejected", "1", rejected=True)

    async def discovered_fill(_selected):
        row = payload["trades"]["trade"]
        row["fill_events"]["partial"] = {"oid": 7, "kind": "waverider", "sz": ".4", "px": "104", "time": 2}
        row["order_observations"] = {cloid: {"status": "unknownOid", "oid": None}}

    engine.capture_fills = discovered_fill
    assert await waverider.resume_exit(engine, "trade") is False
    row = payload["trades"]["trade"]
    assert row["suspended_wave_targets"] == identity and row["waverider"]["phase"] == "pending_reduce"
    assert row["fill_events"]["partial"]["sz"] == ".4"


def tick_failure(monkeypatch, engine, payload, stage, error):
    stamp = 1800000000000
    row = payload["trades"]["trade"]
    row["waverider"] = {"phase": "pending_reenter" if stage == "resume_cycle" else "waiting_retrace", "cycle": 0,
        "checkpoint": stamp, "last_captured_at": stamp}
    expected = {"phase": "waiting_rise" if stage == "resume_cycle" else "pending_reduce", "cycle": 1,
        "checkpoint": stamp, "last_captured_at": stamp, "operation_id": "durable-next-cycle"}
    quote = SimpleNamespace(observed_at=stamp)
    facts = {"fee_rate": ".0004", "atr": "1"}

    async def execute_then_fail(*_args):
        row["waverider"] = copy.deepcopy(expected)
        raise error

    async def completed_resume(*_args):
        row["waverider"] = copy.deepcopy(expected)
        return copy.deepcopy(expected)

    async def fresh(*_args):
        if stage in ("resume_cycle", "before_transition"):
            raise error
        return quote, facts

    monkeypatch.setattr(waverider, "now_ms", lambda: stamp)
    monkeypatch.setattr(waverider.TradePlan, "model_validate", lambda _raw: SimpleNamespace(side="long", market=object()))
    monkeypatch.setattr(waverider, "ended", lambda *_args: False)
    monkeypatch.setattr(waverider, "live_authority", lambda *_args: "operator")
    monkeypatch.setattr(waverider, "fresh_cycle_quote", fresh)
    monkeypatch.setattr(waverider, "cycle_inventory", lambda *_args: (100, 1, 0, 0))
    monkeypatch.setattr(waverider, "cycle_observation", lambda *_args, **_kwargs: (104, .2, .01))
    monkeypatch.setattr(waverider, "advance", lambda state, **_kwargs: ({**state, "phase": "pending_reduce"}, "reduce"))
    monkeypatch.setattr(waverider, "execute", execute_then_fail)
    monkeypatch.setattr(waverider, "resume_cycle", completed_resume)
    return quote, expected


@pytest.mark.parametrize("stage", ["execute", "resume_cycle"])
@pytest.mark.parametrize("error", [TemporaryObservation("read_unavailable", "price", "transport"), KeyError("synthetic"), OSError("synthetic"), sqlite3.Error("synthetic")])
@pytest.mark.asyncio
async def test_tick_failure_preserves_an_already_durable_cycle_transition(monkeypatch, stage, error):
    engine, payload, _calls, _available, _identity, _cloid = exit_proof_engine("filled", "0")
    quote, expected = tick_failure(monkeypatch, engine, payload, stage, error)
    await waverider.tick(engine, "trade", quote)
    assert payload["trades"]["trade"]["waverider"] == expected
    assert vars(engine)["_waverider_observations"]["trade"][1] == expected


@pytest.mark.asyncio
async def test_tick_failure_before_a_transition_still_pauses_the_original_cycle(monkeypatch):
    engine, payload, _calls, _available, _identity, _cloid = exit_proof_engine("filled", "0")
    quote, _expected = tick_failure(monkeypatch, engine, payload, "before_transition", TemporaryObservation("read_unavailable", "price", "transport"))
    await waverider.tick(engine, "trade", quote)
    row = payload["trades"]["trade"]["waverider"]
    assert row["phase"] == "paused" and row["resume_phase"] == "waiting_retrace" and row["cycle"] == 0


def wave_inventory_response(boundary, payload):
    if payload.get("type") == "perpDexs":
        return [None]
    if payload.get("type") == "allPerpMetas":
        return [{"universe": [{"name": "BTC"}]}]
    if payload.get("type") == "frontendOpenOrders":
        return [row["order"] for row in boundary.orders.values() if row["status"] == "open"]
    return None


def delayed_exit_transport(boundary, exit_cloid, submissions, lose_reply):
    def respond(request):
        payload = json.loads(request.content)
        inventory = wave_inventory_response(boundary, payload)
        if request.url.path == "/info" and inventory is not None:
            return httpx.Response(200, json=inventory)
        response = boundary.handle(request)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order" and any(
                row.get("c") == exit_cloid for row in payload["action"]["orders"]):
            submissions.append(exit_cloid)
            if lose_reply:
                raise httpx.ReadTimeout("Synthetic accepted exit with a lost response")
        return response
    return respond


def assert_recovered_exit(boundary, current, quantity, submissions, exit_cloid):
    assert current["status"] == "open" and not current.get("suspended_wave_targets")
    assert current["waverider"]["phase"] == "waiting_rebound"
    assert Decimal(current["waverider"]["sold_quantity"]) == quantity / 2
    assert Decimal(current["remaining_quantity"]) == abs(boundary.position) == quantity / 2
    assert submissions == [exit_cloid]
    stops = [row for row in boundary.orders.values() if row["status"] == "open" and row["order"]["isTrigger"]
             and row["order"]["reduceOnly"]]
    assert sum(Decimal(row["order"]["sz"]) for row in stops) >= abs(boundary.position)


def delayed_exit_status(trading, cloid, enabled, monkeypatch):
    reader = trading.order_status
    if not enabled:
        return reader
    calls = []

    async def observe(selected):
        if selected == cloid:
            calls.append(selected)
            if len(calls) == 2:
                raise httpx.ReadTimeout("Synthetic exact-exit status timeout while history lags")
        return await reader(selected)

    monkeypatch.setattr(trading, "order_status", observe)
    return reader


@pytest.mark.parametrize("lose_reply", [False, True])
@pytest.mark.parametrize("status_timeout", [False, True])
@pytest.mark.asyncio
async def test_shared_account_recovery_keeps_a_proved_exit_pending_while_fills_lag(setup, signal, monkeypatch, lose_reply, status_timeout):
    from test_waverider_execution import wave_trade

    state, boundary, exchange, trading, raw_plan, trade = await wave_trade(setup, signal, monkeypatch, "until_target")
    identity = trade["trade_id"]
    quantity = Decimal(trade["remaining_quantity"])
    known_oids = {row["oid"] for row in trade["fill_events"].values()}
    history = trading.fill_history_for_plan
    exit_cloid = exchange.cloid(f"waverider:{identity}:0:reduce")
    submissions = []
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(delayed_exit_transport(boundary, exit_cloid, submissions, lose_reply)))

    async def lagging_history(plan):
        return [row for row in await history(plan) if row["oid"] in known_oids]

    monkeypatch.setattr(trading, "fill_history_for_plan", lagging_history)
    boundary.mid = "104"
    market = Market.model_validate(raw_plan["market"])
    await waverider.execute(trading, identity, trade["waverider"], "reduce", quantity / 2,
        await exchange.price_snapshot(market), Decimal(".2"))
    exchange.settings.shared_account = True
    status_reader = delayed_exit_status(trading, exit_cloid, status_timeout, monkeypatch)
    reports = await trading.recover()
    current = state.snapshot()["trades"][identity]
    assert current["status"] != "conflicted", reports
    assert current["suspended_wave_targets"] and current["waverider"]["phase"] == "pending_reduce"
    assert current["exit_quantity_evidence_pending"] and submissions == [exit_cloid]
    monkeypatch.setattr(trading, "fill_history_for_plan", history)
    monkeypatch.setattr(trading, "order_status", status_reader)
    restored_reports = await trading.recover()
    restored = state.snapshot()["trades"][identity]
    assert not restored.get("execution_incident"), (restored_reports, restored.get("execution_incident"))
    assert_recovered_exit(boundary, restored, quantity, submissions, exit_cloid)
    await trading.recover()
    assert submissions == [exit_cloid]


def shared_quantity_engine(monkeypatch, side):
    engine, payload, _calls, _available, identity, cloid = exit_proof_engine("filled", "0")
    trade = payload["trades"]["trade"]
    trade["lifecycle_version"] = 2
    trade["plan"]["side"] = side
    payload["operations"][identity]["wire_action"]["orders"][0]["b"] = side == "short"
    engine.state.snapshot = lambda: copy.deepcopy(payload)
    engine.exchange.settings = SimpleNamespace(shared_account=True)
    engine.assert_plan_binding = lambda *_args: None
    engine.validate_owned_exposure = MethodType(Trading.validate_owned_exposure, engine)
    engine.owned_remaining = MethodType(Trading.owned_remaining, engine)
    engine.waves = waverider
    monkeypatch.setattr(Trading.__module__ + ".TradePlan.model_validate", lambda raw: SimpleNamespace(side=raw["side"], market=SimpleNamespace(product="perp")))
    return engine, payload, identity, cloid


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("case", ["proved_gap", "unknown", "mismatched", "manual_extra", "manual_without_wave", "manual_after_complete", "wrong_direction", "quantity_unproved", "status_timeout"])
@pytest.mark.asyncio
async def test_only_exact_own_exit_quantity_can_explain_a_shared_position_difference(monkeypatch, side, case):
    engine, payload, identity, cloid = shared_quantity_engine(monkeypatch, side)
    trade = payload["trades"]["trade"]
    observed = native_wave_status(cloid, "filled", "0")
    observed["order"]["order"]["side"] = "A" if side == "long" else "B"
    signed = Decimal(3) * (1 if side == "long" else -1)
    if case == "unknown":
        observed = {"status": "unknownOid"}
    elif case == "mismatched":
        observed["order"]["order"]["cloid"] = "a-different-order"
    elif case == "manual_extra":
        signed *= Decimal(".9")
    elif case == "manual_without_wave":
        trade["suspended_wave_targets"] = None
    elif case == "manual_after_complete":
        trade["fill_events"]["complete-exit"] = {"oid": 7, "kind": "waverider", "sz": "1", "px": "104", "time": 2}
        signed *= Decimal(".9")
    elif case == "wrong_direction":
        signed *= -1
    elif case == "quantity_unproved":
        observed["order"]["order"].pop("origSz")

    async def order_status(_selected):
        if case == "status_timeout":
            raise httpx.ReadTimeout("Synthetic exit observation timeout")
        return observed

    engine.order_status = order_status
    error = TemporaryObservation if case in ("proved_gap", "status_timeout") else ExternalInterference
    with pytest.raises(error):
        await Trading.observe_owned_remaining(engine, "trade", signed)
    assert bool(trade.get("exit_quantity_evidence_pending")) == (case in ("proved_gap", "status_timeout"))


@pytest.mark.parametrize("entry_pending", [False, True])
@pytest.mark.asyncio
async def test_completed_exit_evidence_clears_only_its_own_pending_cause(monkeypatch, entry_pending):
    engine, payload, _identity, _cloid = shared_quantity_engine(monkeypatch, "long")
    row = payload["trades"]["trade"]
    row.update(exit_quantity_evidence_pending=True, quantity_evidence_pending=entry_pending)
    row["entry_orders"] = [{"kind": "original", "phase": "pending", "confirmed_filled": None}]
    assert await Trading.observe_owned_remaining(engine, "trade", Decimal(4)) == 4
    assert row["exit_quantity_evidence_pending"] is True
    row["fill_events"]["complete-exit"] = {"oid": 7, "kind": "waverider", "sz": "1", "px": "104", "time": 2}
    assert await waverider.resume_exit(engine, "trade") is True
    assert row["exit_quantity_evidence_pending"] is False
    assert row["quantity_evidence_pending"] is entry_pending


@pytest.mark.parametrize("cause", ["quantity_evidence_pending", "exit_quantity_evidence_pending"])
def test_each_unreconciled_quantity_cause_blocks_new_exposure(cause):
    engine = SimpleNamespace(validate_configuration=lambda *_args: None)
    snapshot = {"trades": {"existing": {"status": "open", cause: True}}}
    with pytest.raises(waverider.AqlaError):
        Trading.validate_entry_readiness(engine, snapshot, SimpleNamespace(trade_id="new"), owned=False)


@pytest.mark.parametrize("reentering", [False, True])
@pytest.mark.asyncio
async def test_actual_capture_callers_never_map_a_different_wave_order_to_own_fills(reentering):
    engine, payload, _calls, _available, _identity, cloid = exit_proof_engine("filled", "0", reentering=reentering)
    row = payload["trades"]["trade"]
    row.update(lifecycle_version=2, entry_cloid="original-entry")
    row.setdefault("entry_orders", [{"kind": "original", "index": 0, "quantity": "4", "limit": "100",
        "phase": "terminal", "confirmed_filled": "4", "cloid": "original-entry", "operation_id": "entry:trade"}])
    observed, mapping = {}, {}
    wrong = native_wave_status("previous-cycle-cloid", "filled", "0", reentering)

    async def status(selected):
        if selected == cloid:
            return wrong
        return {"status": "order", "order": {"status": "open", "order": {"oid": 2}}}

    async def history(_plan):
        return [{"oid": 7, "coin": "BTC", "sz": "1", "px": "104", "time": 2, "tid": 10}]

    engine.waves = waverider
    engine.exchange.settings = SimpleNamespace(venue="hyperliquid")
    engine.order_status, engine.fill_history_for_plan = status, history
    engine.capture_wave_order_status = MethodType(Trading.capture_wave_order_status, engine)
    engine.capture_managed_entry_identities = MethodType(Trading.capture_managed_entry_identities, engine)
    engine.cached_terminal_order = Trading.cached_terminal_order
    initial = {"status": "order", "order": {"status": "filled", "order": {"oid": 1}}}
    if reentering:
        await Trading.capture_managed_entry_identities(engine, row, "trade", initial, lambda _cloid: None, observed, mapping)
    else:
        _, observed, mapping = await Trading.capture_order_identities(engine, row, SimpleNamespace(market=SimpleNamespace(symbol="BTC")), "trade", initial)
    owned, _, _ = await Trading.capture_owned_fill_facts(engine, row, SimpleNamespace(market=SimpleNamespace(symbol="BTC")), mapping)
    assert 7 not in mapping and cloid not in observed
    assert owned == row["fill_events"]
