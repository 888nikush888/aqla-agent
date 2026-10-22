"""Lighter snapshots and nonce-linked price-level changes, never stateless deltas."""
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, now_ms

from .entry import BookSnapshot


class LighterBook:
    def __init__(self, market):
        self.market, self.nonce, self.value = market, None, None
        self.bids, self.asks = {}, {}

    def apply(self, frame):
        if frame.get("channel") not in (f"order_book:{self.market.asset_id}", f"order_book/{self.market.asset_id}"):
            return None
        try:
            row = frame["order_book"]
            nonce, begin = row["nonce"], row.get("begin_nonce")
            initial = frame.get("type") == "subscribed/order_book"
            if (type(nonce) is not int or nonce < 0 or row.get("code", 0) != 0
                    or not initial and (self.nonce is None or type(begin) is not int or begin != self.nonce or nonce <= self.nonce)
                    or frame.get("type") not in ("subscribed/order_book", "update/order_book")):
                raise ValueError("Missing snapshot or discontinuous nonce")
            sides = [{}, {}] if initial else [dict(self.bids), dict(self.asks)]
            for side, updates in zip(sides, (row["bids"], row["asks"]), strict=True):
                if not isinstance(updates, list):
                    raise ValueError("Invalid levels")
                seen = set()
                for level in updates:
                    price, size = Decimal(level["price"]), Decimal(level["size"])
                    if not price.is_finite() or price <= 0 or not size.is_finite() or size < 0 or price in seen:
                        raise ValueError("Invalid price level")
                    seen.add(price)
                    if size:
                        side[price] = size
                    else:
                        side.pop(price, None)
                if not side or len(side) > 10000:
                    raise ValueError("Empty or unbounded book")
            stamp = frame["timestamp"]
            if type(stamp) is not int:
                raise ValueError("Missing native time")
            if stamp < 10**12:
                stamp *= 1000
            bids, asks = sorted(sides[0].items(), reverse=True), sorted(sides[1].items())
            bid, ask = bids[0][0], asks[0][0]
            value = BookSnapshot(market=self.market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                exchange_time=stamp, started_at=stamp, observed_at=now_ms(), bids=bids[:1000], asks=asks[:1000]).fresh()
            self.bids, self.asks, self.nonce, self.value = *sides, nonce, value
            return value
        except (AqlaError, ValueError, TypeError, KeyError, ArithmeticError):
            self.bids, self.asks, self.nonce, self.value = {}, {}, None, None
            raise AqlaError("Lighter order book requires a fresh complete snapshot after invalid or missing changes") from None
