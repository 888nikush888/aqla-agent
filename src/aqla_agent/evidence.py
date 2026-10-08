"""Internal read contracts; public venue information payloads remain unchanged."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import NotRequired, TypedDict

from aqla_hyperliquid_mcp.models import Market, digest


class FillObservation(TypedDict):
    oid: int | str
    coin: str
    time: int
    sz: str
    px: str
    tid: NotRequired[int | str]
    fee: NotRequired[str | None]
    feeToken: NotRequired[str | None]


class FundingCoverage(TypedDict):
    complete: bool
    start: NotRequired[int]
    end: NotRequired[int]
    fill_fingerprint: NotRequired[str]
    payments_fingerprint: NotRequired[str]
    reason: NotRequired[str]


def verified_coverage_matches(trade, *, start: int | None = None, end: int | None = None) -> bool:
    """Pure positive-proof check shared by refresh and interrupted job handling."""
    old = trade.get("funding_coverage", {})
    return bool(old.get("complete") and (start is None or old.get("start") == start)
                and (end is None or old.get("end") == end)
                and old.get("fill_fingerprint") == digest(trade.get("fill_events", {}))
                and old.get("payments_fingerprint") == digest(trade.get("funding_events", {})))


def order_observations(adapter) -> AbstractContextManager[None]:
    """Share immutable terminal evidence only inside this explicit account pass."""
    scope = getattr(adapter, "order_observations", None)
    return scope() if scope is not None else nullcontext()


async def market_fills(adapter, market: Market, start: int, end: int) -> list[FillObservation]:
    """Select an immutable native market through an internal reader contract."""
    reader = getattr(adapter, "fill_history", None)
    if reader is not None:
        return await reader(market, start, end)
    # Existing Hyperliquid-compatible readers/test doubles implement only info.
    # No native-market argument is ever added to the public Hyperliquid request.
    return await adapter.info("userFillsByTime", user=adapter.account, startTime=start, endTime=end)
