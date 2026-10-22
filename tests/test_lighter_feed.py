import json

import pytest
from test_lighter_book import delta, snapshot

from aqla_agent.lighter_feed import LighterFeed
from aqla_agent.observations import TemporaryObservation


def test_shared_feed_applies_deltas_without_renewing_old_book_or_mark_times(monkeypatch):
    from aqla_agent import entry
    from aqla_agent.lighter_book import LighterBook
    market, frame = snapshot()
    feed = LighterFeed("wss://example.invalid/stream", market)
    book = LighterBook(market)
    received = []
    feed.listeners.add(received.append)
    feed.receive(frame, book)
    feed.receive({"channel": "market_stats:0", "timestamp": frame["timestamp"], "market_stats": {"mark_price": "100"}}, book)
    original = feed.value
    feed.receive(delta(frame, [], [{"price": "103", "size": "5"}]), book)
    assert feed.value.bid == 99 and feed.value.ask == 101 and len(received) == 2
    monkeypatch.setattr(entry, "now_ms", lambda: original.exchange_time + 2001)
    with pytest.raises(TemporaryObservation):
        feed.value.fresh()


@pytest.mark.parametrize("offset", [-2001, 1000])
def test_a_valid_book_cannot_mask_an_old_or_future_mark(offset):
    from aqla_agent.lighter_book import LighterBook
    from aqla_hyperliquid_mcp.models import now_ms
    market, frame = snapshot()
    feed = LighterFeed("wss://example.invalid/stream", market)
    book = LighterBook(market)
    feed.receive(frame, book)
    with pytest.raises(TemporaryObservation):
        feed.receive({"channel": "market_stats:0", "timestamp": now_ms() + offset, "market_stats": {"mark_price": "100"}}, book)
    assert feed.value is None


@pytest.mark.asyncio
async def test_two_readers_share_one_public_connection_and_full_snapshot():
    import asyncio
    market, frame = snapshot()
    opened = []
    class Socket:
        def __init__(self):
            self.frames = [frame, {"channel": "market_stats:0", "timestamp": frame["timestamp"], "market_stats": {"mark_price": "100"}}]
        async def __aenter__(self):
            opened.append(self)
            return self
        async def __aexit__(self, *_):
            pass
        async def send(self, text):
            assert json.loads(text)["type"] == "subscribe"
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.frames:
                return json.dumps(self.frames.pop(0))
            await asyncio.Event().wait()
    feed = LighterFeed("wss://example.invalid/stream", market, connector=lambda *_args, **_kwargs: Socket())
    try:
        left, right = await asyncio.gather(feed.price(), feed.price())
        assert left == right and len(opened) == 1
    finally:
        await feed.close()
