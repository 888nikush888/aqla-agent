"""Bounded account-payment history, never market-rate estimates or order writes."""
from __future__ import annotations

from decimal import Decimal
from typing import TypedDict

from aqla_hyperliquid_mcp.models import AqlaError, digest


class FundingPayment(TypedDict):
    identity: str
    time: int
    amount: str
    currency: str
    market: str
    signed_size: str | None
    raw: dict[str, object] | None


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


def payment(identity, timestamp, amount, currency, market, *, size=None, raw=None) -> FundingPayment:
    if not identity or isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise AqlaError("Funding payment lacks a stable identity or timestamp")
    amount = Decimal(str(amount))
    quantity = Decimal(str(size)) if size is not None else None
    if not amount.is_finite() or quantity is not None and not quantity.is_finite():
        raise AqlaError("Funding payment is not finite")
    return {"identity": str(identity), "time": timestamp, "amount": str(amount), "currency": currency,
            "market": market, "signed_size": str(quantity) if quantity is not None else None, "raw": raw}


async def hyperliquid_funding(exchange, market, start, end) -> list[FundingPayment]:
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




