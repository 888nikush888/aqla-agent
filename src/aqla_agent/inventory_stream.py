"""Fresh, complete large-DEX inventory over the existing bounded stream transport."""
import asyncio
import json

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from aqla_hyperliquid_mcp.models import AqlaError, now_ms
from aqla_hyperliquid_mcp.streams import Streams

from .observations import TemporaryObservation, read_observation


class InventoryStream(Streams):
    """Keep every order scope live; each read waits for a new aggregate account frame."""
    def __init__(self, adapter, names, connector):
        super().__init__(adapter.settings.base_url, connector=connector, timeout=9,
                         max_subscriptions=513)
        self.account = adapter.account.lower()
        self.names = tuple(names)
        self.states, self.orders, self.sizes = None, {}, {}
        self.state_sequence = 0
        self.aggregate_id, self.aggregate_ack = None, 0
        self.generation = 0
        self.error = None
        self.changed = asyncio.Event()
        self.read_lock = asyncio.Lock()

    def _clear(self):
        self.generation += 1
        self.states, self.orders, self.sizes = None, {}, {}
        self.state_sequence = 0
        self.aggregate_ack = 0

    def _event(self, value):
        self.sequence += 1
        channel, data = value.get("channel"), value.get("data", {})
        if channel in ("connection", "gap"):
            self._clear()
            self.changed.set()
            return
        if channel == "subscriptionResponse" and isinstance(data, dict):
            spec = data.get("subscription", {})
            if data.get("method") == "subscribe" and spec.get("type") == "allDexsClearinghouseState" and str(spec.get("user", "")).lower() == self.account:
                self.aggregate_ack = self.sequence
        if channel == "error":
            self.error = AqlaError("Large DEX snapshot subscription was rejected")
        elif channel in ("allDexsClearinghouseState", "openOrders") and self.error is None:
            try:
                if not isinstance(data, dict) or str(data.get("user", "")).lower() != self.account:
                    raise AqlaError("DEX snapshot belongs to another account")
                if channel == "allDexsClearinghouseState":
                    rows = data.get("clearinghouseStates")
                    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], dict) for row in rows):
                        raise AqlaError("DEX position snapshot is malformed")
                    states = dict(rows)
                    if len(states) != len(rows) or set(states) != set(self.names):
                        raise AqlaError("DEX position snapshot is incomplete")
                    self.states, self.state_sequence = states, self.sequence
                    identity = "states"
                else:
                    dex = data.get("dex")
                    if dex not in self.names or not isinstance(data.get("orders"), list):
                        raise AqlaError("DEX order snapshot is incomplete")
                    self.orders[dex] = data["orders"]
                    identity = "orders:" + dex
                self.sizes[identity] = len(json.dumps(data, separators=(",", ":")).encode())
                if sum(self.sizes.values()) > self.max_event_bytes:
                    raise AqlaError("DEX snapshot exceeds its bounded memory budget")
            except (AqlaError, ValueError, TypeError, KeyError) as error:
                self.error = error if isinstance(error, AqlaError) else AqlaError("DEX snapshot is malformed")
        if self.error is not None:
            self._clear()
        self.changed.set()


async def observe(adapter, names, *, connector=connect):
    expected = set(names)
    if not names or len(names) > 512 or len(expected) != len(names):
        raise AqlaError("Large DEX inventory is incomplete or exceeds the subscription budget")
    feed = getattr(adapter, "_inventory_feed", None)
    if feed is not None and (feed.closed or feed.error is not None or feed.names != tuple(names)
                             or feed.account != adapter.account.lower() or feed.url != adapter.settings.base_url.replace("https://", "wss://") + "/ws"):
        await feed.close()
        feed = None
    new = feed is None
    if new:
        feed = InventoryStream(adapter, names, connector)
        adapter._inventory_feed = feed
    try:
        async with asyncio.timeout(9), feed.read_lock:
            after = 0 if new else feed.sequence
            requested_after = after
            if new:
                subscription = await feed.subscribe({"type": "allDexsClearinghouseState", "user": adapter.account})
                feed.aggregate_id = subscription["subscription_id"]
                for dex in names:
                    await feed.subscribe({"type": "openOrders", "user": adapter.account, "dex": dex})
            else:
                await feed.unsubscribe(feed.aggregate_id)
                await feed.subscribe({"type": "allDexsClearinghouseState", "user": adapter.account})
            while True:
                feed.changed.clear()
                if feed.error is not None:
                    raise feed.error
                states = feed.states
                acknowledged = new or feed.aggregate_ack > requested_after and feed.state_sequence > feed.aggregate_ack
                complete = states is not None and set(feed.orders) == expected and feed.state_sequence > after and feed.ready.is_set() and acknowledged
                def fresh(states=states):
                    return states is not None and all(type(row.get("time")) is int and -2000 <= now_ms() - row["time"] <= 10000 for row in states.values())
                if complete and fresh():
                    # The connected, ordered feed retains all scopes; no budget snapshot is reused.
                    orders = dict(feed.orders)
                    generation, state_sequence = feed.generation, feed.state_sequence
                    limiter = asyncio.Semaphore(3)
                    async def details(dex, orders=orders, states=states, limiter=limiter):
                        values = orders[dex]
                        if values:
                            async with limiter:
                                values = await read_observation(lambda: adapter.info("frontendOpenOrders", user=adapter.account, **({"dex": dex} if dex else {})), "inventory_details")
                        return dex, states[dex], values
                    result = await asyncio.gather(*(details(dex) for dex in names))
                    if feed.error is not None:
                        raise feed.error
                    if fresh() and generation == feed.generation and feed.ready.is_set():
                        return result
                    after = state_sequence
                await feed.changed.wait()
    except BaseException as error:
        await feed.close()
        if getattr(adapter, "_inventory_feed", None) is feed:
            adapter._inventory_feed = None
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, AqlaError):
            raise
        if isinstance(error, (TimeoutError, ConnectionError, OSError, WebSocketException)):
            raise TemporaryObservation("read_unavailable", "inventory", "transport") from None
        raise AqlaError("Complete large-DEX account snapshot is invalid; no entry permitted") from None
