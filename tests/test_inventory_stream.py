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
