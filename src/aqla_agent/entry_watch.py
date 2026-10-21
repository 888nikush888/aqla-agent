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
    elif venue == "aster":
        row = frame.get("data", frame)
        if row.get("s") != market.native_symbol:
            return None
        bids, asks, stamp = row.get("b", row.get("bids")), row.get("a", row.get("asks")), row["E"]
        bid, ask = Decimal(bids[0][0]), Decimal(asks[0][0])
        depth = [bids, asks]
    else:
        if frame.get("channel") not in (f"order_book:{market.asset_id}", f"order_book/{market.asset_id}"):
            return None
        row = frame["order_book"]
        bids, asks, stamp = row["bids"], row["asks"], frame["timestamp"]
        if stamp < 10**12:
            stamp *= 1000
        bid, ask = Decimal(bids[0]["price"]), Decimal(asks[0]["price"])
        depth = [[(level["price"], level["size"]) for level in side if Decimal(level["size"]) > 0] for side in (bids, asks)]
    if type(stamp) is not int or not 0 <= now_ms() - stamp <= 2000 or not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
        raise AqlaError("Entry watcher has no fresh consistent native book")
    return BookSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
        exchange_time=stamp, started_at=stamp, observed_at=now_ms(), bids=depth[0], asks=depth[1]).fresh()


class EntryWatch:
    def __init__(self, router, *, connector=connect):
        self.router, self.connector = router, connector
        self.tasks = {}
        self.closed = False
        self.changed = asyncio.Event()
        self.reroute_task = None
        for engine in router.engines.values():
            engine.entry_wake = self.changed.set

    def latch(self, engine, identity, bid, ask, stamp):
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
            def record(data):
                row = data["trades"][identity]
                if initial:
                    row.setdefault("entry_tp1_reached_at", stamp)
                if wave_end:
                    row["waverider"].setdefault("endpoint_reached_at", stamp)
            engine.state.update(record)

    async def observe(self, engine, identity, market):
        venue = getattr(engine.exchange.settings, "venue", "hyperliquid")
        base = str(engine.exchange.client.base_url).rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        if venue == "hyperliquid":
            url, subscribe = base + "/ws", {"method": "subscribe", "subscription": {"type": "l2Book", "coin": market.symbol, "fast": True}}
        elif venue == "aster":
            url = ("wss://fstream.asterdex-testnet.com" if engine.exchange.settings.network == "testnet" else "wss://fstream.asterdex.com") + f"/ws/{market.native_symbol.lower()}@depth20@100ms"
            subscribe = None
        else:
            url, subscribe = base + "/stream", {"type": "subscribe", "channel": f"order_book/{market.asset_id}"}
        wake = asyncio.Event()
        latest_book = None
        async def manage():
            last, last_sync = 0.0, 0.0
            while True:
                await wake.wait()
                await asyncio.sleep(max(0, 1 - (asyncio.get_running_loop().time() - last)))
                wake.clear()
                try:
                    async with engine.lock:
                        if engine.recovery_checked:
                            trade = engine.state.read("trades", entries={"trades": [identity]})["trades"].get(identity)
                            book = latest_book
                            if not trade or book is None:
                                continue
                            now = asyncio.get_running_loop().time()
                            pending = [row for row in trade.get("entry_orders", []) if not row.get("not_sent") and row.get("phase") != "terminal"]
                            actionable = any(book.ask <= Decimal(row["limit"]) if trade["plan"]["side"] == "long" else book.bid >= Decimal(row["limit"]) for row in pending)
                            targets = trade.get("targets", trade["plan"]["take_profits"])
                            completed = set(trade.get("completed_target_indices", []))
                            actionable |= any((book.bid >= Decimal(target) if trade["plan"]["side"] == "long" else book.ask <= Decimal(target)) for index, target in enumerate(targets) if index not in completed)
                            if actionable and now - last_sync >= 3:
                                await engine.refresh_trade(identity)
                                last_sync = now
                            if trade["plan"]["policy"].get("management_mode") == "waverider" and Decimal(trade["remaining_quantity"]) > 0:
                                from .waverider import tick
                                await tick(engine, identity, book)
                            if Decimal(trade["remaining_quantity"]) > 0:
                                from .waverider import queue_evidence
                                queue_evidence(engine, identity, book)
                except Exception:
                    # The normal recovery pass owns diagnostics and uncertain operations.
                    pass
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
                                latest_book = book
                                self.latch(engine, identity, book.bid, book.ask, book.exchange_time)
                                wake.set()
                except (OSError, TimeoutError, AqlaError, ValueError, KeyError, IndexError, WebSocketException):
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
                        wanted[identity] = engine, Market.model_validate(trade["plan"]["market"])
                removed = [self.tasks.pop(identity) for identity in set(self.tasks) - set(wanted)]
                for task in removed:
                    task.cancel()
                await asyncio.gather(*removed, return_exceptions=True)
                for identity, (engine, market) in wanted.items():
                    if identity not in self.tasks or self.tasks[identity].done():
                        self.tasks[identity] = asyncio.create_task(self.observe(engine, identity, market), name="aqla-book-" + identity[:8])
                if self.reroute_task is None or self.reroute_task.done():
                    self.reroute_task = asyncio.create_task(self.reroute(), name="aqla-zero-fill-handoff")
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
            if self.reroute_task:
                self.reroute_task.cancel()
                tasks.append(self.reroute_task)
            for engine in self.router.engines.values():
                for task in getattr(engine, "_waverider_capture_tasks", {}).values():
                    task.cancel()
                    tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reroute(self):
        try:
            from .liquidity import reroute_waiting
            await reroute_waiting(self.router)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Zero-fill venue handoff awaits reconciliation")
