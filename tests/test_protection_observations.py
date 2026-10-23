
import copy
import json

import httpx
import pytest

from aqla_agent.observations import TemporaryObservation
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, TradePlan, require_first
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


@pytest.mark.asyncio
async def test_inventory_socket_refresh_disconnect_holds_managed_protection_without_orders(setup, monkeypatch):
    from test_inventory_stream import Socket, inputs
    from test_managed_entries import opened_zone
    from websockets.exceptions import ConnectionClosedError

    from aqla_agent.inventory_stream import observe
    state, fake, engine, _plan, trade = await opened_zone(setup)
    first = trade["entry_orders"][0]
    fake.fill(first["cloid"], first["quantity"], first["price"])
    await engine.refresh_trade(trade["trade_id"])
    before, actions = copy.deepcopy(state.snapshot()), copy.deepcopy(fake.actions)
    adapter, names, frames = inputs()
    socket = Socket(frames)
    await observe(adapter, names, connector=lambda *_, **__: socket)
    async def disconnect(_):
        raise ConnectionClosedError(None, None)
    socket.send = disconnect
    original = engine.exchange.account_snapshot
    async def unavailable(_):
        return await observe(adapter, names, connector=lambda *_, **__: socket)
    monkeypatch.setattr(engine.exchange, "account_snapshot", unavailable)
    with pytest.raises(TemporaryObservation) as error:
        await engine.refresh_trade(trade["trade_id"])
    assert error.value.stage == "inventory" and error.value.category == "transport"
    held = state.snapshot()["trades"][trade["trade_id"]]
    assert held["protection_observation_pending"]["retryable"] and not held.get("execution_incident")
    assert not held.get("exit_orders") and not held.get("entries_disabled")
    assert state.snapshot()["paused"] == before["paused"] and state.snapshot()["operations"] == before["operations"]
    assert fake.actions == actions
    monkeypatch.setattr(engine.exchange, "account_snapshot", original)
    await engine.refresh_trade(trade["trade_id"])
    assert not state.snapshot()["trades"][trade["trade_id"]].get("protection_observation_pending")
    assert fake.actions == actions


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,nth', [('sl', 2), ('sl', 3), ('tp', 2)])
@pytest.mark.parametrize('failure', ['timeout', 'rate_limit'])
async def test_protection_read_outage_recovers_without_close_or_disabling_account(setup, signal, kind, nth, failure):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    engine = router.engines['hyperliquid']
    source, interpretation = signal
    preview = await router.preview(source.event_id, interpretation)
    trade = await router.execute(preview['targets'][0]['plan_id'])
    trade_id = trade['trade_id']
    identity = require_first(row['cloid'] for row in trade['orders'] if row['kind'] == kind)
    before = copy.deepcopy(state.snapshot())
    actions = copy.deepcopy(fake.actions)
    original, calls = fake.handle, 0

    def unavailable(request):
        nonlocal calls
        payload = json.loads(request.content)
        if payload.get('type') == 'orderStatus' and payload.get('oid') == identity:
            calls += 1
            if calls >= nth:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('private transport payload')
                return httpx.Response(429, json={'private': 'provider body'})
        return original(request)

    exchange.client._transport = httpx.MockTransport(unavailable)
    try:
        reports = await router.recover()
        after = state.snapshot()
        assert any(row.get('error') for row in reports)
        assert not engine.recovery_checked
        assert after['account_recovery']['hyperliquid:testnet']['category'] == 'incomplete'
        assert after['account_enabled'] == before['account_enabled']
        assert after['venue_enabled'] == before['venue_enabled']
        assert after['trades'][trade_id]['exit_orders'] == []
        assert after['trades'][trade_id]['protection_observation_pending']['retryable']
        assert not after['trades'][trade_id].get('execution_incident')
        assert not any(key.startswith('protection:') for key in after['notifications'])
        assert fake.actions == actions and after['operations'] == before['operations']
        with pytest.raises(TemporaryObservation):
            engine.validate_admission(engine.state.snapshot(), TradePlan.model_validate(trade['plan']), owned=True)
        with pytest.raises(AqlaError, match='reconciliation is incomplete'):
            await engine.execute(preview['targets'][0]['plan_id'])
        assert fake.actions == actions
        exchange.client._transport = httpx.MockTransport(original)
        await router.recover()
        recovered = state.snapshot()
        assert engine.recovery_checked
        assert recovered['account_recovery']['hyperliquid:testnet']['category'] == 'complete'
        assert recovered['trades'][trade_id]['status'] == 'open'
        assert not recovered['trades'][trade_id].get('protection_observation_pending')
        assert not recovered['trades'][trade_id].get('waiting_error')
        assert recovered['account_enabled'] == before['account_enabled']
        assert fake.actions == actions
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_protection_read_recovery_preserves_operator_pause(setup, signal, monkeypatch):
    settings, state, _, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, interpretation = signal
    preview = await router.preview(source.event_id, interpretation)
    trade = await router.execute(preview['targets'][0]['plan_id'])
    engine = router.engines['hyperliquid']
    original = engine.ensure_protection

    async def unavailable(*_):
        raise TemporaryObservation('read_unavailable', 'order_status', 'transport')

    monkeypatch.setattr(engine, 'ensure_protection', unavailable)
    await router.recover()
    state.update(lambda data: data.update(paused=True))
    engine.state.pause_entries('Operator review required')
    monkeypatch.setattr(engine, 'ensure_protection', original)
    await router.recover()
    after = state.snapshot()
    assert after['paused'] and not after['account_enabled']['hyperliquid:testnet']
    assert after['trades'][trade['trade_id']]['status'] == 'open'
    await router.close()


@pytest.mark.asyncio
async def test_restart_retains_protection_hold_for_unsent_and_waiting_entries(setup, signal):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, interpretation = signal
    preview = await router.preview(source.event_id, interpretation)
    trade = await router.execute(preview['targets'][0]['plan_id'])
    actions = copy.deepcopy(fake.actions)

    def pending(data):
        data['trades'][trade['trade_id']]['protection_observation_pending'] = TemporaryObservation('read_unavailable', 'order_status', 'transport').diagnostic()
        row = copy.deepcopy(trade)
        row.update(trade_id='waiting', status='waiting_entry', filled_quantity='0', remaining_quantity='0', orders=[], fill_events={})
        data['trades']['waiting'] = row

    state.update(pending)
    state.close()
    restored = State(settings)
    try:
        router.state = restored
        engine = router.engines['hyperliquid']
        engine.state.root = restored
        engine.recovery_checked = False
        waiting = restored.snapshot()['trades']['waiting']
        assert await engine.check_waiting_entry('waiting') == waiting
        assert await engine.resume_unsent_entry('waiting') == waiting
        with pytest.raises(TemporaryObservation):
            engine.validate_admission(engine.state.snapshot(), TradePlan.model_validate(waiting['plan']), owned=True)
        assert fake.actions == actions
    finally:
        await router.close()
        restored.close()


@pytest.mark.asyncio
async def test_summary_counts_owned_positions_separately_from_three_conflicts(setup, signal):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    router = server.aqla_trading
    source, interpretation = signal
    preview = await router.preview(source.event_id, interpretation)
    trade = await router.execute(preview['targets'][0]['plan_id'])

    def conflicts(data):
        for index in range(3):
            row = copy.deepcopy(trade)
            row.update(trade_id=f'conflict-{index}', status='conflicted')
            data['trades'][row['trade_id']] = row

    state.update(conflicts)
    try:
        response = await server.call_tool('status', {'view': 'summary'})
        summary = response.structured_content or json.loads(response.content[0].text)
        assert summary['open_trade_count'] == 1
        assert summary['active_trade_count'] == 4
        assert summary['conflicted_trade_count'] == 3
        assert summary['execution_counts']['unsettled_ownership'] == 3
    finally:
        await close_server(server)
