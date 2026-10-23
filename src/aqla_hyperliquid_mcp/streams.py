from __future__ import annotations

import asyncio
import base64
import json
import zlib
from collections import deque
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, TypeAdapter, create_model
from typing_extensions import TypedDict
from websockets.asyncio.client import connect

from aqla_agent.notifications import native_response_bytes

from .catalog import Coin, Interval
from .models import Address, AqlaError, Model, digest, now_ms


class StreamFailure(TypedDict):
    category: Literal["decode", "transport", "timeout"]
    observed_at: int


class StreamDiagnostics(TypedDict):
    last_error: StreamFailure | None
    error_counts: dict[str, int]
    buffered_bytes: int
    buffered_events: int
    dropped_events: int


class StreamReadResult(TypedDict):
    events: list[dict]
    next_cursor: int
    gap: bool
    connected: bool
    rest_reconciliation_required: bool
    diagnostics: StreamDiagnostics

SUBSCRIPTION_FIELDS: dict[str, dict[str, Any]] = {
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
                                        **{"type": (Literal[name], name), **fields}) for name, fields in SUBSCRIPTION_FIELDS.items()}
if TYPE_CHECKING:
    Subscription = Model
else:
    # Runtime schemas come from the pinned native API catalog.
    Subscription = Annotated[reduce(or_, SUBSCRIPTION_MODELS.values()), Field(discriminator="type")]
SUBSCRIPTION_ADAPTER: TypeAdapter[Model] = TypeAdapter(Subscription)


class Streams:
    """Bounded feeds and correlated WS posts. Reconnect never repeats a write."""

    def __init__(self, base_url, *, connector=connect, max_events=4096, timeout=15, max_subscriptions=128,
                 max_event_bytes=8 * 2**20, max_read_bytes=600000):
        if any(type(value) is not int or value < 1 for value in (max_events, max_subscriptions, max_event_bytes, max_read_bytes)) or not 1024 <= max_event_bytes or not 1024 <= max_read_bytes <= 600000:
            raise ValueError("Stream budgets require positive counts, at least 1024 ring/read bytes and at most 600000 read bytes")
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
        self.pending_post_kinds = {}
        self.posts = 0
        self.events, self.event_sizes = deque(), deque()
        self.max_events, self.max_subscriptions = max_events, max_subscriptions
        self.max_event_bytes, self.max_read_bytes = max_event_bytes, max_read_bytes
        self.buffered_bytes, self.lost_through, self.dropped_events = 0, 0, 0
        self.last_error = None
        self.error_counts = {"decode": 0, "transport": 0, "timeout": 0}
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
                        try:
                            self.receive(json.loads(raw))
                        except (ValueError, TypeError, KeyError, AttributeError, zlib.error):
                            self._failure("decode")
                            self._event({"channel": "gap", "data": {"reason": "decode_failure", "rest_reconciliation_required": True}})
            except TimeoutError:
                self._failure("timeout")
            except Exception:
                self._failure("transport")
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
                self.pending_post_kinds.clear()
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

    def _failure(self, category):
        self.error_counts[category] += 1
        self.last_error = {"category": category, "observed_at": now_ms()}

    def _diagnostics(self) -> StreamDiagnostics:
        return {"last_error": self.last_error, "error_counts": dict(self.error_counts), "buffered_bytes": self.buffered_bytes,
                "buffered_events": len(self.events), "dropped_events": self.dropped_events}

    def _read_result(self, rows, after) -> StreamReadResult:
        gap = after < self.lost_through
        return {"events": rows, "next_cursor": rows[-1]["sequence"] if rows else after, "gap": gap, "connected": self.ready.is_set(),
                "rest_reconciliation_required": gap or any(row["channel"] in ("connection", "gap") for row in rows), "diagnostics": self._diagnostics()}

    def _event(self, value):
        self.sequence += 1
        row = {**value, "sequence": self.sequence, "observed_at": now_ms()}
        size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        if size > self.max_event_bytes or native_response_bytes(self._read_result([row], self.sequence - 1)) > self.max_read_bytes:
            self.lost_through = self.sequence
            self.dropped_events += 1
            row = {"sequence": self.sequence, "observed_at": row["observed_at"], "channel": "gap",
                   "data": {"reason": "oversize_event", "rest_reconciliation_required": True}}
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.events.append(row)
        self.event_sizes.append(size)
        self.buffered_bytes += size
        while len(self.events) > self.max_events or self.buffered_bytes > self.max_event_bytes:
            lost = self.events.popleft()
            self.buffered_bytes -= self.event_sizes.popleft()
            self.lost_through = max(self.lost_through, lost["sequence"])
            self.dropped_events += 1

    def receive(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("channel"), str):
            raise AqlaError("Malformed WebSocket event")
        if value.get("channel") == "post":
            data = value.get("data", {})
            future = self.pending_posts.get(data.get("id"))
            if future is not None and not future.done():
                response = data.get("response", {})
                if response.get("type") == "error":
                    future.set_exception(AqlaError("WebSocket post rejected by exchange"))
                elif response.get("type") == self.pending_post_kinds.get(data.get("id")):
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
        if identity not in self.subscriptions and len(self.subscriptions) >= self.max_subscriptions:
            raise AqlaError("WebSocket subscription budget exceeded")
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

    def read(self, after=0, limit=100) -> StreamReadResult:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise AqlaError("Stream read limit must be 1..1000")
        if after > self.sequence:
            raise AqlaError("Stream cursor cannot refer to a future event")
        rows: list[dict] = []
        for row in self.events:
            if row["sequence"] <= after:
                continue
            candidate = self._read_result([*rows, row], after)
            if native_response_bytes(candidate) > self.max_read_bytes:
                if rows:
                    break
                row = {"sequence": row["sequence"], "observed_at": row["observed_at"], "channel": "gap",
                       "data": {"reason": "oversize_read_event", "rest_reconciliation_required": True}}
                if native_response_bytes(self._read_result([row], after)) > self.max_read_bytes:
                    raise AqlaError("Stream metadata exceeds the configured read budget")
            rows.append(row)
            if len(rows) == limit:
                break
        return self._read_result(rows, after)

    async def post(self, kind, payload):
        if kind not in ("info", "action"):
            raise AqlaError("WebSocket only supports typed info or action posts")
        self.start()
        await asyncio.wait_for(self.ready.wait(), self.timeout)
        self.posts += 1
        identity = self.posts
        future = asyncio.get_running_loop().create_future()
        self.pending_posts[identity] = future
        self.pending_post_kinds[identity] = kind
        try:
            await self._send({"method": "post", "id": identity, "request": {"type": kind, "payload": payload}})
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending_posts.pop(identity, None)
            self.pending_post_kinds.pop(identity, None)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
