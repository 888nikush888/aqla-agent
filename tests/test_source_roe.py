from decimal import Decimal

import pytest

from aqla_agent.percent_targets import prices
from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    BackupValues,
    Evidence,
    Interpretation,
    Market,
    Policy,
)
from aqla_hyperliquid_mcp.rules import make_plan, verify_evidence


@pytest.mark.parametrize("side,expected", [("long", ["102.5", "105"]), ("short", ["97.5", "95"])])
def test_unlabelled_source_percentages_use_actual_deployed_margin(side, expected):
    value = Interpretation(kind="new_signal", source_refs=["source"], side=side, symbol="BTC", leverage=35,
        stop_loss="90" if side == "long" else "110", take_profits=[50, 100]).model_copy(update={"take_profit_unit": "roe_percent"})
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    plan = make_plan(value, market, AccountSnapshot(equity="10000", available_margin="10000"),
        Policy(leverage_mode="signal"), 1, 1000, ("testnet", "account"), stamp=1000)
    assert plan.signal_leverage == 35 and plan.effective_leverage == 20
    assert plan.take_profits == list(map(Decimal, expected))
    assert plan.source_tp["reference_kind"] == "fill_vwap" and plan.source_tp["margin_leverage"] == "20"
    final = prices("101", side, market, "roe_percent", [50, 100], 20)
    for target, requested in zip(final, [50, 100], strict=True):
        actual = (target - 101) / 101 * 20 * 100 * (1 if side == "long" else -1)
        assert abs(actual - requested) <= Decimal(".01") / 101 * 20 * 100


@pytest.mark.parametrize("side,expected", [("long", "105"), ("short", "95")])
@pytest.mark.parametrize("backup_stop", [False, True])
def test_source_roe_targets_freeze_final_notional_tier_leverage(side, expected, backup_stop):
    stamp = 1000
    market = Market(symbol="BTC", native_symbol="BTCUSDT", asset_id=1, product="perp", size_decimals=3,
        mid="100", max_leverage=20, price_tick=".01", quantity_step=".001", leverage_tiers=[
            {"notional_floor": "0", "notional_cap": "100", "max_leverage": 20},
            {"notional_floor": "100", "notional_cap": "100000", "max_leverage": 10}])
    signal = Interpretation(kind="new_signal", source_refs=["source"], symbol="BTC", side=side,
        leverage=20, entry="limit", entry_prices=[100], take_profits=[50], take_profit_unit="roe_percent",
        stop_loss=None if backup_stop else 90 if side == "long" else 110)
    backup = BackupValues(leverage=20, fallback_stop_loss_percent=40, fallback_stop_loss_unit="roe_percent") if backup_stop else None
    plan = make_plan(signal, market, AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp),
        Policy(leverage_mode="signal"), 1, stamp, ("testnet", "account"), stamp=stamp, backup=backup)
    assert plan.quantity * plan.entry_limit >= 100
    assert plan.effective_leverage == 10 and plan.source_tp["margin_leverage"] == "10"
    assert plan.take_profits == [Decimal(expected)]
    assert list(map(Decimal, plan.source_tp["preview_prices"])) == plan.take_profits
    final = prices(100, side, plan.market, "roe_percent", plan.source_tp["values"], plan.source_tp["margin_leverage"])
    assert final == plan.take_profits
    assert (final[0] - 100) / 100 * plan.effective_leverage * 100 * (1 if side == "long" else -1) == 50


def test_shortened_number_quote_cannot_turn_fifty_percent_into_price(setup, signal):
    _, state, _, _, trading = setup
    source, value = signal
    source = source.model_copy(update={"message_id": 2, "text": "BTC kaufen SL 90 TP 50%"})
    trading.ingest(source)
    value = value.model_copy(update={"source_refs": [source.source_id], "take_profits": [Decimal(50)], "evidence": [
        Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
            ("symbol", "BTC"), ("side", "kaufen"), ("stop_loss", "SL 90"), ("take_profits.0", "50"))]})
    with pytest.raises(AqlaError, match="percentage"):
        verify_evidence(value, state.snapshot()["events"])


@pytest.mark.asyncio
async def test_unlabelled_roe_partial_fill_own_vwap_cap_restart_and_update(setup, signal, monkeypatch):
    import copy

    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import SourceEvent, now_ms
    _, state, fake, exchange, trading = setup
    state.update(lambda data: data["policy"].update(leverage_mode="signal"))
    source = SourceEvent(chat_id="channel", message_id=2, sent_at=now_ms(), text="BTC kaufen Entry 102 SL 90 TP 50% 100% Hebel 35x")
    trading.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", leverage=35,
        entry="limit", entry_prices=[102], stop_loss=90, take_profits=[50, 100], take_profit_unit="roe_percent",
        evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
            ("symbol", "BTC"), ("side", "kaufen"), ("entry_prices.0", "Entry 102"), ("stop_loss", "SL 90"),
            ("take_profits.0", "50%"), ("take_profits.1", "100%"), ("leverage", "35x"))])
    plan = await trading.preview(source.event_id, value)
    assert plan["source_tp"]["reference_entry"] is None and plan["source_tp"]["margin_leverage"] == "20"
    trade = await trading.execute(plan["plan_id"])
    tranche = (Decimal(plan["quantity"]) / 4).quantize(Decimal(".01"))
    fake.fill(trade["entry_cloid"], tranche, "100")
    fake.fill(trade["entry_cloid"], tranche, "102")
    original = exchange.mutate
    async def crash(operation, *args, **kwargs):
        if operation.startswith("targets:"):
            raise SystemExit("simulated crash before TP send")
        return await original(operation, *args, **kwargs)
    monkeypatch.setattr(exchange, "mutate", crash)
    with pytest.raises(SystemExit):
        await trading.recover()
    final = copy.deepcopy(state.snapshot()["trades"][trade["trade_id"]]["percent_finalization"])
    assert Decimal(final["entry_vwap"]) == 101 and fake.orders[trade["entry_cloid"]]["status"] == "canceled"
    for target, requested in zip(final["prices"], [50, 100], strict=True):
        actual = (Decimal(target) - 101) / 101 * 20 * 100
        assert abs(actual - requested) <= Decimal(".01") / 101 * 20 * 100
    monkeypatch.setattr(exchange, "mutate", original)
    restarted = Trading(exchange, state)
    await restarted.recover()
    assert state.snapshot()["trades"][trade["trade_id"]]["percent_finalization"] == final
    update_source = SourceEvent(chat_id="channel", message_id=3, reply_to_id=2, sent_at=now_ms(), text="BTC neue Ziele 60% 120%")
    restarted.ingest(update_source)
    update = Interpretation(kind="trade_update", source_refs=[update_source.source_id], symbol="BTC", side="long",
        update_action="replace_targets", take_profits=[60, 120], take_profit_unit="roe_percent",
        evidence=[Evidence(field=f"take_profits.{i}", source_id=update_source.source_id, quote=f"{value}%") for i, value in enumerate([60, 120])])
    await restarted.update_trade(update_source.event_id, update)
    saved = state.snapshot()["trades"][trade["trade_id"]]
    for target, requested in zip(saved["targets"], [60, 120], strict=True):
        assert abs((Decimal(target) - 101) / 101 * 20 * 100 - requested) <= Decimal(".01") / 101 * 20 * 100
    before = copy.deepcopy(fake.actions)
    await restarted.recover()
    assert fake.actions == before and saved["plan"] == plan


@pytest.mark.parametrize("field,unit,quote,text", [
    ("take_profits.0", "price", "50", "BTC kaufen SL 90 TP 50%"),
    ("take_profits.0", "price", "50", "BTC kaufen SL 90 TP price 50 and 50%"),
    ("take_profits.0", "roe_percent", "50", "BTC kaufen SL 90 TP 50"),
    ("stop_loss", "price", "90", "BTC kaufen SL 90% TP 50"),
])
def test_original_numeric_spans_enforce_percent_vs_price(setup, field, unit, quote, text):
    from aqla_hyperliquid_mcp.models import SourceEvent, now_ms
    _, state, _, _, trading = setup
    source = SourceEvent(chat_id="channel", message_id=2, sent_at=now_ms(), text=text)
    trading.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", stop_loss=90,
        take_profits=[50], take_profit_unit=unit, evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
            ("symbol", "BTC"), ("side", "kaufen"), ("stop_loss", quote if field == "stop_loss" else "90"),
            ("take_profits.0", quote if field != "stop_loss" else "50"))])
    with pytest.raises(AqlaError, match="percentage"):
        verify_evidence(value, state.snapshot()["events"])


def test_precise_absolute_excerpt_in_mixed_source_is_allowed(setup):
    from aqla_hyperliquid_mcp.models import SourceEvent, now_ms
    _, state, _, _, trading = setup
    source = SourceEvent(chat_id="channel", message_id=2, sent_at=now_ms(), text="BTC kaufen SL 90 TP price 50 and 50%")
    trading.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", stop_loss=90,
        take_profits=[50], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
            ("symbol", "BTC"), ("side", "kaufen"), ("stop_loss", "SL 90"), ("take_profits.0", "price 50"))])
    verify_evidence(value, state.snapshot()["events"])


def test_roe_target_label_names_own_margin():
    from aqla_agent.messages import target_label
    label = target_label({"source_tp": {"unit": "roe_percent", "values": ["50"]}}, 0)
    assert "50 % ROE eigene Margin vor Kosten" in label and "Kursbewegung" not in label
