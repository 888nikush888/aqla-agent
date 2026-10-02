import asyncio
import base64
import json
import os
import zlib

import pytest

from aqla_hyperliquid_mcp.streams import SUBSCRIPTION_ADAPTER, SUBSCRIPTION_MODELS, Streams


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
    stream.receive({"channel": "post", "data": {"id": 42, "response": {"type": "action", "payload": {"status": "ok"}}}})
    assert await future == {"status": "ok"}
    assert stream.read()["events"] == []


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
