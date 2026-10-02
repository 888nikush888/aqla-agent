from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.mark.asyncio
async def test_crash_before_initial_targets_dispatch_resumes_exact_staged_action(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.mutate

    async def crashed(identity, action, **kwargs):
        if identity.startswith("targets:"):
            raise RuntimeError("Crash before journal creation")
        return await original(identity, action, **kwargs)

    monkeypatch.setattr(exchange, "mutate", crashed)
    with pytest.raises(RuntimeError):
        await trading.execute(plan["plan_id"])
    staged = state.snapshot()["trades"][plan["trade_id"]]["initial_targets"]
    assert staged["operation_id"] not in state.snapshot()["operations"]
    monkeypatch.setattr(exchange, "mutate", original)
    await trading.recover()
    assert state.snapshot()["trades"][plan["trade_id"]]["status"] == "open"
    assert fake.actions.count(staged["action"]) == 1
    await trading.recover()
    assert fake.actions.count(staged["action"]) == 1


@pytest.mark.asyncio
async def test_owned_stop_exit_cannot_adopt_foreign_reopen_before_refresh(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    stop = next(order for order in trade["orders"] if order["kind"] == "sl")
    fake.fill(stop["cloid"], trade["remaining_quantity"])
    fake.position = 1.0
    count = len([action for action in fake.actions if action["type"] == "order"])
    await trading.recover()
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
    await trading.close_position(trade["trade_id"])
    assert fake.position == 1.0
    assert len([action for action in fake.actions if action["type"] == "order"]) == count


@pytest.mark.asyncio
async def test_unsent_recovery_rechecks_individual_risk_against_fresh_equity(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = trading._entry_steps

    async def crash(*args):
        raise RuntimeError("Crash after admission")

    monkeypatch.setattr(trading, "_entry_steps", crash)
    with pytest.raises(RuntimeError):
        await trading.execute(plan["plan_id"])
    monkeypatch.setattr(trading, "_entry_steps", original)
    account = await exchange.account_snapshot(await exchange.market("BTC", "perp"))

    async def changed(*args):
        return account.model_copy(update={"equity": Decimal(plan["risk_usdc"]) * 100})

    monkeypatch.setattr(exchange, "account_snapshot", changed)
    await trading.recover()
    assert state.snapshot()["trades"][plan["trade_id"]]["status"] == "cancelled"
    assert fake.actions == []


@pytest.mark.asyncio
async def test_interrupted_interpretation_remains_in_native_pending_snapshot(setup, signal):
    settings, state, _, exchange, trading = setup
    source, interpretation = signal
    trading.record_interpretation(source.event_id, interpretation)
    server = create_server(settings, state=state, exchange=exchange)
    result = await server.call_tool("integration", {"action": "snapshot", "token": settings.integration_key_file.read_text()})
    assert source.event_id in str(result)
    assert state.snapshot()["events"][source.event_id]["status"] == "interpreted"
    await close_server(server)


@pytest.mark.asyncio
async def test_spot_source_policy_is_rejected_before_any_exchange_action(setup, signal):
    _, state, fake, _, trading = setup
    state.update(lambda row: row["policy"].update(product="spot", leverage=1))
    source, interpretation = signal
    with pytest.raises(AqlaError, match="perpetual markets"):
        await trading.preview(source.event_id, interpretation)
    assert fake.actions == []


@pytest.mark.asyncio
async def test_fill_between_history_and_position_reads_keeps_live_protection(setup, signal, monkeypatch):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    target = next(row for row in trade["orders"] if row["kind"] == "tp")
    stop = next(row for row in trade["orders"] if row["kind"] == "sl")
    original = trading.positions
    injected = False
    async def late_fill(market):
        nonlocal injected
        if not injected:
            injected = True
            fake.fill(target["cloid"], target["quantity"])
        return await original(market)
    monkeypatch.setattr(trading, "positions", late_fill)
    with pytest.raises(AqlaError, match="inconsistent"):
        await trading.refresh_trade(trade["trade_id"])
    assert fake.orders[stop["cloid"]]["status"] == "open"
    refreshed = await trading.refresh_trade(trade["trade_id"])
    assert refreshed["status"] == "open"
    assert Decimal(refreshed["remaining_quantity"]) == Decimal(str(fake.position)) > 0
    assert "external_reduced_quantity" not in state.snapshot()["trades"][trade["trade_id"]]
    assert fake.orders[stop["cloid"]]["status"] == "open"


@pytest.mark.asyncio
async def test_crash_before_exit_cleanup_keeps_market_reserved_until_restart(setup, signal, monkeypatch):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    stop = next(row for row in trade["orders"] if row["kind"] == "sl")
    fake.fill(stop["cloid"], trade["remaining_quantity"])
    original = trading._cancel_owned_orders
    async def crash(*args, **kwargs):
        raise RuntimeError("Crash immediately before cleanup dispatch")
    monkeypatch.setattr(trading, "_cancel_owned_orders", crash)
    with pytest.raises(RuntimeError):
        await trading.refresh_trade(trade["trade_id"])
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "cleanup"
    newer = source.model_copy(update={"message_id": 2})
    trading.ingest(newer)
    replacement = interpretation.model_copy(update={"source_refs": [newer.source_id], "evidence": [proof.model_copy(update={"source_id": newer.source_id}) for proof in interpretation.evidence]})
    with pytest.raises(AqlaError, match="active trade already owns"):
        await trading.preview(newer.event_id, replacement)
    monkeypatch.setattr(trading, "_cancel_owned_orders", original)
    await trading.recover()
    recovered = state.snapshot()["trades"][trade["trade_id"]]
    assert recovered["status"] == "closed" and recovered["cleanup_complete"]
    assert all(fake.orders[row["cloid"]]["status"] != "open" for row in recovered["orders"])
    count = len(fake.actions)
    await trading.recover()
    assert len(fake.actions) == count
