import copy
from decimal import Decimal

import httpx
import pytest
from conftest import FakeExchange
from test_monthly import data as monthly_data
from test_monthly import trade as monthly_trade

from aqla_agent.percent_targets import prices
from aqla_agent.pipelines import Pipeline, TradingProfile, route_control
from aqla_agent.ranking import RankingPolicy, evaluate, metrics, outcomes, snapshot_rank, weighted_parameters
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    ExecutionSettings,
    Interpretation,
    Market,
    SourceEvent,
    SymbolUnavailable,
    now_ms,
)
from aqla_hyperliquid_mcp.state import State


def rank_data(tier="F"):
    return {"ranking_policy": RankingPolicy(enabled=True).model_dump(mode="json"), "ranking_revision": 1,
            "source_ranks": {"channel": {"tier": tier, "bootstrap": tier == "C", "revision": 1, "evaluated": [], "up": None, "down": 0}},
            "ranking_history": {}, "notifications": {}, "trades": {}, "source_aliases": {}}


def change_ranking(state, enabled, tier=None):
    def change(data):
        data["ranking_policy"]["enabled"] = enabled
        data["ranking_revision"] += 1
        if tier is not None:
            data["source_ranks"]["channel"]["tier"] = tier
    state.update(change)


@pytest.mark.asyncio
async def test_old_plan_rejected_after_ranking_enable(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    await router.recover()
    source, interpretation = signal
    plan = await router.preview(source.event_id, interpretation)
    assert plan.get("rank_snapshot") is None
    change_ranking(state, True, "F")
    with pytest.raises(AqlaError, match="[Rr]anking.*changed"):
        await router.execute(plan["plan_id"])
    assert fake.actions == []
    assert not state.snapshot()["trades"]
    await router.close()


@pytest.mark.asyncio
async def test_old_ranked_plan_rejected_after_ranking_disable(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    change_ranking(state, True, "D")
    await router.recover()
    source, interpretation = signal
    plan = await router.preview(source.event_id, interpretation)
    assert plan["rank_snapshot"]["policy_revision"] == state.snapshot()["ranking_revision"]
    change_ranking(state, False)
    with pytest.raises(AqlaError, match="[Rr]anking.*changed"):
        await router.execute(plan["plan_id"])
    assert fake.actions == []
    assert not state.snapshot()["trades"]
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_ranking_toggle_during_preview_invalidates_weighted_context(setup, signal, monkeypatch, enabled):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    if enabled:
        change_ranking(state, True, "D")
    original = exchange.market
    changed = False

    async def toggle(*args, **kwargs):
        nonlocal changed
        market = await original(*args, **kwargs)
        if not changed:
            changed = True
            change_ranking(state, not enabled, "F")
        return market

    monkeypatch.setattr(exchange, "market", toggle)
    source, interpretation = signal
    with pytest.raises(AqlaError, match="changed.*preview|preview.*changed"):
        await router.preview(source.event_id, interpretation)
    assert not state.snapshot()["plans"]
    assert fake.actions == []
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_component", ["profile", "pipeline"])
async def test_route_changed_during_preview_cannot_stamp_old_risk_as_current(setup, signal, monkeypatch, changed_component):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    profile = state.snapshot()["profiles"]["standard"]
    selected = TradingProfile(id="standard", name=profile["name"], parameters={**profile["parameters"], "risk_percent": "2"})
    state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=selected))
    before = state.snapshot()["profiles"]["standard"]["revision"]
    original = exchange.market
    changed = False

    async def revise(*args, **kwargs):
        nonlocal changed
        market = await original(*args, **kwargs)
        if not changed:
            changed = True
            if changed_component == "profile":
                revised = selected.model_copy(update={"parameters": selected.parameters.model_copy(update={"risk_percent": Decimal("0.1")})})
                state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=revised))
            else:
                pipeline = Pipeline.model_validate({key: value for key, value in state.snapshot()["pipelines"]["legacy"].items() if key != "revision"})
                state.update(lambda data: route_control(data, "set_pipeline", expected_revision=data["configuration_revision"], pipeline=pipeline))
        return market

    monkeypatch.setattr(exchange, "market", revise)
    source, interpretation = signal
    with pytest.raises(AqlaError, match="changed.*preview|preview.*changed"):
        await router.preview(source.event_id, interpretation)
    assert state.snapshot()["profiles"]["standard"]["revision"] == before + (changed_component == "profile")
    assert not state.snapshot()["plans"]
    assert fake.actions == []
    await router.close()


def evidence(value, index):
    return {"setup_id": str(index), "closed_at": index, "net_r": str(value), "trade_ids": ["trade" + str(index)]}


def test_ranking_formula_cost_sensitive_drawdown_and_no_fake_empty_score():
    policy = RankingPolicy()
    assert metrics([], policy)["score"] is None
    result = metrics([evidence("1", 1), evidence("-.5", 2), evidence("-.5", 3), evidence("1", 4)], policy)
    assert Decimal(result["mean_r"]) == Decimal(".25")
    assert Decimal(result["profit_factor"]) == 2
    assert Decimal(result["drawdown_r"]) == 1
    expected = 100 * (Decimal(".4") * Decimal(".5") + Decimal(".25") * (Decimal(1) / Decimal("1.5"))
                      + Decimal(".2") * Decimal(".8") + Decimal(".15") * Decimal(".5"))
    assert Decimal(result["score"]) == expected
    assert metrics([evidence(1, 1)], policy)["profit_factor"] == "infinite"
    assert metrics([evidence(0, 1)], policy)["profit_factor"] == "0"


def test_f_to_c_requires_twenty_distinct_setups_and_restart_never_recounts(monkeypatch):
    snapshot = rank_data()
    rows = []
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: ({"channel": rows}, {}))
    for number in range(1, 21):
        rows.append(evidence(1, number))
        evaluate(snapshot)
        if number in (5, 9):
            assert snapshot["source_ranks"]["channel"]["tier"] == "F"
        if number == 10:
            assert snapshot["source_ranks"]["channel"]["tier"] == "E"
        if number == 15:
            assert snapshot["source_ranks"]["channel"]["tier"] == "D"
    assert snapshot["source_ranks"]["channel"]["tier"] == "C"
    frozen = copy.deepcopy(snapshot)
    evaluate(snapshot)
    assert snapshot == frozen


def test_hard_drawdown_demotes_mainnet_immediately_but_does_not_promote_f(monkeypatch):
    rows = [evidence(-1, number) for number in range(5)]
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: ({"channel": rows}, {}))
    snapshot = rank_data("S")
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["tier"] in ("D", "E")  # Faster normal demotions may precede the hard stop.
    assert snapshot_rank(snapshot, "channel")["network"] == "testnet"
    first = rank_data("F")
    evaluate(first)
    assert first["source_ranks"]["channel"]["tier"] == "F"


def test_rank_weighting_keeps_testnet_cap_and_scales_fixed_notional():
    base = {"risk_percent": "5", "max_notional_quote": "500", "fixed_notional_quote": "100"}
    assert weighted_parameters(base, {"factor": ".25", "network": "testnet"}) == {
        "risk_percent": "1.25", "max_notional_quote": "500", "fixed_notional_quote": "25.00"}
    weighted = weighted_parameters(base, {"factor": "2", "network": "mainnet", "tier": "S"})
    assert Decimal(weighted["risk_percent"]) == 10 and Decimal(weighted["max_notional_quote"]) == 1000
    assert base["risk_percent"] == "5"
    assert weighted_parameters(base, {"factor": "2", "network": "mainnet", "tier": "C"})["max_notional_quote"] == "500"


def test_signal_family_counts_once_and_system_link_fault_remains_in_cash_journal():
    first, second = monthly_trade("a"), monthly_trade("b")
    for row in (first, second):
        row["plan"]["source_refs"] = ["original"]
        row["plan"]["stop_loss"] = "90"
        row["initial_sl_risk"] = {"amount": "22"}
    snapshot = monthly_data(first, second)
    valid, excluded = outcomes(snapshot)
    assert len(valid["-1001"]) == 1 and len(valid["-1001"][0]["trade_ids"]) == 2
    first["execution_incident"] = "Wrong entry/protection"
    valid, excluded = outcomes(snapshot)
    assert not valid and excluded["-1001"] and snapshot["trades"]["a"]["fill_events"]


def test_family_waits_for_unfilled_leg_before_counting_proof():
    first, second = monthly_trade("a"), monthly_trade("b")
    for row in (first, second):
        row["plan"]["source_refs"] = ["original"]
        row["initial_sl_risk"] = {"amount": "22"}
    second.update(fill_events={}, filled_quantity="0", status="waiting_entry")
    snapshot = monthly_data(first, second)
    assert not outcomes(snapshot)[0]
    second["status"] = "cancelled"
    valid, _ = outcomes(snapshot)
    assert len(valid["-1001"]) == 1 and valid["-1001"][0]["trade_ids"] == ["a"]
    second.update(filled_quantity="2.2", status="closed")
    assert not outcomes(snapshot)[0]  # A filled leg with missing fill evidence cannot be ignored.


def test_demotion_two_completions_and_corrected_evidence_never_recounts(monkeypatch):
    rows = [evidence("-.1", number) for number in range(1, 6)]
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: ({"channel": rows}, {}))
    snapshot = rank_data("C")
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["tier"] == "C" and snapshot["source_ranks"]["channel"]["down"] == 1
    rows.append(evidence("-.1", 6))
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["tier"] == "D"
    frozen = copy.deepcopy(snapshot["source_ranks"]["channel"]["evaluated"])
    rows[0]["net_r"] = "-.2"
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["evaluated"] == frozen


def test_late_old_funding_uses_current_window_not_old_winning_prefix(monkeypatch):
    snapshot = rank_data("F")
    rows = [evidence("-.01", number) for number in range(101, 151)]
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: ({"channel": rows}, {}))
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["tier"] == "F"
    # Fifty much older winners gain their missing funding proof. The latest
    # fifty setups still lose; they cannot earn a historical Mainnet promotion.
    rows[:0] = [evidence(1, number) for number in range(1, 51)]
    evaluate(snapshot)
    assert snapshot["source_ranks"]["channel"]["tier"] == "F"
    assert len(snapshot["source_ranks"]["channel"]["evaluated"]) == 100
    assert Decimal(snapshot["source_ranks"]["channel"]["metrics"]["score"]) < 30
    frozen = copy.deepcopy(snapshot)
    evaluate(snapshot)
    assert snapshot == frozen


@pytest.mark.parametrize("unit,quote", [("course_percent", "ROI 20%"), ("course_percent", "TP1 20%"), ("roi_percent", "TP1 20%"), ("roi_percent", "net ROI 20%")])
def test_ambiguous_or_incompatible_percent_proof_is_rejected(setup, unit, quote):
    from aqla_hyperliquid_mcp.rules import verify_evidence
    trading = setup[-1]
    source, value = percentage_signal(trading)
    setup[1].update(lambda data: data["events"][source.event_id]["source"].update(text=data["events"][source.event_id]["source"]["text"] + " " + quote))
    value.take_profit_unit = unit
    value.evidence = [proof for proof in value.evidence if proof.field != "take_profit_unit"] + [Evidence(field="take_profit_unit", quote=quote, source_id=source.source_id)]
    with pytest.raises(AqlaError, match="percentage|ROI definition"):
        verify_evidence(value, setup[1].snapshot()["events"])


def test_rank_roi_fallback_telegram_and_rank_change_are_durable_facts():
    from aqla_agent.messages import telegram_view, trade_block
    trade = monthly_trade("a")
    trade["plan"].update(rank_snapshot={"tier": "B", "factor": "1.25"}, requested_exchange="hyperliquid", exchange="aster",
                         fallback_reason="market absent", source_tp={"unit": "roi_percent", "values": ["20"]}, policy={"risk_percent": "6.25"})
    trade["initial_sl_risk"] = {"amount": "12.5"}
    text = trade_block(monthly_data(trade), trade, [{"previous": {"filled_quantity": "0"}, "change": {"filled_quantity": "2.2"}}])
    assert "Kanalrang" in text and "1,25×" in text and "Hyperliquid → Aster" in text and "6,25 %" in text
    snapshot = rank_data()
    snapshot.update(events={}, operations={}, paused=False)
    snapshot["notifications"]["rank:a"] = {"rank_change": {"source_id": "channel", "old": "F", "new": "E", "network": "testnet", "metrics": {"score": "95", "drawdown_r": "0"}}}
    view = telegram_view(snapshot, {}, sheets_enabled=False)
    assert view["messages"][0]["notification_ids"] == ["rank:a"] and "F → E" in view["messages"][0]["texts"][0]


@pytest.mark.parametrize("side,expected", [("long", ["101.5649452269170610328638498", "103.1298904538341220657276995"]), ("short", ["98.43505477308293896713615024", "96.87010954616587793427230047"])])
def test_roi_uses_original_decimal_leverage_not_floor_or_cap(side, expected):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=5, price_tick=".0000000001")
    result = prices("100", side, market, "roi_percent", ["20", "40"], "12.78")
    for actual, raw in zip(result, expected, strict=True):
        assert abs(actual - Decimal(raw)) < Decimal(".0000000001")
    with pytest.raises(AqlaError, match="original signal leverage"):
        prices("100", side, market, "roi_percent", [20], None)


def percentage_signal(trading, *, entry=True):
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=now_ms(), text="BTC long. Entry 100. SL 90. TP1 ROI 20%. TP2 ROI 40%. Original leverage 10x.")
    trading.ingest(source)
    proofs = [("symbol", "BTC"), ("side", "long"), ("stop_loss", "SL 90"), ("take_profits.0", "ROI 20%"),
              ("take_profits.1", "ROI 40%"), ("take_profit_unit", "TP1 ROI 20%"), ("leverage", "Original leverage 10x")]
    if entry:
        proofs.append(("entry_prices.0", "Entry 100"))
    signal = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", entry="limit" if entry else "market",
                            entry_prices=[100] if entry else [], stop_loss=90, take_profits=[20, 40], take_profit_unit="roi_percent", leverage=10,
                            evidence=[Evidence(field=field, quote=quote, source_id=source.source_id) for field, quote in proofs])
    return source, signal


@pytest.mark.asyncio
@pytest.mark.parametrize("source_entry", [True, False])
async def test_percent_preview_execution_and_frozen_reference(setup, source_entry):
    _, state, _, _, trading = setup
    source, value = percentage_signal(trading, entry=source_entry)
    plan = await trading.preview(source.event_id, value)
    assert [Decimal(value) for value in plan["take_profits"]] == [102, 104]
    assert plan["source_tp"]["reference_kind"] == ("signal_entry" if source_entry else "fill_vwap")
    if source_entry:
        # Fake exchange only auto-fills IOC, so use source market input for the execution case below.
        return
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "open" and trade["percent_finalization"]["reference_entry"] == "100"
    frozen = copy.deepcopy(trade["percent_finalization"])
    await trading.recover()
    assert state.snapshot()["trades"][trade["trade_id"]]["percent_finalization"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", [False, True])
async def test_roi_update_freezes_original_base_preserves_evidence_and_survives_entry(waiting, setup):
    _, state, fake, _, trading = setup
    source, value = percentage_signal(trading, entry=waiting)
    if waiting:
        fake.mid = "110"
        state.update(lambda data: data["events"][source.event_id]["source"].update(text=data["events"][source.event_id]["source"]["text"] + " Deferred entry"))
        value.entry_timing = "deferred"
        value.evidence.append(Evidence(field="entry_timing", quote="Deferred entry", source_id=source.source_id))
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    message = SourceEvent(chat_id="channel", message_id=2, reply_to_id=1, sent_at=now_ms(), text="BTC long TP ROI 30% and ROI 60%")
    trading.ingest(message)
    update = Interpretation(kind="trade_update", source_refs=[message.source_id], symbol="BTC", side="long",
                            update_action="replace_targets", take_profits=[30, 60], take_profit_unit="roi_percent",
                            evidence=[Evidence(field=field, quote=quote, source_id=message.source_id) for field, quote in (
                                ("take_profit_unit", "TP ROI 30%"), ("take_profits.0", "ROI 30%"), ("take_profits.1", "ROI 60%"))])
    await trading.update_trade(message.event_id, update)
    saved = state.snapshot()["trades"][trade["trade_id"]]
    assert list(map(Decimal, saved["targets"])) == [103, 106]
    assert saved["source_tp_override"]["values"] == ["30", "60"]
    assert state.snapshot()["events"][message.event_id]["interpretation"]["take_profits"] == ["30", "60"]
    if waiting:
        fake.mid = "100"
        await trading.recover()
        assert state.snapshot()["trades"][trade["trade_id"]]["targets"] == ["103.00", "106.00"]
    else:
        sends = len(fake.actions)
        await trading.update_trade(message.event_id, update)
        assert len(fake.actions) == sends


@pytest.mark.asyncio
async def test_six_account_identity_network_nonce_and_rank_selection(setup):
    settings, state, primary_fake, exchange, _ = setup
    secondary = settings.model_copy(update={"network": "mainnet"})
    settings.execution_accounts["hyperliquid:mainnet"] = ExecutionSettings(exchange="hyperliquid", network="mainnet", account=settings.account,
                                                                           private_key_file=settings.private_key_file)
    fake = FakeExchange(primary_fake.signer)
    adapter = Hyperliquid(secondary, state, httpx.AsyncClient(base_url=secondary.base_url, transport=httpx.MockTransport(fake.handle)))
    router = Router(settings, state, exchange, adapters={"hyperliquid:mainnet": adapter})
    def enable(data):
        data["ranking_policy"]["enabled"] = True
        data["account_enabled"]["hyperliquid:mainnet"] = True
        data["source_ranks"]["channel"].update(tier="C", bootstrap=True)
    state.update(enable)
    source, value = percentage_signal(router, entry=False)
    plan = await router.preview(source.event_id, value)
    assert plan["account_key"] == "hyperliquid:mainnet" and plan["network"] == "mainnet"
    assert plan["rank_snapshot"]["tier"] == "C"
    result = await router.execute(plan["plan_id"])
    assert result["status"] == "open" and not primary_fake.actions and fake.actions
    assert all(op["account_key"] == "hyperliquid:mainnet" for op in state.snapshot()["operations"].values())
    assert not router.engines["hyperliquid"].state.snapshot()["trades"]
    await router.close()


@pytest.mark.asyncio
async def test_new_f_source_uses_testnet_and_missing_market_fallback_is_typed(setup):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    state.update(lambda data: data["ranking_policy"].update(enabled=True))
    source, value = percentage_signal(router, entry=False)
    plan = await router.preview(source.event_id, value)
    assert plan["network"] == "testnet" and plan["rank_snapshot"]["tier"] == "F"
    assert Decimal(plan["policy"]["risk_percent"]) == Decimal(".125")
    assert isinstance(SymbolUnavailable("absent"), AqlaError)
    await router.close()


def test_existing_schema_migration_bootstraps_c_and_preserves_order_ids(setup):
    settings, state, *_ = setup
    snapshot = state.snapshot()
    for key in ("execution_accounts", "account_limits", "account_enabled", "account_bindings", "account_nonces", "account_events",
                "ranking_policy", "ranking_revision", "source_ranks", "ranking_history"):
        snapshot.pop(key)
    snapshot["schema"] = 2
    from aqla_agent.pipelines import migrate
    frozen = copy.deepcopy(snapshot["operations"])
    assert migrate(snapshot) and snapshot["schema"] == 3
    assert snapshot["source_ranks"]["channel"]["tier"] == "C" and snapshot["operations"] == frozen
    state.close()
    with State(settings) as reopened:
        assert reopened.snapshot()["schema"] == 3
