import asyncio
from collections import Counter

import pytest

from aqla_agent import entry
from aqla_agent.observations import TemporaryObservation
from aqla_hyperliquid_mcp import api
from aqla_hyperliquid_mcp.models import AqlaError, now_ms


@pytest.mark.asyncio
async def test_price_pair_overlapping_reads_fit_the_original_freshness_window(setup, monkeypatch):
    _, _, _, exchange, _ = setup
    market = await exchange.market("BTC")
    stamp = [now_ms()]
    monkeypatch.setattr(api, "now_ms", lambda: stamp[0])
    monkeypatch.setattr(entry, "now_ms", lambda: stamp[0])
    original = exchange.info
    starts = []

    async def slow(kind, **params):
        started = stamp[0]
        starts.append(started)
        await asyncio.sleep(0)
        stamp[0] = max(stamp[0], started + 1200)
        result = await original(kind, **params)
        if kind == "l2Book":
            result["time"] = stamp[0]
        return result

    monkeypatch.setattr(exchange, "info", slow)
    quote = await exchange.price_snapshot(market)
    assert starts == [quote.started_at, quote.started_at]
    assert quote.observed_at - quote.started_at == 1200
    quote.fresh()
    stamp[0] += 801
    with pytest.raises(TemporaryObservation):
        quote.fresh()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["old", "future", "timeout", "invalid_market"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_only_transient_price_proofs_get_one_complete_read_retry(setup, monkeypatch, failure, persistent):
    _, _, fake, exchange, _ = setup
    market = await exchange.market("BTC")
    original = exchange.info
    calls = Counter()

    async def faulty(kind, **params):
        calls[kind] += 1
        result = await original(kind, **params)
        if kind == "l2Book" and (persistent or calls[kind] == 1):
            if failure == "timeout":
                raise TimeoutError
            if failure == "invalid_market":
                result["coin"] = "ETH"
            else:
                result["time"] = now_ms() + (10000 if failure == "future" else -10000)
        return result

    monkeypatch.setattr(exchange, "info", faulty)
    if failure == "invalid_market":
        with pytest.raises(AqlaError, match="executable order book"):
            await exchange.price_snapshot(market)
        assert calls["l2Book"] == 1
    elif persistent:
        with pytest.raises(TimeoutError if failure == "timeout" else TemporaryObservation):
            await exchange.price_snapshot(market)
        assert calls["l2Book"] == 2
    else:
        (await exchange.price_snapshot(market)).fresh()
        assert calls["l2Book"] == 2
    assert not fake.actions
