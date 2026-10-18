from decimal import Decimal

import pytest
from test_updates import stop_update, waiting_gtc

from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


@pytest.mark.asyncio
async def test_pending_stop_survives_restart(setup, signal):
    settings, state, fake, exchange, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading)
    await trading.update_trade(source.event_id, interpretation)
    pending = state.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"]
    state.close()
    restarted = State(settings)
    try:
        exchange.state = restarted
        trading = Trading(exchange, restarted)
        assert restarted.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"] == pending
        fake.mid = "100"
        fake.fill(trade["entry_cloid"], trade["plan"]["quantity"])
        await trading.recover()
        await trading.recover()
        current = restarted.snapshot()["trades"][trade["trade_id"]]
        assert current["pending_stop_update"]["phase"] == "confirmed"
        assert current["pending_stop_update"]["operation_id"] == pending["operation_id"]
        assert current["plan"] == trade["plan"]
        assert restarted.snapshot()["events"][source.event_id]["status"] == "updated"
        assert len([action for action in fake.actions if action["type"] == "order" and any(order["t"].get("trigger", {}).get("triggerPx") == "95" for order in action["orders"])]) == 1
    finally:
        restarted.close()


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

    class SimulatedProcessCrash(BaseException):
        """Process death bypasses caught admission failures."""

    async def crash(*args):
        raise SimulatedProcessCrash("Crash after admission")

    monkeypatch.setattr(trading, "_entry_steps", crash)
    with pytest.raises(SimulatedProcessCrash):
        await trading.execute(plan["plan_id"])
    monkeypatch.setattr(trading, "_entry_steps", original)
    account = await exchange.account_snapshot(await exchange.market("BTC", "perp"))

    async def changed(*args):
        return account.model_copy(update={"equity": Decimal(plan["risk_usdc"]) * 100})

    monkeypatch.setattr(exchange, "account_snapshot", changed)
    await trading.recover()
    trade = state.snapshot()["trades"][plan["trade_id"]]
    assert trade["status"] == "open"
    assert Decimal(trade["plan"]["risk_usdc"]) <= Decimal(plan["risk_usdc"]) * 100 * Decimal(plan["policy"]["risk_percent"]) / 100
    assert Decimal(trade["plan"]["quantity"]) < Decimal(plan["quantity"])
    assert state.snapshot()["plans"][plan["plan_id"]]["plan"] == plan
    assert len([action for action in fake.actions if action["type"] == "order" and any(not row["r"] for row in action["orders"])]) == 1


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


@pytest.mark.asyncio
@pytest.mark.parametrize("percent", [Decimal(100), Decimal(50)])
async def test_crash_before_close_dispatch_resumes_exact_saved_intent_once(setup, signal, monkeypatch, percent):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    original = exchange.mutate

    async def interrupted(identity, action, **kwargs):
        if identity == "saved-close":
            raise RuntimeError("Crash before operation creation")
        return await original(identity, action, **kwargs)

    monkeypatch.setattr(exchange, "mutate", interrupted)
    with pytest.raises(RuntimeError):
        await trading.close_position(trade["trade_id"], percent, "saved-close")
    staged = state.snapshot()["trades"][trade["trade_id"]]
    intent = staged["close_intents"]["saved-close"]
    assert staged["status"] == "closing" and staged["management_revision"] == 1
    assert "saved-close" not in state.snapshot()["operations"]
    assert Decimal(intent["action"]["orders"][0]["s"]) == Decimal(trade["remaining_quantity"]) * percent / 100
    fake.mid = "115"  # Recovery must not recompute the saved price from a newer market read.
    state.update(lambda row: row.update(paused=True, sources=[], policy_revision=2))
    monkeypatch.setattr(exchange, "mutate", original)
    await trading.recover()
    await trading.recover()
    assert fake.actions.count(intent["action"]) == 1
    assert state.snapshot()["operations"]["saved-close"]["wire_action"] == intent["action"]
    assert Decimal(str(fake.position)) == Decimal(trade["remaining_quantity"]) * (100 - percent) / 100


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "sending", "unknown"])
async def test_close_recovery_never_resends_an_existing_operation(setup, signal, phase):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    fake.lost_response = True
    result = await trading.close_position(trade["trade_id"], Decimal(50), "unknown-close")
    assert result["phase"] == "unknown"
    state.update(lambda row: row["operations"]["unknown-close"].update(phase=phase))
    action = state.snapshot()["trades"][trade["trade_id"]]["close_intents"]["unknown-close"]["action"]
    await trading.recover()
    await trading.close_position(trade["trade_id"], Decimal(50), "unknown-close")
    assert fake.actions.count(action) == 1
    assert Decimal(str(fake.position)) == Decimal(trade["remaining_quantity"]) / 2
