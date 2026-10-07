"""Bounded account-payment history, never market-rate estimates or order writes."""
from __future__ import annotations

from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, digest


async def bounded_history(fetch, start, end, *, limit, window_ms=7 * 86_400_000, budget=64):
    if start < 0 or end < start:
        raise AqlaError("Invalid funding history window")
    result, calls = {}, 0

    async def window(low, high):
        nonlocal calls
        calls += 1
        if calls > budget:
            raise AqlaError("Funding history exceeded the bounded request budget")
        rows = await fetch(low, high)
        if not isinstance(rows, list):
            raise AqlaError("Funding history returned no complete payment list")
        if len(rows) >= limit:
            if low == high:
                raise AqlaError("Funding history saturated within one millisecond")
            middle = (low + high) // 2
            await window(low, middle)
            await window(middle + 1, high)
        else:
            for row in rows:
                result[digest(row)] = row
    for low in range(start, end + 1, window_ms):
        await window(low, min(end, low + window_ms - 1))
    return list(result.values())


def payment(identity, timestamp, amount, currency, market, *, size=None, raw=None):
    if not identity or isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise AqlaError("Funding payment lacks a stable identity or timestamp")
    amount = Decimal(str(amount))
    quantity = Decimal(str(size)) if size is not None else None
    if not amount.is_finite() or quantity is not None and not quantity.is_finite():
        raise AqlaError("Funding payment is not finite")
    return {"identity": str(identity), "time": timestamp, "amount": str(amount), "currency": currency,
            "market": market, "signed_size": str(quantity) if quantity is not None else None, "raw": raw}


async def hyperliquid_funding(exchange, market, start, end):
    async def fetch(low, high):
        return await exchange.info("userFunding", user=exchange.account, startTime=low, endTime=high)
    rows = await bounded_history(fetch, start, end, limit=500)
    result = []
    for row in rows:
        delta = row["delta"]
        if delta["coin"] != market.symbol:
            continue
        if delta.get("type") != "funding" or not start <= row["time"] <= end:
            raise AqlaError("Funding payment does not match the requested contract/window")
        result.append(payment(digest([row["hash"], row["time"], delta["coin"]]), row["time"], delta["usdc"],
                              market.quote_token, market.symbol, size=delta["szi"], raw=row))
    return result


async def aster_funding(exchange, market, start, end):
    async def fetch(low, high):
        return await exchange.read("income", {"symbol": market.native_symbol, "incomeType": "FUNDING_FEE",
                                              "startTime": str(low), "endTime": str(high), "limit": "1000"}, private=True)
    rows = await bounded_history(fetch, start, end, limit=1000)
    result = []
    for row in rows:
        if row.get("incomeType") != "FUNDING_FEE" or row.get("symbol") != market.native_symbol or not start <= row["time"] <= end:
            raise AqlaError("Aster income does not match the requested payment window")
        result.append(payment(digest([row["incomeType"], row["tranId"], row["asset"], row["symbol"]]),
                              row["time"], row["income"], row["asset"], market.symbol, raw=row))
    return result


async def lighter_funding(exchange, market, start, end):
    result, cursor, seen = {}, None, set()
    for _ in range(64):
        raw = await exchange.read("positionFunding", {"account_index": exchange.account, "limit": 100,
                                  "market_ids": market.asset_id, "start_timestamp": start // 1000,
                                  "end_timestamp": (end + 999) // 1000, **({"cursor": cursor} if cursor else {})}, private=True)
        if not isinstance(raw.get("position_fundings"), list):
            raise AqlaError("Lighter returned no complete account funding list")
        for row in raw["position_fundings"]:
            timestamp = row["timestamp"] * 1000 if row["timestamp"] < 10**12 else row["timestamp"]
            if row["market_id"] != market.asset_id or row["position_side"] not in ("long", "short"):
                raise AqlaError("Lighter returned funding from another market")
            # Timestamp filters are second-resolution, while fills are stored in ms.
            if not start <= timestamp <= end:
                continue
            size = abs(Decimal(row["position_size"])) * (1 if row["position_side"] == "long" else -1)
            fact = payment(digest([row["market_id"], row["funding_id"]]), timestamp, row["change"], "USDC", market.symbol, size=size, raw=row)
            old = result.get(fact["identity"])
            if old and old != fact:
                raise AqlaError("Lighter funding identity changed between pages")
            result[fact["identity"]] = fact
        cursor = raw.get("next_cursor")
        if not cursor:
            return list(result.values())
        if cursor in seen:
            raise AqlaError("Lighter funding cursor repeated; coverage is incomplete")
        seen.add(cursor)
    raise AqlaError("Lighter funding exceeded the bounded cursor budget")
