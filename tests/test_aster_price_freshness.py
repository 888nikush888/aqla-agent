# ruff: noqa: F811
import json
import time
from email.utils import formatdate

import httpx
import pytest
from test_aster_adapter import aster  # noqa: F401

from aqla_agent.observations import TemporaryObservation


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', [None, 'date_old', 'date_future', 'date_missing', 'date_invalid', 'cached', 'book_old', 'trade_future', 'trade_missing'])
async def test_contract_last_trade_age_is_distinct_from_current_response_and_book_freshness(aster, damage):
    adapter, boundary, _ = aster
    market = await adapter.market('BTC')
    def handle(request):
        response = boundary.handle(request)
        value = json.loads(response.content)
        headers = dict(response.headers)
        if request.url.path.endswith('/ticker/price'):
            value['time'] -= 600000
            if damage == 'date_old':
                headers['date'] = formatdate(time.time() - 60, usegmt=True)
            elif damage == 'date_future':
                headers['date'] = formatdate(time.time() + 60, usegmt=True)
            elif damage == 'date_missing':
                headers.pop('date')
            elif damage == 'date_invalid':
                headers['date'] = 'invalid'
            elif damage == 'cached':
                headers['age'] = '10'
            elif damage == 'trade_future':
                value['time'] = int(time.time() * 1000) + 60000
            elif damage == 'trade_missing':
                value.pop('time')
        if damage == 'book_old' and request.url.path.endswith('/ticker/bookTicker'):
            value['time'] -= 60000
        return httpx.Response(response.status_code, json=value, headers=headers)
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(base_url='https://fapi.asterdex-testnet.com', transport=httpx.MockTransport(handle))
    if damage:
        with pytest.raises(TemporaryObservation, match='stale'):
            await adapter.price_snapshot(market)
    else:
        quote = await adapter.price_snapshot(market)
        assert quote.stop_reference == 100 and quote.stop_reference_kind == 'contract'
        assert 0 <= int(time.time() * 1000) - quote.exchange_time <= 2000
    assert not boundary.posts
    await adapter.close()
