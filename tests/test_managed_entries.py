from decimal import Decimal

import httpx
import pytest

from aqla_agent.managed_entries import cap_quantity, position_basis
from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    Evidence,
    Interpretation,
    Market,
    Policy,
    SourceEvent,
    TradePlan,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import make_plan


def zone(side="long"):
    return Interpretation(kind="new_signal", source_refs=["source"], symbol="BTC", side=side, entry="range",
        entry_prices=[90, 100], stop_loss=80 if side == "long" else 110,
        take_profits=[120, 130] if side == "long" else [70, 60])


@pytest.mark.parametrize("side", ["long", "short"])
def test_zone_partitions_one_budget_and_round_trips(side):
    plan = make_plan(zone(side), Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20),
        AccountSnapshot(equity=10000, available_margin=10000), Policy(entry_range_choice="split"), 1, now_ms(), ("testnet", "account"))
    assert len(plan.entry_legs) == 2
    assert sum(leg.quantity for leg in plan.entry_legs) == plan.quantity
    assert {leg.price for leg in plan.entry_legs} == {Decimal(90), Decimal(100)}
    assert plan.entry_legs[0].price == (100 if side == "long" else 90)
    assert plan.risk_usdc <= 50
    assert TradePlan.model_validate(plan.model_dump(mode="json")) == plan


def test_tiny_second_entry_is_not_dropped_or_enlarged():
    with pytest.raises(AqlaError, match="entry leg|minimum|TP leg"):
        make_plan(zone(), Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20, min_notional=40),
            AccountSnapshot(equity=100, available_margin=100), Policy(entry_range_choice="split"), 1, now_ms(), ("testnet", "account"))


@pytest.mark.parametrize("unit", ["price", "roe_percent"])
def test_adaptive_targets_fit_each_entry_without_increasing_quantity_or_risk(unit):
    signal = zone().model_copy(update={"take_profits": [Decimal(v) for v in ([120, 130, 140, 150] if unit == "price" else [50, 100, 150, 200])], "take_profit_unit": unit})
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20, min_notional=30)
    account = AccountSnapshot(equity=10000, available_margin=10000)
    policy = Policy(entry_range_choice="split", take_profit_allocation="adaptive")
    stamp = now_ms()
    plan = make_plan(signal, market, account, policy, 1, stamp, ("testnet", "account"), stamp=stamp)
    assert len(plan.entry_legs) == 2 and len(plan.take_profits) == 3
    assert plan.risk_usdc <= 50 and plan.quantity <= Decimal("2.50") and plan.stop_loss == 80
    from aqla_hyperliquid_mcp.rules import split_targets
    for leg in plan.entry_legs:
        sizes = split_targets(leg.quantity, len(plan.take_profits), policy, market)
        assert all(size * price >= market.min_notional for size, price in zip(sizes, plan.take_profits, strict=True))
    if unit == "roe_percent":
        assert plan.source_tp["values"] == ["50", "100", "150"]
        assert len(plan.source_tp["preview_prices"]) == 3
    else:
        assert plan.take_profits == [120, 130, 140]
    smaller = cap_quantity(plan, Decimal(".8"))
    assert len(smaller.entry_legs) == 2 and len(smaller.take_profits) == 1
    assert smaller.risk_usdc < plan.risk_usdc and smaller.quantity == Decimal(".8")
    if unit == "roe_percent":
        assert smaller.source_tp["values"] == ["50"]


def test_fixed_target_allocation_is_not_silently_made_adaptive_for_split_entries():
    signal = zone().model_copy(update={"take_profits": [Decimal(v) for v in [120, 130, 140, 150]]})
    with pytest.raises(AqlaError, match="TP leg|minimum"):
        make_plan(signal, Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20, min_notional=30),
                  AccountSnapshot(equity=10000, available_margin=10000), Policy(entry_range_choice="split", take_profit_allocation="halving"),
                  1, now_ms(), ("testnet", "account"))


def test_short_zone_caps_notional_and_margin_for_both_favorable_entries():
    signal = Interpretation(kind="new_signal", source_refs=["source"], symbol="SOL", side="short", entry="range",
        entry_prices=["105.4", "108.862"], stop_loss="110.9043", take_profits=["102.238", "99.076"])
    plan = make_plan(signal, Market(symbol="SOL", asset_id=0, product="perp", mid=107, size_decimals=3, max_leverage=20),
        AccountSnapshot(equity=10000, available_margin=50), Policy(entry_range_choice="split", leverage=10),
        1, now_ms(), ("testnet", "account"))
    assert plan.quantity * plan.stop_loss <= 500
    assert sum(leg.quantity * leg.limit for leg in plan.entry_legs) <= 500


def source_zone(trading):
    source = SourceEvent(chat_id="channel", message_id=7, sent_at=now_ms(), text="BTC LONG\nEntry zone: 90 - 100\nSL: 80\nTP1: 120\nTP2: 130")
    trading.ingest(source)
    value = zone()
    value.source_refs = [source.source_id]
    value.evidence = [Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
        ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "90"), ("entry_prices.1", "100"),
        ("stop_loss", "80"), ("take_profits.0", "120"), ("take_profits.1", "130"))]
    return source, value


async def opened_zone(setup):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    source, value = source_zone(trading)
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    return state, fake, trading, plan, trade


@pytest.mark.asyncio
async def test_second_entry_waits_for_first_fill_and_confirmed_protection(setup):
    state, fake, trading, plan, trade = await opened_zone(setup)
    assert trade["lifecycle_version"] == 2
    assert len([order for order in fake.orders.values() if not order["order"]["reduceOnly"]]) == 1
    first, second = trade["entry_orders"]
    fake.fill(first["cloid"], first["quantity"], first["price"])
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "open"
    assert second["cloid"] in fake.orders
    assert Decimal(current["remaining_quantity"]) == Decimal(first["quantity"])
    assert Decimal(current["reserved_risk"]) <= Decimal(plan["risk_usdc"])
    sends = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    assert len(fake.actions) == sends
    fake.mid = second["price"]
    fake.fill(second["cloid"], second["quantity"], second["price"])
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert Decimal(current["remaining_quantity"]) == Decimal(plan["quantity"])
    assert Decimal(current["source_entry_vwap"]) == sum(Decimal(row["quantity"]) * Decimal(row["price"]) for row in (first, second)) / Decimal(plan["quantity"])


@pytest.mark.asyncio
async def test_adaptive_split_protects_the_first_fill_before_sending_second_entry(setup, monkeypatch):
    _, state, fake, exchange, trading = setup
    handle = fake.handle
    def boundary(request):
        response = handle(request)
        if request.url.path == "/info" and '"clearinghouseState"' in request.content.decode():
            data = response.json()
            data.update(marginSummary={"accountValue": "3000"}, withdrawable="3000")
            return httpx.Response(200, json=data)
        return response
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(boundary))
    state.update(lambda data: data["policy"].update(entry_range_choice="split", take_profit_allocation="adaptive"))
    text = "BTC LONG\nEntry zone: 90 - 100\nSL: 80\nTP1: 120\nTP2: 130\nTP3: 140\nTP4: 150"
    source = SourceEvent(chat_id="channel", message_id=8, sent_at=now_ms(), text=text)
    trading.ingest(source)
    value = zone().model_copy(update={"source_refs": [source.source_id], "take_profits": [Decimal(v) for v in [120, 130, 140, 150]]})
    value.evidence = [Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
        ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "90"), ("entry_prices.1", "100"), ("stop_loss", "80"),
        ("take_profits.0", "120"), ("take_profits.1", "130"), ("take_profits.2", "140"), ("take_profits.3", "150"))]
    raw = await trading.preview(source.event_id, value)
    assert len(raw["take_profits"]) == 3
    trade = await trading.execute(raw["plan_id"])
    first, second = trade["entry_orders"]
    fake.fill(first["cloid"], first["quantity"], first["price"])
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "open" and second["cloid"] in fake.orders
    targets = [row for row in current["orders"] if row["kind"] == "tp" and not row.get("retired")]
    assert len(targets) == 3
    assert all(Decimal(row["quantity"]) * Decimal(row["price"]) >= 10 for row in targets)
    assert sum(Decimal(row["quantity"]) for row in targets) == Decimal(first["quantity"])


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_unsent_leverage_guard_does_not_strand_a_managed_trade(setup, monkeypatch, permanent):
    from aqla_agent.observations import TemporaryObservation
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    original = trading.live_entry_guard
    failed = False
    reads = {}
    async def guard(plan, **kwargs):
        nonlocal failed
        operation = kwargs.get("dispatch_operation", "")
        reads[operation] = reads.get(operation, 0) + 1
        if not failed and operation.startswith("leverage:") and reads[operation] == 3:
            failed = True
            if permanent:
                raise AqlaError("Shared account risk no longer permits this entry")
            raise TemporaryObservation("read_unavailable", "inventory", "transport")
        return await original(plan, **kwargs)
    monkeypatch.setattr(trading, "live_entry_guard", guard)
    source, value = source_zone(trading)
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    prior = state.snapshot()["operations"]["leverage:" + trade["trade_id"]]
    assert prior["phase"] == "rejected" and prior["outcome"] == "not_sent" and not fake.orders
    if permanent:
        assert trade["status"] == "rejected" and trade["reserved_risk"] == "0"
        return
    assert trade["managed_entry_retry"]["attempts"] == 1
    state.update(lambda data: data["trades"][trade["trade_id"]]["managed_entry_retry"].update(next_retry_at=0))
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "opening" and current["plan"]["deadline_at"] == plan["deadline_at"]
    assert state.snapshot()["operations"]["leverage:" + trade["trade_id"]] == prior
    assert current["leverage_operation_id"] != "leverage:" + trade["trade_id"]
    assert current["entry_orders"][0]["cloid"] == trade["entry_orders"][0]["cloid"]
    sent = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    assert len(fake.actions) == sent


@pytest.mark.asyncio
async def test_managed_unsent_configuration_retry_keeps_the_five_attempt_budget(setup, monkeypatch):
    from aqla_agent.observations import TemporaryObservation
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    original = trading.live_entry_guard
    reads = {}
    async def unavailable(plan, **kwargs):
        operation = kwargs.get("dispatch_operation", "")
        reads[operation] = reads.get(operation, 0) + 1
        if reads[operation] == 3:
            raise TemporaryObservation("read_unavailable", "inventory", "transport")
        return await original(plan, **kwargs)
    monkeypatch.setattr(trading, "live_entry_guard", unavailable)
    source, value = source_zone(trading)
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    for _ in range(4):
        state.update(lambda data: data["trades"][trade["trade_id"]]["managed_entry_retry"].update(next_retry_at=0))
        await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "rejected" and current["reserved_risk"] == "0"
    assert current["managed_entry_retry"]["exhausted"] and current["managed_entry_retry"]["attempts"] == 5
    assert len(current["leverage_attempts"]) == 4 and not fake.actions and not fake.orders


@pytest.mark.asyncio
async def test_unknown_leverage_result_is_never_retried_as_a_locally_unsent_request(setup):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    source, value = source_zone(trading)
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    # Isolated restart snapshot: configuration may have reached the venue, entry has not.
    identity = "leverage:" + trade["trade_id"]
    def interrupted(data):
        current = data["trades"][trade["trade_id"]]
        current["entry_orders"][0]["phase"] = "pending"
        current["orders"] = []
        data["operations"].pop(current["entry_orders"][0]["operation_id"])
        data["operations"][identity].update(phase="unknown", outcome="unknown")
    state.update(interrupted)
    before = len(fake.actions)
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "unknown" and not current.get("leverage_attempts")
    assert len(fake.actions) == before and state.snapshot()["operations"][identity]["phase"] == "unknown"


@pytest.mark.asyncio
async def test_tp1_cross_without_fill_cancels_all_original_entries(setup):
    state, fake, trading, _, trade = await opened_zone(setup)
    fake.mid = "121"
    await trading.refresh_trade(trade["trade_id"])
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "rejected"
    assert current["entries_disabled"]
    assert all(row["status"] != "open" for row in fake.orders.values())
    assert fake.position == 0


def test_wave_turnover_does_not_inflate_current_position():
    fills = {str(index): {"kind": kind, "sz": str(size), "px": str(price), "fee": ".01", "time": index,
             "tid": index, "oid": index} for index, (kind, size, price) in enumerate([
                 ("entry", 2, 100), ("waverider", 1, 110), ("entry", 1, 105)])}
    amount, average, realized, fees = position_basis({"plan": {"side": "long"}, "fill_events": fills})
    assert (amount, average, realized, fees) == (Decimal(2), Decimal("102.5"), Decimal(10), Decimal(".03"))
