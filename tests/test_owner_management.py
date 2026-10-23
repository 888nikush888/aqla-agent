import copy
from decimal import Decimal

import pytest

from aqla_agent.operator_management import ManagementRequest, dispatch, fingerprint, submit
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError


async def opened(setup, signal):
    settings, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    return state, fake, Router(settings, state, exchange), trade


@pytest.mark.asyncio
async def test_owner_reduction_has_one_fixed_quantity_and_identity(setup, signal):
    state, fake, router, trade = await opened(setup, signal)
    original = copy.deepcopy(trade["plan"])
    request = ManagementRequest(trade_id=trade["trade_id"], action="reduce", percent=50, expected_fingerprint=fingerprint(trade))
    await submit(router, request, "one", lambda: None)
    sent = len(fake.actions)
    result = await submit(router, request, "one", lambda: None)
    assert result["duplicate"] and len(fake.actions) == sent
    saved = state.snapshot()["operator_requests"]["operator:one"]
    assert saved["original_quantity"] == trade["remaining_quantity"]
    assert state.snapshot()["trades"][trade["trade_id"]]["plan"] == original
    assert "operator:one" in state.snapshot()["operations"]
    await router.close()


@pytest.mark.asyncio
async def test_owner_stop_uses_real_pending_stop_recovery_without_a_source(setup, signal):
    state, fake, router, trade = await opened(setup, signal)
    events = copy.deepcopy(state.snapshot()["events"])
    request = ManagementRequest(trade_id=trade["trade_id"], action="move_stop", stop=95, expected_fingerprint=fingerprint(trade))
    result = await submit(router, request, "stop", lambda: None)
    current = state.snapshot()
    assert result["status"] == "done"
    assert current["events"] == events
    assert Decimal(current["trades"][trade["trade_id"]]["stop_price"]) == Decimal(95)
    assert current["trades"][trade["trade_id"]]["pending_stop_update"]["source_revisions"] == {}
    assert current["operator_requests"]["operator:stop"]["status"] == "done"
    await router.close()


@pytest.mark.asyncio
async def test_stale_owner_action_and_stop_widening_never_send_or_persist(setup, signal):
    state, fake, router, trade = await opened(setup, signal)
    count = len(fake.actions)
    for base, stop in (("0" * 64, 95), (fingerprint(trade), 80)):
        request = ManagementRequest(trade_id=trade["trade_id"], action="move_stop", stop=stop, expected_fingerprint=base)
        with pytest.raises(AqlaError):
            await submit(router, request, "invalid", lambda: None)
    assert len(fake.actions) == count and not state.snapshot().get("operator_requests")
    await router.close()


@pytest.mark.asyncio
async def test_owner_request_survives_crash_before_native_dispatch(setup, signal, monkeypatch):
    state, fake, router, trade = await opened(setup, signal)
    request = ManagementRequest(trade_id=trade["trade_id"], action="reduce", percent=25, expected_fingerprint=fingerprint(trade))
    async def crash(*_):
        raise RuntimeError("simulated process loss")
    with monkeypatch.context() as patch:
        patch.setattr("aqla_agent.operator_management.dispatch", crash)
        with pytest.raises(RuntimeError):
            await submit(router, request, "restart", lambda: None)
    saved = state.snapshot()["operator_requests"]["operator:restart"]
    assert saved["status"] == "pending"
    engine = router.engine_for_plan(trade["plan"])
    await dispatch(engine, "operator:restart")
    count = len(fake.actions)
    await dispatch(engine, "operator:restart")
    assert len(fake.actions) == count
    await router.close()


@pytest.mark.asyncio
async def test_an_unclear_existing_order_blocks_a_new_management_identity(setup, signal):
    state, fake, router, trade = await opened(setup, signal)
    identity = next(iter(state.snapshot()["operations"]))
    state.update(lambda value: value["operations"][identity].update(phase="unknown"))
    count = len(fake.actions)
    request = ManagementRequest(trade_id=trade["trade_id"], action="reduce", percent=25, expected_fingerprint=fingerprint(trade))
    with pytest.raises(AqlaError, match="Ungeklärte Börsenoperation"):
        await submit(router, request, "another", lambda: None)
    assert len(fake.actions) == count and not state.snapshot().get("operator_requests")
    await router.close()
