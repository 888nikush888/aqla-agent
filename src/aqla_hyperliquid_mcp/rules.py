from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

from .models import (
    EVIDENCE_FIELDS,
    AccountSnapshot,
    AqlaError,
    BackupValues,
    Interpretation,
    Market,
    Policy,
    TradePlan,
    digest,
    margin_leverage,
    now_ms,
)


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


def selected_targets(prices, policy):
    maximum = 5 if policy.take_profit_allocation == "adaptive" else 1 if policy.take_profit_allocation == "first_target" else 20
    return list(prices[:maximum])


def split_targets(quantity: Decimal, count: int, policy: Policy, market: Market):
    if not 1 <= count <= 20:
        raise AqlaError("TP distribution requires 1–20 targets")
    if policy.take_profit_allocation == "adaptive":
        if count > 5:
            raise AqlaError("Adaptive distribution uses at most five targets")
        weights = [Decimal(value) for value in ("50", "25", "12.5", "7.5")[:count - 1]]
        weights += [100 - sum(weights)]
    elif policy.take_profit_allocation == "first_target":
        if count != 1:
            raise AqlaError("First-target distribution uses exactly one target")
        weights = [Decimal(100)]
    elif policy.take_profit_allocation == "custom":
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
    # A sentence-ending period is punctuation; decimal/thousands dots stay untouched.
    normalized = re.sub(r"(?<=\d)\.(?=$|[\s\"'»”);,!?\]])", " ", normalized)
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


def quoted_leverage_numbers(quote):
    # General multiplier notation, not channel patterns. Keep price evidence parsing unchanged.
    numeric = r"\d+(?:[.,]\d+)*"
    quote = re.sub(rf"(?<![\w.])[xх×]\s*({numeric})(?!\w)", r" \1 ", quote, flags=re.I)
    quote = re.sub(rf"(?<![\w.])({numeric})\s*(?:[xх×]|fach)(?!\w)", r" \1 ", quote, flags=re.I)
    return quoted_numbers(quote)


def effective_leverage(signal: Interpretation, market: Market, policy: Policy, backup=None):
    if policy.leverage_mode == "signal":
        if signal.leverage is None:
            if backup is None:
                raise AqlaError("Signal leverage is missing; signal mode never guesses a leverage")
            return min(backup.leverage, market.max_leverage)
        return min(int(Decimal(signal.leverage).to_integral_value(rounding=ROUND_FLOOR)), market.max_leverage)
    if policy.leverage > market.max_leverage:
        raise AqlaError("Configured leverage exceeds this market's limit")
    return policy.leverage


def backup_prices(reference, side, market, stop_percent=None, percentages=(), *,
                  stop_unit="course_percent", take_profit_unit="course_percent", leverage=None):
    """Nominal ROE uses the original position margin; grid away from entry."""
    uses_roe = stop_percent is not None and stop_unit == "roe_percent" or percentages and take_profit_unit == "roe_percent"
    if uses_roe:
        try:
            effective = Decimal(leverage) if leverage is not None and not isinstance(leverage, bool) else Decimal(0)
        except (InvalidOperation, ValueError, TypeError):
            effective = Decimal(0)
        if market.product != "perp" or not effective.is_finite() or effective < 1 or effective != effective.to_integral_value():
            raise AqlaError("Backup ROE requires valid effective perpetual leverage")
        if stop_percent is not None and stop_unit == "roe_percent":
            stop_percent /= effective
        if take_profit_unit == "roe_percent":
            percentages = [value / effective for value in percentages]
    long = side == "long"
    stop = price_grid(reference * (1 + (-1 if long else 1) * stop_percent / 100), market, up=not long) if stop_percent is not None else None
    targets = [price_grid(reference * (1 + (1 if long else -1) * value / 100), market, up=long) for value in percentages]
    if (stop is not None and (stop <= 0 or (stop >= reference if long else stop <= reference))
            or any(value <= 0 or (value <= reference if long else value >= reference) for value in targets)
            or targets != sorted(set(targets), reverse=not long)):
        raise AqlaError("Backup prices collapse or have invalid direction after tick rounding")
    return stop, targets


def latest_sources(events: dict, refs=None):
    from aqla_agent.source_queue import canonical_sources
    return canonical_sources(events, refs)


def slippage_price(reference: Decimal, percent: Decimal, market: Market, buy: bool):
    bound = reference * (1 + (1 if buy else -1) * percent / 100)
    result = price_grid(bound, market, up=not buy)
    if result <= 0 or (result > bound if buy else result < bound):
        raise AqlaError("No positive price grid point satisfies the slippage bound")
    return result


def verify_evidence(signal: Interpretation, events: dict):
    texts = {ref: row["source"]["text"] for ref, row in latest_sources(events, signal.source_refs).items()}
    if any(ref not in texts for ref in signal.source_refs):
        raise AqlaError("Unknown source reference")
    fields = {}
    for proof in signal.evidence:
        if proof.source_id not in signal.source_refs or proof.quote not in texts[proof.source_id]:
            raise AqlaError("Evidence does not occur in its original source")
        fields.setdefault(proof.field, []).append(proof.quote)
    errors = []
    unsupported = sorted(set(fields) - set(EVIDENCE_FIELDS))
    if unsupported:
        errors.append("Unsupported source evidence fields: " + ", ".join(unsupported))
    if signal.kind == "new_signal":
        for field in ("symbol", "side"):
            if not fields.get(field):
                errors.append(f"Missing contextual source evidence for {field}")
    numeric = {"stop_loss": signal.stop_loss}
    numeric.update({f"take_profits.{i}": value for i, value in enumerate(signal.take_profits)})
    numeric.update({f"entry_prices.{i}": value for i, value in enumerate(signal.entry_prices)})
    for field, value in numeric.items():
        if value is None:
            continue
        quotes = fields.get(field, [])
        supported = [quote for quote in quotes if value in set(quoted_numbers(quote))]
        if not supported:
            errors.append(f"Price not supported by source evidence: {field}")
        elif not any(len(re.findall(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w.])", quote)) == 1 for quote in supported):
            errors.append(f"Numeric source evidence must isolate one number: {field}")
    if signal.leverage is not None and not any(
            signal.leverage in set(quoted_leverage_numbers(quote)) for quote in fields.get("leverage", [])):
        errors.append("Signal leverage is not supported by source evidence")
    if signal.entry_timing == "deferred" and not fields.get("entry_timing"):
        errors.append("Deferred entry timing needs contextual source evidence")
    if signal.source_exchange is not None and not any(signal.source_exchange.casefold() in value.casefold() for value in fields.get("source_exchange", [])):
        errors.append("Source exchange needs contextual source evidence")
    if signal.take_profit_unit not in ("price", "roe_percent") and signal.take_profits and not fields.get("take_profit_unit"):
        errors.append("Percentage TP unit needs contextual source evidence")
    if errors:
        required = [field for field, value in numeric.items() if value is not None]
        repair = "; use one exact original quote per field, including % when present"
        if required:
            repair += "; numeric fields required: " + ", ".join(required)
        # Model fields bound this list to 25 required paths and 60 submitted paths.
        raise AqlaError("; ".join(errors) + repair)
    if signal.take_profit_unit not in ("price", "roe_percent") and signal.take_profits:
        unit_proof = " ".join(fields["take_profit_unit"])
        roi = re.search(r"\b(?:ROI|ROE)\b|return on (?:equity|investment)|рентабельност", unit_proof, re.I)
        movement = re.search(r"price|kurs|course|цены|движени|unleveraged|без плеч|hebelunabhängig", unit_proof, re.I)
        if "%" not in unit_proof or (not roi if signal.take_profit_unit == "roi_percent" else not movement or roi):
            raise AqlaError("Ambiguous percentage TP definition; evidence must distinguish price movement from nominal ROI/ROE")
        if signal.take_profit_unit == "roi_percent" and re.search(r"annuali[sz]ed|net ROI|net ROE|netto|годов|после (?:комисс|издерж)", unit_proof, re.I):
            raise AqlaError("This ROI definition cannot be converted as nominal linear ROI before costs")
    for field, value in numeric.items():
        if value is None or not (field == "stop_loss" or field.startswith("take_profits.")):
            continue
        percentage = field.startswith("take_profits.") and signal.take_profit_unit != "price"
        modes = []
        for proof in signal.evidence:
            if proof.field != field:
                continue
            original = texts[proof.source_id]
            for occurrence in re.finditer(re.escape(proof.quote), original):
                for number in re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w.])", proof.quote):
                    if value not in set(quoted_numbers(number.group())):
                        continue
                    tail = original[occurrence.start() + number.end():]
                    modes.append(bool(re.match(r"\s*%", tail)))
        if not modes or any(mode != percentage for mode in modes):
            raise AqlaError(f"Source percentage/price evidence needs a precise original span: {field}")
    if signal.take_profit_unit == "roe_percent" and signal.take_profits:
        if re.search(r"annuali[sz]ed|net ROI|net ROE|netto|годов|после (?:комисс|издерж)", " ".join(texts.values()), re.I):
            raise AqlaError("Special percentage definition cannot be converted as nominal ROE before costs")


@dataclass(frozen=True)
class EntryPrices:
    kind: Literal["limit", "market"]
    price: Decimal
    limit: Decimal


def entry_prices(signal: Interpretation, market: Market, policy: Policy, *, maker_price: Decimal | None = None) -> EntryPrices:
    """Pure price selection after the source, product and account guards."""
    long = signal.side == "long"
    kind: Literal["limit", "market"] = "limit" if signal.entry in ("limit", "range") else "market"
    if kind == "limit":
        if len(signal.entry_prices) != (2 if signal.entry == "range" else 1):
            raise AqlaError("Entry prices do not match entry type")
        low, high = signal.entry_prices[0], signal.entry_prices[-1]
        if policy.entry_range_choice == "midpoint":
            price = (low + high) / 2
        else:
            price = (high if long else low) if policy.entry_range_choice == "near" else (low if long else high)
        price = price_grid(price, market, up=not long)
        if policy.entry_execution == "maker_only" and signal.entry == "range" and not low <= price <= high:
            raise AqlaError("No maker price grid point satisfies the source entry range")
        limit = price
    else:
        if policy.entry_execution == "maker_only":
            if maker_price is None:
                raise AqlaError("Maker market intent requires a fresh passive book price")
            price = price_grid(maker_price, market, up=not long)
            if not market.mid * (1 - policy.max_slippage_percent / 100) <= price <= market.mid * (1 + policy.max_slippage_percent / 100):
                raise AqlaError("Passive maker price exceeds the source slippage tolerance")
            limit = price
        else:
            price = market.mid
            limit = slippage_price(price, policy.max_slippage_percent, market, buy=long)
    if signal.entry_timing == "deferred" and policy.entry_execution == "standard":
        limit = slippage_price(price, policy.max_slippage_percent, market, buy=long)
    return EntryPrices(kind=kind, price=price, limit=limit)


def make_plan(signal: Interpretation, market: Market, account: AccountSnapshot, policy: Policy,
              revision: int, source_time: int, binding: tuple[str, str], reserved_risk=Decimal(0), stamp=None, principal=None,
              backup: BackupValues | None = None, backup_revision=None, maker_price: Decimal | None = None) -> TradePlan:
    stamp = now_ms() if stamp is None else stamp
    with localcontext() as context:
        context.prec = 50
        if signal.kind != "new_signal" or not signal.symbol or not signal.side:
            raise AqlaError("New trade needs an evidenced market and direction")
        fields = []
        if signal.stop_loss is None:
            if backup is None or backup.fallback_stop_loss_percent is None:
                raise AqlaError("Signal SL is missing and Backup SL percentage is not configured")
            fields.append("stop_loss")
        if not signal.take_profits:
            if backup is None or not backup.fallback_take_profit_percentages:
                raise AqlaError("Signal TPs are missing and Backup TP percentages are not configured")
            fields.append("take_profits")
        if signal.leverage is None and policy.leverage_mode == "signal" and backup is not None:
            fields.append("leverage")
        if market.product != policy.product:
            raise AqlaError("Source product differs from configured product")
        if stamp >= source_time + policy.max_signal_age_seconds * 1000:
            raise AqlaError("Signal deadline expired; it is never renewed")
        if stamp - account.observed_at > 10000:
            raise AqlaError("Account observation is stale")
        leverage = effective_leverage(signal, market, policy, backup)
        if market.product == "spot" and signal.side == "short":
            raise AqlaError("Spot short signals are unsupported")
        long = signal.side == "long"
        if signal.entry == "unspecified":
            if backup is not None and backup.entry_mode == "source_or_market":
                fields.append("entry")
            elif policy.entry_mode != "source_or_market":
                raise AqlaError("Entry missing and Backup market entry is not configured")
        selected_entry = entry_prices(signal, market, policy, maker_price=maker_price)
        kind, entry, limit = selected_entry.kind, selected_entry.price, selected_entry.limit
        derived_stop, derived_targets = backup_prices(entry, signal.side, market,
            backup.fallback_stop_loss_percent if "stop_loss" in fields else None,
            selected_targets(backup.fallback_take_profit_percentages, policy) if "take_profits" in fields else (),
            stop_unit=backup.fallback_stop_loss_unit if backup else "course_percent",
            take_profit_unit=backup.fallback_take_profit_unit if backup else "course_percent", leverage=leverage)
        stop = derived_stop if "stop_loss" in fields else price_grid(signal.stop_loss, market, up=not long)
        source_tp = None
        from aqla_agent.percent_targets import reference_entry
        source_reference = reference_entry(signal, policy)
        if signal.take_profit_unit != "price" and signal.take_profits:
            from aqla_agent.percent_targets import prices
            reference = None if signal.take_profit_unit == "roe_percent" else source_reference
            selected = selected_targets(signal.take_profits, policy)
            targets = prices(reference or entry, signal.side, market, signal.take_profit_unit, selected, margin_leverage(leverage, market) if signal.take_profit_unit == "roe_percent" else signal.leverage)
            source_tp = {"unit": signal.take_profit_unit, "values": [str(value) for value in selected],
                         "reference_kind": "signal_entry" if reference is not None else "fill_vwap",
                         "reference_entry": str(reference) if reference is not None else None,
                         "signal_leverage": str(signal.leverage) if signal.leverage is not None else None,
                         "preview_reference": str(reference or entry), "preview_prices": [str(value) for value in targets], "nominal_before_costs": True}
            if signal.take_profit_unit == "roe_percent":
                source_tp.update(margin_leverage=str(margin_leverage(leverage, market)), rules_version="source-roe-v1")
        else:
            targets = derived_targets if "take_profits" in fields else [price_grid(value, market, up=not long) for value in selected_targets(signal.take_profits, policy)]
        ordered = sorted(set(targets), reverse=not long)
        if targets != ordered or not (stop < min(entry, limit) and all(t > max(entry, limit) for t in targets) if long else stop > max(entry, limit) and all(t < min(entry, limit) for t in targets)):
            raise AqlaError("SL, entry and TPs have invalid direction/ordering after rounding")
        budget = min(account.equity * policy.risk_percent / 100,
                     account.equity * policy.max_total_risk_percent / 100 - reserved_risk)
        cost = limit * policy.cost_reserve_bps / 10000
        distance = abs(limit - stop) + cost
        position_price = max(entry, limit, market.mid if kind == "limit" else entry)
        if signal.entry_timing == "deferred":
            position_price = max(position_price, entry * (1 + policy.max_slippage_percent / 100))
        maximum = min(budget / distance, policy.max_notional_usdc / position_price,
                      account.available_margin * margin_leverage(leverage, market) / position_price)
        if policy.sizing_mode == "fixed_notional":
            maximum = min(maximum, policy.fixed_notional_usdc / position_price)
        quantity = quantity_grid(min(maximum, market.max_quantity) if market.max_quantity else maximum, market)
        if market.leverage_tiers:
            if backup is not None and backup.uses_roe(fields):
                # Lower-only convergence: a wider ROE stop can reduce notional into
                # another bracket. Never raise leverage or quantity to chase a tier.
                for _ in range(len(market.leverage_tiers) + 1):
                    tier = next((tier for tier in market.leverage_tiers if tier.notional_floor <= quantity * limit < tier.notional_cap), None)
                    if tier is None:
                        raise AqlaError("Planned notional is outside the supported leverage brackets")
                    market = market.model_copy(update={"max_leverage": min(market.max_leverage, tier.max_leverage)})
                    leverage = effective_leverage(signal, market, policy, backup)
                    derived_stop, derived_targets = backup_prices(entry, signal.side, market,
                        backup.fallback_stop_loss_percent if "stop_loss" in fields else None,
                        selected_targets(backup.fallback_take_profit_percentages, policy) if "take_profits" in fields else (),
                        stop_unit=backup.fallback_stop_loss_unit, take_profit_unit=backup.fallback_take_profit_unit,
                        leverage=leverage)
                    if "stop_loss" in fields:
                        stop = derived_stop
                    if "take_profits" in fields:
                        targets = derived_targets
                    if not (stop < min(entry, limit) and all(t > max(entry, limit) for t in targets) if long else stop > max(entry, limit) and all(t < min(entry, limit) for t in targets)):
                        raise AqlaError("SL, entry and TPs have invalid direction/ordering after rounding")
                    distance = abs(limit - stop) + cost
                    reduced = quantity_grid(min(quantity, budget / distance,
                                                account.available_margin * margin_leverage(leverage, market) / limit), market)
                    if reduced == quantity:
                        break
                    quantity = reduced
                else:
                    raise AqlaError("Backup ROE leverage brackets did not converge")
            else:
                tier = next((tier for tier in market.leverage_tiers if tier.notional_floor <= quantity * limit < tier.notional_cap), None)
                if tier is None:
                    raise AqlaError("Planned notional is outside the supported leverage brackets")
                market = market.model_copy(update={"max_leverage": min(market.max_leverage, tier.max_leverage)})
                leverage = effective_leverage(signal, market, policy, backup)
                quantity = quantity_grid(min(quantity, account.available_margin * margin_leverage(leverage, market) / limit), market)
        if quantity <= 0 or quantity < market.min_quantity or quantity * limit < market.min_notional:
            raise AqlaError("Insufficient risk/margin budget or exchange minimum notional")
        tp_sizes = split_targets(quantity, len(targets), policy, market)
        if any(size < market.min_quantity or size * price < market.min_notional for size, price in zip(tp_sizes, targets, strict=True)):
            raise AqlaError("A TP leg is below the exchange notional minimum")
        trade_id = digest([*binding, sorted(signal.source_refs)])[:32]
        values = dict(trade_id=trade_id, source_refs=signal.source_refs, policy_revision=revision, policy=policy,
                      signal_leverage=int(Decimal(signal.leverage).to_integral_value(rounding=ROUND_FLOOR)) if signal.leverage is not None else None,
                      signal_leverage_original=signal.leverage, effective_leverage=leverage, source_symbol=signal.symbol,
                      backup_fields=fields, backup_values=backup.model_copy(deep=True) if fields else None, backup_profile_revision=backup_revision if fields else None,
                      network=binding[0], account=binding[1], principal=principal, market=market, side=signal.side, entry_kind=kind,
                      entry_price=entry, entry_limit=limit, stop_loss=stop, take_profits=targets, quantity=quantity,
                      source_take_profit_count=len(signal.take_profits),
                      tp_quantities=tp_sizes, risk_usdc=quantity * distance,
                      deadline_at=source_time + policy.max_signal_age_seconds * 1000, created_at=stamp)
        if policy.entry_execution != "standard":
            values["entry_execution"] = policy.entry_execution
        if policy.take_profit_execution != "standard":
            values["take_profit_execution"] = policy.take_profit_execution
        if signal.entry_timing == "deferred":
            values.update(entry_timing="deferred", entry_gate={"origin_mid": str(market.mid),
                          "direction": "down" if market.mid > entry else "up" if market.mid < entry else "at_entry"})
        if signal.source_exchange is not None:
            values["source_exchange"] = signal.source_exchange
        if source_tp is not None:
            values["source_tp"] = source_tp
        if source_reference is not None:
            values["source_entry_reference"] = source_reference
        return TradePlan(plan_id=digest(values), **values)
