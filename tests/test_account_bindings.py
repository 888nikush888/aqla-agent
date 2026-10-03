import json

import pytest

from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.state import State
from aqla_hyperliquid_mcp.trades import Trading

VAULT = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def write_legacy(state):
    data = state.snapshot()
    data.pop("trading_account")
    for row in [*data["plans"].values(), *data["trades"].values()]:
        row["plan"].pop("principal", None)
    state.close()
    state.path.write_text(json.dumps(data), encoding="utf-8")


def test_persisted_state_rejects_vault_change_without_rewriting(setup):
    settings, state, *_ = setup
    state.close()
    original = state.path.read_bytes()
    with pytest.raises(AqlaError, match="trading target"):
        State(settings.model_copy(update={"vault_address": VAULT}))
    assert state.path.read_bytes() == original


@pytest.mark.parametrize("vault", [None, VAULT])
def test_empty_legacy_state_can_only_infer_its_principal(setup, vault):
    settings, state, *_ = setup
    write_legacy(state)
    original = state.path.read_bytes()
    bound = settings.model_copy(update={"vault_address": vault})
    if vault:
        with pytest.raises(AqlaError, match="Legacy execution state"):
            State(bound)
        assert state.path.read_bytes() == original
    else:
        with State(bound) as restored:
            assert restored.snapshot()["trading_account"] == settings.account.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, OTHER])
async def test_legacy_vault_plan_proves_only_its_recorded_target(setup, signal, changed):
    settings, _, _, exchange, _ = setup
    source, interpretation = signal
    settings = settings.model_copy(update={"vault_address": VAULT, "state_path": settings.state_path.with_name("vault.json")})
    state = State(settings)
    vault_exchange = Hyperliquid(settings, state, exchange.client)
    trading = Trading(vault_exchange, state)
    trading.ingest(source)
    plan = await trading.preview(source.event_id, interpretation)
    assert plan["account"] == VAULT and plan["principal"] == settings.account.lower()
    write_legacy(state)
    original = state.path.read_bytes()
    bound = settings.model_copy(update={"vault_address": changed or VAULT})
    if changed:
        with pytest.raises(AqlaError, match="Legacy execution state"):
            State(bound)
        assert state.path.read_bytes() == original
    else:
        with State(bound) as restored:
            assert restored.snapshot()["trading_account"] == VAULT


@pytest.mark.asyncio
async def test_legacy_vault_operation_alone_proves_target(setup):
    settings, _, fake, exchange, _ = setup
    settings = settings.model_copy(update={"vault_address": VAULT, "state_path": settings.state_path.with_name("vault.json")})
    state = State(settings)
    vault_exchange = Hyperliquid(settings, state, exchange.client)
    await vault_exchange.mutate("vault-leverage", {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2})
    write_legacy(state)
    with State(settings) as restored:
        assert restored.snapshot()["trading_account"] == VAULT
    assert len(fake.actions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plan_target", "plan_principal", "operation_target", "operation_network"])
async def test_saved_execution_records_must_match_persisted_binding(setup, signal, change):
    settings, state, _, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    await exchange.mutate("leverage", {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2})
    data = state.snapshot()
    if change.startswith("plan_"):
        data["plans"][plan["plan_id"]]["plan"]["account" if change == "plan_target" else "principal"] = OTHER
    else:
        data["operations"]["leverage"]["request"]["account" if change == "operation_target" else "network"] = OTHER if change == "operation_target" else "mainnet"
    state.close()
    state.path.write_text(json.dumps(data), encoding="utf-8")
    original = state.path.read_bytes()
    with pytest.raises(AqlaError, match="Saved .* another trading target"):
        State(settings)
    assert state.path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plan_target", "plan_principal", "vault", "principal"])
async def test_recovery_never_manages_trade_after_account_binding_changes(setup, signal, change):
    settings, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    if change.startswith("plan_"):
        state.update(lambda row: row["trades"][trade["trade_id"]]["plan"].update(
            {"account" if change == "plan_target" else "principal": OTHER}))
    elif change == "vault":
        settings.vault_address = VAULT
    else:
        settings.account = OTHER
    before = len(fake.actions)
    reports = await trading.recover()
    assert any(row.get("error") for row in reports)
    assert not trading.recovery_checked
    with pytest.raises(AqlaError, match="trading target"):
        await trading.close_position(trade["trade_id"])
    assert len(fake.actions) == before


@pytest.mark.asyncio
async def test_preview_cannot_persist_plan_after_target_changes_during_read(setup, signal, monkeypatch):
    settings, state, fake, exchange, trading = setup
    source, interpretation = signal
    original = exchange.account_snapshot

    async def changed(market):
        account = await original(market)
        settings.vault_address = VAULT
        return account

    monkeypatch.setattr(exchange, "account_snapshot", changed)
    with pytest.raises(AqlaError, match="trading target"):
        await trading.preview(source.event_id, interpretation)
    assert state.snapshot()["plans"] == {}
    assert fake.actions == []
