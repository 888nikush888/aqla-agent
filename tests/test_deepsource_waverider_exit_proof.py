"""Exact native exit quantities must precede Waverider target restoration."""
import copy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aqla_agent import waverider
from aqla_hyperliquid_mcp.models import Market


def exit_proof_engine(status, remaining, *, rejected=False):
    identity = "waverider:trade:0:reduce"
    cloid = "owned-wave-cloid"
    original = {"oid": 1, "kind": "entry", "sz": "4", "px": "100", "time": 1}
    payload = {
        "trades": {"trade": {
            "suspended_wave_targets": identity,
            "waverider": {"phase": "pending_reduce", "cycle": 0, "operation_id": identity, "action": "reduce"},
            "fill_events": {"entry": original},
            "plan": {"side": "long", "market": {"asset_id": 0}},
            "orders": [{"kind": "sl", "cloid": "original-stop"}],
            "exit_orders": [{"kind": "waverider", "operation_id": identity, "cloid": cloid, "quantity": "1", "price": "103"}],
        }},
        "operations": {identity: {"operation_id": identity, "phase": "rejected" if rejected else "unknown",
            "outcome": "rejected" if rejected else "unknown", "result": {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Native order price rejected"}]}}} if rejected else None,
            "wire_action": {"type": "order", "orders": [{"a": 0, "b": False, "r": True, "c": cloid, "s": "1", "p": "103"}]}}},
    }
    calls = []
    available = []

    class State:
        def read(self, *_args, **_kwargs):
            return copy.deepcopy(payload)

        def update(self, callback):
            return callback(payload)

    async def order_status(selected):
        calls.append(("status", selected))
        if rejected:
            return {"status": "unknownOid"}
        return {"status": "order", "order": {"status": status, "order": {
            "oid": 7, "cloid": cloid, "origSz": "1", "sz": remaining}}}

    async def capture_fills(selected):
        calls.append(("fills", selected))
        for index, size in enumerate(available):
            payload["trades"][selected]["fill_events"][f"exit-{index}"] = {
                "oid": 7, "tid": index + 10, "kind": "waverider", "sz": size, "px": "104", "time": index + 2}

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
        return {"status": "order", "order": {"status": "open", "order": {"oid": 7, "origSz": "1", "sz": "1"}}}

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
        return {"status": "order", "order": {"status": "filled", "order": {"oid": 7, "origSz": "1", "sz": "0"}}}

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
