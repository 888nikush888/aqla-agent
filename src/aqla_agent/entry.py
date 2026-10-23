"""Fresh executable prices, source-bound waiting gates and pre/post-fill checks."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, Positive, now_ms
from aqla_hyperliquid_mcp.rules import price_grid, slippage_price

from .observations import TemporaryObservation


class BookSnapshot(Model):
    market: str
    bid: Positive
    ask: Positive
    mid: Positive
    exchange_time: int = Field(ge=0)
    started_at: int = Field(ge=0)
    observed_at: int = Field(ge=0)
    bids: list[tuple[Positive, Positive]] = Field(default_factory=list, max_length=1000)
    asks: list[tuple[Positive, Positive]] = Field(default_factory=list, max_length=1000)

    @model_serializer(mode="wrap")
    def compatible_depth(self, handler):
        result = handler(self)
        for key in ("bids", "asks"):
            if not getattr(self, key):
                result.pop(key, None)
        return result

    @model_validator(mode="after")
    def valid_book(self):
        if self.bid > self.ask or not self.bid <= self.mid <= self.ask:
            raise ValueError("Inconsistent executable book snapshot")
        for rows, reverse, best in ((self.bids, True, self.bid), (self.asks, False, self.ask)):
            if rows and (rows[0][0] != best or [p for p, _ in rows] != sorted({p for p, _ in rows}, reverse=reverse)):
                raise ValueError("Order book depth is unordered or differs from its best quote")
        return self

    def executable(self, *, buy, limit, quantity, as_of=None):
        self.fresh(as_of=as_of)
        rows = self.asks if buy else self.bids
        if not rows:
            raise AqlaError("Executable order book depth is unavailable")
        filled, value = Decimal(0), Decimal(0)
        for price, available in rows:
            if price > limit if buy else price < limit:
                break
            take = min(available, quantity - filled)
            filled, value = filled + take, value + take * price
            if filled == quantity:
                break
        return filled, value / filled if filled else None

    def fresh(self, *, as_of=None):
        stamp = now_ms() if as_of is None else as_of
        if any(not 0 <= stamp - value <= 2000 for value in (self.exchange_time, self.started_at, self.observed_at)):
            raise TemporaryObservation("price_stale", "price")
        return self


class PriceSnapshot(BookSnapshot):
    stop_reference: Positive
    stop_reference_kind: Literal["mark", "contract"]


def gate_state(plan, quote, *, as_of=None):
    quote.fresh(as_of=as_of)
    origin = Decimal(plan.entry_gate["origin_mid"])
    low = plan.entry_price * (1 - plan.policy.max_slippage_percent / 100)
    high = plan.entry_price * (1 + plan.policy.max_slippage_percent / 100)
    if origin > plan.entry_price:
        if quote.mid > plan.entry_price:
            return "waiting"
        if quote.mid < low:
            return "missed"
    elif origin < plan.entry_price:
        if quote.mid < plan.entry_price:
            return "waiting"
        if quote.mid > high:
            return "missed"
    elif not low <= quote.mid <= high:
        return "missed"
    return "reached"


def execution_limit(plan):
    return slippage_price(plan.entry_price, plan.policy.max_slippage_percent, plan.market, buy=plan.side == "long") if plan.entry_timing == "deferred" and plan.entry_execution == "standard" else plan.entry_limit


def passive_entry_price(market, side, quote) -> Decimal:
    quote.fresh()
    if quote.market != market.symbol:
        raise AqlaError("Maker entry quote belongs to another market")
    price = price_grid(quote.bid if side == "long" else quote.ask, market, up=side == "short")
    if price <= 0 or (price >= quote.ask if side == "long" else price <= quote.bid):
        raise AqlaError("Fresh maker entry price would cross the executable book")
    return price


def validate_entry_execution(plan, quote, price, limit, maker, as_of):
    if maker and (limit >= quote.ask if plan.side == "long" else limit <= quote.bid):
        raise AqlaError("Saved maker entry price would cross the executable book")
    # Resting ordinary limit orders may wait for a retracement; evaluate immediate
    # fill risk only when marketable. Deferred IOC entries must be executable now.
    marketable = price <= limit if plan.side == "long" else price >= limit
    if plan.entry_timing == "deferred":
        if gate_state(plan, quote, as_of=as_of) != "reached":
            raise AqlaError("Deferred entry is not currently at its source entry")
        low = plan.entry_price * (1 - plan.policy.max_slippage_percent / 100)
        high = plan.entry_price * (1 + plan.policy.max_slippage_percent / 100)
        if not low <= price <= high:
            raise AqlaError("Executable entry price exceeds the source slippage tolerance")
    elif plan.entry_kind == "market" and not marketable and not maker:
        raise AqlaError("Executable entry price exceeds the saved market limit")
    return marketable


def valid_entry_structure(plan, quote, price, limit, stop, targets):
    return (stop < min(price, limit, quote.stop_reference) and all(value > max(price, limit) for value in targets)
            if plan.side == "long" else stop > max(price, limit, quote.stop_reference) and all(value < min(price, limit) for value in targets))


def validate_entry_risk(plan, price, limit, stop):
    worst_price = max(price, limit)
    if plan.quantity * worst_price > plan.policy.max_notional_usdc:
        raise AqlaError("Live execution would exceed the maximum position value")
    risk = plan.quantity * (abs(price - stop) + worst_price * plan.policy.cost_reserve_bps / 10000)
    if risk > plan.risk_usdc:
        raise AqlaError("Live execution would exceed the reserved SL risk")
    return risk


def validate_entry_prices(plan, quote, *, stop=None, targets=None, as_of=None):
    if plan.entry_legs:
        return {"legs": [validate_entry_prices(leg_view(plan, {"index": leg.index, "quantity": str(leg.quantity)}),
                      quote, stop=stop, targets=targets, as_of=as_of) for leg in plan.entry_legs if leg.timing != "deferred"]}
    quote.fresh(as_of=as_of)
    if quote.market != plan.market.symbol:
        raise AqlaError("Entry price observation belongs to another market")
    stop = Decimal(str(stop)) if stop is not None else plan.stop_loss
    targets = [Decimal(str(value)) for value in targets] if targets is not None else plan.take_profits
    limit = execution_limit(plan)
    price = quote.ask if plan.side == "long" else quote.bid
    maker = plan.entry_execution == "maker_only"
    marketable = validate_entry_execution(plan, quote, price, limit, maker, as_of)
    if not marketable and not maker:
        return {"marketable": False, "limit": str(limit)}
    if maker:
        price = limit
    safe = valid_entry_structure(plan, quote, price, limit, stop, targets)
    if not safe:
        raise AqlaError("Live entry/SL/TP structure is invalid on the target exchange")
    risk = validate_entry_risk(plan, price, limit, stop)
    return {"marketable": marketable, "limit": str(limit), "expected_entry": str(price), "expected_risk": str(risk)}


def leg_view(plan, row):
    leg = next((leg for leg in plan.entry_legs if leg.index == row["index"]), None)
    return plan.model_copy(update={"entry_legs": [], "entry_kind": row.get("entry_kind", leg.kind if leg else plan.entry_kind),
        "entry_price": Decimal(row.get("price", str(leg.price if leg else plan.entry_price))),
        "entry_limit": Decimal(row.get("limit", str(leg.limit if leg else plan.entry_limit))),
        "quantity": Decimal(row["quantity"]), "entry_timing": row.get("timing", leg.timing if leg else plan.entry_timing),
        "entry_gate": row.get("entry_gate", plan.entry_gate)})
