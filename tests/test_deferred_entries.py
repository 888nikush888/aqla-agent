"""Delayed LINK reproduction, executable-price guards and immutable lifecycle facts."""

import copy
import json
from decimal import Decimal

import pytest
from routing_fixture import routed  # noqa: F401 -- shared independent-target fixture

from aqla_agent.entry import PriceSnapshot
from aqla_agent.lifecycle import close_reason
from aqla_agent.messages import telegram_view
from aqla_agent.pipelines import TradingProfile, route_control
from aqla_agent.router import Router
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    Interpretation,
    SourceEvent,
    TradePlan,
    digest,
    now_ms,
    require_first,
)
from aqla_hyperliquid_mcp.rules import verify_evidence


def quote(price="100", *, bid=None, ask=None, mark=None, age=0):
    stamp = now_ms() - age
    return PriceSnapshot(market="BTC", bid=bid or price, ask=ask or price, mid=price,
                         stop_reference=mark or price, stop_reference_kind="mark", exchange_time=stamp, started_at=stamp, observed_at=stamp)


def delayed_source(trading, *, entry="100", stop="110", targets=("90", "80"), side="short", message=1, upper=None):
    text = f"BTC {side} Entry {entry} SL {stop} TP {' '.join(targets)}. Deferred entry, not now, exactly at entry. Exchange Bybit."
    if upper:
        text += f" Range upper {upper}."
    source = SourceEvent(chat_id="channel", message_id=message, sent_at=now_ms(), text=text)
    trading.ingest(source)
    proof = [("symbol", "BTC"), ("side", side), ("entry_prices.0", f"Entry {entry}"), ("stop_loss", f"SL {stop}"),
             ("entry_timing", "Deferred entry, not now, exactly at entry"), ("source_exchange", "Exchange Bybit")]
    proof += [(f"take_profits.{index}", value) for index, value in enumerate(targets)]
    if upper:
        proof.append(("entry_prices.1", f"Range upper {upper}"))
    signal = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side=side, entry="range" if upper else "limit",
                            entry_timing="deferred", source_exchange="Bybit", entry_prices=[entry, upper] if upper else [entry], stop_loss=stop, take_profits=list(targets),
                            evidence=[Evidence(field=field, quote=value, source_id=source.source_id) for field, value in proof])
    return source, signal


@pytest.mark.asyncio
async def test_link_delayed_entry_waits_even_beyond_future_sl_without_orders(setup):
    _, state, fake, _, trading = setup
    fake.mid = "13.3615"
    source, value = delayed_source(trading, entry="13.052", stop="13.328", targets=("12.854", "12.723"))
    saved = await trading.preview(source.event_id, value)
    trade = await trading.execute(saved["plan_id"])
    assert trade["status"] == "waiting_entry"
    assert fake.actions == [] and state.snapshot()["operations"] == {}
    assert Decimal(trade["reserved_risk"]) > 0
    assert trade["plan"]["entry_gate"]["direction"] == "down"
    assert trade["plan"]["source_exchange"] == "Bybit"
    assert trade["plan"]["stop_loss"] == "13.328"
    assert (await trading.execute(saved["plan_id"]))["trade_id"] == trade["trade_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side,origin,stop,targets", [("short", "105", "110", ("90", "80")), ("long", "95", "90", ("110", "120")),
                                                    ("short", "95", "110", ("90", "80")), ("long", "105", "90", ("110", "120"))])
async def test_waiting_from_both_directions_reaches_entry_uses_ioc_and_stable_identity(setup, side, origin, stop, targets):
    _, state, fake, exchange, trading = setup
    fake.mid = origin
    source, value = delayed_source(trading, side=side, stop=stop, targets=targets)
    plan = await trading.preview(source.event_id, value)
    waiting = await trading.execute(plan["plan_id"])
    assert waiting["status"] == "waiting_entry" and not fake.actions
    fake.mid = "100"
    restarted = Trading(exchange, state)
    await restarted.recover()
    current = state.snapshot()["trades"][waiting["trade_id"]]
    assert current["status"] == "open"
    entries = [action for action in fake.actions if action.get("grouping") == "normalTpsl"]
    assert len(entries) == 1 and entries[0]["orders"][0]["t"] == {"limit": {"tif": "Ioc"}}
    await restarted.recover()
    assert len([action for action in fake.actions if action.get("grouping") == "normalTpsl"]) == 1
    assert [Decimal(price) for price in current["plan"]["take_profits"]] == [Decimal(price) for price in targets]
    assert Decimal(current["plan"]["stop_loss"]) == Decimal(stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("side,origin,missed,stop,targets", [("short", "105", "99", "110", ("90", "80")), ("long", "95", "101", "90", ("110", "120"))])
async def test_missed_crossing_after_restart_is_not_chased(setup, side, origin, missed, stop, targets):
    _, state, fake, _, trading = setup
    fake.mid = origin
    source, value = delayed_source(trading, side=side, stop=stop, targets=targets)
    saved = await trading.preview(source.event_id, value)
    waiting = await trading.execute(saved["plan_id"])
    fake.mid = missed
    await trading.recover()
    row = state.snapshot()["trades"][waiting["trade_id"]]
    assert row["status"] == "cancelled" and row["reserved_risk"] == "0" and not fake.actions
    assert "missed" in row["unsent_reason"]


@pytest.mark.asyncio
async def test_normal_marketable_limit_cannot_fill_beyond_stop(setup):
    _, _, fake, _, trading = setup
    fake.mid = "13.3615"
    source, signal = delayed_source(trading, entry="13.052", stop="13.328", targets=("12.854", "12.723"))
    signal.entry_timing = "standard"
    with pytest.raises(AqlaError, match="Live entry/SL/TP"):
        await trading.preview(source.event_id, signal)
    assert not fake.actions


@pytest.mark.asyncio
async def test_last_guard_rejects_race_before_dispatch_and_nonce_is_not_reused(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    original = exchange.price_snapshot
    async def price(market):
        result = await original(market)
        operations = state.snapshot()["operations"]
        if any(key.startswith("entry:") and row["phase"] == "prepared" for key, row in operations.items()):
            result.stop_reference = Decimal("80")
        return result
    monkeypatch.setattr(exchange, "price_snapshot", price)
    row = await trading.execute(plan["plan_id"])
    assert row["status"] == "rejected" and row["reserved_risk"] == "0"
    assert not any(action["type"] == "order" for action in fake.actions)
    op = state.snapshot()["operations"][row["entry_operation_id"]]
    assert op["phase"] == "rejected" and op["outcome"] == "not_sent"
    await trading.execute(plan["plan_id"])
    assert state.snapshot()["operations"][row["entry_operation_id"]]["nonce"] == op["nonce"]


@pytest.mark.asyncio
async def test_owner_pause_during_final_price_await_blocks_prepared_order(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, value = signal
    stamp = now_ms()
    # Exercise the owner-pause race independently of disk-induced quote expiry.
    monkeypatch.setattr("aqla_hyperliquid_mcp.models.time.time_ns", lambda: stamp * 1_000_000)
    plan = await trading.preview(source.event_id, value)
    original = exchange.price_snapshot
    async def paused(market):
        snapshot = await original(market)
        if any(identity.startswith("entry:") and row["phase"] == "prepared" for identity, row in state.snapshot()["operations"].items()):
            state.update(lambda data: data.update(paused=True))
        return snapshot
    monkeypatch.setattr(exchange, "price_snapshot", paused)
    row = await trading.execute(plan["plan_id"])
    assert state.snapshot()["paused"] is True
    assert row["status"] == "rejected"
    assert state.snapshot()["operations"][row["entry_operation_id"]]["outcome"] == "not_sent"
    assert not any(action["type"] == "order" for action in fake.actions)


@pytest.mark.asyncio
async def test_definitely_unfilled_deferred_ioc_is_not_resent(setup):
    _, state, fake, exchange, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading)
    plan = await trading.preview(source.event_id, value)
    waiting = await trading.execute(plan["plan_id"])
    original = fake.handle
    def empty(request):
        payload = json.loads(request.content)
        if request.url.path == "/exchange" and payload["action"].get("grouping") == "normalTpsl":
            response = original(request)
            fake.fills = []
            fake.position = 0
            fake.orders[waiting["entry_cloid"]]["status"] = "canceled"
            fake.orders[waiting["entry_cloid"]]["order"]["sz"] = fake.orders[waiting["entry_cloid"]]["order"]["origSz"]
            return response
        return original(request)
    exchange.client._transport = __import__("httpx").MockTransport(empty)
    fake.mid = "100"
    await trading.recover()
    await trading.recover()
    assert state.snapshot()["trades"][waiting["trade_id"]]["status"] == "rejected"
    assert "without any confirmed fill" in state.snapshot()["trades"][waiting["trade_id"]]["unsent_reason"]
    assert len([action for action in fake.actions if action.get("grouping") == "normalTpsl"]) == 1


@pytest.mark.asyncio
async def test_deferred_partial_ioc_has_confirmed_remainder_and_replacement_sl(setup):
    _, state, fake, exchange, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading)
    plan = await trading.preview(source.event_id, value)
    waiting = await trading.execute(plan["plan_id"])
    original = fake.handle
    def partial(request):
        payload = json.loads(request.content)
        response = original(request)
        if request.url.path == "/exchange" and payload["action"].get("grouping") == "normalTpsl":
            full = Decimal(fake.fills[0]["sz"])
            half = (full / 2).quantize(Decimal(".01"))
            fake.fills[0]["sz"] = str(half)
            fake.position = -float(half)
            entry = fake.orders[waiting["entry_cloid"]]
            entry["status"] = "canceled"
            entry["order"]["sz"] = str(full - half)
            fake.orders[require_first(key for key in fake.orders if key != waiting["entry_cloid"])]["status"] = "canceled"
        return response
    exchange.client._transport = __import__("httpx").MockTransport(partial)
    fake.mid = "100"
    await trading.recover()
    current = state.snapshot()["trades"][waiting["trade_id"]]
    assert current["status"] == "open"
    assert Decimal(current["filled_quantity"]) < Decimal(plan["quantity"])
    assert Decimal(current["entry_accounting"]["entry_quantity"]) == Decimal(current["filled_quantity"])
    assert any(row["kind"] == "sl" and fake.orders[row["cloid"]]["status"] == "open" for row in current["orders"])


@pytest.mark.asyncio
async def test_paused_waiting_entry_resumes_but_expired_or_changed_source_does_not(setup):
    _, state, fake, _, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading)
    saved = await trading.preview(source.event_id, value)
    row = await trading.execute(saved["plan_id"])
    state.update(lambda data: data.update(paused=True))
    fake.mid = "100"
    await trading.recover()
    assert state.snapshot()["trades"][row["trade_id"]]["status"] == "waiting_entry" and not fake.actions
    state.update(lambda data: data["trades"][row["trade_id"]]["plan"].update(deadline_at=now_ms() - 1))
    await trading.recover()
    assert state.snapshot()["trades"][row["trade_id"]]["status"] == "cancelled" and not fake.actions


@pytest.mark.asyncio
async def test_waiting_blocks_same_market_and_profile_revision_cancels_it(setup):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    fake.mid = "105"
    source, value = delayed_source(router)
    preview = await router.preview(source.event_id, value)
    row = await router.execute(preview["plan_id"])
    second, other = delayed_source(router, message=2)
    with pytest.raises(AqlaError, match="active trade"):
        await router.preview(second.event_id, other)
    profile = state.snapshot()["profiles"]["standard"]
    profile.pop("revision")
    profile["parameters"]["leverage"] = 3
    state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=TradingProfile.model_validate(profile)))
    await router.recover()
    assert state.snapshot()["trades"][row["trade_id"]]["status"] == "cancelled" and not fake.actions


@pytest.mark.asyncio
async def test_unavailable_wait_quote_does_not_make_account_recovery_fail(setup, monkeypatch):
    _, state, fake, exchange, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading)
    saved = await trading.preview(source.event_id, value)
    row = await trading.execute(saved["plan_id"])
    async def missing(*_):
        raise AqlaError("Fresh prices unavailable")
    monkeypatch.setattr(exchange, "price_snapshot", missing)
    await trading.recover()
    assert trading.recovery_checked
    assert state.snapshot()["trades"][row["trade_id"]]["waiting_error"] == "Fresh prices unavailable"


@pytest.mark.parametrize("age", [2001, -100])
def test_quote_staleness_and_clock_skew_block_entry(age):
    with pytest.raises(AqlaError, match="timestamp|stale"):
        quote(age=age).fresh()


def test_entry_timing_and_source_exchange_require_original_evidence(setup):
    _, state, _, _, trading = setup
    _source, value = delayed_source(trading)
    for field in ("entry_timing", "source_exchange"):
        invalid = value.model_copy(deep=True)
        invalid.evidence = [proof for proof in invalid.evidence if proof.field != field]
        with pytest.raises(AqlaError, match="evidence"):
            verify_evidence(invalid, state.snapshot()["events"])
    assert "entry_timing" not in value.model_copy(update={"entry_timing": "standard"}).model_dump(mode="json")


@pytest.mark.asyncio
async def test_legacy_plan_fields_hashes_remain_unchanged(setup, signal):
    _, _, _, _, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    assert "entry_timing" not in plan and "entry_gate" not in plan and "source_exchange" not in plan
    assert TradePlan.model_validate(plan).model_dump(mode="json") == plan
    assert plan["plan_id"] == digest({key: item for key, item in plan.items() if key != "plan_id"})


@pytest.mark.asyncio
async def test_immediate_entry_and_stop_exit_produce_separate_frozen_messages(setup, signal):
    _, state, fake, _, trading = setup
    source, value = signal
    saved = await trading.preview(source.event_id, value)
    row = await trading.execute(saved["plan_id"])
    entry_notice = copy.deepcopy(state.snapshot()["notifications"]["lifecycle:" + row["trade_id"] + ":entry_fill"])
    sl = require_first(order for order in row["orders"] if order["kind"] == "sl")
    native = fake.orders[sl["cloid"]]
    native["status"] = "filled"
    fake.fills.append({"oid": native["order"]["oid"], "tid": 888, "coin": "BTC", "time": min(f["time"] for f in fake.fills) + 500,
                       "sz": row["filled_quantity"], "px": "90", "fee": "0.1", "feeToken": "USDC"})
    fake.position = 0
    await trading.refresh_trade(row["trade_id"])
    snapshot = state.snapshot()
    assert snapshot["notifications"]["lifecycle:" + row["trade_id"] + ":entry_fill"] == entry_notice
    cards = telegram_view(snapshot, {})["messages"]
    texts = ["\n".join(card["texts"]) for card in cards]
    first = require_first(index for index, text in enumerate(texts) if "Einstieg ausgeführt" in text)
    last = require_first(index for index, text in enumerate(texts) if "Trade geschlossen" in text)
    assert first < last and "Stop-Loss" in texts[last]
    assert "100 USDC" in texts[first] and "Schutz:" in texts[first]
    notice_ids = [identity for card in cards for identity in card["notification_ids"]]
    assert len(notice_ids) == len(set(notice_ids))
    state.update(lambda data: data["notifications"].clear())
    await trading.recover()
    assert not any("lifecycle" in notice for notice in state.snapshot()["notifications"].values())


@pytest.mark.asyncio
async def test_postfill_price_violation_closes_own_position_and_pauses_only_venue(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    original = fake.handle
    def crossed(request):
        result = original(request)
        if request.url.path == "/info" and json.loads(request.content).get("type") == "userFillsByTime" and fake.position:
            for fill in fake.fills:
                if fill["px"] == "100":
                    fill["px"] = "80"
        return result
    # Change the actual entry fill record immediately after execution, before capture.
    def handle(request):
        result = original(request)
        if request.url.path == "/exchange" and json.loads(request.content)["action"].get("grouping") == "normalTpsl":
            for fill in fake.fills:
                fill["px"] = "80"
        return result
    exchange.client._transport = __import__("httpx").MockTransport(handle)
    source, value = signal
    plan = await router.preview(source.event_id, value)
    row = await router.execute(plan["plan_id"])
    assert row["status"] in ("closing", "cleanup", "closed")
    assert state.snapshot()["venue_enabled"]["hyperliquid"] is False
    assert fake.position == 0


def test_close_reason_does_not_invent_unproved_exit():
    assert close_reason({"fill_events": {}})["kind"] == "unconfirmed"




@pytest.mark.asyncio
async def test_delayed_range_uses_saved_profile_range_choice(setup):
    _, _, fake, _, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading, entry="99", upper="101")
    plan = await trading.preview(source.event_id, value)
    assert Decimal(plan["entry_price"]) == 100
    assert (await trading.execute(plan["plan_id"]))["status"] == "waiting_entry"




@pytest.mark.asyncio
async def test_delayed_lost_entry_result_recovers_without_second_order(setup):
    _, state, fake, _, trading = setup
    fake.mid = "105"
    source, value = delayed_source(trading)
    plan = await trading.preview(source.event_id, value)
    waiting = await trading.execute(plan["plan_id"])
    fake.mid = "100"
    fake.lose_entry_response = True
    await trading.recover()
    await trading.recover()
    assert len([action for action in fake.actions if action.get("grouping") == "normalTpsl"]) == 1
    assert Decimal(state.snapshot()["trades"][waiting["trade_id"]]["filled_quantity"]) > 0


@pytest.mark.asyncio
async def test_spread_and_mark_divergence_are_checked_separately(setup, signal):
    _, _, _, _, trading = setup
    source, value = signal
    raw = await trading.preview(source.event_id, value)
    plan = TradePlan.model_validate(raw)
    from aqla_agent.entry import validate_entry_prices
    with pytest.raises(AqlaError, match="executable|Executable"):
        validate_entry_prices(plan, quote(bid="99", ask="101"))
    with pytest.raises(AqlaError, match="structure"):
        validate_entry_prices(plan, quote(mark="89"))


@pytest.mark.asyncio
async def test_diagnostics_are_read_only_and_include_exact_wire_type(setup, signal):
    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, trading = setup
    source, value = signal
    saved = await trading.preview(source.event_id, value)
    trade = await trading.execute(saved["plan_id"])
    server = create_server(settings, state=state, exchange=exchange)
    before = state.snapshot()
    actions = copy.deepcopy(fake.actions)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["trade_diagnostics"].annotations.read_only_hint
    result = await server.call_tool("trade_diagnostics", {"trade_id": trade["trade_id"]})
    assert result.is_error is not True and "Ioc" in str(result)
    assert (await server.call_tool("trade_diagnostics", {"symbol": "BTC"})).is_error is not True
    assert state.snapshot() == before and fake.actions == actions
    await close_server(server)
