"""Backup ROE is measured against original deployed margin, before costs."""
import copy
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from oracles.rules_9ce4972 import make_plan as legacy_plan
from test_aster_adapter import aster  # noqa: F401 -- native fixture
from test_backup_and_names import missing
from test_lighter_adapter import lighter  # noqa: F401 -- native fixture

from aqla_agent.pipelines import Parameters, TradingProfile, route_control
from aqla_agent.router import Router
from aqla_agent.setup import SetupBinding, write_password_hash
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    BackupValues,
    Evidence,
    Interpretation,
    Market,
    Policy,
    TradePlan,
)
from aqla_hyperliquid_mcp.rules import backup_prices, make_plan
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_sheets_mcp.parameters import parameter_values


def backup(**changes):
    return BackupValues(leverage=2, fallback_stop_loss_percent="40", fallback_take_profit_percentages=["25", "50"],
        fallback_stop_loss_unit="roe_percent", fallback_take_profit_unit="roe_percent", **changes)


def configure_roe(state):
    values = {**state.snapshot()["profiles"]["backup"]["parameters"], **backup().model_dump(mode="json")}
    profile = TradingProfile(id="backup", name="Backup", parameters=Parameters(**values))
    state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=profile))


def margin_ratios(entry, stop, targets, quantity, leverage, tick=Decimal(".01")):
    # Independent PnL/margin oracle; includes the complete originally deployed position.
    margin = quantity * entry / leverage
    tolerance = quantity * tick / margin
    assert abs(quantity * abs(entry - stop) / margin - Decimal(".40")) <= tolerance
    for target, expected in zip(targets, (Decimal(".25"), Decimal(".50")), strict=True):
        assert abs(quantity * abs(target - entry) / margin - expected) <= tolerance


@pytest.mark.parametrize("side,leverage,stop,targets", [
    ("long", 2, "80", ["112.5", "125"]), ("short", 2, "120", ["87.5", "75"]),
    ("long", 5, "92", ["105", "110"]), ("long", 10, "96", ["102.5", "105"]),
    ("short", 5, "108", ["95", "90"]), ("short", 10, "104", ["97.5", "95"]),
])
def test_independent_margin_baselines(signal, side, leverage, stop, targets):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    value = missing(signal[1], "stop_loss", "take_profits").model_copy(update={"side": side})
    plan = make_plan(value, market, AccountSnapshot(equity="10000", available_margin="10000"),
        Policy(leverage=leverage), 1, 1_000, ("testnet", "owner"), stamp=1_001, backup=backup())
    assert plan.stop_loss == Decimal(stop) and plan.take_profits == list(map(Decimal, targets))
    margin_ratios(plan.entry_price, plan.stop_loss, plan.take_profits, plan.quantity, plan.effective_leverage)


def test_rounded_market_and_notional_caps_use_final_leverage(signal):
    stamp = 1_000
    value = missing(signal[1], "stop_loss", "take_profits").model_copy(update={"leverage": Decimal("12.78")})
    account = AccountSnapshot(equity="1000", available_margin="1000", observed_at=stamp)
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=10)
    policy = Policy(leverage_mode="signal")
    plan = make_plan(value, market, account, policy, 1, stamp, ("testnet", "owner"), stamp=stamp, backup=backup())
    assert (plan.signal_leverage_original, plan.signal_leverage, plan.effective_leverage) == (Decimal("12.78"), 12, 10)
    assert plan.stop_loss == 96 and plan.take_profits == [Decimal("102.5"), Decimal(105)]
    # Adversarial brackets: reduced quantity crosses into a stricter lower bracket.
    for lower_cap, expected_leverage, expected_stop, expected_targets in (
        (2, 2, "80", ["112.5", "125"]), (20, 5, "92", ["105", "110"]),
    ):
        tiered = market.model_copy(update={"leverage_tiers": Market.model_validate({**market.model_dump(), "leverage_tiers": [
            {"notional_floor": "0", "notional_cap": "90", "max_leverage": lower_cap},
            {"notional_floor": "90", "notional_cap": "1000", "max_leverage": 5},
        ]}).leverage_tiers})
        result = make_plan(value, tiered, account, policy, 1, stamp, ("testnet", "owner"), stamp=stamp, backup=backup())
        assert result.effective_leverage == expected_leverage and result.quantity < plan.quantity
        assert result.stop_loss == Decimal(expected_stop) and result.take_profits == list(map(Decimal, expected_targets))
        assert result.quantity * result.entry_limit < 90
        assert result.risk_usdc == result.quantity * (abs(result.entry_limit - result.stop_loss) + result.entry_limit * Decimal(".001"))
        assert result.risk_usdc <= 5
        margin_ratios(result.entry_price, result.stop_loss, result.take_profits, result.quantity, result.effective_leverage)


@pytest.mark.parametrize("leverage", [None, 0, -2, True, "NaN", "Infinity", "bad", "2.5"])
def test_roe_requires_valid_effective_leverage(leverage):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100")
    with pytest.raises(AqlaError, match="valid effective"):
        backup_prices(Decimal(100), "long", market, Decimal(40), [Decimal(25), Decimal(50)],
            stop_unit="roe_percent", take_profit_unit="roe_percent", leverage=leverage)


def test_legacy_backup_bytes_and_plan_oracle_remain_identical(signal):
    legacy = BackupValues(leverage=2, fallback_stop_loss_percent="1", fallback_take_profit_percentages=["1", "2"])
    assert legacy.model_dump_json() == '{"leverage":2,"entry_mode":"source_or_market","fallback_stop_loss_percent":"1","fallback_take_profit_percentages":["1","2"]}'
    for model in (Policy(), Parameters(), legacy):
        assert "fallback_stop_loss_unit" not in model.model_dump(mode="json")
        assert "fallback_take_profit_unit" not in model.model_dump(mode="json")
    stamp = 1_000
    value = missing(signal[1], "stop_loss", "take_profits")
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    args = value, market, AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp), Policy(), 7, stamp, ("testnet", "owner")
    expected = legacy_plan(*args, stamp=stamp, backup=legacy, backup_revision=3)
    actual = make_plan(*args, stamp=stamp, backup=legacy, backup_revision=3)
    assert actual.model_dump_json() == expected.model_dump_json()
    assert actual.plan_id == expected.plan_id and actual.trade_id == expected.trade_id
    # Legacy validation still fills missing fixed leverage; ROE requires it explicitly.
    old = actual.model_dump(mode="json")
    old.pop("effective_leverage")
    assert TradePlan.model_validate(old).effective_leverage == 2


@pytest.mark.parametrize("explicit_defaults", [False, True])
def test_pre_roe_backup_identity_matches_frozen_release_models(explicit_defaults):
    # Produced with models AND rules from the recorded pre-ROE Git revision.
    # Importing old rules with current model classes cannot detect repr drift.
    case = json.loads((Path(__file__).parent / "oracles" / "backup_legacy_0a36219.json").read_text())
    units = {"fallback_stop_loss_unit": "course_percent", "fallback_take_profit_unit": "course_percent"} if explicit_defaults else {}
    policy = Policy.model_validate({**case["policy"], **units})
    configured = BackupValues.model_validate({**case["backup"], **units})
    plan = make_plan(Interpretation.model_validate(case["signal"]), Market.model_validate(case["market"]),
        AccountSnapshot.model_validate(case["account"]), policy, 7, 1000, ("testnet", "owner"),
        stamp=1000, backup=configured, backup_revision=3)
    assert plan.model_dump(mode="json") == case["plan"]
    assert plan.plan_id == "e825dceb4ce3f43b99b5478a1118780bb1ef661a91e71048f38f46f56078849d"
    assert plan.trade_id == "f2de51483cb6852947a6f0e24fcdc841"
    assert "fallback_stop_loss_unit" not in str(policy) and "fallback_take_profit_unit" not in str(configured)


@pytest.mark.parametrize("roe_model", ["policy", "backup"])
def test_roe_unit_changes_identity_even_with_identical_one_x_prices(roe_model):
    stamp = 1000
    value = Interpretation(kind="new_signal", source_refs=["channel:1"], symbol="BTC", side="long")
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=1)
    account = AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp)
    policy = Policy(leverage_mode="signal")
    configured = BackupValues(leverage=1, fallback_stop_loss_percent="1", fallback_take_profit_percentages=["1", "2"])
    course = make_plan(value, market, account, policy, 7, stamp, ("testnet", "owner"), stamp=stamp, backup=configured)
    units = {"fallback_stop_loss_unit": "roe_percent", "fallback_take_profit_unit": "roe_percent"}
    if roe_model == "policy":
        policy = Policy.model_validate({**policy.model_dump(), **units})
    else:
        configured = BackupValues.model_validate({**configured.model_dump(), **units})
    roe = make_plan(value, market, account, policy, 7, stamp, ("testnet", "owner"), stamp=stamp, backup=configured)
    assert (roe.stop_loss, roe.take_profits, roe.quantity, roe.risk_usdc) == (course.stop_loss, course.take_profits, course.quantity, course.risk_usdc)
    assert roe.trade_id == course.trade_id and roe.plan_id != course.plan_id
    assert "roe_percent" in str(policy if roe_model == "policy" else configured)


@pytest.mark.parametrize("unit,values,expected", [
    ("price", ["110", "120"], ["110", "120"]),
    ("roi_percent", ["25", "50"], ["101.96", "103.92"]),
])
def test_source_targets_keep_their_own_rules_and_snapshot_is_frozen(signal, unit, values, expected):
    configured = backup()
    value = missing(signal[1], "stop_loss").model_copy(update={"leverage": Decimal("12.78"),
        "take_profit_unit": unit, "take_profits": list(map(Decimal, values))})
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=10)
    plan = make_plan(value, market, AccountSnapshot(equity="10000", available_margin="10000"),
        Policy(leverage_mode="signal"), 1, 1_000, ("testnet", "owner"), stamp=1_001, backup=configured)
    assert plan.backup_fields == ["stop_loss"] and plan.stop_loss == 96
    assert plan.take_profits == list(map(Decimal, expected))
    configured.fallback_stop_loss_unit = "course_percent"
    configured.fallback_take_profit_percentages[:] = [Decimal(99)]
    assert plan.backup_values.fallback_stop_loss_unit == "roe_percent"
    assert plan.backup_values.fallback_take_profit_percentages == [25, 50]


@pytest.mark.asyncio
async def test_missing_saved_roe_leverage_rejects_before_wire(setup, signal):
    _, state, fake, _, trading = setup
    configure_roe(state)
    plan = await trading.preview(signal[0].event_id, missing(signal[1], "stop_loss", "take_profits"))
    state.update(lambda data: data["plans"][plan["plan_id"]]["plan"].update(effective_leverage=None))
    with pytest.raises(ValueError, match="explicit effective leverage"):
        await trading.execute(plan["plan_id"])
    assert fake.actions == [] and state.snapshot()["operations"] == {}


@pytest.mark.asyncio
async def test_own_partial_vwap_finalization_survives_restart_and_profile_change(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    configure_roe(state)
    state.update(lambda data: data["policy"].update(leverage=10))
    source, value = signal
    source = source.model_copy(update={"message_id": 2, "text": "BTC kaufen. Entry 102."})
    trading.ingest(source)
    value = missing(value, "stop_loss", "take_profits").model_copy(update={"source_refs": [source.source_id],
        "entry": "limit", "entry_prices": [Decimal(102)], "evidence": [
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "BTC"), ("side", "kaufen"), ("entry_prices.0", "Entry 102"))]})
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    tranche = (Decimal(plan["quantity"]) / 4).quantize(Decimal(".01"))
    fake.fill(trade["entry_cloid"], tranche, "100")
    fake.fill(trade["entry_cloid"], tranche, "102")
    original_mutate = exchange.mutate
    async def crash(operation, *args, **kwargs):
        if operation.startswith("targets:"):
            raise SystemExit("simulated death before target send")
        return await original_mutate(operation, *args, **kwargs)
    monkeypatch.setattr(exchange, "mutate", crash)
    with pytest.raises(SystemExit):
        await trading.recover()
    final = state.snapshot()["trades"][trade["trade_id"]]["backup_finalization"]
    assert final["phase"] == "applied" and Decimal(final["reference_entry"]) == 101
    assert Decimal(final["stop_loss"]) == Decimal("96.96")
    assert list(map(Decimal, final["take_profits"])) == [Decimal("103.53"), Decimal("106.05")]
    assert fake.orders[trade["entry_cloid"]]["status"] == "canceled"
    monkeypatch.setattr(exchange, "mutate", original_mutate)
    state.update(lambda data: data["profiles"]["backup"]["parameters"].update(
        fallback_stop_loss_unit="course_percent", fallback_take_profit_unit="course_percent",
        fallback_stop_loss_percent="1", fallback_take_profit_percentages=["1", "2"]))
    restarted = Trading(exchange, state)
    await restarted.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "open" and current["plan"] == plan
    assert current["backup_finalization"] == final
    margin_ratios(Decimal(101), Decimal(current["stop_price"]), list(map(Decimal, current["targets"])),
        Decimal(current["filled_quantity"]), 10)
    assert all(row["fee"] == "0.01" for row in fake.fills)
    sent = copy.deepcopy(fake.actions)
    await restarted.recover()
    assert fake.actions == sent


@pytest.mark.asyncio
async def test_final_roe_risk_budget_failure_closes_and_pauses(setup, signal):
    _, state, fake, _, trading = setup
    configure_roe(state)
    source, value = signal
    value = missing(value, "stop_loss", "take_profits").model_copy(update={"entry": "limit", "entry_prices": [Decimal(100)]})
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    fake.fill(trade["entry_cloid"], plan["quantity"], "101")
    await trading.recover()
    assert fake.position == 0 and state.snapshot()["paused"]
    assert "budget" in state.snapshot()["trades"][trade["trade_id"]]["execution_incident"]


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["hyperliquid", "aster", "lighter"])
async def test_profile_mcp_router_plan_reaches_native_roe_prices(setup, signal, aster, lighter, tmp_path, monkeypatch, venue):  # noqa: F811 -- fixtures
    settings, state, fake, exchange, _ = setup
    adapter, boundary = (exchange, fake) if venue == "hyperliquid" else (aster if venue == "aster" else lighter)[:2]
    original_market = adapter.market
    async def capped_market(*args):
        return (await original_market(*args)).model_copy(update={"max_leverage": 10})
    monkeypatch.setattr(adapter, "market", capped_market)
    verifier = tmp_path / "synthetic-roe-verifier.json"
    write_password_hash(verifier, "synthetic-roe-fixture")
    settings.setup_password_hash_file, settings.telegram_owner_id = verifier, "12345"
    monkeypatch.setattr("aqla_hyperliquid_mcp.server.Router", lambda *args: Router(*args, adapters={} if venue == "hyperliquid" else {venue: adapter}))
    server = create_server(settings, state=state, exchange=exchange)
    binding = SetupBinding("12345", "12345", "roe-gateway", "roe-session")
    gate = server._aqla_setup_gate
    gate.register(binding, "d" * 64, None, gate.bind(binding))
    auth = server.aqla_authority.sign("operator", "native-roe-command", setup={"identity": "d" * 64, "binding": asdict(binding)})
    for identity, changes in (("standard", {"leverage_mode": "signal"}), ("backup", backup().model_dump(mode="json"))):
        before = state.snapshot()["profiles"][identity]["parameters"]
        result = await server.call_tool("control", {"action": "set_profile", "context_auth": auth,
            "expected_revision": state.snapshot()["configuration_revision"],
            "profile": {"id": identity, "name": identity.title(), "parameters": {**before, **changes}}})
        assert not result.is_error
    result = await server.call_tool("control", {"action": "set_pipeline", "context_auth": auth,
        "expected_revision": state.snapshot()["configuration_revision"], "pipeline": {
            "id": "legacy", "name": "ROE fixture", "sources": ["channel"], "enabled": True,
            "targets": [{"exchange": venue, "profile_id": "standard"}]}})
    assert not result.is_error
    source, value = signal
    source = source.model_copy(update={"message_id": 2, "text": source.text + " 12.78x"})
    server.aqla_trading.ingest(source)
    value = missing(value, "stop_loss", "take_profits")
    value = value.model_copy(update={"source_refs": [source.source_id], "leverage": Decimal("12.78"),
        "evidence": [row.model_copy(update={"source_id": source.source_id}) for row in value.evidence]
            + [Evidence(field="leverage", source_id=source.source_id, quote="12.78x")]})
    preview = await server.aqla_trading.preview(source.event_id, value)
    plan = preview["targets"][0]["plan"]
    assert plan["effective_leverage"] == 10 and Decimal(plan["stop_loss"]) == 96
    assert list(map(Decimal, plan["take_profits"])) == [Decimal("102.5"), Decimal(105)]
    assert plan["backup_values"]["fallback_take_profit_percentages"] == ["25", "50"]
    trade = await server.aqla_trading.execute(plan["plan_id"])
    assert trade["status"] == "open" and trade["backup_finalization"]["phase"] == "applied"
    tick = Decimal(".1") if venue != "hyperliquid" else Decimal(".01")
    margin_ratios(Decimal(trade["entry_vwap"]), Decimal(trade["stop_price"]), list(map(Decimal, trade["targets"])),
        Decimal(trade["filled_quantity"]), 10, tick)
    if venue == "hyperliquid":
        assert all(row["order"]["reduceOnly"] for identity, row in boundary.orders.items() if identity != trade["entry_cloid"])
        assert {Decimal(row["order"]["limitPx"]) for row in boundary.orders.values() if row["order"]["reduceOnly"] and not row["order"]["isTrigger"]} == {Decimal("102.5"), Decimal(105)}
    elif venue == "aster":
        assert all(row["reduceOnly"] for identity, row in boundary.orders.items() if identity != trade["entry_cloid"])
        assert Decimal(trade["entry_vwap"]) == Decimal("100.3")
        assert {Decimal(row["price"]) for row in boundary.orders.values() if row["reduceOnly"] and row["type"] == "LIMIT"} == {Decimal("102.9"), Decimal("105.4")}
    else:
        targets = [row for kind, row in lighter[2].calls if kind == "order" and row["reduce_only"] and row["order_type"] != 2]
        assert {row["price"] for row in targets} == {1029, 1054}
        assert all(isinstance(row["client_order_index"], int) for row in targets)
        assert [row["nonce"] for _, row in lighter[2].calls] == list(range(boundary.nonce))
    rows = {row[0]: row for row in parameter_values(state.snapshot())}
    assert rows["profile:backup:fallback_stop_loss_percent"][4] == "% Marginverlust"
    assert rows["profile:backup:fallback_take_profit_percentages"][4] == "% ROE"
    assert state.snapshot()["trades"][trade["trade_id"]]["plan"] == plan
    await close_server(server)
    for unused in (aster[0], lighter[0]):
        if unused is not adapter:
            await unused.close()
