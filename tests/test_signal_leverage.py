from decimal import Decimal

import pytest

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
from aqla_hyperliquid_mcp.rules import make_plan, quoted_leverage_numbers, quoted_numbers, verify_evidence
from aqla_hyperliquid_mcp.trades import Trading


def market():
    return Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)


def leveraged_signal(setup, signal, leverage):
    *_, trading = setup
    source, interpretation = signal
    source = source.model_copy(update={"message_id": 2, "text": source.text + f" Hebel {leverage}x."})
    interpretation = interpretation.model_copy(update={
        "source_refs": [source.source_id], "leverage": leverage,
        "evidence": [proof.model_copy(update={"source_id": source.source_id}) for proof in interpretation.evidence]
            + [Evidence(field="leverage", source_id=source.source_id, quote=f"Hebel {leverage}x")],
    })
    trading.ingest(source)
    return source, interpretation


@pytest.mark.parametrize("requested,expected", [(1, 1), (10, 10), (20, 20), (50, 20), (250, 20)])
def test_signal_mode_preserves_requested_leverage_and_caps_only_at_market_max(signal, requested, expected):
    _, interpretation = signal
    interpretation = interpretation.model_copy(update={"leverage": requested})
    account = AccountSnapshot(equity="10000", available_margin="10000")
    plan = make_plan(interpretation, market(), account, Policy(leverage_mode="signal"), 1, now_ms(), ("testnet", "account"))
    fixed = make_plan(interpretation, market(), account, Policy(leverage=expected), 1, now_ms(), ("testnet", "account"))
    assert plan.signal_leverage == requested
    assert plan.effective_leverage == expected
    assert plan.policy.leverage == 2
    assert plan.quantity == fixed.quantity
    assert plan.risk_usdc == fixed.risk_usdc


def test_signal_mode_missing_leverage_never_uses_fixed_value_as_fallback(signal):
    _, interpretation = signal
    with pytest.raises(AqlaError, match="Signal leverage is missing"):
        make_plan(interpretation, market(), AccountSnapshot(equity="10000", available_margin="10000"),
                  Policy(leverage_mode="signal", leverage=7), 1, now_ms(), ("testnet", "account"))


def test_fixed_mode_ignores_reported_leverage_and_keeps_existing_limit_rejection(signal):
    _, interpretation = signal
    interpretation = interpretation.model_copy(update={"leverage": 250})
    account = AccountSnapshot(equity="10000", available_margin="10000")
    plan = make_plan(interpretation, market(), account, Policy(leverage=3), 1, now_ms(), ("testnet", "account"))
    assert plan.effective_leverage == 3
    with pytest.raises(AqlaError, match="Configured leverage exceeds"):
        make_plan(interpretation, market(), account, Policy(leverage=21), 1, now_ms(), ("testnet", "account"))


@pytest.mark.parametrize("value", [0, -1, 0.78, True, "ambiguous", "NaN", "Infinity"])
def test_signal_leverage_must_be_a_valid_number_at_least_one(value):
    with pytest.raises(ValueError):
        Interpretation(kind="new_signal", source_refs=["source"], leverage=value)


@pytest.mark.parametrize("quote", ["20x", "x20", "20×", "×20", "Hebel: 20", "20fach", "Leverage 20 x",
    "Hebel: 20x.", "Hebel x20.", "Hebel 20fach."])
def test_multiplier_evidence_is_supported_without_loosening_price_evidence(quote):
    assert Decimal(20) in set(quoted_leverage_numbers(quote))
    if quote in ("20x", "x20", "20fach"):
        assert Decimal(20) not in set(quoted_numbers(quote))


def test_signal_leverage_requires_a_quote_from_the_actual_source(setup, signal):
    _, state, *_ = setup
    source, interpretation = leveraged_signal(setup, signal, 50)
    verify_evidence(interpretation, state.snapshot()["events"])
    forged = interpretation.model_copy(update={"leverage": 60})
    with pytest.raises(AqlaError, match="Signal leverage is not supported"):
        verify_evidence(forged, state.snapshot()["events"])
    absent = interpretation.model_copy(update={"evidence": [proof for proof in interpretation.evidence if proof.field != "leverage"]})
    with pytest.raises(AqlaError, match="Signal leverage is not supported"):
        verify_evidence(absent, state.snapshot()["events"])
    assert source.source_id in interpretation.source_refs


@pytest.mark.asyncio
async def test_execution_sends_capped_leverage_and_uses_it_for_fresh_margin_admission(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = leveraged_signal(setup, signal, 50)
    state.update(lambda data: data["policy"].update(leverage_mode="signal"))
    original = exchange.account_snapshot
    async def small_margin(selected):
        return (await original(selected)).model_copy(update={"available_margin": Decimal(30)})
    monkeypatch.setattr(exchange, "account_snapshot", small_margin)
    plan = await trading.preview(source.event_id, interpretation)
    # This exact risk-sized plan fits 20x but would fail admission using the unused fixed 2x.
    assert Decimal(plan["quantity"]) * Decimal(plan["entry_limit"]) / 20 <= 30
    assert Decimal(plan["quantity"]) * Decimal(plan["entry_limit"]) / 2 > 30
    trade = await trading.execute(plan["plan_id"])
    assert trade["status"] == "open"
    assert trade["plan"]["signal_leverage"] == 50
    assert trade["plan"]["effective_leverage"] == 20
    assert [action["leverage"] for action in fake.actions if action["type"] == "updateLeverage"] == [20]
    await trading.execute(plan["plan_id"])
    assert len([action for action in fake.actions if action["type"] == "updateLeverage"]) == 1


@pytest.mark.asyncio
async def test_decreased_market_limit_after_preview_blocks_all_exchange_mutations(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = leveraged_signal(setup, signal, 50)
    state.update(lambda data: data["policy"].update(leverage_mode="signal"))
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.market
    async def lower_limit(*args, **kwargs):
        return (await original(*args, **kwargs)).model_copy(update={"max_leverage": 10})
    monkeypatch.setattr(exchange, "market", lower_limit)
    with pytest.raises(AqlaError, match="leverage limit decreased"):
        await trading.execute(plan["plan_id"])
    assert fake.actions == []
    assert plan["trade_id"] not in state.snapshot()["trades"]


@pytest.mark.asyncio
async def test_restart_after_leverage_ack_keeps_resolved_value_without_resending_it(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = leveraged_signal(setup, signal, 50)
    state.update(lambda data: data["policy"].update(leverage_mode="signal"))
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.mutate
    class SimulatedProcessCrash(BaseException):
        """Process death preserves the durable admission and leverage ACK."""

    async def crash_before_entry(identity, *args, **kwargs):
        if identity.startswith("entry:"):
            raise SimulatedProcessCrash("Simulated service interruption before entry dispatch")
        return await original(identity, *args, **kwargs)
    monkeypatch.setattr(exchange, "mutate", crash_before_entry)
    with pytest.raises(SimulatedProcessCrash, match="Simulated service interruption"):
        await trading.execute(plan["plan_id"])
    assert [action["leverage"] for action in fake.actions if action["type"] == "updateLeverage"] == [20]
    monkeypatch.setattr(exchange, "mutate", original)
    restarted = Trading(exchange, state)
    await restarted.recover()
    recovered = state.snapshot()["trades"][plan["trade_id"]]
    assert recovered["status"] == "open"
    assert recovered["plan"]["effective_leverage"] == 20
    assert [action["leverage"] for action in fake.actions if action["type"] == "updateLeverage"] == [20]


@pytest.mark.asyncio
async def test_legacy_fixed_plan_and_interpretation_keep_execution_identity(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    plan.pop("signal_leverage")
    plan.pop("effective_leverage")
    plan["policy"].pop("leverage_mode")
    plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
    identity = plan["plan_id"]
    def legacy(data):
        data["policy"].pop("leverage_mode")
        data["plans"] = {identity: {"event_id": source.event_id, "plan": plan}}
    state.update(legacy)
    restored = TradePlan.model_validate(plan)
    assert restored.plan_id == identity and restored.effective_leverage == 2
    trade = await trading.execute(identity)
    assert trade["plan"]["plan_id"] == identity
    assert [action["leverage"] for action in fake.actions if action["type"] == "updateLeverage"] == [2]
    state.update(lambda data: data["events"][source.event_id]["interpretation"].pop("leverage"))
    assert trading.record_interpretation(source.event_id, interpretation)["status"] == "executed"


def test_signal_plan_cannot_replace_capped_leverage_with_another_number(signal):
    _, interpretation = signal
    plan = make_plan(interpretation.model_copy(update={"leverage": 50}), market(),
        AccountSnapshot(equity="10000", available_margin="10000"), Policy(leverage_mode="signal"),
        1, now_ms(), ("testnet", "account"))
    raw = plan.model_dump(mode="json")
    raw["effective_leverage"] = 15
    with pytest.raises(ValueError, match="Effective leverage differs"):
        TradePlan.model_validate(raw)
    raw.pop("effective_leverage")
    with pytest.raises(ValueError):
        TradePlan.model_validate(raw)


def test_parameter_page_names_signal_mode_and_does_not_present_fixed_value_as_active(setup):
    from aqla_sheets_mcp.journal import project
    _, state, *_ = setup
    data = state.snapshot()
    data["policy"]["leverage_mode"] = "signal"
    rows = {row["record_id"]: row["values"] for row in project(data).values() if row["table"] == "Handelsparameter"}
    assert rows["leverage_mode"][3] == "Aus Signal, am Börsenmaximum gedeckelt"
    assert rows["leverage"][3] == "Nicht verwendet (Signalmodus)"
    assert rows["leverage"][6] == 2
