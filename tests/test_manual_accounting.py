import copy
from decimal import Decimal

import httpx
import pytest
from oracles import journal_02765ac as journal_oracle
from test_conflict_auto import foreign_fill, native_entry
from test_operator_recovery import conflicted, terminal_orders

from aqla_agent.funding import payment
from aqla_agent.journal import HEADERS, project
from aqla_agent.manual_accounting import verified_manual_accounting
from aqla_agent.monthly import report_row
from aqla_agent.presentation import PresentationScope, build_presentation
from aqla_agent.reporting import Reporting
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, now_ms


async def manual_trade(setup, signal, monkeypatch, *, mixed=False):
    settings, state, fake, exchange, _ = setup
    state.update(lambda data: [target.update(fallback_exchanges=[]) for pipeline in data['pipelines'].values() for target in pipeline['targets']])
    original = fake.handle
    def with_fee_currency(request):
        result = original(request)
        for fill in fake.fills:
            fill.setdefault('feeToken', 'USDC')
        return result
    exchange.client._transport = httpx.MockTransport(with_fee_currency)
    _, _, engine, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()['trades'][identity]['filled_quantity'])
    native_entry(fake, start=str(own * 2) if mixed else '0')
    stamp = max(now_ms(), fake.fills[0]['time']) + 10
    start = own * 3 if mixed else own
    if not mixed:
        foreign_fill(fake, own / 2, start, 'A', stamp, 9100)
        start = own / 2
    foreign_fill(fake, start, start, 'A', stamp + 10, 9200)
    fake.position = 0
    terminal_orders(fake)
    await engine.recover()
    trade = state.snapshot()['trades'][identity]
    assert trade['status'] == 'closed'
    payments = [payment('funding', stamp - 1, '0.3', 'USDC', 'BTC', size=str(own * 3 if mixed else own))]
    async def funding(*_):
        return copy.deepcopy(payments)
    monkeypatch.setattr(exchange, 'funding_payments', funding)
    router = Router(settings, state, exchange)
    return state, fake, identity, Reporting(state, router), payments


@pytest.mark.asyncio
@pytest.mark.parametrize('mixed', [False, True])
async def test_manual_pnl_is_evidence_bound_and_reaches_trade_fill_and_visible_journal(setup, signal, monkeypatch, mixed):
    state, fake, identity, reporting, payments = await manual_trade(setup, signal, monkeypatch, mixed=mixed)
    before = copy.deepcopy(state.snapshot()['trades'][identity])
    actions = copy.deepcopy(fake.actions)
    outcome = await reporting.refresh(identity)
    assert outcome == {'complete': True, 'reason': 'complete'}, state.snapshot()['trades'][identity]['funding_coverage']
    snapshot = state.snapshot()
    trade = snapshot['trades'][identity]
    own = Decimal(trade['filled_quantity'])
    report = report_row(snapshot, trade)
    assert report['complete'] and not report['issues']
    assert Decimal(report['gross_pnl']) == own * 10
    fees = Decimal('0.01') + Decimal('0.01') / 3 if mixed else Decimal('0.03')
    funding = Decimal('0.1') if mixed else Decimal('0.3')
    assert Decimal(report['fees']) == fees and Decimal(report['funding']) == funding
    assert Decimal(report['net_pnl']) == own * 10 - fees + funding
    assert trade['manual_closure']['pnl_complete'] is True
    assert trade['fill_events'] == before['fill_events'] and trade['plan'] == before['plan']
    assert fake.actions == actions
    native = trade['funding_events']['funding']
    assert native['allocation']['account_amount'] == payments[0]['amount']
    assert native['signed_size'] == payments[0]['signed_size']
    projected = project(snapshot)
    assert projected == journal_oracle.project(snapshot)
    row = dict(zip(HEADERS['Trades'], projected['Trades:' + identity]['values'], strict=True))
    assert row['PnL realisiert vor Gebühren/Funding'] == float(own * 10)
    assert row['Gebühren USD'] == float(fees)
    assert row['Netto vor Funding USD'] == float(own * 10 - fees)
    closes = [value for key, value in projected.items() if key.startswith('Fills:') and value['values'][1] == identity and value['values'][4] != 'entry']
    assert len(closes) == (1 if mixed else 2)
    if mixed:
        assert closes[0]['values'][4] == 'manual_close_allocated'
    visible = build_presentation(snapshot, PresentationScope(network='testnet', period='all'))
    selected = next(row for row in visible.journal if row.trade_id == identity)
    assert selected.complete and selected.net_pnl == Decimal(report['net_pnl']) and selected.exit_price == 110
    assert ('anteilig' in ' '.join(selected.issues)) == mixed
    saved = copy.deepcopy(trade['manual_accounting'])
    await reporting.refresh(identity)
    assert state.snapshot()['trades'][identity]['manual_accounting'] == saved
    assert fake.actions == actions
    await reporting.router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing_fill', 'changed_fee', 'fee_currency', 'funding_size', 'missing_funding', 'mixed_partial', 'entry_price', 'duplicate_fill'])
async def test_incomplete_or_changed_manual_pnl_is_never_reported_as_complete(setup, signal, monkeypatch, damage):
    state, fake, identity, reporting, payments = await manual_trade(setup, signal, monkeypatch, mixed=True)
    if damage == 'missing_fill':
        fake.fills.pop()
    elif damage == 'changed_fee':
        fake.fills[0]['fee'] = '999'
    elif damage == 'fee_currency':
        fake.fills[-1]['feeToken'] = 'USDT'
    elif damage == 'funding_size':
        payments[0]['signed_size'] = '999'
    elif damage == 'missing_funding':
        payments[0]['signed_size'] = None
    elif damage == 'mixed_partial':
        raw = fake.fills[-1]
        amount = Decimal(raw['sz']) / 2
        raw['sz'] = str(amount)
        fake.fills.append({**raw, 'oid': 9300, 'tid': 9300, 'time': raw['time'] + 1, 'startPosition': str(amount)})
        state.update(lambda data: data['trades'][identity].update(closed_at=raw['time'] + 1))
    elif damage == 'duplicate_fill':
        fake.fills.append({**fake.fills[-1], 'fee': '999'})
    else:
        state.update(lambda data: data['trades'][identity].update(entry_vwap='999'))
    before = copy.deepcopy(state.snapshot()['trades'][identity]['fill_events'])
    result = await reporting.refresh(identity)
    assert not result['complete']
    row = state.snapshot()['trades'][identity]
    assert not verified_manual_accounting(row)
    assert report_row(state.snapshot(), row)['net_pnl'] is None
    assert row['fill_events'] == before
    await reporting.router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['funding', 'exit_fee'])
async def test_manual_accounting_outage_preserves_verified_result_but_positive_change_invalidates_it(setup, signal, monkeypatch, changed):
    state, fake, identity, reporting, payments = await manual_trade(setup, signal, monkeypatch)
    await reporting.refresh(identity)
    before = report_row(state.snapshot(), state.snapshot()['trades'][identity])
    original = reporting.router.exchange.funding_payments
    async def unavailable(*_):
        raise AqlaError('Temporarily unavailable')
    monkeypatch.setattr(reporting.router.exchange, 'funding_payments', unavailable)
    assert (await reporting.refresh(identity))['reason'] == 'unavailable'
    assert report_row(state.snapshot(), state.snapshot()['trades'][identity]) == before
    monkeypatch.setattr(reporting.router.exchange, 'funding_payments', original)
    if changed == 'funding':
        payments[0]['amount'] = '999'
    else:
        fake.fills[-1]['fee'] = '999'
    assert not (await reporting.refresh(identity))['complete']
    assert report_row(state.snapshot(), state.snapshot()['trades'][identity])['net_pnl'] is None
    assert state.snapshot()['trades'][identity]['manual_closure']['pnl_complete'] is False
    await reporting.router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('root', ['fill_events', 'plan', 'manual_closure', 'entry_accounting', 'manual_accounting'])
async def test_manual_accounting_cannot_outlive_changed_bound_evidence(setup, signal, monkeypatch, root):
    state, _, identity, reporting, _ = await manual_trade(setup, signal, monkeypatch)
    assert (await reporting.refresh(identity))['complete']
    def change(data):
        row = data['trades'][identity]
        if root == 'fill_events':
            next(iter(row[root].values()))['px'] = '999'
        elif root == 'manual_accounting':
            row[root]['gross_pnl'] = '999'
        else:
            row[root]['unexpected_change'] = 'changed'
    state.update(change)
    snapshot = state.snapshot()
    assert not verified_manual_accounting(snapshot['trades'][identity])
    projected = project(snapshot)
    assert projected == journal_oracle.project(snapshot)
    await reporting.router.close()
