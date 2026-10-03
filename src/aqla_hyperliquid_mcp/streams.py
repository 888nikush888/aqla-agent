from __future__ import annotations

import asyncio
import base64
import json
import zlib
from collections import deque
from functools import reduce
from operator import or_
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, create_model
from websockets.asyncio.client import connect

from .catalog import Coin, Interval
from .models import Address, AqlaError, Model, digest, now_ms

SUBSCRIPTION_FIELDS = {
    "allMids": {"dex": (str | None, None)},
    "notification": {"user": (Address, ...)},
    "webData3": {"user": (Address, ...)},
    "twapStates": {"user": (Address, ...), "dex": (str | None, None)},
    "clearinghouseState": {"user": (Address, ...), "dex": (str | None, None)},
    "openOrders": {"user": (Address, ...), "dex": (str | None, None)},
    "candle": {"coin": (Coin, ...), "interval": (Interval, ...)},
    "l2Book": {"coin": (Coin, ...), "nSigFigs": (Literal[2, 3, 4, 5] | None, None), "mantissa": (Literal[1, 2, 5] | None, None), "fast": (bool | None, None)},
    "trades": {"coin": (Coin, ...)},
    "orderUpdates": {"user": (Address, ...)},
    "userEvents": {"user": (Address, ...)},
    "userFills": {"user": (Address, ...), "aggregateByTime": (bool | None, None)},
    "userFundings": {"user": (Address, ...)},
    "userNonFundingLedgerUpdates": {"user": (Address, ...)},
    "activeAssetCtx": {"coin": (Coin, ...)},
    "activeAssetData": {"user": (Address, ...), "coin": (Coin, ...)},
    "userTwapSliceFills": {"user": (Address, ...)},
    "userTwapHistory": {"user": (Address, ...)},
    "bbo": {"coin": (Coin, ...)},
    "spotState": {"user": (Address, ...), "isPortfolioMargin": (bool | None, None)},
    "allDexsClearinghouseState": {"user": (Address, ...)},
    "allDexsAssetCtxs": {},
    "outcomeMetaUpdates": {},
    "fastAssetCtxs": {},
}
SUBSCRIPTION_MODELS = {name: create_model("Subscribe" + name[:1].upper() + name[1:], __base__=Model,
                                        type=(Literal[name], name), **fields) for name, fields in SUBSCRIPTION_FIELDS.items()}
Subscription = Annotated[reduce(or_, SUBSCRIPTION_MODELS.values()), Field(discriminator="type")]
SUBSCRIPTION_ADAPTER = TypeAdapter(Subscription)


class Streams:
    """Bounded feeds and correlated WS posts. Reconnect never repeats a write."""

    def __init__(self, base_url, *, connector=connect, max_events=4096, timeout=15):
        self.url = base_url.replace("https://", "wss://") + "/ws"
        self.connector = connector
        self.timeout = timeout
        self.subscriptions = {}
        self.socket = None
        self.ready = asyncio.Event()
        self.sends = asyncio.Lock()
        self.subscription_sync = asyncio.Lock()
        self.active_subscriptions = {}
        self.pending_posts = {}
        self.posts = 0
        self.events = deque(maxlen=max_events)
        self.sequence = 0
        self.task = None
        self.closed = False

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="aqla-hyperliquid-streams")

    async def _send(self, payload, *, socket=None):
        async with self.sends:
            if self.socket is None or socket is not None and socket is not self.socket:
                raise AqlaError("WebSocket is disconnected")
            await self.socket.send(json.dumps(payload, separators=(",", ":")))

    async def _run(self):
        retry = 1
        while not self.closed:
            try:
                async with self.connector(self.url, ping_interval=20, ping_timeout=20, max_size=2**20) as socket:
                    self.socket = socket
                    self.active_subscriptions = {}
                    await self._sync_subscriptions()
                    self.ready.set()
                    retry = 1
                    async for raw in socket:
                        self.receive(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self.ready.clear()
                self.socket = None
                self.active_subscriptions = {}
                if not self.closed:
                    self._event({"channel": "connection", "data": {"connected": False, "rest_reconciliation_required": True}})
                for future in self.pending_posts.values():
                    if not future.done():
                        future.set_exception(AqlaError("WebSocket response lost; no action was retried"))
                self.pending_posts.clear()
            if not self.closed:
                await asyncio.sleep(retry)
                retry = min(retry * 2, 30)

    async def _sync_subscriptions(self):
        # Desired changes remain immediate while a transport send is suspended.
        # Recompute after every awaited send before marking a connection ready.
        async with self.subscription_sync:
            socket = self.socket
            while socket is not None and socket is self.socket:
                removed = next((key for key in self.active_subscriptions if key not in self.subscriptions), None)
                added = next((key for key in self.subscriptions if key not in self.active_subscriptions), None)
                if removed is not None:
                    subscription = self.active_subscriptions[removed]
                    await self._send({"method": "unsubscribe", "subscription": subscription}, socket=socket)
                    if socket is self.socket:
                        self.active_subscriptions.pop(removed, None)
                elif added is not None:
                    subscription = self.subscriptions[added]
                    await self._send({"method": "subscribe", "subscription": subscription}, socket=socket)
                    if socket is self.socket:
                        self.active_subscriptions[added] = subscription
                else:
                    return

    @staticmethod
    def decode_fast(value):
        decoder = zlib.decompressobj(wbits=-15)
        output = decoder.decompress(base64.b64decode(value, validate=True), max_length=2**20 + 1)
        if len(output) > 2**20 or decoder.unconsumed_tail or not decoder.eof:
            raise AqlaError("Fast asset context exceeds the bounded decoding limit")
        return json.loads(output.decode("utf-8"))

    def _event(self, value):
        self.sequence += 1
        self.events.append({"sequence": self.sequence, "observed_at": now_ms(), **value})

    def receive(self, value):
        if value.get("channel") == "post":
            data = value.get("data", {})
            future = self.pending_posts.get(data.get("id"))
            if future is not None and not future.done():
                response = data.get("response", {})
                if response.get("type") == "error":
                    future.set_exception(AqlaError("WebSocket post rejected by exchange"))
                elif response.get("type") in ("info", "action"):
                    future.set_result(response.get("payload"))
                else:
                    future.set_exception(AqlaError("Unknown WebSocket post response type"))
            return
        if value.get("channel") == "fastAssetCtxs":
            value = {**value, "data": self.decode_fast(value["data"])}
        self._event(value)

    async def subscribe(self, request):
        subscription = SUBSCRIPTION_ADAPTER.validate_python(request).model_dump(mode="json", exclude_none=True)
        if subscription.get("mantissa") is not None and subscription.get("nSigFigs") != 5:
            raise AqlaError("Book mantissa requires five significant figures")
        identity = digest(subscription)
        self.subscriptions[identity] = subscription
        self.start()
        if self.ready.is_set():
            await self._sync_subscriptions()
        return {"subscription_id": identity, "state": "requested", "confirmation_in_events": True}

    async def unsubscribe(self, subscription_id):
        self.subscriptions.pop(subscription_id, None)
        if self.ready.is_set():
            await self._sync_subscriptions()
        return {"subscription_id": subscription_id, "desired": False}

    def read(self, after=0, limit=100):
        if not 1 <= limit <= 1000:
            raise AqlaError("Stream read limit must be 1..1000")
        gap = bool(self.events) and after > 0 and after < self.events[0]["sequence"] - 1
        rows = [row for row in self.events if row["sequence"] > after][:limit]
        return {"events": rows, "next_cursor": rows[-1]["sequence"] if rows else after,
                "gap": gap, "connected": self.ready.is_set(), "rest_reconciliation_required": gap or any(row["channel"] == "connection" for row in rows)}

    async def post(self, kind, payload):
        if kind not in ("info", "action"):
            raise AqlaError("WebSocket only supports typed info or action posts")
        self.start()
        await asyncio.wait_for(self.ready.wait(), self.timeout)
        self.posts += 1
        identity = self.posts
        future = asyncio.get_running_loop().create_future()
        self.pending_posts[identity] = future
        try:
            await self._send({"method": "post", "id": identity, "request": {"type": kind, "payload": payload}})
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending_posts.pop(identity, None)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
