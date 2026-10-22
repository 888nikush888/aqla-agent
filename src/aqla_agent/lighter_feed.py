"""One bounded public Lighter book/mark stream shared by its read-only consumers."""
import asyncio
import json

from websockets.asyncio.client import connect

from aqla_hyperliquid_mcp.models import now_ms

from .entry import PriceSnapshot
from .lighter_book import LighterBook
from .observations import TemporaryObservation


class LighterFeed:
    def __init__(self, url, market, *, connector=None):
        self.url, self.market, self.connector = url, market, connector or connect
        self.task, self.value, self.book, self.mark = None, None, None, None
        self.listeners, self.changed = set(), asyncio.Event()
        self.last_used = now_ms()

    def start(self):
        self.last_used = now_ms()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="aqla-lighter-" + self.market.symbol)
        return self

    def receive(self, frame, reconstructed):
        channel = frame.get("channel")
        if channel in (f"order_book:{self.market.asset_id}", f"order_book/{self.market.asset_id}"):
            self.book = reconstructed.apply(frame)
            for listener in tuple(self.listeners):
                listener(self.book)
        elif channel in (f"market_stats:{self.market.asset_id}", f"market_stats/{self.market.asset_id}"):
            values = frame["market_stats"]
            values = values.get(str(self.market.asset_id), values)
            stamp = frame["timestamp"]
            if type(stamp) is not int:
                raise ValueError("Missing native mark time")
            stamp = stamp * 1000 if stamp < 10**12 else stamp
            if not 0 <= now_ms() - stamp <= 2000:
                raise TemporaryObservation("price_stale", "price")
            self.mark = values["mark_price"], stamp
        else:
            return
        if self.book is not None and self.mark is not None:
            book, (price, stamp) = self.book, self.mark
            self.value = PriceSnapshot(market=self.market.symbol, bid=book.bid, ask=book.ask, mid=book.mid,
                stop_reference=price, stop_reference_kind="mark", exchange_time=min(stamp, book.exchange_time),
                started_at=min(stamp, book.started_at), observed_at=now_ms(), bids=book.bids, asks=book.asks)
        self.changed.set()

    async def price(self):
        self.start()
        try:
            async with asyncio.timeout(2):
                while True:
                    self.changed.clear()
                    if self.value is not None:
                        try:
                            return self.value.fresh()
                        except TemporaryObservation:
                            pass
                    await self.changed.wait()
        except TimeoutError:
            raise TemporaryObservation("price_stale", "price") from None

    async def run(self):
        delay = 1
        try:
            while True:
                self.value, self.book, self.mark = None, None, None
                reconstructed = LighterBook(self.market)
                try:
                    async with self.connector(self.url, open_timeout=2, max_size=1_000_000) as socket:
                        for channel in ("order_book", "market_stats"):
                            await socket.send(json.dumps({"type": "subscribe", "channel": f"{channel}/{self.market.asset_id}"}))
                        delay = 1
                        messages = socket.__aiter__()
                        while True:
                            try:
                                async with asyncio.timeout(15):
                                    raw = await anext(messages)
                            except StopAsyncIteration:
                                break
                            except TimeoutError:
                                raw = None
                            if not self.listeners and now_ms() - self.last_used > 60000:
                                return
                            if raw is None:
                                continue
                            frame = json.loads(raw)
                            if frame.get("type") == "ping":
                                await socket.send(json.dumps({"type": "pong"}))
                            else:
                                try:
                                    self.receive(frame, reconstructed)
                                except Exception:
                                    self.value, self.book, self.mark = None, None, None
                                    self.changed.set()
                                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                self.value, self.book, self.mark = None, None, None
                self.changed.set()
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)
        finally:
            self.value, self.book, self.mark = None, None, None
            self.changed.set()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def market_feed(adapter, market):
    registry = adapter.public_feeds
    key = (str(adapter.client.base_url), market.asset_id, market.symbol)
    if key not in registry:
        if len(registry) >= 128:
            expired = next((key for key, feed in registry.items() if not feed.listeners and now_ms() - feed.last_used > 60000), None)
            if expired is None:
                raise TemporaryObservation("read_unavailable", "price", "transport")
            old = registry.pop(expired)
            if old.task:
                old.task.cancel()
        registry[key] = LighterFeed(str(adapter.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/stream", market)
    return registry[key].start()
