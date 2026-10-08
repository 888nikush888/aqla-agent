"""Evidenced source percentages; source leverage is never the exchange-capped leverage."""
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError


def reference_entry(signal, policy):
    if signal.entry not in ("limit", "range") or not signal.entry_prices:
        return None
    low, high = signal.entry_prices[0], signal.entry_prices[-1]
    if policy.entry_range_choice == "midpoint":
        return (low + high) / 2
    long = signal.side == "long"
    return (high if long else low) if policy.entry_range_choice == "near" else (low if long else high)


def prices(reference, side, market, unit, values, leverage=None):
    from aqla_hyperliquid_mcp.rules import price_grid
    reference = Decimal(str(reference))
    if unit not in ("course_percent", "roi_percent"):
        raise AqlaError("Unknown source percentage interpretation")
    if unit == "roi_percent" and (leverage is None or Decimal(str(leverage)) < 1):
        raise AqlaError("ROI targets need the evidenced original signal leverage; profile/Backup leverage cannot replace it")
    percentages = [Decimal(str(value)) / (Decimal(str(leverage)) if unit == "roi_percent" else 1) for value in values]
    if percentages != sorted(set(percentages)) or any(value <= 0 or not value.is_finite() for value in percentages):
        raise AqlaError("Source percentage targets must be finite, positive, unique and ascending")
    long = side == "long"
    raw = [reference * (1 + (1 if long else -1) * percentage / 100) for percentage in percentages]
    targets = [price_grid(value, market, up=long) for value in raw if value > 0]
    if (len(targets) != len(values) or targets != sorted(set(targets), reverse=not long)
            or any(value <= 0 or (value <= reference if long else value >= reference) for value in targets)):
        raise AqlaError("Source percentage prices collapse or have invalid direction on the exchange tick grid")
    return targets
