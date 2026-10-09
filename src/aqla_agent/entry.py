"""Fresh executable prices, source-bound waiting gates and pre/post-fill checks."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, Positive, now_ms
from aqla_hyperliquid_mcp.rules import price_grid, slippage_price


class PriceSnapshot(Model):
    market: str
    bid: Positive
    ask: Positive
    mid: Positive
    stop_reference: Positive
    stop_reference_kind: Literal["mark", "contract"]
    exchange_time: int = Field(ge=0)
    started_at: int = Field(ge=0)
    observed_at: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_book(self):
        if self.bid > self.ask or not self.bid <= self.mid <= self.ask:
            raise ValueError("Inconsistent executable book snapshot")
        return self

    def fresh(self):
        stamp = now_ms()
        if any(not 0 <= stamp - value <= 2000 for value in (self.exchange_time, self.started_at, self.observed_at)):
            raise AqlaError("Entry price observation is stale or has an invalid timestamp")
        return self


def gate_state(plan, quote):
    quote.fresh()
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


def validate_entry_prices(plan, quote, *, stop=None, targets=None):
    quote.fresh()
    if quote.market != plan.market.symbol:
        raise AqlaError("Entry price observation belongs to another market")
    stop = Decimal(str(stop)) if stop is not None else plan.stop_loss
    targets = [Decimal(str(value)) for value in targets] if targets is not None else plan.take_profits
    limit = execution_limit(plan)
    price = quote.ask if plan.side == "long" else quote.bid
    maker = plan.entry_execution == "maker_only"
    if maker and (limit >= quote.ask if plan.side == "long" else limit <= quote.bid):
        raise AqlaError("Saved maker entry price would cross the executable book")
    # Resting ordinary limit orders may wait for a retracement; evaluate immediate
    # fill risk only when marketable. Deferred IOC entries must be executable now.
    marketable = price <= limit if plan.side == "long" else price >= limit
    if plan.entry_timing == "deferred":
        if gate_state(plan, quote) != "reached":
            raise AqlaError("Deferred entry is not currently at its source entry")
        low = plan.entry_price * (1 - plan.policy.max_slippage_percent / 100)
        high = plan.entry_price * (1 + plan.policy.max_slippage_percent / 100)
        if not low <= price <= high:
            raise AqlaError("Executable entry price exceeds the source slippage tolerance")
    elif plan.entry_kind == "market" and not marketable:
        if not maker:
            raise AqlaError("Executable entry price exceeds the saved market limit")
    if not marketable and not maker:
        return {"marketable": False, "limit": str(limit)}
    if maker:
        price = limit
    safe = (stop < min(price, limit, quote.stop_reference) and all(value > max(price, limit) for value in targets)
            if plan.side == "long" else stop > max(price, limit, quote.stop_reference) and all(value < min(price, limit) for value in targets))
    if not safe:
        raise AqlaError("Live entry/SL/TP structure is invalid on the target exchange")
    worst_price = max(price, limit)
    if plan.quantity * worst_price > plan.policy.max_notional_usdc:
        raise AqlaError("Live execution would exceed the maximum position value")
    risk = plan.quantity * (abs(price - stop) + worst_price * plan.policy.cost_reserve_bps / 10000)
    if risk > plan.risk_usdc:
        raise AqlaError("Live execution would exceed the reserved SL risk")
    return {"marketable": marketable, "limit": str(limit), "expected_entry": str(price), "expected_risk": str(risk)}
