"""Shared native decimal grids and unchanged slippage bounds; no I/O."""
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .models import AqlaError, Market


def quantity_grid(value: Decimal, market: Market):
    if market.quantity_step is not None:
        return (value / market.quantity_step).to_integral_value(rounding=ROUND_DOWN) * market.quantity_step
    return value.quantize(Decimal(1).scaleb(-market.size_decimals), rounding=ROUND_DOWN)


def price_grid(value: Decimal, market: Market, up=False):
    if market.price_tick is not None:
        return (value / market.price_tick).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * market.price_tick
    decimals = (6 if market.product == "perp" else 8) - market.size_decimals
    exponent = max(-decimals, value.adjusted() - 4) if value < 100000 else 0
    return value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_UP if up else ROUND_DOWN)


def slippage_price(reference: Decimal, percent: Decimal, market: Market, buy: bool):
    bound = reference * (1 + (1 if buy else -1) * percent / 100)
    result = price_grid(bound, market, up=not buy)
    if result <= 0 or (result > bound if buy else result < bound):
        raise AqlaError("No positive price grid point satisfies the slippage bound")
    return result
