from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext

from .models import AccountSnapshot, AqlaError, Interpretation, Market, Policy, TradePlan, digest, now_ms


def quantity_grid(value: Decimal, market: Market):
    return value.quantize(Decimal(1).scaleb(-market.size_decimals), rounding=ROUND_DOWN)


def price_grid(value: Decimal, market: Market, up=False):
    decimals = (6 if market.product == "perp" else 8) - market.size_decimals
    exponent = max(-decimals, value.adjusted() - 4) if value < 100000 else 0
    return value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_UP if up else ROUND_DOWN)


def split_targets(quantity: Decimal, count: int, policy: Policy, market: Market):
    if policy.take_profit_allocation == "custom":
        weights = policy.take_profit_weights
        if len(weights) != count:
            raise AqlaError("TP weight count differs from signal target count")
    elif policy.take_profit_allocation == "halving":
        weights = [Decimal(100) / (Decimal(2) ** (i + 1)) for i in range(count - 1)]
        weights += [100 - sum(weights)]
    else:
        weights = [Decimal(100) / count] * count
    quantities = [quantity_grid(quantity * weight / 100, market) for weight in weights[:-1]]
    quantities.append(quantity - sum(quantities))
    if any(size <= 0 for size in quantities):
        raise AqlaError("A TP leg rounds to zero; choose fewer targets or different weights")
    return quantities


def quoted_numbers(quote):
    # General decimal notation only; no channel-specific syntax or field-role regex.
    normalized = quote.replace("\u00a0", " ").replace("\u202f", " ")
    normalized = re.sub(r"(?<=\d) (?=\d{3}(?:\b|[.,]))", "", normalized)
    for token in re.findall(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w.])", normalized):
        if "." in token and "," in token:
            token = token.replace(",", "") if token.rfind(".") > token.rfind(",") else token.replace(".", "").replace(",", ".")
        elif "," in token:
            parts = token.split(",")
            if parts[0] != "0" and all(len(part) == 3 for part in parts[1:]):
                yield Decimal("".join(parts))
            token = token.replace(",", ".")
        elif "." in token and token.split(".")[0] != "0" and all(len(part) == 3 for part in token.split(".")[1:]):
            yield Decimal(token.replace(".", ""))
        try:
            yield Decimal(token)
        except Exception:
            continue


def verify_evidence(signal: Interpretation, events: dict):
    texts = {value["source_id"]: value["source"]["text"] for value in events.values()}
    if any(ref not in texts for ref in signal.source_refs):
        raise AqlaError("Unknown source reference")
    fields = {}
    for proof in signal.evidence:
        if proof.source_id not in signal.source_refs or proof.quote not in texts[proof.source_id]:
            raise AqlaError("Evidence does not occur in its original source")
        fields.setdefault(proof.field, []).append(proof.quote)
    if signal.kind == "new_signal":
        for field in ("symbol", "side"):
            if not fields.get(field):
                raise AqlaError(f"Missing contextual source evidence for {field}")
    numeric = {"stop_loss": signal.stop_loss}
    numeric.update({f"take_profits.{i}": value for i, value in enumerate(signal.take_profits)})
    numeric.update({f"entry_prices.{i}": value for i, value in enumerate(signal.entry_prices)})
    for field, value in numeric.items():
        if value is not None and not any(value in set(quoted_numbers(quote)) for quote in fields.get(field, [])):
            raise AqlaError(f"Price not supported by source evidence: {field}")


def make_plan(signal: Interpretation, market: Market, account: AccountSnapshot, policy: Policy,
              revision: int, source_time: int, binding: tuple[str, str], reserved_risk=Decimal(0), stamp=None):
    stamp = now_ms() if stamp is None else stamp
    with localcontext() as context:
        context.prec = 50
        if signal.kind != "new_signal" or not signal.symbol or not signal.side or not signal.stop_loss or not signal.take_profits:
            raise AqlaError("New trade needs a market, direction, SL and at least one TP")
        if market.product != policy.product:
            raise AqlaError("Source product differs from configured product")
        if stamp >= source_time + policy.max_signal_age_seconds * 1000:
            raise AqlaError("Signal deadline expired; it is never renewed")
        if stamp - account.observed_at > 10000:
            raise AqlaError("Account observation is stale")
        if policy.leverage > market.max_leverage:
            raise AqlaError("Configured leverage exceeds this market's limit")
        if market.product == "spot" and signal.side == "short":
            raise AqlaError("Spot short signals are unsupported")
        long = signal.side == "long"
        kind = "limit" if signal.entry in ("limit", "range") else "market"
        if signal.entry == "unspecified" and policy.entry_mode != "source_or_market":
            raise AqlaError("Entry missing and market fallback is not configured")
        if kind == "limit":
            if len(signal.entry_prices) != (2 if signal.entry == "range" else 1):
                raise AqlaError("Entry prices do not match entry type")
            low, high = signal.entry_prices[0], signal.entry_prices[-1]
            if policy.entry_range_choice == "midpoint":
                entry = (low + high) / 2
            else:
                entry = (high if long else low) if policy.entry_range_choice == "near" else (low if long else high)
            entry = price_grid(entry, market, up=not long)
            limit = entry
        else:
            entry = market.mid
            limit = price_grid(entry * (1 + (1 if long else -1) * policy.max_slippage_percent / 100), market, up=long)
        stop = price_grid(signal.stop_loss, market, up=not long)
        targets = [price_grid(value, market, up=not long) for value in signal.take_profits]
        ordered = sorted(set(targets), reverse=not long)
        if targets != ordered or not (stop < min(entry, limit) and all(t > max(entry, limit) for t in targets) if long else stop > max(entry, limit) and all(t < min(entry, limit) for t in targets)):
            raise AqlaError("SL, entry and TPs have invalid direction/ordering after rounding")
        budget = min(account.equity * policy.risk_percent / 100,
                     account.equity * policy.max_total_risk_percent / 100 - reserved_risk)
        cost = limit * policy.cost_reserve_bps / 10000
        distance = abs(limit - stop) + cost
        maximum = min(budget / distance, policy.max_notional_usdc / limit,
                      account.available_margin * policy.leverage / limit)
        if policy.sizing_mode == "fixed_notional":
            maximum = min(maximum, policy.fixed_notional_usdc / limit)
        quantity = quantity_grid(maximum, market)
        if quantity <= 0 or quantity * limit < market.min_notional:
            raise AqlaError("Insufficient risk/margin budget or exchange minimum notional")
        tp_sizes = split_targets(quantity, len(targets), policy, market)
        if any(size * price < market.min_notional for size, price in zip(tp_sizes, targets, strict=True)):
            raise AqlaError("A TP leg is below the exchange notional minimum")
        trade_id = digest([*binding, sorted(signal.source_refs)])[:32]
        values = dict(trade_id=trade_id, source_refs=signal.source_refs, policy_revision=revision, policy=policy,
                      network=binding[0], account=binding[1], market=market, side=signal.side, entry_kind=kind,
                      entry_price=entry, entry_limit=limit, stop_loss=stop, take_profits=targets, quantity=quantity,
                      tp_quantities=tp_sizes, risk_usdc=quantity * distance,
                      deadline_at=source_time + policy.max_signal_age_seconds * 1000, created_at=stamp)
        return TradePlan(plan_id=digest(values), **values)
