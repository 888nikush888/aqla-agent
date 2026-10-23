"""Native book events wake owned entry management; feeds never authorize orders."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from aqla_hyperliquid_mcp.models import AqlaError, Market, now_ms

from .entry import BookSnapshot


def parse_book(venue, market, frame):
    if venue == "hyperliquid":
        if frame.get("channel") != "l2Book" or frame.get("data", {}).get("coin") != market.symbol:
            return None
        row = frame["data"]
        bids, asks, stamp = row["levels"][0], row["levels"][1], row["time"]
        bid, ask = Decimal(bids[0]["px"]), Decimal(asks[0]["px"])
        depth = [[(level["px"], level["sz"]) for level in side if Decimal(level["sz"]) > 0] for side in (bids, asks)]
    else:
        raise AqlaError("Entry watcher supports Hyperliquid only")
    if type(stamp) is not int or not 0 <= now_ms() - stamp <= 2000 or not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
        raise AqlaError("Entry watcher has no fresh consistent native book")
    return BookSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
        exchange_time=stamp, started_at=stamp, observed_at=now_ms(), bids=depth[0], asks=depth[1]).fresh()


def record_endpoints(data, identity, initial, wave_end, stamp):
    row = data["trades"][identity]
    if initial:
        row.setdefault("entry_tp1_reached_at", stamp)
    if wave_end:
        row["waverider"].setdefault("endpoint_reached_at", stamp)


class EntryWatch:
    def __init__(self, router, *, connector=connect):
        self.router, self.connector = router, connector
        self.tasks, self.groups = {}, {}
        self.closed = False
        self.changed = asyncio.Event()
        for engine in router.engines.values():
            engine.entry_wake = self.changed.set

    @staticmethod
    def latch(engine, identity, bid, ask, stamp):
        trade = engine.state.read("trades", entries={"trades": [identity]})["trades"].get(identity)
        if not trade or trade["status"] not in ("waiting_entry", "preparing", "opening", "protecting", "open", "unknown"):
            return
        plan = trade["plan"]
        targets = trade.get("targets", plan["take_profits"])
        def crossed(target):
            return bid >= Decimal(target) if plan["side"] == "long" else ask <= Decimal(target)
        source_target = not plan.get("source_tp") and "take_profits" not in plan.get("backup_fields", [])
        initial = (Decimal(trade["filled_quantity"]) > 0 or source_target) and crossed(targets[0])
        wave = trade.get("waverider", {})
        endpoint = plan["policy"].get("waverider_until_tp", 1)
        endpoint = len(targets) if endpoint == "last" else min(endpoint, len(targets))
        wave_end = bool(wave and not wave.get("finished") and crossed(targets[endpoint - 1]))
        if initial and not trade.get("entry_tp1_reached_at") or wave_end and not wave.get("endpoint_reached_at"):
            engine.state.update(lambda data: record_endpoints(data, identity, initial, wave_end, stamp))

    async def observe(self, engine, identity, market):
        venue = getattr(engine.exchange.settings, "venue", "hyperliquid")
        base = str(engine.exchange.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        if venue != "hyperliquid":
            raise AqlaError("Entry watcher supports Hyperliquid only")
        url, subscribe = base + "/ws", {"method": "subscribe", "subscription": {"type": "l2Book", "coin": market.symbol, "fast": True}}
        wake = asyncio.Event()
        latest_book = None
        group_key = (venue, engine.exchange.settings.network, base, market.asset_id, market.symbol)
        last_sync = {}
        def subscribers():
            return self.groups.get(group_key, [(engine, identity)])
        def receive(book):
            nonlocal latest_book
            latest_book = book
            for current_engine, current_id in subscribers():
                self.latch(current_engine, current_id, book.bid, book.ask, book.exchange_time)
            wake.set()
        async def manage_one(current_engine, current_id, book):
            try:
                async with current_engine.lock.work(30):
                    if not current_engine.recovery_checked:
                        return
                    trade = current_engine.state.read("trades", entries={"trades": [current_id]})["trades"].get(current_id)
                    if not trade:
                        return
                    now = asyncio.get_running_loop().time()
                    pending = [row for row in trade.get("entry_orders", []) if not row.get("not_sent") and row.get("phase") != "terminal"]
                    actionable = any(book.ask <= Decimal(row["limit"]) if trade["plan"]["side"] == "long" else book.bid >= Decimal(row["limit"]) for row in pending)
                    completed = set(trade.get("completed_target_indices", []))
                    actionable |= any((book.bid >= Decimal(target) if trade["plan"]["side"] == "long" else book.ask <= Decimal(target))
                        for index, target in enumerate(trade.get("targets", trade["plan"]["take_profits"])) if index not in completed)
                    if actionable and now - last_sync.get(current_id, 0) >= 3:
                        await current_engine.refresh_trade(current_id)
                        last_sync[current_id] = now
                    if trade["plan"]["policy"].get("management_mode") == "waverider" and Decimal(trade["remaining_quantity"]) > 0:
                        from .waverider import tick
                        await tick(current_engine, current_id, book)
                    if Decimal(trade["remaining_quantity"]) > 0:
                        from .waverider import queue_evidence
                        queue_evidence(current_engine, current_id, book)
            except Exception:
                # Native recovery owns financial diagnostics; another account must still be served.
                pass
        async def manage():
            last = 0.0
            while True:
                await wake.wait()
                await asyncio.sleep(max(0, 1 - (asyncio.get_running_loop().time() - last)))
                wake.clear()
                book = latest_book
                if book is not None:
                    await asyncio.gather(*(manage_one(current_engine, current_id, book) for current_engine, current_id in subscribers()))
                last = asyncio.get_running_loop().time()
        manager = asyncio.create_task(manage(), name="aqla-entry-events")
        delay = 1
        try:
            while not self.closed:
                try:
                    async with self.connector(url, max_size=1_000_000, open_timeout=5) as socket:
                        if subscribe:
                            await socket.send(json.dumps(subscribe))
                        delay = 1
                        async for message in socket:
                            frame = json.loads(message)
                            if frame.get("type") == "ping":
                                await socket.send(json.dumps({"type": "pong"}))
                                continue
                            book = parse_book(venue, market, frame)
                            if book:
                                receive(book)
                except (OSError, ValueError, KeyError, IndexError, WebSocketException):
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)
        finally:
            manager.cancel()
            await asyncio.gather(manager, return_exceptions=True)

    async def run(self):
        try:
            while not self.closed:
                self.changed.clear()
                wanted = {}
                for identity, trade in self.router.state.read("trades")["trades"].items():
                    if trade.get("lifecycle_version") != 2 or trade["status"] not in ("waiting_entry", "preparing", "opening", "protecting", "open", "unknown"):
                        continue
                    engine = self.router.engine_for_plan(trade["plan"])
                    if engine:
                        market = Market.model_validate(trade["plan"]["market"])
                        base = str(engine.exchange.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
                        key = (getattr(engine.exchange.settings, "venue", "hyperliquid"), engine.exchange.settings.network, base, market.asset_id, market.symbol)
                        wanted.setdefault(key, []).append((engine, identity, market))
                self.groups = {key: [(engine, identity) for engine, identity, _ in rows] for key, rows in wanted.items()}
                removed = [self.tasks.pop(key) for key in set(self.tasks) - set(wanted)]
                for task in removed:
                    task.cancel()
                await asyncio.gather(*removed, return_exceptions=True)
                for key, rows in wanted.items():
                    if key not in self.tasks or self.tasks[key].done():
                        engine, identity, market = rows[0]
                        self.tasks[key] = asyncio.create_task(self.observe(engine, identity, market), name="aqla-book-" + market.symbol)
                try:
                    async with asyncio.timeout(10):
                        await self.changed.wait()
                except TimeoutError:
                    pass
        finally:
            self.closed = True
            for task in self.tasks.values():
                task.cancel()
            tasks = list(self.tasks.values())
            for engine in self.router.engines.values():
                for task in getattr(engine, "_waverider_capture_tasks", {}).values():
                    task.cancel()
                    tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)

