import asyncio
import copy
import hashlib
import json
import sqlite3

import pytest
from test_pipelines import routed  # noqa: F401
from test_token_efficiency import call, efficiency_server  # noqa: F401

from aqla_agent.aster import Aster
from aqla_agent.observations import TemporaryObservation
from aqla_agent.source_disposition import verify_disposition
from aqla_agent.tool_outputs import PAGE_BYTES, OutputStore, reader_text
from aqla_agent.usage_metrics import UsageRun
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, Market, SourceEvent


@pytest.mark.asyncio
async def test_context_batch_uses_one_snapshot_without_changing_evidence(efficiency_server, setup, monkeypatch):  # noqa: F811
    _, state, fake, _, trading = setup
    sources = [SourceEvent(chat_id='channel', message_id=i+1, sent_at=1000+i,
        reply_to_id=1 if i else None, text='Original отчёт '+str(i)) for i in range(8)]
    for source in sources:
        trading.ingest(source)
    before = state.snapshot()
    expected = {source.event_id: efficiency_server.aqla_trading.context(source.event_id) for source in sources}
    snapshot, calls = state.snapshot, []
    def counted():
        calls.append(1)
        return snapshot()
    monkeypatch.setattr(state, 'snapshot', counted)
    result = await call(efficiency_server, 'source_context_batch', event_ids=[source.event_id for source in sources])
    assert len(calls) == 1
    for item, source in zip(result['items'], sources, strict=True):
        rows = expected[source.event_id]['messages']
        assert {key: result['messages'][key]['source'] for key in item['message_ids']} == {row['event_id']: row['source'] for row in rows}
        value = Interpretation(kind='report', source_refs=[source.source_id], reason='Original report',
            evidence=[Evidence(field='classification', source_id=source.source_id, quote=source.text)])
        verify_disposition(before, source.event_id, value, item['source_read_receipt'], efficiency_server.aqla_authority.key)
    assert snapshot() == before and not fake.actions


def test_larger_unicode_pages_preserve_originals_and_small_budget(tmp_path):
    body = json.dumps({'messages': [{'text': ('Результат цель достигнута, нового входа нет. '*35)} for _ in range(20)]}, ensure_ascii=False)
    store = OutputStore(tmp_path)
    identity = store.save(body, 'session', 'call')
    counts = []
    for budget in (8192, PAGE_BYTES):
        chunks, offset = [], 0
        while True:
            page = store.read(identity, 'session', offset, 16384, page_bytes=budget)
            assert len(json.dumps(reader_text(page), ensure_ascii=True).encode()) <= budget-768
            chunks.append(page['content'])
            if page['eof']:
                break
            offset = page['next_offset']
        assert ''.join(chunks) == body
        counts.append(len(chunks))
    assert counts[1] <= 9 and counts[1] <= counts[0]/2
    with pytest.raises(ValueError):
        store.read(identity, 'another-session', 0, 16384)


def test_usage_stage_timings_are_opaque_and_do_not_claim_exchange_fills(tmp_path, monkeypatch):
    monkeypatch.setattr('aqla_agent.usage_metrics.time.time', lambda: 1000)
    run = UsageRun('source', started=999)
    event = 'a'*32
    source = {'received_at': 998000, 'historical': False, 'text': 'PRIVATE_SOURCE_DO_NOT_PERSIST'}
    context = {'event': {'event_id': event, 'source': source}}
    run.tool('mcp__aqla__source_context', json.dumps(context), False, .25)
    run.tool('mcp__aqla__interpret_source', json.dumps({'event_id': event, 'status': 'report'}), False, .1)
    run.tool('mcp__aqla__trade_preview', json.dumps({'trade_id': 'trade', 'source_revisions': {'source': event}}), False, .2)
    run.tool('mcp__aqla__trade_execute', json.dumps({'trade_id': 'trade', 'status': 'preparing'}), False, .3)
    run.tool('mcp__aqla__status', '{}', False, float('nan'))
    run.persist(tmp_path, {})
    with sqlite3.connect(tmp_path/'cache/aqla-usage.sqlite3') as db:
        raw = db.execute('SELECT metrics FROM runs').fetchone()[0]
    assert event not in raw and source['text'] not in raw
    saved = json.loads(raw)
    timing = saved['source_timings'][hashlib.sha256(event.encode()).hexdigest()]
    assert timing == {'received_at': 998000, 'turn_started_at': 999000, 'context_reply_at': 1000000,
        'interpretation_reply_at': 1000000, 'preview_reply_at': 1000000, 'execution_reply_at': 1000000}
    assert 'fill' not in raw and 'duration_seconds' not in saved['tools']['mcp__aqla__status']
    assert saved['tools']['mcp__aqla__source_context']['duration_seconds'] == [.25]
    before = copy.deepcopy(run.source_timings)
    run.tool('mcp__aqla__interpret_source', json.dumps({'event_id': event, 'status': 'error'}), False)
    assert run.source_timings == before


@pytest.mark.asyncio
@pytest.mark.parametrize('venue', ['hyperliquid', 'aster'])
@pytest.mark.parametrize('invalid', [None, 'identity', 'stale'])
async def test_price_identity_and_freshness_validation_stay_strict(monkeypatch, venue, invalid):
    for module in ('aqla_agent.entry', 'aqla_hyperliquid_mcp.api', 'aqla_agent.aster'):
        monkeypatch.setattr(module+'.now_ms', lambda: 10000)
    adapter = object.__new__(Hyperliquid if venue == 'hyperliquid' else Aster)
    market = Market(symbol='BTC', native_symbol='BTCUSDT', asset_id=1, product='perp', size_decimals=2, max_leverage=20, mid='100')
    started = []
    async def read(kind, *args, **kwargs):
        started.append(kind)
        stamp = 7999 if invalid == 'stale' else 10000
        if venue == 'hyperliquid':
            if kind == 'metaAndAssetCtxs':
                return [{'universe': [{'name': 'BTC'}]}, [{'markPx': '100'}]]
            return {'coin': 'ETH' if invalid == 'identity' else 'BTC', 'time': stamp,
                    'levels': [[{'px': '99'}], [{'px': '101'}]]}
        return {'symbol': 'ETHUSDT' if invalid == 'identity' else 'BTCUSDT', 'time': stamp,
                'price': '100', 'bidPrice': '99', 'askPrice': '101'}
    monkeypatch.setattr(adapter, 'info' if venue == 'hyperliquid' else 'read', read)
    if invalid:
        with pytest.raises(TemporaryObservation if invalid == 'stale' else AqlaError):
            await adapter.price_snapshot(market)
    else:
        quote = await adapter.price_snapshot(market)
        assert quote.market == 'BTC' and quote.mid == quote.stop_reference == 100
    assert len(started) == (4 if venue == 'hyperliquid' and invalid == 'stale' else 2)


@pytest.mark.asyncio
async def test_ranked_preview_resolves_once_under_lock_and_execute_resolves_again(routed, setup, signal, monkeypatch):  # noqa: F811
    router, boundaries = routed
    setup[1].update(lambda data: data['ranking_policy'].update(enabled=True))
    setup[1].update(lambda data: data['source_ranks']['channel'].update(tier='D'))
    calls = {name: [] for name in router.engines}
    for name, engine in router.engines.items():
        original = engine.exchange.market
        async def counted(*args, _name=name, _engine=engine, _original=original, **kwargs):
            assert _engine.lock.locked()
            calls[_name].append(1)
            return await _original(*args, **kwargs)
        monkeypatch.setattr(engine.exchange, 'market', counted)
    source, value = signal
    lock = router.engines['aster'].lock
    async with lock:
        pending = asyncio.create_task(router.preview(source.event_id, value))
        await asyncio.sleep(.01)
        assert not any(calls.values())
    result = await pending
    assert all(row['status'] == 'planned' for row in result['targets']), result
    assert all(len(rows) == 1 for rows in calls.values())
    assert all(not boundary.actions for boundary in boundaries.values())
    executed = await router.execute_many([row['plan_id'] for row in result['targets']])
    assert all(row['status'] == 'open' for row in executed['targets']), executed
    assert all(len(rows) > 1 for rows in calls.values())
    await router.close()
