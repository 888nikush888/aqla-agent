import asyncio
import copy
import json
from decimal import Decimal

import pytest
from test_hermes_integration import message

from aqla_agent.events import EXTENSIONS, run_native_turn
from aqla_hyperliquid_mcp.models import AqlaError

pytest_plugins = ['test_hermes_integration']


def notice(ext, *, version='new', processed=True, stale=False):
    payload = {'notification_versions': {'n': version}, 'telegram_view': {'messages': [
        {'notification_ids': ['n'], 'notification_versions': {'n': version}, 'texts': ['Confirmed entry event']}]}}
    ext.after_tool('mcp__aqla__notifications_read', {}, json.dumps({'result': json.dumps(payload)}))
    if processed:
        ext.after_tool('mcp__aqla__notifications_ack', {'versions': {'n': version}},
            json.dumps({'result': json.dumps({'processed': [] if stale else ['n'], 'stale': ['n'] if stale else []})}))


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('stale', [False, True])
async def test_automatic_native_turn_publishes_only_versioned_event_facts(extension, monkeypatch, changed, stale):
    ext, _ = extension
    ext.efficiency['usage_metrics'] = False
    incoming = message(injection=True)
    gateway = object()
    ext.gateway = gateway
    monkeypatch.setattr(ext, 'refresh_master', lambda *_: None)
    EXTENSIONS.add(ext)
    async def callback():
        claim = ext.turn_claim.get()
        ext.after_tool('mcp__aqla__status', {}, json.dumps({'view': 'summary', 'notification_pending': 0}))
        if changed:
            notice(ext, stale=stale)
        # The plugin transforms before persistence and before queued final delivery.
        transformed = ext.transform_output(response_text='Status-Runde: Unverändert; nothing to do')
        assert transformed == ('Confirmed entry event' if changed and not stale else 'NO_REPLY')
        return {'final_response': transformed, 'completed': True, 'claim': claim}
    try:
        result = await ext.with_event(incoming, gateway, lambda: run_native_turn(gateway, incoming.source, callback))
        assert result['final_response'] == ('Confirmed entry event' if changed and not stale else 'NO_REPLY')
        assert result['response_transformed']
        assert ext.active_context is None
    finally:
        EXTENSIONS.discard(ext)


def test_operator_responses_and_unacknowledged_or_other_turn_notices_are_not_published(extension):
    ext, _ = extension
    with ext.claim_scope(('operator', 'owner')):
        assert ext.transform_output(response_text='Requested status') is None
        assert 'view' not in ext.before_tool('mcp__aqla__status', {'view': 'full'})['args']
        notice(ext)
        assert ext.source_reply(ext.turn_claim.get()) == 'NO_REPLY'
    with ext.claim_scope(('source', 'wake')):
        claim = ext.turn_claim.get()
        claim.automatic = True
        assert ext.before_tool('mcp__aqla__status', {'view': 'full'})['args']['view'] == 'summary'
        notice(ext, processed=False)
        assert ext.transform_output(response_text='should not send') == 'NO_REPLY'
        notice(ext)
        notice(ext)
        assert ext.source_reply(claim) == 'Confirmed entry event'
    with ext.claim_scope(('source', 'next-wake')):
        assert ext.source_reply(ext.turn_claim.get()) == 'NO_REPLY'


@pytest.mark.asyncio
async def test_persistent_reader_outage_is_announced_only_on_transition_with_ready_work_retained(extension, monkeypatch):
    ext, ctx = extension
    from aqla_agent.recovery_notices import reader_notice_signature
    ext._reader_status_observed = True
    ext.reader_error = {'code': 'reader_unavailable', 'sources': [], 'attempt': 14, 'next_retry_at': 123}
    ext.reported_reader_error = {'code': 'reader_unavailable', 'sources': [], 'attempt': 1, 'next_retry_at': 10}
    assert reader_notice_signature(ext.reader_error) == reader_notice_signature(ext.reported_reader_error)
    calls, wakes = [], []
    async def snapshot(*_):
        calls.append(1)
        if len(calls) > 2:
            raise asyncio.CancelledError
        return {'pending': ['unprocessed'], 'notification_pending': 0, 'source_queue': {'ready_total': 1}}
    async def sleep(*_):
        pass
    ext.call = snapshot
    ctx.inject_message = lambda text, **_: wakes.append(text) or True
    monkeypatch.setattr('aqla_agent.events.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await ext.worker()
    assert len(wakes) == 1 and 'unprocessed' in wakes[0]
    assert 'reader_unavailable' not in wakes[0] and 'next_retry_at' not in wakes[0]
    assert 'JEDE ready-ID' in wakes[0] and 'NO_REPLY' in wakes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('violation', ['equity_drift', 'stop_widened', 'legacy_without_snapshot'])
async def test_after_fill_risk_uses_equity_authorized_at_dispatch_and_preserves_hard_limits(setup, signal, monkeypatch, violation):
    _, state, fake, exchange, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan['plan_id'])
    identity = trade['trade_id']
    original = exchange.account_snapshot
    actual_risk = Decimal(trade['filled_quantity']) * (abs(Decimal(trade['entry_vwap']) - Decimal(trade['stop_price']))
        + Decimal(trade['entry_vwap']) * Decimal(plan['policy']['cost_reserve_bps']) / 10000)
    async def lower_equity(market):
        return (await original(market)).model_copy(update={
            'equity': actual_risk * 100 / Decimal(plan['policy']['risk_percent']) * Decimal('0.99')})
    monkeypatch.setattr(exchange, 'account_snapshot', lower_equity)
    def pending(data):
        row = data['trades'][identity]
        row.pop('entry_validated_fingerprint', None)
        if violation == 'stop_widened':
            row['stop_price'] = '1'
        if violation == 'legacy_without_snapshot':
            row.pop('entry_equity_snapshot', None)
    state.update(pending)
    actions = copy.deepcopy(fake.actions)
    if violation == 'equity_drift':
        await engine.validate_actual_entry(identity)
        check = state.snapshot()['trades'][identity]['price_checks'][-1]
        assert check['permitted'] and Decimal(check['accepted_equity']) > Decimal(check['observed_equity'])
    else:
        with pytest.raises(AqlaError, match='Confirmed entry violates'):
            await engine.validate_actual_entry(identity)
    assert fake.actions == actions


@pytest.mark.asyncio
async def test_source_update_without_own_position_is_terminal_instead_of_stalling_ready_queue(setup, signal):
    from test_updates import update

    from aqla_agent.router import Router
    from aqla_agent.source_queue import queue_overview
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = update(router, 2, 'break_even', 'Stop auf Einstand')
    result = await router.update_trade(source.event_id, value)
    assert result['status'] == 'rejected'
    assert state.snapshot()['events'][source.event_id]['update_disposition'] == 'no_matching_own_position'
    assert source.event_id not in queue_overview(state.snapshot())['ready']
    assert not fake.actions
    await router.close()
