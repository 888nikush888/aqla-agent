
import asyncio
import base64
import json
import os
import zlib

import pytest

from aqla_agent.notifications import native_response_bytes
from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.streams import SUBSCRIPTION_ADAPTER, SUBSCRIPTION_MODELS, Streams


@pytest.mark.asyncio
async def test_subscription_cap_is_checked_before_desired_mutation_and_duplicate_costs_nothing(monkeypatch):
    stream = Streams("https://example.invalid", max_subscriptions=2)
    monkeypatch.setattr(stream, "start", lambda: None)
    btc = await stream.subscribe({"type": "trades", "coin": "BTC"})
    await stream.subscribe({"type": "trades", "coin": "ETH"})
    assert await stream.subscribe({"type": "trades", "coin": "BTC"}) == btc
    before = dict(stream.subscriptions)
    with pytest.raises(AqlaError, match="budget"):
        await stream.subscribe({"type": "trades", "coin": "SOL"})
    assert stream.subscriptions == before


def test_event_byte_overflow_has_gap_and_exact_utf8_accounting():
    stream = Streams("https://example.invalid", max_events=100, max_event_bytes=1500)
    for _ in range(10):
        stream.receive({"channel": "trades", "data": "🚀" * 80, "sequence": -10})
    assert stream.sequence == 10 and stream.events[-1]["sequence"] == 10
    actual = sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) for row in stream.events)
    assert stream.buffered_bytes == actual <= 1500
    assert len(stream.events) < 10 and stream.read()["gap"] and stream.read()["rest_reconciliation_required"]


def test_oversize_single_event_is_visible_gap_with_bounded_forward_progress():
    stream = Streams("https://example.invalid", max_read_bytes=2000)
    stream.receive({"channel": "trades", "data": "private-payload" * 100000})
    stream.receive({"channel": "trades", "data": {"small": True}})
    page = stream.read(limit=1)
    assert native_response_bytes(page) <= 2000 and page["next_cursor"] == 1
    assert page["events"][0]["channel"] == "gap" and page["rest_reconciliation_required"]
    assert "private-payload" not in json.dumps(page)
    assert stream.read(after=page["next_cursor"])["next_cursor"] == 2


def test_complete_stream_read_byte_limit_pages_every_emitted_event():
    stream = Streams("https://example.invalid", max_read_bytes=4000)
    for _ in range(40):
        stream.receive({"channel": "trades", "data": {"proof": "\\\"🚀" * 80}})
    cursor, found = 0, []
    while cursor < stream.sequence:
        page = stream.read(after=cursor, limit=1000)
        assert native_response_bytes(page) <= 4000 and page["events"]
        assert page["next_cursor"] == page["events"][-1]["sequence"]
        found.extend(row["sequence"] for row in page["events"])
        cursor = page["next_cursor"]
    assert found == list(range(1, 41))
    with pytest.raises(AqlaError, match="future"):
        stream.read(after=41)


@pytest.mark.asyncio
async def test_info_response_cannot_confirm_pending_action():
    stream = Streams("https://example.invalid")
    future = asyncio.get_running_loop().create_future()
    stream.pending_posts[1], stream.pending_post_kinds[1] = future, "action"
    stream.receive({"channel": "post", "data": {"id": 1, "response": {"type": "info", "payload": {"untrusted": True}}}})
    with pytest.raises(AqlaError, match="response type"):
        await future


def test_stream_diagnostics_contain_only_categories_times_and_counts():
    stream = Streams("https://example.invalid")
    stream._failure("decode")
    stream._failure("transport")
    stream._failure("timeout")
    page = stream.read()
    assert set(page["diagnostics"]["last_error"]) == {"category", "observed_at"}
    assert page["diagnostics"]["error_counts"] == {"decode": 1, "transport": 1, "timeout": 1}
    assert page["diagnostics"]["last_error"]["category"] == "timeout"


def test_all_documented_subscriptions_have_strict_schemas():
    assert len(SUBSCRIPTION_MODELS) == 24
    for model in SUBSCRIPTION_MODELS.values():
        assert model.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValueError):
        SUBSCRIPTION_ADAPTER.validate_python({"type": "trades", "coin": "BTC", "send_order": {}})


def test_fast_asset_context_is_decoded_and_bounded():
    value = {"BTC": {"markPx": "97500.5"}}
    compressed = zlib.compress(json.dumps(value).encode())[2:-4]
    stream = Streams("https://example.invalid")
    stream.receive({"channel": "fastAssetCtxs", "data": base64.b64encode(compressed).decode()})
    assert stream.read()["events"][0]["data"] == value


def test_gap_is_visible_after_buffer_overflow():
    stream = Streams("https://example.invalid", max_events=2)
    for i in range(5):
        stream.receive({"channel": "trades", "data": i})
    assert stream.read(after=1)["gap"] is True
    assert stream.read(after=1)["rest_reconciliation_required"] is True


@pytest.mark.asyncio
async def test_post_response_is_correlated_not_mistaken_for_a_feed_event():
    stream = Streams("https://example.invalid")
    future = asyncio.get_running_loop().create_future()
    stream.pending_posts[42] = future
    stream.pending_post_kinds[42] = "action"
    stream.receive({"channel": "post", "data": {"id": 42, "response": {"type": "action", "payload": {"status": "ok"}}}})
    assert await future == {"status": "ok"}
    assert stream.read()["events"] == []


class PausedSocket:
    def __init__(self):
        self.payloads = []
        self.entered, self.release, self.done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(self, payload):
        self.payloads.append(json.loads(payload))
        if len(self.payloads) == 1:
            self.entered.set()
            await self.release.wait()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.done.wait()
        raise StopAsyncIteration


class PausedConnection:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_actual_stream_loop_decodes_failures_continues_and_cancels_without_payload_leak():
    class Socket(PausedSocket):
        def __init__(self):
            super().__init__()
            self.rows = iter(['{"private-payload"', json.dumps({"channel": "fastAssetCtxs", "data": "bad-sensitive-base64"}),
                              json.dumps({"channel": "trades", "data": {"valid": True}})])
            self.consumed = asyncio.Event()
        async def __anext__(self):
            try:
                return next(self.rows)
            except StopIteration:
                self.consumed.set()
                await self.done.wait()
                raise StopAsyncIteration from None
    socket = Socket()
    stream = Streams("https://unused.invalid", connector=lambda *_args, **_kwargs: PausedConnection(socket))
    stream.start()
    await asyncio.wait_for(socket.consumed.wait(), 1)
    assert stream.read()["diagnostics"]["error_counts"] == {"decode": 2, "transport": 0, "timeout": 0}
    assert stream.read()["events"][-1]["data"] == {"valid": True}
    assert stream.read()["rest_reconciliation_required"]
    assert "sensitive" not in json.dumps(stream.read()) and "private-payload" not in json.dumps(stream.read())
    await stream.close()
    assert not stream.ready.is_set() and stream.task.cancelled()


@pytest.mark.asyncio
async def test_actual_stream_timeout_is_categorized_without_exception_text_or_retry_action():
    class Connection:
        async def __aenter__(self):
            stream.closed = True
            raise TimeoutError("sensitive-credentials")
        async def __aexit__(self, *_):
            return None
    stream = Streams("https://unused.invalid", connector=lambda *_args, **_kwargs: Connection())
    await stream._run()
    assert stream.read()["diagnostics"]["last_error"]["category"] == "timeout"
    assert "sensitive" not in json.dumps(stream.read()) and stream.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_btc", [False, True])
async def test_desired_changes_during_connect_are_sent_before_ready_without_duplicates(remove_btc):
    socket = PausedSocket()
    stream = Streams("https://unused.invalid", connector=lambda *_args, **_kwargs: PausedConnection(socket))
    try:
        btc = await stream.subscribe({"type": "trades", "coin": "BTC"})
        await asyncio.wait_for(socket.entered.wait(), 1)
        eth = await stream.subscribe({"type": "trades", "coin": "ETH"})
        assert await stream.subscribe({"type": "trades", "coin": "ETH"}) == eth
        if remove_btc:
            await stream.unsubscribe(btc["subscription_id"])
        assert not stream.ready.is_set()
        socket.release.set()
        await asyncio.wait_for(stream.ready.wait(), 1)
        sent = [(row["method"], row["subscription"]["coin"]) for row in socket.payloads]
        assert sent == ([('subscribe', 'BTC'), ('unsubscribe', 'BTC'), ('subscribe', 'ETH')] if remove_btc else
                        [('subscribe', 'BTC'), ('subscribe', 'ETH')])
        assert set(stream.active_subscriptions) == set(stream.subscriptions)
        await stream.subscribe({"type": "trades", "coin": "ETH"})
        assert len(socket.payloads) == len(sent)
        await stream.unsubscribe(eth["subscription_id"])
        assert socket.payloads[-1] == {"method": "unsubscribe", "subscription": {"type": "trades", "coin": "ETH"}}
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_subscription_sync_waiting_for_send_does_not_cross_into_replacement_socket():
    old, replacement = PausedSocket(), PausedSocket()
    stream = Streams("https://unused.invalid")
    stream.socket = old
    stream.subscriptions["btc"] = {"type": "trades", "coin": "BTC"}
    await stream.sends.acquire()
    sync = asyncio.create_task(stream._sync_subscriptions())
    await asyncio.sleep(0)
    stream.socket = replacement
    stream.sends.release()
    with pytest.raises(AqlaError, match="disconnected"):
        await sync
    assert old.payloads == replacement.payloads == []
    assert stream.active_subscriptions == {}
    assert "btc" in stream.subscriptions


@pytest.mark.skipif(os.name == "nt" and os.getenv("AQLA_LOCAL_NETWORK_TESTS") != "1", reason="Local socket permission must be explicitly enabled on the Windows sandbox")
@pytest.mark.asyncio
async def test_local_server_reconnect_unsubscribe_and_lost_action_is_not_reposted(setup):
    from websockets.asyncio.server import serve

    from aqla_hyperliquid_mcp.catalog import SDK_ADAPTER

    _, state, _, exchange, _ = setup
    connected = 0
    accepted_posts = []
    subscribed = []
    reconnected = asyncio.Event()
    unsubscribed = asyncio.Event()

    async def server(socket):
        nonlocal connected
        connected += 1
        async for raw in socket:
            value = json.loads(raw)
            if value["method"] == "subscribe":
                subscribed.append(value["subscription"])
                if connected > 1:
                    reconnected.set()
            elif value["method"] == "unsubscribe":
                unsubscribed.set()
            elif value["method"] == "post":
                accepted_posts.append(value)
                await socket.close()

    async with serve(server, "127.0.0.1", 0) as local:
        stream = Streams("https://unused.invalid", timeout=5)
        stream.url = "ws://127.0.0.1:" + str(local.sockets[0].getsockname()[1])
        subscription = await stream.subscribe({"type": "trades", "coin": "BTC"})
        request = SDK_ADAPTER.validate_python({"operation": "update_leverage", "name": "BTC", "leverage": 2, "is_cross": False})
        try:
            result = await exchange.sdk_action("lost-ws", request, sender=lambda payload: stream.post("action", payload))
            assert result["phase"] == "unknown"
            await asyncio.wait_for(reconnected.wait(), 5)
            again = await exchange.sdk_action("lost-ws", request, sender=lambda payload: stream.post("action", payload))
            assert again["phase"] == "unknown"
            assert len(accepted_posts) == 1
            assert len(subscribed) == 2
            await stream.unsubscribe(subscription["subscription_id"])
            await asyncio.wait_for(unsubscribed.wait(), 5)
            assert state.snapshot()["operations"]["lost-ws"]["phase"] == "unknown"
            assert any(event["channel"] == "connection" for event in stream.read()["events"])
        finally:
            await stream.close()
