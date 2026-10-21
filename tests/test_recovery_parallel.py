import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from aqla_agent.router import Router
from aqla_hyperliquid_mcp.server import close_server, create_server


def accounts(setup, **options):
    settings, state, _, exchange, _ = setup
    async def close():
        pass
    adapter = SimpleNamespace(settings=settings.model_copy(update={"account": "0x" + "9" * 40}),
        market_cache={}, close=close)
    return Router(settings, state, exchange, adapters={"aster": adapter}, **options)


@pytest.mark.asyncio
async def test_hanging_account_does_not_block_independent_protection_check(setup, monkeypatch):
    router = accounts(setup)
    entered, protected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow():
        entered.set()
        await release.wait()
        router.engines["hyperliquid"].recovery_checked = True
        return []
    async def fast():
        engine = router.engines["aster"]
        engine.state.assert_binding(engine.exchange.settings)
        engine.recovery_checked = True
        protected.set()
        return []
    monkeypatch.setattr(router.engines["hyperliquid"], "recover", slow)
    monkeypatch.setattr(router.engines["aster"], "recover", fast)
    task = asyncio.create_task(router.recover())
    await entered.wait()
    try:
        await asyncio.wait_for(protected.wait(), timeout=1)
    finally:
        release.set()
        await task
    assert protected.is_set()
    await router.close()


@pytest.mark.asyncio
async def test_recovery_parallelism_respects_constructor_limit_and_account_lock(setup, monkeypatch):
    router = accounts(setup, recovery_concurrency=1)
    entered, protected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow():
        assert router.engines["hyperliquid"].lock.locked()
        entered.set()
        await release.wait()
        return []
    async def fast():
        assert router.engines["aster"].lock.locked()
        protected.set()
        return []
    monkeypatch.setattr(router.engines["hyperliquid"], "recover", slow)
    monkeypatch.setattr(router.engines["aster"], "recover", fast)
    task = asyncio.create_task(router.recover())
    await entered.wait()
    await asyncio.sleep(0)
    assert not protected.is_set()
    release.set()
    await task
    assert protected.is_set()
    await router.close()


@pytest.mark.asyncio
async def test_account_timeout_and_fault_preserve_facts_and_emit_only_safe_diagnostics(setup, monkeypatch):
    router = accounts(setup, recovery_timeout_seconds=.02)
    state = setup[1]
    state.update(lambda data: data.update(account_recovery={"hyperliquid:testnet": {"last_success_at": 123}}))
    before = copy.deepcopy(state.snapshot())
    async def hanging():
        await asyncio.Event().wait()
    async def faulty():
        raise RuntimeError("untrusted-provider-payload")
    monkeypatch.setattr(router.engines["hyperliquid"], "recover", hanging)
    monkeypatch.setattr(router.engines["aster"], "recover", faulty)
    rows = await router.recover()
    after = state.snapshot()
    diagnostic = after["account_recovery"]
    assert diagnostic["hyperliquid:testnet"]["category"] == "timeout"
    assert diagnostic["hyperliquid:testnet"]["last_success_at"] == 123
    assert diagnostic["aster:testnet"]["category"] == "unavailable"
    assert all(row["duration_ms"] >= 0 for row in diagnostic.values())
    assert "untrusted-provider-payload" not in str(diagnostic) and "untrusted-provider-payload" not in str(rows)
    assert after["trades"] == before["trades"] and after["operations"] == before["operations"]
    assert router.engines["hyperliquid"].recovery_checked is False
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 3, 8])
async def test_serial_trade_budget_is_scoped_to_each_account(setup, signal, monkeypatch, count):
    router = accounts(setup, recovery_timeout_seconds=1)
    source, interpretation = signal
    plan = await setup[4].preview(source.event_id, interpretation)
    def seed(data):
        for index in range(count):
            data["trades"][str(index)] = {"plan": copy.deepcopy(plan), "status": "open", "remaining_quantity": "1"}
        data["trades"]["closed"] = {"plan": copy.deepcopy(plan), "status": "closed"}
    setup[1].update(seed)
    timeouts = []
    wait_for = asyncio.wait_for
    async def bounded(work, *, timeout):
        timeouts.append(timeout)
        return await wait_for(work, timeout=timeout)
    async def recovered(engine):
        engine.recovery_checked = True
        return []
    monkeypatch.setattr(asyncio, "wait_for", bounded)
    monkeypatch.setattr(router, "_recover_account", recovered)
    try:
        await router.recover()
        assert sorted(timeouts) == sorted([max(1, count), 1])
        assert setup[1].snapshot()["account_recovery"]["hyperliquid:testnet"]["category"] == "complete"
        assert setup[2].actions == []
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_all_recovery_accounts_and_releases_locks(setup, monkeypatch):
    router = accounts(setup)
    entered = {name: asyncio.Event() for name in router.engines}
    canceled = set()
    for name, engine in router.engines.items():
        async def wait(identity=name):
            entered[identity].set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.add(identity)
        monkeypatch.setattr(engine, "recover", wait)
    task = asyncio.create_task(router.recover())
    await asyncio.gather(*(event.wait() for event in entered.values()))
    await router.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert canceled == set(entered)
    assert all(not engine.lock.locked() for engine in router.engines.values())


@pytest.mark.asyncio
async def test_status_recovery_diagnostics_are_bounded_and_discard_untrusted_extra_fields(setup):
    settings, state, _, exchange, _ = setup
    record = {"last_attempt_at": 100, "last_success_at": 90, "duration_ms": 5, "category": "complete", "error_count": 0,
              "untrusted_extra": "provider-payload" * 1000}
    state.update(lambda data: data.update(account_recovery={"hyperliquid:testnet": record,
        **{"unexpected-" + str(index): record for index in range(100)}}))
    server = create_server(settings, state=state, exchange=exchange)
    before = state.snapshot()
    try:
        result = await server.call_tool("status", {})
        payload = json.loads(result.content[0].text)["account_recovery"]
        assert payload == {"hyperliquid:testnet": {key: value for key, value in record.items() if key != "untrusted_extra"}}
        assert "provider-payload" not in str(payload)
        assert state.snapshot() == before
    finally:
        await close_server(server)
