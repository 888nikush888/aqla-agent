"""Internal read contracts; public venue information payloads remain unchanged."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import NotRequired, TypedDict

from aqla_hyperliquid_mcp.models import Market


class FillObservation(TypedDict):
    oid: int | str
    coin: str
    time: int
    sz: str
    px: str
    tid: NotRequired[int | str]
    fee: NotRequired[str | None]
    feeToken: NotRequired[str | None]


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
