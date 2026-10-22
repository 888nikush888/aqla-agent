import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from aqla_agent.inventory_stream import observe
from aqla_hyperliquid_mcp.models import AqlaError, now_ms


class Socket:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.frames:
            await asyncio.sleep(.001)
        await asyncio.sleep(.001)
        return await self.recv()

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self.frames:
            raise OSError("disconnected before completeness")
        return json.dumps(self.frames.pop(0))


def inputs():
    account = "0x" + "1" * 40
    names = [""] + ["dex" + str(index) for index in range(267)]
    states = {name: {"time": now_ms(), "assetPositions": [], "withdrawable": "100", "marginSummary": {"accountValue": "100"}} for name in names}
    frames = [{"channel": "allDexsClearinghouseState", "data": {"user": account, "clearinghouseStates": list(map(list, states.items()))}}]
    frames += [{"channel": "openOrders", "data": {"user": account, "dex": name, "orders": []}} for name in names]
    return SimpleNamespace(account=account, settings=SimpleNamespace(base_url="https://api.hyperliquid-testnet.xyz")), names, frames


@pytest.mark.asyncio
async def test_repeated_inventory_waits_for_new_account_frame_without_resubscribing_orders():
    adapter, names, frames = inputs()
    aggregate = copy.deepcopy(frames[0])
    socket = Socket(frames)
    connections = []
    def connector(*args, **kwargs):
        connections.append(args)
        return socket
    first = await observe(adapter, names, connector=connector)
    task = asyncio.create_task(observe(adapter, names, connector=connector))
    await asyncio.sleep(.01)
    assert not task.done(), "A previously returned budget is not a fresh observation"
    aggregate["data"]["clearinghouseStates"][0][1]["marginSummary"]["accountValue"] = "42"
    socket.frames.extend([{"channel": "subscriptionResponse", "data": {"method": "subscribe", "subscription": {"type": "allDexsClearinghouseState", "user": adapter.account}}}, aggregate])
    second = await task
    assert first[0][1]["marginSummary"]["accountValue"] == "100"
    assert second[0][1]["marginSummary"]["accountValue"] == "42"
    assert len(connections) == 1 and len(socket.sent) == len(names) + 3
    assert sum(row["subscription"]["type"] == "openOrders" for row in socket.sent) == len(names)
    await adapter._inventory_feed.close()


@pytest.mark.asyncio
async def test_gap_discards_every_cached_order_scope_before_readmission():
    adapter, names, frames = inputs()
    aggregate = copy.deepcopy(frames[0])
    order_frames = copy.deepcopy(frames[1:])
    socket = Socket(frames)
    def connector(*args, **kwargs):
        return socket
    await observe(adapter, names, connector=connector)
    feed = adapter._inventory_feed
    feed._event({"channel": "gap", "data": {"reason": "decode_failure"}})
    assert feed.states is None and not feed.orders
    task = asyncio.create_task(observe(adapter, names, connector=connector))
    await asyncio.sleep(.01)
    socket.frames.extend([{"channel": "subscriptionResponse", "data": {"method": "subscribe", "subscription": {"type": "allDexsClearinghouseState", "user": adapter.account}}}, aggregate])
    await asyncio.sleep(.01)
    assert not task.done(), "A new balance cannot replace missing order proofs"
    socket.frames.extend(order_frames)
    assert len(await task) == len(names)
    await feed.close()


@pytest.mark.asyncio
async def test_disconnect_during_rest_details_cannot_return_a_cached_budget():
    adapter, names, frames = inputs()
    frames[1]["data"]["orders"] = [{"coin": "BTC"}]
    entered = asyncio.Event()
    async def info(*args, **kwargs):
        feed = adapter._inventory_feed
        feed.ready.clear()
        feed._event({"channel": "connection", "data": {"connected": False}})
        entered.set()
        return [{"coin": "BTC", "reduceOnly": False}]
    adapter.info = info
    task = asyncio.create_task(observe(adapter, names, connector=lambda *args, **kwargs: Socket(frames)))
    await entered.wait()
    await asyncio.sleep(.01)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter._inventory_feed is None


@pytest.mark.asyncio
async def test_disconnect_during_aggregate_refresh_is_retryable_and_clears_all_proofs():
    from websockets.exceptions import ConnectionClosedError

    from aqla_agent.observations import TemporaryObservation
    adapter, names, frames = inputs()
    socket = Socket(frames)
    await observe(adapter, names, connector=lambda *args, **kwargs: socket)
    previous = adapter._inventory_feed
    async def disconnected(_):
        raise ConnectionClosedError(None, None)
    socket.send = disconnected
    with pytest.raises(TemporaryObservation) as error:
        await observe(adapter, names, connector=lambda *args, **kwargs: socket)
    assert error.value.stage == "inventory" and error.value.category == "transport"
    assert previous.closed and adapter._inventory_feed is None
    _, _, restored = inputs()
    restored[0]["data"]["clearinghouseStates"][0][1]["marginSummary"]["accountValue"] = "42"
    result = await observe(adapter, names, connector=lambda *args, **kwargs: Socket(restored))
    assert result[0][1]["marginSummary"]["accountValue"] == "42"
    await adapter._inventory_feed.close()


@pytest.mark.asyncio
async def test_inventory_memory_limit_discards_all_prior_proofs():
    adapter, names, frames = inputs()
    await observe(adapter, names, connector=lambda *args, **kwargs: Socket(frames))
    feed = adapter._inventory_feed
    feed.max_event_bytes = 1024
    feed._event({"channel": "openOrders", "data": {"user": adapter.account, "dex": "", "orders": [{"coin": "BTC", "extra": "x" * 2048}]}})
    assert isinstance(feed.error, AqlaError) and feed.states is None and not feed.orders
    await feed.close()


@pytest.mark.asyncio
async def test_account_change_never_borrows_existing_stream_coverage():
    adapter, names, frames = inputs()
    socket = Socket(frames)
    await observe(adapter, names, connector=lambda *args, **kwargs: socket)
    previous = adapter._inventory_feed
    adapter.account = "0x" + "2" * 40
    _, _, replacement = inputs()
    for frame in replacement:
        frame["data"]["user"] = adapter.account
    await observe(adapter, names, connector=lambda *args, **kwargs: Socket(replacement))
    assert previous.closed and adapter._inventory_feed is not previous
    await adapter._inventory_feed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[2,11,65])
async def test_shared_account_uses_complete_stream_before_rest_fanout_exhausts_rate_budget(setup,monkeypatch,count):
    from aqla_agent import inventory_stream
    settings,_,fake,adapter,_=setup
    settings.shared_account=True
    market=await adapter.market('BTC')
    names=['']+['dex'+str(i) for i in range(count-1)]
    states={dex:{'time':now_ms(),'assetPositions':[],'withdrawable':'100','marginSummary':{'accountValue':'100'}} for dex in names}
    frames=[{'channel':'allDexsClearinghouseState','data':{'user':adapter.account,'clearinghouseStates':list(map(list,states.items()))}}]
    frames += [{'channel':'openOrders','data':{'user':adapter.account,'dex':dex,'orders':[]}} for dex in names]
    socket=Socket(frames)
    reads=[]
    async def info(kind,**params):
        reads.append(kind)
        return {'userAbstraction':'disabled','perpDexs':[None]+[{'name':dex} for dex in names[1:]],
                'allPerpMetas':[{'universe':[{'name':'BTC'}] if not dex else []} for dex in names]}[kind]
    monkeypatch.setattr(adapter,'info',info)
    async def stream(adapter,names):
        return await observe(adapter,names,connector=lambda *args,**kwargs:socket)
    monkeypatch.setattr(inventory_stream,'observe',stream)
    account=await adapter.account_snapshot(market)
    assert account.equity==100 and account.positions=={} and len(socket.sent)==count+1
    assert sorted(reads)==['allPerpMetas','perpDexs','userAbstraction']
    assert not fake.actions


@pytest.mark.asyncio
async def test_268_dex_snapshot_requires_every_order_scope_and_only_reads_nonempty_details():
    adapter, names, frames = inputs()
    frames[2]["data"]["orders"] = [{"coin": "dex0:BTC"}]
    reads = []
    async def info(kind, **fields):
        reads.append((kind, fields))
        return [{"coin": "dex0:BTC", "reduceOnly": False}]
    adapter.info = info
    socket = Socket(frames)
    result = await observe(adapter, names, connector=lambda *args, **kwargs: socket)
    assert len(result) == 268 and len(socket.sent) == 269
    assert reads == [("frontendOpenOrders", {"user": adapter.account, "dex": "dex0"})]
    assert result[1][2][0]["reduceOnly"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_orders", "wrong_user", "stale", "duplicate_dex", "rejected"])
async def test_unproved_aggregate_snapshot_cannot_admit_entry(failure):
    adapter, names, frames = inputs()
    if failure == "missing_orders":
        frames.pop()
    elif failure == "wrong_user":
        frames[0]["data"]["user"] = "0x" + "2" * 40
    elif failure == "stale":
        frames[0]["data"]["clearinghouseStates"][0][1]["time"] -= 11000
    elif failure == "duplicate_dex":
        frames[0]["data"]["clearinghouseStates"].append(frames[0]["data"]["clearinghouseStates"][0])
    else:
        frames.insert(0, {"channel": "error"})
    with pytest.raises(AqlaError):
        await observe(adapter, names, connector=lambda *args, **kwargs: Socket(frames))


@pytest.mark.asyncio
@pytest.mark.parametrize("first_clock", ["stale", "unclocked"])
async def test_followup_aggregate_within_same_subscription_recovers(first_clock):
    import copy
    adapter, names, frames = inputs()
    followup = copy.deepcopy(frames[0])
    if first_clock == "stale":
        frames[0]["data"]["clearinghouseStates"][0][1]["time"] -= 11000
    else:
        frames[0]["data"]["clearinghouseStates"][0][1].pop("time")
    frames.append(followup)
    class NoDuplicateSubscription(Socket):
        async def send(self, text):
            assert json.loads(text) not in self.sent, "Real sockets must not be subscribed twice"
            await super().send(text)
    socket = NoDuplicateSubscription(frames)
    result = await observe(adapter, names, connector=lambda *args, **kwargs: socket)
    assert len(result) == len(names) and len(socket.sent) == len(names) + 1
    assert result[0][1]["time"] == followup["data"]["clearinghouseStates"][0][1]["time"]


@pytest.mark.asyncio
async def test_nonempty_rest_details_recheck_exchange_age_and_consume_fresh_aggregate(monkeypatch):
    import copy

    from aqla_agent import inventory_stream
    adapter, names, frames = inputs()
    stamp = now_ms()
    current = [stamp]
    for _, raw in frames[0]["data"]["clearinghouseStates"]:
        raw["time"] = stamp - 9000
    followup = copy.deepcopy(frames[0])
    for _, raw in followup["data"]["clearinghouseStates"]:
        raw["time"] = stamp + 2001
    frames.append(followup)
    frames[1]["data"]["orders"] = [{"coin": "BTC"}]
    calls = 0
    async def info(*args, **kwargs):
        nonlocal calls
        calls += 1
        current[0] = stamp + 2001
        return [{"coin": "BTC", "reduceOnly": False}]
    adapter.info = info
    monkeypatch.setattr(inventory_stream, "now_ms", lambda: current[0])
    result = await observe(adapter, names, connector=lambda *args, **kwargs: Socket(frames))
    assert calls == 2 and result[0][1]["time"] == stamp + 2001
