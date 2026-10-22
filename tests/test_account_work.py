import asyncio

import pytest

from aqla_agent.account_work import AccountLock


@pytest.mark.asyncio
async def test_close_overtakes_routine_and_optional_work_and_is_reentrant():
    lock, order = AccountLock(), []
    async def work(name, priority):
        async with lock.work(priority):
            assert lock.locked()
            async with lock:
                order.append(name)
    async with lock:
        tasks = [asyncio.create_task(work(name, priority)) for name, priority in (("wave", 30), ("routine", 20), ("close", 1), ("reconcile", 0))]
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    assert order == ["reconcile", "close", "routine", "wave"] and not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_lose_the_account_lock():
    lock = AccountLock()
    async def work():
        async with lock:
            return True
    async with lock:
        canceled = asyncio.create_task(work())
        next_work = asyncio.create_task(work())
        await asyncio.sleep(0)
        canceled.cancel()
        await asyncio.gather(canceled, return_exceptions=True)
    assert await asyncio.wait_for(next_work, 1) and not lock.locked()


@pytest.mark.asyncio
async def test_recovery_releases_the_lock_between_real_trade_packets(setup, signal, monkeypatch):
    _, state, _, _, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    await engine.execute(plan["plan_id"])
    first, close_waiting = asyncio.Event(), asyncio.Event()
    order = []
    async def packet(*_):
        assert engine.lock.locked()
        order.append("routine")
        first.set()
        await close_waiting.wait()
        return []
    monkeypatch.setattr(engine, "_recover_trade", packet)
    current = state.snapshot()["trades"][plan["trade_id"]]
    state.update(lambda data: data["trades"].__setitem__("second", current))
    async def close():
        await first.wait()
        close_waiting.set()
        async with engine.lock.work(1):
            order.append("close")
    await asyncio.wait_for(asyncio.gather(engine.recover(), close()), 2)
    assert order == ["routine", "close", "routine"]
