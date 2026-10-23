
import copy
import json
from decimal import Decimal

import httpx
import pytest

from aqla_agent.messages import trade_block
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    Interpretation,
    SourceEvent,
    TradePlan,
    now_ms,
    require_first,
)


@pytest.fixture
async def selectable_updates(setup, monkeypatch):
    from aqla_hyperliquid_mcp.server import close_server, create_server

    settings, state, fake, exchange, _ = setup
    original = fake.handle

    def markets(request):
        payload = json.loads(request.content)
        if request.url.path == "/info":
            universe = [{"name": symbol, "szDecimals": 2, "maxLeverage": 20} for symbol in ("BTC", "ETH")]
            if payload.get("type") == "allMids":
                return httpx.Response(200, json={"BTC": "100", "ETH": "100"})
            if payload.get("type") == "meta":
                return httpx.Response(200, json={"universe": universe})
            if payload.get("type") == "metaAndAssetCtxs":
                return httpx.Response(200, json=[{"universe": universe}, [{"markPx": "100"}] * 2])
        return original(request)

    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(markets))
    server = create_server(settings, state=state, exchange=exchange)
    sources, trades = [], []
    try:
        for index, symbol in enumerate(("BTC", "ETH"), 1):
            source = SourceEvent(chat_id="channel", message_id=index, sent_at=now_ms(),
                                 text=f"{symbol} long Entry 95 später. SL 90 TP 110")
            server.aqla_trading.ingest(source)
            interpretation = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol=symbol,
                side="long", entry="limit", entry_prices=[95], entry_timing="deferred", stop_loss=90, take_profits=[110],
                evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                    ("symbol", symbol), ("side", "long"), ("entry_prices.0", "95"), ("entry_timing", "später"),
                    ("stop_loss", "90"), ("take_profits.0", "110"))])
            plan = await server.aqla_trading.preview(source.event_id, interpretation)
            trade = await server.aqla_trading.execute(plan["plan_id"])
            assert trade["status"] == "waiting_entry"
            sources.append(source)
            trades.append(trade)
        yield server, state, fake, sources, trades
    finally:
        await close_server(server)


@pytest.mark.asyncio
async def test_explicit_trade_id_selects_one_same_source_setup_through_public_mcp(selectable_updates):
    server, state, fake, _, trades = selectable_updates
    source = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="Close this trade")
    server.aqla_trading.ingest(source)
    value = Interpretation(kind="trade_update", source_refs=[source.source_id], update_action="close",
                           bound_trade_id=trades[0]["trade_id"], evidence=[
                               Evidence(field="update_action", source_id=source.source_id, quote="Close")])
    arguments = {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                 "context_auth": server.aqla_authority.sign("operator", "explicit-owner-selection")}
    result = await server.call_tool("trade_update", arguments)
    assert not result.is_error
    result = result.structured_content or json.loads(result.content[0].text)
    assert result["trade_id"] == trades[0]["trade_id"] and "error" not in result
    after = state.snapshot()
    assert after["trades"][trades[0]["trade_id"]]["status"] == "cancelled"
    assert after["trades"][trades[1]["trade_id"]] == trades[1]
    assert after["bindings"]["update:" + source.event_id]["trade_id"] == trades[0]["trade_id"]
    await server.call_tool("trade_update", arguments)
    assert state.snapshot()["bindings"]["update:" + source.event_id] == after["bindings"]["update:" + source.event_id]
    assert not fake.actions
    with pytest.raises(AqlaError, match="immutable|bound|independent setups"):
        await server.aqla_trading.update_trade(source.event_id, value.model_copy(update={"bound_trade_id": trades[1]["trade_id"]}))
    assert not fake.actions and state.snapshot()["trades"][trades[1]["trade_id"]] == trades[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["unknown_id", "channel", "reply", "unknown_reply", "symbol", "side", "conflicted", "cleanup"])
async def test_explicit_trade_id_cannot_override_authoritative_update_constraints(selectable_updates, constraint):
    from mcp.server.mcpserver.exceptions import ToolError

    server, state, fake, sources, trades = selectable_updates
    fields, text = {}, "Close this trade"
    if constraint == "channel":
        state.update(lambda data: data["sources"].append("another-source"))
    if constraint == "symbol":
        fields["symbol"], text = "ETH", "ETH Close this trade"
    if constraint == "side":
        fields["side"], text = "short", "Close this short trade"
    if constraint in ("conflicted", "cleanup"):
        state.update(lambda data: data["trades"][trades[0]["trade_id"]].update(status=constraint))
    source = SourceEvent(chat_id="another-source" if constraint == "channel" else "channel", message_id=3,
                         sent_at=now_ms(), reply_to_id=sources[1].message_id if constraint == "reply" else 777 if constraint == "unknown_reply" else None,
                         text=text)
    server.aqla_trading.ingest(source)
    value = Interpretation(kind="trade_update", source_refs=[source.source_id], update_action="close", **fields,
                           bound_trade_id="unknown-trade" if constraint == "unknown_id" else trades[0]["trade_id"],
                           evidence=[Evidence(field="update_action", source_id=source.source_id, quote="Close")])
    before = state.snapshot()
    arguments = {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                 "context_auth": server.aqla_authority.sign("operator", "explicit-owner-selection")}
    if constraint in ("conflicted", "cleanup"):
        result = await server.call_tool("trade_update", arguments)
        result = result.structured_content or json.loads(result.content[0].text)
        assert result["targets"][0]["error"]
    else:
        with pytest.raises(ToolError, match="independent setups|replied source"):
            await server.call_tool("trade_update", arguments)
    after = state.snapshot()
    assert after["trades"] == before["trades"] and after["operations"] == before["operations"]
    assert "update:" + source.event_id not in after["bindings"] and not fake.actions


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
    if action == "reduce" and not evidence:
        evidence = [Evidence(field="reduce_percent", source_id=source.source_id, quote=text)]
    interpretation = Interpretation(kind="trade_update", source_refs=[source.source_id], update_action=action, evidence=evidence, **fields)
    return source, interpretation


async def waiting_gtc(setup, signal):
    _, _, fake, _, trading = setup
    source, interpretation = signal
    fake.mid = "101"
    interpretation = interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal("100")]})
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "opening" and Decimal(trade["filled_quantity"]) == 0
    return trade


def stop_update(trading, message_id=2, stop="95"):
    source, interpretation = update(trading, message_id, "move_stop", "SL auf " + stop, stop_loss=stop)
    interpretation.evidence = [Evidence(field="stop_loss", source_id=source.source_id, quote=stop)]
    return source, interpretation


def management_update(trading, action):
    if action == "move_stop":
        return stop_update(trading, 3, "97")
    if action == "replace_targets":
        source, interpretation = update(trading, 3, action, "Neue Ziele 115 und 125", take_profits=["115", "125"])
        interpretation.evidence = [Evidence(field="take_profits.0", source_id=source.source_id, quote="115"),
                                   Evidence(field="take_profits.1", source_id=source.source_id, quote="125")]
        return source, interpretation
    return update(trading, 3, action, "Halbe Position schließen" if action == "reduce" else "Rest schließen",
                  **({"reduce_percent": "50"} if action == "reduce" else {}))


async def unconfirmed_stop(setup, signal):
    _, state, fake, exchange, trading = setup
    trade = await opened(setup, signal)
    source, interpretation = stop_update(trading)
    cloid = exchange.cloid("update:" + source.event_id)
    visibility = {"hidden": True}
    original = fake.handle

    def lost_and_hidden(request):
        payload = json.loads(request.content)
        if request.url.path == "/info" and payload.get("type") == "orderStatus" and payload.get("oid") == cloid and visibility["hidden"]:
            return httpx.Response(200, json={"status": "unknownOid"})
        response = original(request)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order" and any(order.get("c") == cloid for order in payload["action"]["orders"]):
            raise httpx.ReadTimeout("Accepted stop with a lost response and delayed order evidence")
        return response

    exchange.client._transport = httpx.MockTransport(lost_and_hidden)
    result = await trading.update_trade(source.event_id, interpretation)
    assert result["result"]["pending_stop_update"] is True
    assert state.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"]["phase"] == "unknown"
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    return trade, source, interpretation, visibility


async def lagged_final_entry(setup, signal, second_price="100"):
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(sizing_mode="fixed_notional", fixed_notional_usdc="480"))
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal("100")]}))
    fake.partial_limit_fraction = .5
    original = fake.handle
    visibility = {"hidden": True}
    partial_fills = []
    injected = False

    def delayed(request):
        nonlocal injected, partial_fills
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"]["type"] == "cancelByCloid" and not injected:
            identity = payload["action"]["cancels"][0]["cloid"]
            order = fake.orders.get(identity)
            if order and not order["order"]["reduceOnly"] and order["status"] == "open":
                partial_fills = copy.deepcopy(fake.fills)
                fake.fill(identity, order["order"]["sz"], second_price)
                injected = True
        response = original(request)
        if injected and visibility["hidden"] and request.url.path == "/info":
            if payload.get("type") == "userFillsByTime":
                return httpx.Response(200, json=partial_fills)
            if payload.get("type") == "clearinghouseState":
                value = response.json()
                for row in value["assetPositions"]:
                    row["position"]["szi"] = "2.40"
                return httpx.Response(200, json=value)
        return response

    exchange.client._transport = httpx.MockTransport(delayed)
    trade = await trading.execute(plan["plan_id"])
    assert injected and Decimal(trade["filled_quantity"]) == Decimal("2.40")
    assert Decimal(trade["entry_fill_quantity_confirmed"]) == Decimal("4.80")
    return trade, visibility


@pytest.mark.asyncio
async def test_pending_stop_waits_for_terminal_quantity_and_complete_own_fills(setup, signal):
    _, state, fake, _, trading = setup
    trade, visibility = await lagged_final_entry(setup, signal)
    source, instruction = stop_update(trading)
    before = len(fake.actions)
    result = await trading.update_trade(source.event_id, instruction)
    assert result["result"]["pending_stop_update"] is True
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    assert "update:" + source.event_id not in state.snapshot()["operations"]
    assert len(fake.actions) == before
    old_stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    assert fake.orders[old_stop["cloid"]]["status"] == "open"
    visibility["hidden"] = False
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert Decimal(current["remaining_quantity"]) == Decimal("4.80")
    assert Decimal(current["stop_price"]) == 95
    assert current["plan"] == trade["plan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old_stop", ["canceled", "malformed"])
# Sequential safety assertions; control flow (including test doubles): 10.
# skipcq: PY-R1000
async def test_terminal_quantity_lag_does_not_silence_missing_initial_protection(setup, signal, old_stop):
    _, state, fake, exchange, trading = setup
    exchange.fail_closed_protection = True
    trade, visibility = await lagged_final_entry(setup, signal)
    initial = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    if old_stop == "canceled":
        fake.orders[initial["cloid"]]["status"] = "canceled"
    else:
        fake.orders[initial["cloid"]]["order"]["reduceOnly"] = False
    source, instruction = stop_update(trading)
    original_entry = copy.deepcopy(state.snapshot()["operations"][trade["entry_operation_id"]])
    await trading.update_trade(source.event_id, instruction)
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    effective = [order for order in fake.orders.values() if order["status"] == "open"
                 and order["order"].get("reduceOnly") is True and order["order"].get("isTrigger") is True
                 and order["order"].get("side") == "A" and Decimal(order["order"].get("sz", "0"))
                 >= Decimal(current["remaining_quantity"])]
    assert effective or Decimal(str(fake.position)) == 0
    assert Decimal(current["filled_quantity"]) == Decimal(current["remaining_quantity"]) == Decimal("2.40")
    assert Decimal(current["entry_vwap"]) == 100 and Decimal(current["reserved_risk"]) == Decimal(trade["reserved_risk"])
    assert current["status"] == "unknown" and current["quantity_evidence_pending"] is True
    with pytest.raises(AqlaError, match="quantity evidence.*reconciliation"):
        trading.validate_admission(state.snapshot(), TradePlan.model_validate(current["plan"]), owned=True)
    assert current["pending_stop_update"]["phase"] == "waiting_fill"
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    assert "update:" + source.event_id not in state.snapshot()["operations"]
    assert any(Decimal(order["order"]["sz"]) == Decimal("2.40") for order in effective)
    before = len(fake.actions)
    await trading.recover()
    assert len(fake.actions) == before
    visibility["hidden"] = False
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert current["quantity_evidence_pending"] is False
    assert trading.validate_admission(state.snapshot(), TradePlan.model_validate(current["plan"]), owned=True) is None
    assert Decimal(current["remaining_quantity"]) == Decimal("4.80") and Decimal(current["stop_price"]) == 95
    assert current["plan"] == trade["plan"]
    assert state.snapshot()["operations"][trade["entry_operation_id"]] == original_entry
    assert len([action for action in fake.actions if action["type"] == "order" and any(order.get("c") == exchange.cloid("update:" + source.event_id) for order in action["orders"])]) == 1


@pytest.mark.asyncio
async def test_break_even_uses_final_vwap_before_binding_stop_identity(setup, signal):
    _, state, fake, _, trading = setup
    trade, visibility = await lagged_final_entry(setup, signal, "98")
    assert Decimal(trade["entry_vwap"]) == 100
    visibility["hidden"] = False
    fake.mid = "105"
    source, instruction = update(trading, 2, "break_even", "SL auf Einstand")
    await trading.update_trade(source.event_id, instruction)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["entry_vwap"]) == 99
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert Decimal(current["stop_price"]) == Decimal(current["pending_stop_update"]["stop"]) == 99
    action = state.snapshot()["operations"]["update:" + source.event_id]["wire_action"]
    assert Decimal(action["orders"][0]["t"]["trigger"]["triggerPx"]) == 99
    assert current["plan"] == trade["plan"]


@pytest.mark.asyncio
async def test_break_even_reference_drift_during_cancel_never_sends_old_price(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(sizing_mode="fixed_notional", fixed_notional_usdc="480"))
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal("100")]}))
    fake.partial_limit_fraction = .5
    original_refresh = trading.refresh_trade

    async def crash(*args):
        raise RuntimeError("Crash after partial entry dispatch")

    monkeypatch.setattr(trading, "refresh_trade", crash)
    with pytest.raises(RuntimeError):
        await trading.execute(plan["plan_id"])
    monkeypatch.setattr(trading, "refresh_trade", original_refresh)
    await trading.capture_fills(plan["trade_id"])
    original = fake.handle
    injected = False

    def late_fill(request):
        nonlocal injected
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"]["type"] == "cancelByCloid" and not injected:
            identity = payload["action"]["cancels"][0]["cloid"]
            if identity == exchange.cloid("entry:" + plan["trade_id"]):
                fake.fill(identity, fake.orders[identity]["order"]["sz"], "98")
                injected = True
        return original(request)

    exchange.client._transport = httpx.MockTransport(late_fill)
    fake.mid = "105"
    changed, instruction = update(trading, 2, "break_even", "SL auf Einstand")
    result = await trading.update_trade(changed.event_id, instruction)
    current = state.snapshot()["trades"][plan["trade_id"]]
    assert injected and Decimal(current["entry_vwap"]) == 99
    assert result["result"]["phase"] == "rejected"
    assert "reference changed" in current["pending_stop_update"]["reason"]
    assert "update:" + changed.event_id not in state.snapshot()["operations"]
    assert Decimal(current["stop_price"]) == 90
    assert current["plan"] == plan


@pytest.mark.asyncio
async def test_unfilled_gtc_stop_update_remains_pending_until_confirmed(setup, signal):
    _, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading)
    before = len(fake.actions)
    result = await trading.update_trade(source.event_id, interpretation)
    assert result["result"]["pending_stop_update"] is True
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "waiting_fill"
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    assert current["plan"] == trade["plan"] and Decimal(current["stop_price"]) == 90
    await trading.update_trade(source.event_id, interpretation)
    assert len(fake.actions) == before
    fake.mid = "100"
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], "100")
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert state.snapshot()["events"][source.event_id]["status"] == "updated"
    assert Decimal(current["stop_price"]) == 95
    assert any(fake.orders[order["cloid"]]["status"] == "open" and fake.orders[order["cloid"]]["order"]["triggerPx"] == "95"
               for order in current["orders"] if order["kind"] == "sl")
    assert current["plan"] == trade["plan"]


@pytest.mark.asyncio
async def test_superseded_stop_update_finishes_original_event(setup, signal):
    _, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    first, instruction = stop_update(trading)
    await trading.update_trade(first.event_id, instruction)
    second, newer = stop_update(trading, 3, "97")
    await trading.update_trade(second.event_id, newer)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert state.snapshot()["events"][first.event_id]["status"] == "superseded"
    assert current["stop_update_history"]["update:" + first.event_id]["phase"] == "superseded"
    assert current["pending_stop_update"]["event_id"] == second.event_id
    assert current["management_revision"] == 2
    fake.mid = "100"
    fake.fill(trade["entry_cloid"], Decimal(trade["plan"]["quantity"]) / 2)
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed" and Decimal(current["stop_price"]) == 97
    assert current["plan"] == trade["plan"]
    assert "update:" + first.event_id not in state.snapshot()["operations"]
    before = len(fake.actions)
    assert (await trading.update_trade(first.event_id, instruction))["duplicate"]
    assert len(fake.actions) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replace_targets", "reduce", "close"])
async def test_independent_management_supersedes_unsent_stop_request(setup, signal, action):
    _, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    original, instruction = stop_update(trading)
    await trading.update_trade(original.event_id, instruction)
    later, management = management_update(trading, action)
    await trading.update_trade(later.event_id, management)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "superseded"
    assert state.snapshot()["events"][original.event_id]["status"] == "superseded"
    assert current["pending_stop_update"]["reason"]
    assert "update:" + original.event_id not in state.snapshot()["operations"]
    assert current["plan"] == trade["plan"]
    if action == "replace_targets":
        fake.mid = "100"
        fake.fill(trade["entry_cloid"], trade["plan"]["quantity"])
    await trading.recover()
    assert not any(action["type"] == "order" and any(order["t"].get("trigger", {}).get("triggerPx") == "95" for order in action["orders"]) for action in fake.actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["move_stop", "replace_targets", "reduce", "close"])
async def test_unknown_stop_is_reconciled_before_independent_management(setup, signal, action):
    _, state, fake, _, trading = setup
    trade, original, _, visibility = await unconfirmed_stop(setup, signal)
    operation_id = "update:" + original.event_id
    saved = copy.deepcopy(state.snapshot()["operations"][operation_id])
    old_stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    later, instruction = management_update(trading, action)
    before = len(fake.actions)
    with pytest.raises(AqlaError, match="[Rr]econcile"):
        await trading.update_trade(later.event_id, instruction)
    assert len(fake.actions) == before
    assert "update:" + later.event_id not in state.snapshot()["bindings"]
    assert state.snapshot()["events"][original.event_id]["status"] == "needs_reconciliation"
    assert fake.orders[old_stop["cloid"]]["status"] == "open"
    visibility["hidden"] = False
    await trading.recover()
    assert state.snapshot()["events"][original.event_id]["status"] == "updated"
    await trading.update_trade(later.event_id, instruction)
    current = state.snapshot()["operations"][operation_id]
    assert all(current[key] == saved[key] for key in ("request", "request_hash", "nonce", "wire_action"))
    assert len([row for row in fake.actions if row["type"] == "order" and any(order["c"] == saved["wire_action"]["orders"][0]["c"] for order in row["orders"])]) == 1


@pytest.mark.asyncio
async def test_unknown_pending_stop_defers_automatic_stop_revision_without_emergency_close(setup, signal):
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(break_even_after_tp=1))
    exchange.fail_closed_protection = True
    trade, source, _, visibility = await unconfirmed_stop(setup, signal)
    old_stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    target = require_first(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.mid = "115"
    fake.fill(target["cloid"], target["quantity"], "110")
    remaining = Decimal(str(fake.position))
    before = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    assert Decimal(str(fake.position)) == remaining > 0
    assert fake.orders[old_stop["cloid"]]["status"] == "open"
    assert len(fake.actions) == before
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    visibility["hidden"] = False
    await trading.recover()
    assert state.snapshot()["events"][source.event_id]["status"] == "updated"
    await trading.recover()
    assert Decimal(state.snapshot()["trades"][trade["trade_id"]]["stop_price"]) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("old_stop", ["missing", "malformed"])
# Sequential safety assertions; control flow (including test doubles): 15.
# skipcq: PY-R1000
async def test_unknown_pending_stop_does_not_bypass_missing_or_malformed_initial_protection(setup, signal, old_stop):
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(break_even_after_tp=1))
    exchange.fail_closed_protection = True
    trade, source, _, visibility = await unconfirmed_stop(setup, signal)
    original_stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    if old_stop == "missing":
        fake.orders[original_stop["cloid"]]["status"] = "canceled"
    else:
        fake.orders[original_stop["cloid"]]["order"]["reduceOnly"] = False
    target = require_first(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.mid = "115"
    fake.fill(target["cloid"], target["quantity"], "110")
    before = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    assert len(fake.actions) > before
    current = state.snapshot()["trades"][trade["trade_id"]]
    effective = [row for identity, row in fake.orders.items() if identity != exchange.cloid("update:" + source.event_id)
                 and row["status"] == "open" and row["order"]["reduceOnly"] is True and row["order"]["isTrigger"] is True
                 and row["order"]["side"] == "A" and Decimal(row["order"]["triggerPx"]) == 90
                 and Decimal(row["order"]["sz"]) >= Decimal(current["remaining_quantity"])]
    assert effective or Decimal(str(fake.position)) == 0
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    assert fake.orders[original_stop["cloid"]]["status"] == "canceled"
    visibility["hidden"] = False
    await trading.recover()
    assert state.snapshot()["events"][source.event_id]["status"] == "updated"
    assert len([action for action in fake.actions if action["type"] == "order" and any(order.get("c") == exchange.cloid("update:" + source.event_id) for order in action["orders"])]) == 1


@pytest.mark.asyncio
async def test_rejected_pending_replacement_waits_for_confirmed_emergency_exit(setup, signal):
    _, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, instruction = stop_update(trading)
    await trading.update_trade(source.event_id, instruction)
    old_stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
    fake.mid = "100"
    fake.reject_stop = True
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"])
    await trading.refresh_trade(trade["trade_id"])
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    assert fake.orders[old_stop["cloid"]]["status"] == "open"
    assert fake.position == 0
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "closed" and current["pending_stop_update"]["phase"] == "rejected"
    assert state.snapshot()["events"][source.event_id]["status"] == "rejected"
    assert "SL-Update abgelehnt" in trade_block(state.snapshot(), current, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("fraction", [Decimal("0.5"), Decimal(1)])
async def test_pending_stop_applies_own_fill_during_entry_cancellation(setup, signal, fraction):
    _, state, fake, exchange, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, instruction = stop_update(trading)
    await trading.update_trade(source.event_id, instruction)
    original = fake.handle
    injected = False

    def fill_during_cancel(request):
        nonlocal injected
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"]["type"] == "cancelByCloid" and not injected:
            if payload["action"]["cancels"][0]["cloid"] == trade["entry_cloid"]:
                fake.fill(trade["entry_cloid"], Decimal(trade["plan"]["quantity"]) * fraction)
                injected = True
        return original(request)

    exchange.client._transport = httpx.MockTransport(fill_during_cancel)
    fake.mid = "100"
    await trading.cancel_entry(trade["trade_id"])
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert injected and current["pending_stop_update"]["phase"] == "confirmed"
    assert Decimal(current["remaining_quantity"]) == Decimal(trade["plan"]["quantity"]) * fraction
    stop = fake.orders[exchange.cloid("update:" + source.event_id)]
    assert stop["status"] == "open" and Decimal(stop["order"]["sz"]) == Decimal(current["remaining_quantity"])
    assert current["plan"] == trade["plan"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "management", "account"])
async def test_stop_acceptance_freezes_origin_before_fill_and_position_awaits(setup, signal, monkeypatch, change):
    settings, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, instruction = stop_update(trading)
    original = trading.positions

    async def changed(market):
        result = await original(market)
        if change == "source":
            trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1, "text": "SL auf 97"}))
        elif change == "management":
            state.update(lambda data: data["trades"][trade["trade_id"]].update(management_revision=1))
        else:
            settings.vault_address = "0x" + "1" * 40
        return result

    monkeypatch.setattr(trading, "positions", changed)
    before = len(fake.actions)
    with pytest.raises(AqlaError):
        await trading.update_trade(source.event_id, instruction)
    assert "update:" + source.event_id not in state.snapshot()["bindings"]
    assert "pending_stop_update" not in state.snapshot()["trades"][trade["trade_id"]]
    assert state.snapshot()["events"][source.event_id]["status"] != "updated"
    assert len(fake.actions) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("initialization", ["initial_protection", "backup"])
async def test_initial_protection_revision_preserves_pending_stop(setup, signal, initialization):
    _, state, fake, _, trading = setup
    if initialization == "backup":
        state.update(lambda data: data["profiles"]["backup"]["parameters"].update(fallback_stop_loss_percent="10"))
        source, interpretation = signal
        signal = source, interpretation.model_copy(update={"stop_loss": None, "evidence": [item for item in interpretation.evidence if item.field != "stop_loss"]})
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading)
    await trading.update_trade(source.event_id, interpretation)
    original = copy.deepcopy(state.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"])
    if initialization == "initial_protection":
        old = require_first(order for order in trade["orders"] if order["kind"] == "sl")
        fake.orders[old["cloid"]]["status"] = "canceled"
    fake.mid = "101" if initialization == "backup" else "100"
    amount = Decimal(trade["plan"]["quantity"]) / (2 if initialization == "backup" else 1)
    fake.fill(trade["entry_cloid"], amount, fake.mid)
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    pending = current["pending_stop_update"]
    assert pending["phase"] == "confirmed"
    assert pending["management_revision"] == current["management_revision"] >= 3
    assert all(pending[key] == original[key] for key in ("stop", "source_binding", "operation_id"))
    assert Decimal(current["stop_price"]) == 95
    assert current["plan"] == trade["plan"]
    assert state.snapshot()["events"][source.event_id]["status"] == "updated"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop,allowed", [("80", False), ("80", True), ("89.95", True)])
async def test_pending_stop_validates_full_planned_budget_before_binding(setup, signal, stop, allowed):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(allow_stop_widening=allowed))
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading, stop=stop)
    before = len(fake.actions)
    with pytest.raises(AqlaError, match="widening"):
        await trading.update_trade(source.event_id, interpretation)
    assert "update:" + source.event_id not in state.snapshot()["bindings"]
    assert "pending_stop_update" not in state.snapshot()["trades"][trade["trade_id"]]
    assert len(fake.actions) == before


@pytest.mark.asyncio
async def test_cancel_without_fill_finishes_pending_stop_without_sending(setup, signal):
    _, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading)
    await trading.update_trade(source.event_id, interpretation)
    await trading.cancel_entry(trade["trade_id"])
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "closed_without_application"
    assert state.snapshot()["events"][source.event_id]["status"] == "rejected"
    assert "update:" + source.event_id not in state.snapshot()["operations"]
    assert fake.position == 0


@pytest.mark.asyncio
async def test_router_and_messages_keep_pending_stop_unfinished_until_confirmed(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    await router.recover()
    source, interpretation = signal
    fake.mid = "101"
    plan = await router.preview(source.event_id, interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal("100")]}))
    trade = await router.execute(plan["plan_id"])
    changed, instruction = stop_update(router)
    result = await router.update_trade(changed.event_id, instruction)
    assert result["result"]["pending_stop_update"] is True
    assert state.snapshot()["events"][changed.event_id]["status"] == "needs_reconciliation"
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert "SL-Update wartet" in trade_block(state.snapshot(), current, [])
    fake.mid = "100"
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"])
    await router.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["pending_stop_update"]["phase"] == "confirmed"
    assert state.snapshot()["events"][changed.event_id]["status"] == "updated"
    assert "SL-Update bestätigt" in trade_block(state.snapshot(), current, [])
    assert current["plan"] == trade["plan"]
    await router.close()


@pytest.mark.asyncio
async def test_router_supersedes_original_stop_event_on_new_acceptance(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    await router.recover()
    source, interpretation = signal
    fake.mid = "101"
    plan = await router.preview(source.event_id, interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal("100")]}))
    await router.execute(plan["plan_id"])
    first, instruction = stop_update(router)
    await router.update_trade(first.event_id, instruction)
    later, replacement = stop_update(router, 3, "97")
    await router.update_trade(later.event_id, replacement)
    assert state.snapshot()["events"][first.event_id]["status"] == "superseded"
    assert state.snapshot()["events"][later.event_id]["status"] == "needs_reconciliation"
    await router.close()


@pytest.mark.asyncio
# Sequential safety assertions; control flow (including test doubles): 10.
# skipcq: PY-R1000
async def test_crossed_pending_stop_waits_for_flat_position(setup, signal):
    _, state, fake, exchange, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading, stop="105")
    await trading.update_trade(source.event_id, interpretation)
    original = fake.handle
    lag = False
    visible_fills = []

    def lagged_close(request):
        nonlocal lag, visible_fills
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"]["type"] == "order" and any(order["r"] and order["t"].get("limit", {}).get("tif") == "Ioc" for order in payload["action"]["orders"]):
            visible_fills = copy.deepcopy(fake.fills)
            lag = True
        response = original(request)
        if request.url.path == "/info" and lag:
            if payload.get("type") == "userFillsByTime":
                return httpx.Response(200, json=visible_fills)
            if payload.get("type") == "clearinghouseState":
                return httpx.Response(200, json={"assetPositions": [{"position": {"coin": "BTC", "szi": trade["plan"]["quantity"], "entryPx": "100"}}],
                                                "marginSummary": {"accountValue": "10000"}, "withdrawable": "10000"})
        return response

    exchange.client._transport = httpx.MockTransport(lagged_close)
    fake.mid = "100"
    fake.fill(trade["entry_cloid"], trade["plan"]["quantity"])
    await trading.refresh_trade(trade["trade_id"])
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "closing" and current["pending_stop_update"]["phase"] == "unknown"
    assert state.snapshot()["events"][source.event_id]["status"] == "needs_reconciliation"
    close_id = "stop-crossed:update:" + source.event_id
    assert state.snapshot()["operations"][close_id]["phase"] == "acknowledged"
    before = len(fake.actions)
    lag = False
    await trading.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "closed"
    assert current["pending_stop_update"]["phase"] == "closed_without_application"
    assert state.snapshot()["events"][source.event_id]["status"] == "rejected"
    assert len([action for action in fake.actions[:before] if action["type"] == "order" and any(order["r"] and order["t"].get("limit", {}).get("tif") == "Ioc" for order in action["orders"])]) == 1
    assert "update:" + source.event_id not in state.snapshot()["operations"]


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
    target = require_first(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
    fake.mid = "115"
    consumed = Decimal(target["quantity"]) / 2
    fake.fill(target["cloid"], consumed, "110")
    await trading.refresh_trade(trade["trade_id"])
    source, interpretation = update(trading, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=["115", "125"])
    interpretation.evidence = [Evidence(field="take_profits.0", source_id=source.source_id, quote="115"), Evidence(field="take_profits.1", source_id=source.source_id, quote="125")]
    await trading.update_trade(source.event_id, interpretation)
    current = state.snapshot()["trades"][trade["trade_id"]]
    active = [order for order in current["orders"] if order["kind"] == "tp" and not order.get("retired")]
    first = require_first(order for order in active if order["index"] == 0)
    assert Decimal(first["quantity"]) == Decimal(target["quantity"]) - consumed
    assert sum(Decimal(order["quantity"]) for order in active) == Decimal(str(fake.position))
    assert all(order["cloid"] != target["cloid"] for order in active)


@pytest.mark.asyncio
async def test_completed_tp_budget_moves_stop_only_from_own_exchange_fills(setup, signal):
    _, state, fake, _, trading = setup
    state.update(lambda row: row["policy"].update(break_even_after_tp=1))
    trade = await opened(setup, signal)
    fake.mid = "115"
    target = require_first(order for order in trade["orders"] if order["kind"] == "tp" and order["index"] == 0)
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
    target = require_first(order for order in current["orders"] if order["kind"] == "tp" and order["index"] == 0 and not order.get("retired"))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("quote,amount,accepted", [
    ("25%", 100, False), (None, 25, False), ("25", 25, False), ("half", 25, False),
    ("25%", 25, True), ("half", 50, True), ("Hälfte", 50, True), ("Halbe", 50, True),
    ("половину", 50, True), ("quarter", 25, True), ("Viertel", 25, True), ("четверть", 25, True),
    ("1/2", 50, True), ("1/4", 25, True), ("3/4", 75, True),
])
async def test_reduction_requires_matching_original_quantity_before_any_order(setup, signal, quote, amount, accepted):
    _, state, fake, _, trading = setup
    trade = await opened(setup, signal)
    source = SourceEvent(chat_id="channel", message_id=2, reply_to_id=1, sent_at=now_ms(),
        text="Close " + (quote or "25%") + " of the position now.")
    trading.ingest(source)
    interpretation = Interpretation(kind="trade_update", source_refs=[source.source_id], update_action="reduce",
        reduce_percent=amount, evidence=[Evidence(field="reduce_percent", source_id=source.source_id, quote=quote)] if quote else [])
    before = copy.deepcopy(fake.actions)
    if accepted:
        await trading.update_trade(source.event_id, interpretation)
        expected = Decimal(trade["filled_quantity"]) * (100 - amount) / 100
        assert abs(Decimal(str(fake.position))) == expected
        assert state.snapshot()["trades"][trade["trade_id"]]["plan"] == trade["plan"]
    else:
        with pytest.raises(AqlaError, match="Reduction percentage.*source evidence"):
            await trading.update_trade(source.event_id, interpretation)
        assert fake.actions == before
        assert "update:" + source.event_id not in state.snapshot()["bindings"]
        assert abs(Decimal(str(fake.position))) == Decimal(trade["filled_quantity"])


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("reserve_bps", [10, 1000])
async def test_stop_widening_preserves_cost_reserve_and_allows_reduced_own_quantity(setup, signal, side, reserve_bps):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(allow_stop_widening=True, cost_reserve_bps=reserve_bps))
    source, interpretation = signal
    if side == "short":
        source = source.model_copy(update={"edited_at": source.sent_at + 1, "text": "BTC verkaufen. SL 110. Ziele 90 und 80."})
        trading.ingest(source)
        interpretation = interpretation.model_copy(update={"side": "short", "stop_loss": Decimal(110), "take_profits": [Decimal(90), Decimal(80)], "evidence": [
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "BTC"), ("side", "verkaufen"), ("stop_loss", "110"), ("take_profits.0", "90"), ("take_profits.1", "80"))]})
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    entry, size, budget = Decimal(trade["entry_vwap"]), Decimal(trade["remaining_quantity"]), Decimal(trade["reserved_risk"])
    stop = entry + (1 if side == "short" else -1) * budget / size
    before = copy.deepcopy(fake.actions)
    with pytest.raises(AqlaError, match="original loss budget"):
        await trading.replace_stop(trade["trade_id"], stop, "widen-without-cost-reserve")
    assert fake.actions == before and "widen-without-cost-reserve" not in state.snapshot()["operations"]
    await trading.close_position(trade["trade_id"], Decimal(50), "reduce-own-risk")
    await trading.refresh_trade(trade["trade_id"])
    result = await trading.replace_stop(trade["trade_id"], stop, "widen-reduced-own-quantity")
    assert result["replacement_confirmed"]
    current = state.snapshot()["trades"][trade["trade_id"]]
    modeled_risk = Decimal(current["remaining_quantity"]) * (abs(entry - Decimal(current["stop_price"])) + entry * reserve_bps / 10000)
    assert modeled_risk <= budget
    assert current["plan"] == plan and current["reserved_risk"] == trade["reserved_risk"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve_bps", [10, 1000])
async def test_pending_stop_widening_after_confirmed_partial_entry_keeps_cost_reserve(setup, signal, reserve_bps):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(allow_stop_widening=True, cost_reserve_bps=reserve_bps,
        sizing_mode="fixed_notional", fixed_notional_usdc="480"))
    source, interpretation = signal
    interpretation = interpretation.model_copy(update={"entry": "limit", "entry_prices": [Decimal(100)]})
    plan = await trading.preview(source.event_id, interpretation)
    fake.partial_limit_fraction = .5
    trade = await trading.execute(plan["plan_id"])
    assert Decimal(trade["filled_quantity"]) == Decimal(plan["quantity"]) / 2
    source, interpretation = stop_update(trading, stop="80")
    result = await trading.update_trade(source.event_id, interpretation)
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert result["result"]["phase"] == current["pending_stop_update"]["phase"] == "confirmed"
    assert Decimal(current["stop_price"]) == 80
    risk = Decimal(current["remaining_quantity"]) * (20 + Decimal(100) * reserve_bps / 10000)
    assert risk <= Decimal(trade["reserved_risk"])
    assert current["plan"] == plan and current["reserved_risk"] == trade["reserved_risk"]
