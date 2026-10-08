"""Fresh, complete large-DEX inventory without hundreds of weighted REST calls."""
import asyncio
import json

from websockets.asyncio.client import connect

from aqla_hyperliquid_mcp.models import AqlaError, now_ms


async def observe(adapter, names, *, connector=connect):
    states, orders = None, {}
    expected = set(names)
    if not names or len(names) > 512 or len(expected) != len(names):
        raise AqlaError("Large DEX inventory is incomplete or exceeds the subscription budget")
    try:
        async with asyncio.timeout(9):
            async with connector(adapter.settings.base_url.replace("https://", "wss://") + "/ws", max_size=8_000_000, open_timeout=5) as socket:
                specs = [{"type": "allDexsClearinghouseState", "user": adapter.account}]
                specs += [{"type": "openOrders", "user": adapter.account, "dex": dex} for dex in names]
                for spec in specs:
                    await socket.send(json.dumps({"method": "subscribe", "subscription": spec}))
                while states is None or set(orders) != expected:
                    frame = json.loads(await socket.recv())
                    channel, data = frame.get("channel"), frame.get("data", {})
                    if channel == "error":
                        raise AqlaError("Large DEX snapshot subscription was rejected")
                    if channel not in ("allDexsClearinghouseState", "openOrders"):
                        continue
                    if not isinstance(data, dict) or str(data.get("user", "")).lower() != adapter.account.lower():
                        raise AqlaError("DEX snapshot belongs to another account")
                    if channel == "allDexsClearinghouseState":
                        rows = data.get("clearinghouseStates")
                        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], dict) for row in rows):
                            raise AqlaError("DEX position snapshot is malformed")
                        states = dict(rows)
                        if len(states) != len(rows) or set(states) != expected:
                            raise AqlaError("DEX position snapshot is incomplete")
                    else:
                        dex = data.get("dex")
                        if dex not in expected or not isinstance(data.get("orders"), list):
                            raise AqlaError("DEX order snapshot is incomplete")
                        orders[dex] = data["orders"]
                if any(type(row.get("time")) is not int or not -2000 <= now_ms() - row["time"] <= 10000 for row in states.values()):
                    raise AqlaError("DEX position snapshot is stale or unclocked")
                # Native openOrders may omit reduceOnly. Empty lists prove absence;
                # nonempty DEXs get the full REST contract before classification.
                limiter = asyncio.Semaphore(3)
                async def details(dex):
                    if orders[dex]:
                        async with limiter:
                            orders[dex] = await adapter.info("frontendOpenOrders", user=adapter.account, **({"dex": dex} if dex else {}))
                    return dex, states[dex], orders[dex]
                return await asyncio.gather(*(details(dex) for dex in names))
    except AqlaError:
        raise
    except Exception:
        raise AqlaError("Complete large-DEX account snapshot is unavailable; no entry permitted") from None
