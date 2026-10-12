import asyncio
import copy
import json
from decimal import Decimal

import httpx
import pytest

from aqla_agent import entry, observations
from aqla_agent import trading as trading_module
from aqla_agent.observations import TemporaryObservation
from aqla_agent.router import Router
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp import api
from aqla_hyperliquid_mcp.models import now_ms
from aqla_hyperliquid_mcp.state import State


def entries(fake):
    return [action for action in fake.actions if action["type"] == "order" and any(not row["r"] for row in action["orders"])]


@pytest.fixture
def clocked(setup, monkeypatch):
    import conftest
    stamp = [now_ms()]
    for module in (entry, observations, trading_module, api, conftest):
        monkeypatch.setattr(module, "now_ms", lambda: stamp[0])
    exchange = setup[3]
    original = exchange.account_snapshot
    async def account(market):
        return (await original(market)).model_copy(update={"observed_at": stamp[0]})
    monkeypatch.setattr(exchange, "account_snapshot", account)
    return stamp


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_at", ["signing", "audit_write", "sending_write"])
async def test_slow_local_preparation_reobserves_before_entry_transport(setup, signal, clocked, monkeypatch, delay_at):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original_update, original_sign = state.update, api.signing.sign_l1_action
    injected = False
    def update(callback):
        nonlocal injected
        result = original_update(callback)
        snap = state.snapshot()
        operation = snap["operations"].get("entry:" + plan["trade_id"])
        checks = snap["trades"].get(plan["trade_id"], {}).get("price_checks", [])
        if not injected and (delay_at == "sending_write" and operation and operation["phase"] == "sending"
                             or delay_at == "audit_write" and any(row["phase"] == "before_send" for row in checks)):
            injected = True
            clocked[0] += 2001
        return result
    def signing(*args, **kwargs):
        nonlocal injected
        result = original_sign(*args, **kwargs)
        if not injected:
            injected = True
            clocked[0] += 2001
        return result
    monkeypatch.setattr(state, "update", update)
    if delay_at == "signing":
        monkeypatch.setattr(api.signing, "sign_l1_action", signing)
    original_transport = fake.handle
    def transport(request):
        if request.url.path == "/exchange":
            action = json.loads(request.content)["action"]
            if action["type"] == "order" and any(not row["r"] for row in action["orders"]):
                row = state.snapshot()["operations"]["entry:" + plan["trade_id"]]
                assert row["phase"] == "sending"
                assert 0 <= clocked[0] - exchange._test_last_quote.observed_at <= 2000
        return original_transport(request)
    original_quote = exchange.price_snapshot
    async def quote(market):
        value = await original_quote(market)
        exchange._test_last_quote = value
        return value
    monkeypatch.setattr(exchange, "price_snapshot", quote)
    exchange.client._transport = httpx.MockTransport(transport)
    result = await engine.execute(plan["plan_id"])
    assert injected and result["status"] == "open" and len(entries(fake)) == 1
    assert state.snapshot()["plans"][plan["plan_id"]]["plan"] == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "source", "ranking", "deadline"])
async def test_after_sending_persistence_last_admission_blocks_transport(setup, signal, clocked, monkeypatch, change):
    _, state, fake, _, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original_update = state.update
    injected = False
    def update(callback):
        nonlocal injected
        result = original_update(callback)
        row = state.snapshot()["operations"].get("entry:" + plan["trade_id"])
        if row and row["phase"] == "sending" and not injected:
            injected = True
            if change == "deadline":
                clocked[0] = plan["deadline_at"]
            else:
                def mutate(data):
                    if change == "pause":
                        data["paused"] = True
                    elif change == "ranking":
                        data["ranking_revision"] += 1
                    else:
                        edited = copy.deepcopy(data["events"][source.event_id])
                        edited["event_id"] = "source-new-revision"
                        edited["source"]["edited_at"] = clocked[0] + 1
                        edited["source"]["text"] += " updated"
                        data["events"][edited["event_id"]] = edited
                original_update(mutate)
        return result
    monkeypatch.setattr(state, "update", update)
    result = await engine.execute(plan["plan_id"])
    operation = state.snapshot()["operations"]["entry:" + plan["trade_id"]]
    assert injected and result["status"] == "rejected"
    assert operation["outcome"] == "not_sent" and operation["result"]["diagnostic"]["retryable"] is False
    assert not entries(fake)


@pytest.mark.asyncio
async def test_execute_read_failure_restart_recovery_holds_risk_and_executes_once(setup, signal, monkeypatch):
    settings, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original = exchange.account_snapshot
    async def unavailable(market):
        raise httpx.ReadTimeout("PRIVATE_PROVIDER_PASSWORD_MARKER")
    monkeypatch.setattr(exchange, "account_snapshot", unavailable)
    pending = await engine.execute(plan["plan_id"])
    assert pending["observation_retry"]["attempts"] == 1
    assert pending["reserved_risk"] == str(Decimal(plan["risk_usdc"]))
    assert not entries(fake)
    assert "PRIVATE_PROVIDER_PASSWORD_MARKER" not in json.dumps(state.snapshot())
    for _ in range(3):
        assert (await engine.execute(plan["plan_id"]))["observation_retry"]["attempts"] == 1
    # Reopen the actual durable SQLite state, not merely a new engine object.
    state.close()
    reopened = State(settings)
    try:
        exchange.state = reopened
        restarted = Trading(exchange, reopened)
        monkeypatch.setattr(exchange, "account_snapshot", original)
        stamp = now_ms()
        monkeypatch.setattr(observations, "now_ms", lambda: stamp + 30001)
        await asyncio.gather(restarted.recover(), restarted.recover())
        current = reopened.snapshot()["trades"][plan["trade_id"]]
        assert current["status"] == "open" and len(entries(fake)) == 1
        assert current["observation_retry"]["completed"]
        await restarted.recover()
        assert (await restarted.execute(plan["plan_id"]))["status"] == "open"
        assert len(entries(fake)) == 1
    finally:
        exchange.state = state
        reopened.close()


@pytest.mark.asyncio
async def test_known_unsent_wire_preserved_new_attempt_reuses_accepted_leverage(setup, signal, clocked, monkeypatch):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original = exchange.price_snapshot
    fail = True
    async def prices(market):
        row = state.snapshot()["operations"].get("entry:" + plan["trade_id"])
        if row and row["phase"] == "sending" and fail:
            raise TemporaryObservation("price_stale", "price")
        return await original(market)
    monkeypatch.setattr(exchange, "price_snapshot", prices)
    pending = await engine.execute(plan["plan_id"])
    old = copy.deepcopy(state.snapshot()["operations"]["entry:" + plan["trade_id"]])
    assert old["outcome"] == "not_sent" and not entries(fake)
    assert pending["status"] == "preparing"
    fail = False
    clocked[0] += 30001
    await engine.recover()
    current = state.snapshot()["trades"][plan["trade_id"]]
    assert current["status"] == "open"
    assert state.snapshot()["operations"][old["operation_id"]] == old
    assert current["entry_operation_id"] != old["operation_id"]
    assert current["entry_attempts"] == [old["operation_id"]]
    assert sum(row["type"] == "updateLeverage" for row in fake.actions) == 1
    assert len(entries(fake)) == 1
    new = state.snapshot()["operations"][current["entry_operation_id"]]
    assert new["nonce"] > old["nonce"] and new["request"]["deadline"] == old["request"]["deadline"]


@pytest.mark.asyncio
async def test_retry_exhaustion_and_historical_not_sent_never_resurrect(setup, signal, monkeypatch):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    async def bad(market):
        raise TemporaryObservation("account_stale", "account")
    monkeypatch.setattr(exchange, "account_snapshot", bad)
    current = await engine.execute(plan["plan_id"])
    for attempt in (2, 3, 4, 5):
        marker = current["observation_retry"]
        monkeypatch.setattr(observations, "now_ms", lambda marker=marker: marker["next_retry_at"])
        current = await engine.resume_unsent_entry(current["trade_id"])
        assert current["observation_retry"]["attempts"] == attempt
        assert current["observation_retry"]["next_retry_at"] <= plan["deadline_at"]
    assert current["status"] == "rejected" and current["reserved_risk"] == "0"
    before = copy.deepcopy(state.snapshot()["trades"][plan["trade_id"]])
    await engine.recover()
    assert (await engine.execute(plan["plan_id"])) == before
    state.update(lambda data: data["trades"][plan["trade_id"]].pop("observation_retry"))
    historical = copy.deepcopy(state.snapshot()["trades"][plan["trade_id"]])
    await Trading(exchange, state).recover()
    assert state.snapshot()["trades"][plan["trade_id"]] == historical and not entries(fake)


@pytest.mark.asyncio
async def test_preview_failure_stays_ready_preview_alone_never_autoruns(setup, signal, monkeypatch):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, interpretation = signal
    original = exchange.account_snapshot
    async def bad(market):
        raise TemporaryObservation("account_stale", "account")
    monkeypatch.setattr(exchange, "account_snapshot", bad)
    first = await router.preview(source.event_id, interpretation)
    assert first["targets"][0]["status"] == "pending"
    assert first["targets"][0]["observation_retry"]["authority"] == "preview"
    from aqla_agent.source_queue import queue_overview
    assert queue_overview(state.snapshot())["ready_total"] == 1
    again = await router.preview(source.event_id, interpretation)
    assert again["targets"][0]["observation_retry"]["attempts"] == 1
    marker = first["targets"][0]["observation_retry"]
    monkeypatch.setattr(observations, "now_ms", lambda: marker["next_retry_at"])
    monkeypatch.setattr(exchange, "account_snapshot", original)
    plan = await router.preview(source.event_id, interpretation)
    assert plan["plan_id"] and queue_overview(state.snapshot())["ready_total"] == 1
    await router.recover()
    assert state.snapshot()["trades"] == {} and fake.actions == []
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ValueError, KeyError, RuntimeError])
async def test_unclassified_entry_failure_retains_safe_phase_and_exception_type(setup, signal, monkeypatch, kind):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original = exchange.price_snapshot
    async def failure(market):
        row = state.snapshot()["operations"].get("entry:" + plan["trade_id"])
        if row and row["phase"] == "sending":
            raise kind("NEVER_STORE_PROVIDER_SECRET_MARKER")
        return await original(market)
    monkeypatch.setattr(exchange, "price_snapshot", failure)
    result = await engine.execute(plan["plan_id"])
    row = state.snapshot()["operations"]["entry:" + plan["trade_id"]]
    assert result["status"] == "rejected" and not entries(fake)
    assert row["result"]["diagnostic"]["exception_type"] == kind.__name__
    assert row["result"]["diagnostic"]["stage"] == "transport_guard"
    assert row["result"]["diagnostic"]["retryable"] is False
    assert "NEVER_STORE_PROVIDER_SECRET_MARKER" not in json.dumps(state.snapshot())


@pytest.mark.asyncio
async def test_lost_entry_response_after_retry_is_reconciled_not_resubmitted(setup, signal, monkeypatch):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original = exchange.account_snapshot
    async def bad(market):
        raise TemporaryObservation("account_stale", "account")
    monkeypatch.setattr(exchange, "account_snapshot", bad)
    pending = await engine.execute(plan["plan_id"])
    monkeypatch.setattr(exchange, "account_snapshot", original)
    monkeypatch.setattr(observations, "now_ms", lambda: pending["observation_retry"]["next_retry_at"])
    fake.lose_entry_response = True
    await engine.recover()
    await Trading(exchange, state).recover()
    await engine.recover()
    assert len(entries(fake)) == 1 and state.snapshot()["trades"][plan["trade_id"]]["status"] == "open"


@pytest.mark.asyncio
async def test_original_deadline_expires_pending_retry_without_rearming(setup, signal, monkeypatch):
    _, state, fake, exchange, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    async def bad(market):
        raise TemporaryObservation("account_stale", "account")
    monkeypatch.setattr(exchange, "account_snapshot", bad)
    await engine.execute(plan["plan_id"])
    monkeypatch.setattr(trading_module, "now_ms", lambda: plan["deadline_at"])
    await engine.recover()
    current = state.snapshot()["trades"][plan["trade_id"]]
    assert current["status"] == "cancelled" and current["reserved_risk"] == "0"
    assert current["observation_retry"]["exhausted"] and not entries(fake)


@pytest.mark.asyncio
async def test_slow_final_snapshot_copy_never_sends_stale_entry(setup, signal, clocked, monkeypatch):
    _, state, fake, _, engine = setup
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    original_snapshot = state.snapshot
    delayed = False
    def snapshot():
        nonlocal delayed
        result = original_snapshot()
        row = result["operations"].get("entry:" + plan["trade_id"])
        if row and row["phase"] == "sending":
            clocked[0] += 2001
            delayed = True
        return result
    monkeypatch.setattr(state, "snapshot", snapshot)
    current = await engine.execute(plan["plan_id"])
    assert delayed and not entries(fake)
    assert current["observation_retry"]["code"] == "price_stale"
    assert state.snapshot()["operations"]["entry:" + plan["trade_id"]]["outcome"] == "not_sent"


@pytest.mark.asyncio
async def test_interrupted_retry_claims_consume_budget_and_preserve_deadline(setup, signal, monkeypatch):
    _, state, fake, exchange, engine = setup
    state.update(lambda data: data["policy"].update(max_signal_age_seconds=3600))
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    async def bad(market):
        raise TemporaryObservation("account_stale", "account")
    monkeypatch.setattr(exchange, "account_snapshot", bad)
    current = await engine.execute(plan["plan_id"])
    for attempt in (2, 3, 4, 5):
        marker = current["observation_retry"]
        monkeypatch.setattr(observations, "now_ms", lambda marker=marker: marker["next_retry_at"])
        current = engine.rearm_proven_unsent(current["trade_id"])
        assert current["observation_retry"]["attempts"] == attempt
        assert current["observation_retry"]["attempt_inflight"]
        assert state.snapshot()["plans"][plan["plan_id"]]["observation_retry"] == current["observation_retry"]
        # Abrupt process death before any await/order: durable claim survives.
        engine = Trading(exchange, state)
    marker = current["observation_retry"]
    monkeypatch.setattr(observations, "now_ms", lambda: marker["next_retry_at"])
    await engine.recover()
    current = state.snapshot()["trades"][plan["trade_id"]]
    assert current["status"] == "cancelled" and current["observation_retry"]["exhausted"]
    assert current["observation_retry"]["deadline_at"] == plan["deadline_at"] and not entries(fake)
