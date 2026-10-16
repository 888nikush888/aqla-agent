# ruff: noqa: F811
import json
from decimal import Decimal

import httpx
import pytest
from test_aster_adapter import aster  # noqa: F401
from test_pipelines import routed  # noqa: F401

from aqla_agent.messages import context
from aqla_agent.observations import TemporaryObservation
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, ExchangeMinimum, SymbolUnavailable


def ranked(state, *, replacements=None, explicit=False):
    def configure(data):
        data['ranking_policy']['enabled'] = True
        data['source_ranks']['channel']['tier'] = 'D'
        targets = data['pipelines']['source-one']['targets'][:2 if explicit else 1]
        if replacements is not None:
            targets[0]['fallback_exchanges'] = replacements
        data['pipelines']['source-one']['targets'] = targets
    state.update(configure)


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['ARC', 'ZIL'])
async def test_legacy_pipeline_resolves_missing_hl_symbol_through_native_aster_preview(aster, setup, signal, symbol):
    adapter, boundary, _ = aster
    settings, state, primary_boundary, primary, _ = setup
    def handle(request):
        response = boundary.handle(request)
        value = json.loads(response.content.replace(b'BTC', symbol.encode()))
        if request.url.path.endswith('/account'):
            value.update(totalMarginBalance='50', availableBalance='50')
        if request.url.path.endswith('/exchangeInfo'):
            value['symbols'][0]['filters'][-1]['notional'] = '5'
        return httpx.Response(response.status_code, json=value)
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(base_url='https://fapi.asterdex-testnet.com', transport=httpx.MockTransport(handle))
    router = Router(settings, state, primary, adapters={'aster': adapter})
    def configure(data):
        data['ranking_policy']['enabled'] = True
        data['source_ranks']['channel']['tier'] = 'D'
        data['profiles']['standard']['parameters'].update(risk_percent='5', take_profit_allocation='adaptive')
    state.update(configure)
    source, value = signal
    source = source.model_copy(update={'message_id': 77, 'text': source.text.replace('BTC', symbol)})
    router.ingest(source)
    value = value.model_copy(update={'symbol': symbol, 'source_refs': [source.source_id],
        'evidence': [e.model_copy(update={'source_id': source.source_id, 'quote': e.quote.replace('BTC', symbol)}) for e in value.evidence]})
    result = await router.preview(source.event_id, value)
    plan = result['targets'][0]['plan']
    assert plan['exchange'] == 'aster' and plan['requested_exchange'] == 'hyperliquid'
    assert plan['pipeline_id'] == 'legacy' and plan['profile_id'] == 'standard'
    assert plan['market']['symbol'] == symbol and plan['market']['native_symbol'] == symbol + 'USDT'
    assert plan['market']['quote_token'] == 'USDT' and plan['policy']['risk_percent'] == '3.75'
    assert plan['policy']['max_total_risk_percent'] == '1.5'
    assert Decimal(plan['risk_usdc']) <= Decimal('50') * Decimal('.015')
    assert len(plan['take_profits']) == 1 and plan['source_take_profit_count'] == 2
    assert not state.snapshot()['operations'] and not boundary.posts and not primary_boundary.actions
    await router.close()


@pytest.mark.asyncio
async def test_real_tp_minimum_falls_back_without_changing_profile_targets_or_size(routed, setup, signal, monkeypatch):
    router, boundaries = routed
    state = setup[1]
    ranked(state)
    primary = router.engines['hyperliquid'].exchange
    market = primary.market
    async def high_minimum(*args):
        return (await market(*args)).model_copy(update={'min_notional': Decimal(150)})
    monkeypatch.setattr(primary, 'market', high_minimum)
    source, value = signal
    plan = (await router.preview(source.event_id, value))['targets'][0]['plan']
    assert plan['exchange'] == 'aster' and plan['requested_exchange'] == 'hyperliquid'
    assert plan['profile_id'] == 'a' and plan['network'] == 'testnet'
    assert plan['routing_leg_id'] == 'source-one:hyperliquid'
    assert plan['policy']['risk_percent'] == '0.1875'  # Rank D retains its 0.75 factor.
    assert Decimal(plan['risk_usdc']) <= Decimal(10000) * Decimal('.1875') / 100
    assert Decimal(plan['quantity']) * Decimal(plan['entry_limit']) <= 500
    assert len(plan['take_profits']) == len(value.take_profits)
    assert plan['deadline_at'] == source.sent_at + plan['policy']['max_signal_age_seconds'] * 1000
    assert plan['fallback_reason'] == 'A TP leg is below the exchange notional minimum'
    assert 'mindestgröße' in context(state.snapshot(), plan) and 'nicht gelistet' not in context(state.snapshot(), plan)
    assert not state.snapshot()['operations'] and all(not b.actions for b in boundaries.values())
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [SymbolUnavailable, ExchangeMinimum])
async def test_replacement_minimum_can_continue_to_next_eligible_venue(routed, setup, signal, monkeypatch, failure):
    router, _ = routed
    ranked(setup[1])
    async def missing(*args):
        raise failure('Confirmed primary constraint')
    async def too_small(*args, **kwargs):
        raise ExchangeMinimum('A TP leg rounds to zero; choose fewer targets or different weights')
    monkeypatch.setattr(router.engines['hyperliquid'].exchange, 'market', missing)
    monkeypatch.setattr(router.engines['aster'], 'preview', too_small)
    source, value = signal
    target = (await router.preview(source.event_id, value))['targets'][0]
    assert target['exchange'] == 'lighter' and target['plan']['requested_exchange'] == 'hyperliquid'
    assert target['skipped_replacements'] == ['aster: A TP leg rounds to zero; choose fewer targets or different weights']
    assert not setup[1].snapshot()['operations']
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [AqlaError('Insufficient margin'), AqlaError('Fresh price data unavailable'),
                                 AqlaError('Protection unavailable'), TemporaryObservation('read_unavailable', 'account')])
async def test_replacement_failure_reports_actual_venue_and_cannot_bypass_its_guards(routed, setup, signal, monkeypatch, error):
    router, boundaries = routed
    ranked(setup[1])
    async def missing(*args):
        raise SymbolUnavailable('Confirmed primary absence')
    async def fail(*args, **kwargs):
        raise error
    async def forbidden(*args, **kwargs):
        pytest.fail('No further venue after a budget, price, protection or transport failure')
    monkeypatch.setattr(router.engines['hyperliquid'].exchange, 'market', missing)
    monkeypatch.setattr(router.engines['aster'], 'preview', fail)
    monkeypatch.setattr(router.engines['lighter'], 'preview', forbidden)
    source, value = signal
    target = (await router.preview(source.event_id, value))['targets'][0]
    assert target['exchange'] == 'aster' and target['requested_exchange'] == 'hyperliquid'
    assert target['fallback_reason'] == 'Confirmed primary absence' and target['error'] == str(error)
    assert target['status'] == ('pending' if isinstance(error, TemporaryObservation) else 'rejected')
    assert all(not b.actions for b in boundaries.values()) and not setup[1].snapshot()['operations']
    await router.close()


@pytest.mark.asyncio
async def test_retry_keeps_explicit_reservation_and_original_logical_leg(routed, setup, signal, monkeypatch):
    router, _ = routed
    state = setup[1]
    ranked(state, explicit=True)
    async def missing(*args):
        raise SymbolUnavailable('Confirmed absent')
    monkeypatch.setattr(router.engines['hyperliquid'].exchange, 'market', missing)
    candidate = router.engines['lighter']
    preview = candidate.preview
    calls = 0
    async def unavailable_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TemporaryObservation('read_unavailable', 'account')
        return await preview(*args, **kwargs)
    monkeypatch.setattr(candidate, 'preview', unavailable_once)
    source, value = signal
    first = await router.preview(source.event_id, value)
    assert [t['exchange'] for t in first['targets']] == ['lighter', 'aster']
    assert first['targets'][0]['status'] == 'pending' and first['targets'][1]['status'] == 'planned'
    state.update(lambda data: data['events'][source.event_id]['targets']['source-one:hyperliquid']['observation_retry'].update(next_retry_at=0))
    second = await router.preview(source.event_id, value)
    assert [t['exchange'] for t in second['targets']] == ['lighter', 'aster']
    assert second['targets'][0]['plan']['routing_leg_id'] == 'source-one:hyperliquid'
    assert second['targets'][0]['plan']['requested_exchange'] == 'hyperliquid'
    assert second['targets'][1]['plan_id'] == first['targets'][1]['plan_id']
    assert len(state.snapshot()['plans']) == 2 and not state.snapshot()['operations']
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [SymbolUnavailable, ExchangeMinimum])
async def test_explicit_empty_fallback_list_stays_disabled(routed, setup, signal, monkeypatch, failure):
    router, _ = routed
    ranked(setup[1], replacements=[])
    async def fail(*args):
        raise failure('Confirmed constraint')
    async def forbidden(*args, **kwargs):
        pytest.fail('Fallback was explicitly disabled')
    monkeypatch.setattr(router.engines['hyperliquid'].exchange, 'market', fail)
    monkeypatch.setattr(router.engines['aster'], 'preview', forbidden)
    monkeypatch.setattr(router.engines['lighter'], 'preview', forbidden)
    source, value = signal
    target = (await router.preview(source.event_id, value))['targets'][0]
    assert target['status'] == 'rejected' and 'Confirmed constraint' in target['error']
    assert not setup[1].snapshot()['plans'] and not setup[1].snapshot()['operations']
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['entry:', 'leverage:'])
async def test_pending_target_with_started_operation_cannot_switch_venue(routed, setup, signal, monkeypatch, prefix):
    router, _ = routed
    state = setup[1]
    ranked(state)
    source, value = signal
    first = (await router.preview(source.event_id, value))['targets'][0]
    def started(data):
        data['events'][source.event_id]['targets']['source-one:hyperliquid']['status'] = 'pending'
        data['operations'][prefix + first['plan']['trade_id']] = {'account_key': 'hyperliquid:testnet'}
    state.update(started)
    async def missing(*args):
        raise SymbolUnavailable('Confirmed absent')
    async def forbidden(*args, **kwargs):
        pytest.fail('Execution preparation already started')
    monkeypatch.setattr(router.engines['hyperliquid'].exchange, 'market', missing)
    monkeypatch.setattr(router.engines['aster'], 'preview', forbidden)
    target = (await router.preview(source.event_id, value))['targets'][0]
    assert target['status'] == 'rejected' and target['exchange'] == 'hyperliquid'
    assert target['error'] == 'Confirmed absent' and len(state.snapshot()['plans']) == 1
    await router.close()
