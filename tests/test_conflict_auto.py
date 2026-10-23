
import copy
from decimal import Decimal

import httpx
import pytest
from test_operator_recovery import conflicted, manual_fill, terminal_orders

from aqla_agent.conflict_review import observe_conflict
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, digest, now_ms, require_first
from aqla_hyperliquid_mcp.state import State


def native_entry(fake, *, start='0'):
    fake.fills[0].update(startPosition=start, dir='Open Long', side='B')


def foreign_fill(fake, amount, start, side, stamp, oid):
    fake.fills.append({'oid': oid, 'tid': oid, 'coin': 'BTC', 'time': stamp, 'sz': str(amount), 'px': '110',
        'fee': '0.01', 'feeToken': 'USDC', 'startPosition': str(start), 'side': side,
        'dir': 'Open Long' if side == 'B' else 'Close Long'})


@pytest.mark.asyncio
async def test_recovery_resolves_proved_manual_close_without_setup_or_exchange_writes(setup, signal):
    settings, _, _, exchange, _ = setup
    state, fake, _, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    manual_fill(fake, own, start=own)
    fake.position = 0
    terminal_orders(fake)
    before = copy.deepcopy(state.snapshot())
    actions = copy.deepcopy(fake.actions)
    router = Router(settings, state, exchange)
    reports = await router.recover()
    after = state.snapshot()
    assert any(row.get('conflict_resolved') for row in reports)
    assert after['account_recovery']['hyperliquid:testnet']['category'] == 'complete'
    row = after['trades'][identity]
    assert row['status'] == 'closed' and row['reserved_risk'] == row['remaining_quantity'] == '0'
    assert row['manual_closure']['pnl_complete'] is False
    assert all(row['fill_events'][key] == value for key, value in before['trades'][identity]['fill_events'].items())
    assert row['plan'] == before['trades'][identity]['plan'] and after['operations'] == before['operations']
    assert fake.actions == actions
    generation = copy.deepcopy(after['notifications'])
    await router.recover()
    assert state.snapshot()['notifications'] == generation and fake.actions == actions
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mixed', [False, True])
async def test_native_flat_boundary_resolves_old_trade_and_preserves_later_foreign_position(setup, signal, mixed):
    state, fake, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    native_entry(fake, start='1' if mixed else '0')
    stamp = max(now_ms(), fake.fills[0]['time']) + 1
    if mixed:
        foreign_fill(fake, '0.5', own + 1, 'B', stamp, 9100)
    total = own + Decimal('1.5') if mixed else own
    foreign_fill(fake, total, total, 'A', stamp + 1, 9200)
    foreign_fill(fake, '2', '0', 'B', stamp + 2, 9300)
    fake.position = 2
    terminal_orders(fake)
    original = copy.deepcopy(state.snapshot()['trades'][identity]['fill_events'])
    actions, orders = copy.deepcopy(fake.actions), copy.deepcopy(fake.orders)
    review, _ = await observe_conflict(engine, identity)
    assert review['resolvable'] and review['position_size'] == '2'
    await engine.recover()
    row = state.snapshot()['trades'][identity]
    assert row['status'] == 'closed' and row['closed_at'] == stamp + 1
    assert row['fill_events'] == original
    assert row['manual_closure']['basis'] == 'proved_account_flat_after_last_owned_fill'
    assert row['manual_closure']['pnl_complete'] is False
    assert all(fill['oid'] != 9300 for fill in row['manual_closure']['account_position_proof'])
    assert fake.position == 2 and fake.actions == actions and fake.orders == orders


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_fill', 'changed_fill', 'partial', 'missing_start', 'live_order',
    'unknown_operation', 'wrong_order', 'entry_quantity', 'late_own_fill', 'read_timeout'])
async def test_automatic_resolution_requires_complete_identity_and_flat_boundary(setup, signal, case, monkeypatch):
    state, fake, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    native_entry(fake)
    stamp = max(now_ms(), fake.fills[0]['time']) + 1
    foreign_fill(fake, '1', own, 'B', stamp, 9100)
    foreign_fill(fake, own + 1, own + 1, 'A', stamp + 1, 9200)
    fake.position = 0
    if case != 'live_order':
        terminal_orders(fake)
    if case == 'missing_fill':
        fake.fills.pop(1)
    elif case == 'changed_fill':
        fake.fills[0]['fee'] = '999'
    elif case == 'partial':
        fake.fills[-1]['sz'] = str(own)
        fake.position = 1
    elif case == 'missing_start':
        fake.fills[-1].pop('startPosition')
    elif case == 'unknown_operation':
        operation = require_first(iter(state.snapshot()['operations']))
        state.update(lambda data: data['operations'][operation].update(phase='unknown'))
        async def unavailable(*_):
            raise AqlaError('Reconciliation unavailable')
        monkeypatch.setattr(engine.exchange, 'reconcile', unavailable)
    elif case == 'wrong_order':
        fake.orders[state.snapshot()['trades'][identity]['entry_cloid']]['order']['cloid'] = 'foreign'
    elif case == 'entry_quantity':
        fake.orders[state.snapshot()['trades'][identity]['entry_cloid']]['order']['origSz'] = str(own + 1)
    elif case == 'late_own_fill':
        fake.fills.append({**fake.fills[0], 'tid': 99999, 'sz': '0.01', 'time': stamp + 2, 'startPosition': '0'})
    elif case == 'read_timeout':
        async def timeout(*_):
            raise httpx.ReadTimeout('private provider error')
        monkeypatch.setattr(engine.exchange, 'order_status', timeout)
    actions = copy.deepcopy(fake.actions)
    await engine.recover()
    assert state.snapshot()['trades'][identity]['status'] == 'conflicted'
    assert fake.actions == actions


@pytest.mark.asyncio
async def test_native_same_millisecond_fill_hashes_follow_position_chain(setup, signal):
    state, fake, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    native_entry(fake)
    stamp = max(now_ms(), fake.fills[0]['time']) + 1
    foreign_fill(fake, '0.3', own, 'B', stamp, 9900)
    foreign_fill(fake, '0.2', own + Decimal('0.3'), 'B', stamp, 9000)
    foreign_fill(fake, own + Decimal('0.5'), own + Decimal('0.5'), 'A', stamp + 1, 9200)
    fake.position = 0
    terminal_orders(fake)
    await engine.recover()
    row = state.snapshot()['trades'][identity]
    assert row['status'] == 'closed'
    assert [fill['oid'] for fill in row['manual_closure']['account_position_proof']][1:3] == [9900, 9000]


@pytest.mark.asyncio
async def test_changed_local_trade_during_proof_cannot_commit(setup, signal, monkeypatch):
    state, fake, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    manual_fill(fake, own, start=own)
    fake.position = 0
    terminal_orders(fake)
    original = engine._fills
    async def changed(plan):
        rows = await original(plan)
        state.update(lambda data: data['trades'][identity].update(management_revision=999))
        return rows
    monkeypatch.setattr(engine, '_fills', changed)
    await engine.recover()
    assert state.snapshot()['trades'][identity]['status'] == 'conflicted'


@pytest.mark.asyncio
async def test_restart_keeps_resolved_conflict_terminal_without_repeated_notice(setup, signal):
    settings, state, fake, exchange, _ = setup
    _, _, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    manual_fill(fake, own, start=own)
    fake.position = 0
    terminal_orders(fake)
    await engine.recover()
    before = copy.deepcopy(state.snapshot())
    state.close()
    restored = State(settings)
    router = Router(settings, restored, exchange)
    try:
        await router.recover()
        assert digest(restored.snapshot()['trades'][identity]) == digest(before['trades'][identity])
        assert restored.snapshot()['notifications'] == before['notifications']
    finally:
        await router.close()
        restored.close()
