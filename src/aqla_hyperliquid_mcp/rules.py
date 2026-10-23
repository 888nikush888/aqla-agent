from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from aqla_agent.source_text import (
    SOURCE_NUMBERS,
    number_is_ordinal,
    numeric_evidence_values,
    quoted_leverage_numbers,
    quoted_numbers,
)
from aqla_agent.source_text import (
    complete_numeric_span as _complete_numeric_span,
)

from .models import (
    EVIDENCE_FIELDS,
    AccountSnapshot,
    AqlaError,
    BackupValues,
    EntryLegPlan,
    ExchangeMinimum,
    Interpretation,
    Market,
    Policy,
    SourceEntryLeg,
    TradePlan,
    digest,
    management_steps,
    margin_leverage,
    now_ms,
)
from .precision import price_grid, quantity_grid, slippage_price

UPDATE_VALUE_FIELDS = {
    "move_stop": "stop_loss",
    "replace_targets": "take_profits",
    "reduce": "reduce_percent",
}


def selected_targets(prices, policy):
    maximum = (
        5
        if policy.take_profit_allocation == "adaptive"
        else 1
        if policy.take_profit_allocation == "first_target"
        else 20
    )
    return list(prices[:maximum])


def split_targets(quantity: Decimal, count: int, policy: Policy, market: Market):
    if not 1 <= count <= 20:
        raise AqlaError("TP distribution requires 1–20 targets")
    if policy.take_profit_allocation == "adaptive":
        if count > 5:
            raise AqlaError("Adaptive distribution uses at most five targets")
        weights = [Decimal(value) for value in ("50", "25", "12.5", "7.5")[: count - 1]]
        weights += [Decimal(100) - sum(weights, Decimal(0))]
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
        weights += [Decimal(100) - sum(weights, Decimal(0))]
    else:
        weights = [Decimal(100) / count] * count
    quantities = [quantity_grid(quantity * weight / 100, market) for weight in weights[:-1]]
    quantities.append(quantity - sum(quantities))
    if any(size <= 0 for size in quantities):
        raise ExchangeMinimum("A TP leg rounds to zero; choose fewer targets or different weights")
    return quantities


def executable_targets(quantity, targets, policy, market):
    targets = selected_targets(targets, policy)
    counts = range(len(targets), 0, -1) if policy.take_profit_allocation == "adaptive" else (len(targets),)
    for count in counts:
        try:
            sizes = split_targets(quantity, count, policy, market)
        except AqlaError:
            if policy.take_profit_allocation != "adaptive":
                raise
            continue
        prices = targets[:count]
        if all(
            size >= market.min_quantity and size * price >= market.min_notional
            for size, price in zip(sizes, prices, strict=True)
        ):
            return prices, sizes
    raise ExchangeMinimum("A TP leg is below the exchange notional minimum")


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


def backup_prices(
    reference,
    side,
    market,
    stop_percent=None,
    percentages=(),
    *,
    stop_unit="course_percent",
    take_profit_unit="course_percent",
    leverage=None,
):
    """Nominal ROE uses the original position margin; grid away from entry."""
    uses_roe = (
        stop_percent is not None
        and stop_unit == "roe_percent"
        or percentages
        and take_profit_unit == "roe_percent"
    )
    if uses_roe:
        try:
            effective = (
                Decimal(leverage) if leverage is not None and not isinstance(leverage, bool) else Decimal(0)
            )
        except (InvalidOperation, ValueError, TypeError):
            effective = Decimal(0)
        if (
            market.product != "perp"
            or not effective.is_finite()
            or effective < 1
            or effective != effective.to_integral_value()
        ):
            raise AqlaError("Backup ROE requires valid effective perpetual leverage")
        effective = margin_leverage(effective, market)
        if stop_percent is not None and stop_unit == "roe_percent":
            stop_percent /= effective
        if take_profit_unit == "roe_percent":
            percentages = [value / effective for value in percentages]
    long = side == "long"
    stop = (
        price_grid(reference * (1 + (-1 if long else 1) * stop_percent / 100), market, up=not long)
        if stop_percent is not None
        else None
    )
    targets = [
        price_grid(reference * (1 + (1 if long else -1) * value / 100), market, up=long)
        for value in percentages
    ]
    if (
        stop is not None
        and (stop <= 0 or (stop >= reference if long else stop <= reference))
        or any(value <= 0 or (value <= reference if long else value >= reference) for value in targets)
        or targets != sorted(set(targets), reverse=not long)
    ):
        raise AqlaError("Backup prices collapse or have invalid direction after tick rounding")
    return stop, targets


def latest_sources(events: dict, refs=None):
    from aqla_agent.source_queue import canonical_sources

    return canonical_sources(events, refs)


def verify_setup_references(signal, anchor_source_id, own_setup_refs=()):
    if signal.kind not in ("new_signal", "trade_update"):
        if set(signal.source_refs) != {anchor_source_id}:
            raise AqlaError(
                "Non-actional source_refs must contain only the current anchor; keep reply context separate from classification targets"
            )
        return
    supported = {
        anchor_source_id,
        *(proof.source_id for proof in signal.evidence if proof.field != "classification"),
    }
    own_setup_refs = set(own_setup_refs)
    if signal.kind == "trade_update":
        supported.update(own_setup_refs)
        changed = UPDATE_VALUE_FIELDS.get(signal.update_action)
        if changed and any(
            proof.source_id != anchor_source_id
            and (proof.field == changed or proof.field.startswith(changed + "."))
            for proof in signal.evidence
        ):
            raise AqlaError(
                "Changed update values for "
                + changed
                + " need evidence from the current anchor, not copied parent values. Keep parents for market identity or own setup binding; re-read and quote only the newly instructed value"
            )
    if signal.kind in ("new_signal", "trade_update") and set(signal.source_refs) - supported:
        raise AqlaError(
            "Actionable source_refs include context without setup evidence; classification quotes alone do not contribute a setup field. Remove that parent reference and its classification proof; retain original field contributions and existing own setup bindings"
        )
    if signal.kind == "trade_update" and any(
        proof.field == "symbol" and proof.source_id == anchor_source_id for proof in signal.evidence
    ):
        for ref in set(signal.source_refs) - {anchor_source_id} - own_setup_refs:
            if {proof.field for proof in signal.evidence if proof.source_id == ref} - {"classification"} == {
                "symbol"
            }:
                raise AqlaError(
                    "source_refs include a redundant parent symbol: the current update already evidences its market. Remove only that duplicate parent reference and its symbol proof; retain genuine parent contributions and existing own setup bindings"
                )


def verify_entry_shape(signal):
    if signal.kind == "new_signal" and signal.entry == "split":
        if len(signal.entry_legs) != 2 or signal.entry_prices:
            raise AqlaError("Split entry needs both separate entry legs, never flattened prices")
        return
    if (
        signal.kind == "new_signal"
        and signal.entry in ("market", "unspecified")
        and len(signal.entry_prices) > 1
    ):
        raise AqlaError(
            "entry_prices: market/unspecified entry permits at most one reference price. Multiple order legs cannot be combined; do not drop a leg or rename separate orders to a range. Re-read the original and use ambiguous when one supported entry cannot represent it"
        )
    if signal.kind == "new_signal" and signal.entry in ("limit", "range"):
        expected_prices = 2 if signal.entry == "range" else 1
        if len(signal.entry_prices) != expected_prices:
            raise AqlaError(
                "Source entry type/prices disagree; limit needs exactly one evidenced price, range exactly two. Re-read source_context and correct the interpretation without inventing bounds"
            )


def verify_update_shape(signal):
    if signal.kind != "trade_update":
        return
    if signal.update_action is None:
        raise AqlaError("Trade update requires update_action")
    if signal.update_action == "sequence":
        for child in management_steps(signal):
            verify_update_shape(child)
        return
    required = UPDATE_VALUE_FIELDS.get(signal.update_action)
    if required and not getattr(signal, required):
        raise AqlaError(
            "Trade update "
            + signal.update_action
            + " requires "
            + required
            + " with original source evidence; updates never use backup values"
        )


def verify_evidence(signal: Interpretation, events: dict):
    if signal.management_steps:
        for child in management_steps(signal):
            verify_evidence(child, events)
        return
    texts = {ref: row["source"]["text"] for ref, row in latest_sources(events, signal.source_refs).items()}
    if any(ref not in texts for ref in signal.source_refs):
        raise AqlaError("Unknown source reference")
    fields: dict[str, list[str]] = {}
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
    for index, leg in enumerate(signal.entry_legs):
        numeric[f"entry_legs.{index}.price"] = leg.price
        if leg.weight_percent is not None:
            from aqla_agent.evidence import position_fractions

            quotes = fields.get(f"entry_legs.{index}.weight_percent", [])
            weight_supported = any(
                {value for _, value in position_fractions(quote)} == {leg.weight_percent}
                or len(list(re.finditer(r"\d+(?:[.,]\d+)?\s*%", quote))) == 1
                and leg.weight_percent in set(quoted_numbers(quote))
                for quote in quotes
            )
            if not weight_supported:
                errors.append(
                    f"Entry position weight needs its own fraction/percent evidence: entry_legs.{index}.weight_percent"
                )
        if not fields.get(f"entry_legs.{index}.kind"):
            errors.append(f"Entry leg form needs original contextual evidence: entry_legs.{index}.kind")
        if leg.timing == "deferred" and not fields.get(f"entry_legs.{index}.timing"):
            errors.append(f"Deferred leg needs original timing evidence: entry_legs.{index}.timing")
    for field, value in numeric.items():
        if value is None:
            continue
        quotes = fields.get(field, [])
        supported = [quote for quote in quotes if value in set(quoted_numbers(quote))]
        if not supported:
            errors.append(f"Price not supported by source evidence: {field}")
        elif not any(
            value in (numeric_evidence_values(texts[proof.source_id], proof.quote) or set())
            for proof in signal.evidence
            if proof.field == field and proof.quote in supported
        ):
            errors.append(
                f"Numeric source evidence must isolate one number: {field} (complete compatible original value; numbering labels are not prices)"
            )
    if signal.reduce_percent is not None:
        from aqla_agent.evidence import position_fractions

        reduction_supported = False
        for quote in fields.get("reduce_percent", []):
            quantities = [{value} for _, value in position_fractions(quote)]
            quantities.extend(
                set(quoted_numbers(match.group()))
                for match in re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)*\s*%", quote)
            )
            if len(quantities) == 1 and signal.reduce_percent in quantities[0]:
                reduction_supported = True
        if not reduction_supported:
            errors.append("Reduction percentage is not supported by source evidence: reduce_percent")
    if signal.leverage is not None and not any(
        signal.leverage in set(quoted_leverage_numbers(quote)) for quote in fields.get("leverage", [])
    ):
        errors.append("Signal leverage is not supported by source evidence")
    if signal.entry_timing == "deferred" and not fields.get("entry_timing"):
        errors.append("Deferred entry timing needs contextual source evidence")
    if signal.source_exchange is not None and not any(
        signal.source_exchange.casefold() in value.casefold() for value in fields.get("source_exchange", [])
    ):
        errors.append("Source exchange needs contextual source evidence")
    if (
        signal.take_profit_unit not in ("price", "roe_percent")
        and signal.take_profits
        and not fields.get("take_profit_unit")
    ):
        errors.append("Percentage TP unit needs contextual source evidence")
    if errors:
        required = [field for field, value in numeric.items() if value is not None]
        repair = "; use one exact original quote per field, including % when present"
        if required:
            repair += "; numeric fields required: " + ", ".join(required)
        # Model fields bound this list to 25 required paths and 60 submitted paths.
        raise AqlaError("; ".join(errors) + repair)
    if signal.kind == "new_signal":
        from aqla_agent.semantic_contract import analysis_only_entry_evidence

        entry_values = {f"entry_prices.{i}": value for i, value in enumerate(signal.entry_prices)}
        entry_values.update(
            {
                f"entry_legs.{i}.price": leg.price
                for i, leg in enumerate(signal.entry_legs)
                if leg.price is not None
            }
        )
        for field, value in entry_values.items():
            proofs = [proof for proof in signal.evidence if proof.field == field]
            if proofs and all(
                analysis_only_entry_evidence(texts[proof.source_id], proof.quote, value) for proof in proofs
            ):
                raise AqlaError("Entry evidence selects only an analysis level: " + field)
    if signal.take_profit_unit not in ("price", "roe_percent") and signal.take_profits:
        unit_proof = " ".join(fields["take_profit_unit"])
        roi = re.search(r"\b(?:ROI|ROE)\b|return on (?:equity|investment)|рентабельност", unit_proof, re.I)
        movement = re.search(
            r"price|kurs|course|цены|движени|unleveraged|без плеч|hebelunabhängig", unit_proof, re.I
        )
        if "%" not in unit_proof or (
            not roi if signal.take_profit_unit == "roi_percent" else not movement or roi
        ):
            raise AqlaError(
                "Ambiguous percentage TP definition; evidence must distinguish price movement from nominal ROI/ROE"
            )
        if signal.take_profit_unit == "roi_percent" and re.search(
            r"annuali[sz]ed|net ROI|net ROE|netto|годов|после (?:комисс|издерж)", unit_proof, re.I
        ):
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
            tokens = [match.span() for match in SOURCE_NUMBERS.finditer(original)]
            for occurrence in re.finditer(re.escape(proof.quote), original):
                for number in SOURCE_NUMBERS.finditer(proof.quote):
                    if not _complete_numeric_span(
                        original,
                        occurrence.start() + number.start(),
                        occurrence.start() + number.end(),
                        tokens,
                    ):
                        continue
                    if number_is_ordinal(
                        original, occurrence.start() + number.start(), occurrence.start() + number.end()
                    ):
                        continue
                    if value not in set(quoted_numbers(number.group())):
                        continue
                    if field.startswith("take_profits."):
                        from aqla_agent.percent_targets import percentage_intervals

                        position = (occurrence.start() + number.start(), occurrence.start() + number.end())
                        if any(
                            position in (interval.span("lower"), interval.span("upper"))
                            for interval in percentage_intervals(original)
                        ):
                            raise AqlaError("TP evidence selects a percentage-interval endpoint: " + field)
                    tail = original[occurrence.start() + number.end() :]
                    modes.append(bool(re.match(r"\s*%", tail)))
        if not modes or any(mode != percentage for mode in modes):
            raise AqlaError(f"Source percentage/price evidence needs a precise original span: {field}")
    if signal.take_profit_unit == "roe_percent" and signal.take_profits:
        if re.search(
            r"annuali[sz]ed|net ROI|net ROE|netto|годов|после (?:комисс|издерж)",
            " ".join(texts.values()),
            re.I,
        ):
            raise AqlaError("Special percentage definition cannot be converted as nominal ROE before costs")


@dataclass(frozen=True)
class EntryPrices:
    kind: Literal["limit", "market"]
    price: Decimal
    limit: Decimal


def entry_prices(
    signal: Interpretation, market: Market, policy: Policy, *, maker_price: Decimal | None = None
) -> EntryPrices:
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
            price = (
                (high if long else low) if policy.entry_range_choice == "near" else (low if long else high)
            )
        price = price_grid(price, market, up=not long)
        if policy.entry_execution == "maker_only" and signal.entry == "range" and not low <= price <= high:
            raise AqlaError("No maker price grid point satisfies the source entry range")
        limit = price
    else:
        if policy.entry_execution == "maker_only":
            if maker_price is None:
                raise AqlaError("Maker market intent requires a fresh passive book price")
            price = price_grid(maker_price, market, up=not long)
            if (
                not market.mid * (1 - policy.max_slippage_percent / 100)
                <= price
                <= market.mid * (1 + policy.max_slippage_percent / 100)
            ):
                raise AqlaError("Passive maker price exceeds the source slippage tolerance")
            limit = price
        else:
            price = market.mid
            limit = slippage_price(price, policy.max_slippage_percent, market, buy=long)
    if signal.entry_timing == "deferred" and policy.entry_execution == "standard":
        limit = slippage_price(price, policy.max_slippage_percent, market, buy=long)
    return EntryPrices(kind=kind, price=price, limit=limit)


class PlanBuilder:
    """Pure plan phases inside one fixed decimal context; no exchange effects."""

    fields: list[str]
    leverage: int | None
    long: bool
    kind: str
    entry: Decimal
    limit: Decimal
    managed: bool
    backup_reference: Decimal
    derived_stop: Decimal | None
    derived_targets: list[Decimal]
    stop: Decimal
    source_tp: dict[str, Any] | None
    source_reference: Decimal | None
    selected: list[Decimal]
    targets: list[Decimal]
    budget: Decimal
    cost: Decimal
    distance: Decimal
    position_price: Decimal
    quantity: Decimal
    tp_sizes: list[Decimal]

    def __init__(
        self,
        signal,
        market,
        account,
        policy,
        revision,
        source_time,
        binding,
        reserved_risk,
        stamp,
        principal,
        backup,
        backup_revision,
        maker_price,
        _managed,
    ):
        self.signal = signal
        self.market = market
        self.account = account
        self.policy = policy
        self.revision = revision
        self.source_time = source_time
        self.binding = binding
        self.reserved_risk = reserved_risk
        self.stamp = stamp
        self.principal = principal
        self.backup = backup
        self.backup_revision = backup_revision
        self.maker_price = maker_price
        self._managed = _managed

    def require_backups(self):
        if self.signal.kind != "new_signal" or not self.signal.symbol or (not self.signal.side):
            raise AqlaError("New trade needs an evidenced market and direction")
        self.fields = []
        if self.signal.stop_loss is None:
            if self.backup is None or self.backup.fallback_stop_loss_percent is None:
                raise AqlaError("Signal SL is missing and Backup SL percentage is not configured")
            self.fields.append("stop_loss")
        if not self.signal.take_profits:
            if self.backup is None or not self.backup.fallback_take_profit_percentages:
                raise AqlaError("Signal TPs are missing and Backup TP percentages are not configured")
            self.fields.append("take_profits")
        if (
            self.signal.leverage is None
            and self.policy.leverage_mode == "signal"
            and (self.backup is not None)
        ):
            self.fields.append("leverage")

    def validate_observations(self):
        if self.market.product != self.policy.product:
            raise AqlaError("Source product differs from configured product")
        if self.stamp >= self.source_time + self.policy.max_signal_age_seconds * 1000:
            raise AqlaError("Signal deadline expired; it is never renewed")
        if self.stamp - self.account.observed_at > 10000:
            raise AqlaError("Account observation is stale")

    def select_entry(self):
        self.leverage = effective_leverage(self.signal, self.market, self.policy, self.backup)
        if self.market.product == "spot" and self.signal.side == "short":
            raise AqlaError("Spot short signals are unsupported")
        self.long = self.signal.side == "long"
        if self.signal.entry == "unspecified":
            if self.backup is not None and self.backup.entry_mode == "source_or_market":
                self.fields.append("entry")
            elif self.policy.entry_mode != "source_or_market":
                raise AqlaError("Entry missing and Backup market entry is not configured")
        selected_entry = entry_prices(self.signal, self.market, self.policy, maker_price=self.maker_price)
        self.kind, self.entry, self.limit = (selected_entry.kind, selected_entry.price, selected_entry.limit)
        self.managed = (
            self._managed
            or self.policy.entry_range_choice == "split"
            or self.policy.management_mode == "waverider"
        )
        self.backup_reference = self.limit if self.managed else self.entry

    def derive_backup_prices(self):
        self.derived_stop, self.derived_targets = backup_prices(
            self.backup_reference,
            self.signal.side,
            self.market,
            self.backup.fallback_stop_loss_percent
            if self.backup is not None and "stop_loss" in self.fields
            else None,
            selected_targets(self.backup.fallback_take_profit_percentages, self.policy)
            if self.backup is not None and "take_profits" in self.fields
            else (),
            stop_unit=self.backup.fallback_stop_loss_unit if self.backup else "course_percent",
            take_profit_unit=self.backup.fallback_take_profit_unit if self.backup else "course_percent",
            leverage=self.leverage,
        )

    def select_stop(self):
        if "stop_loss" in self.fields:
            self.stop = self.derived_stop
        elif self.signal.stop_loss is not None:
            self.stop = price_grid(self.signal.stop_loss, self.market, up=not self.long)
        else:
            raise AqlaError("Source stop or approved Backup stop is required")
        if self.stop is None:
            raise AqlaError("Approved stop could not be established")

    def select_targets(self):
        self.source_tp: dict[str, Any] | None = None
        from aqla_agent.percent_targets import reference_entry

        self.source_reference = reference_entry(self.signal, self.policy)
        if self.signal.take_profit_unit != "price" and self.signal.take_profits:
            self.prepare_percent_targets()
        else:
            self.targets = (
                self.derived_targets
                if "take_profits" in self.fields
                else [
                    price_grid(value, self.market, up=not self.long)
                    for value in selected_targets(self.signal.take_profits, self.policy)
                ]
            )

    def prepare_percent_targets(self):
        from aqla_agent.percent_targets import prices

        reference = None if self.signal.take_profit_unit == "roe_percent" else self.source_reference
        self.selected = selected_targets(self.signal.take_profits, self.policy)
        self.targets = prices(
            reference or self.entry,
            self.signal.side,
            self.market,
            self.signal.take_profit_unit,
            self.selected,
            margin_leverage(self.leverage, self.market)
            if self.signal.take_profit_unit == "roe_percent"
            else self.signal.leverage,
        )
        self.source_tp = {
            "unit": self.signal.take_profit_unit,
            "values": [str(value) for value in self.selected],
            "reference_kind": "signal_entry" if reference is not None else "fill_vwap",
            "reference_entry": str(reference) if reference is not None else None,
            "signal_leverage": str(self.signal.leverage) if self.signal.leverage is not None else None,
            "preview_reference": str(reference or self.entry),
            "preview_prices": [str(value) for value in self.targets],
            "nominal_before_costs": True,
        }
        if self.signal.take_profit_unit == "roe_percent":
            self.source_tp.update(
                margin_leverage=str(margin_leverage(self.leverage, self.market)),
                rules_version="source-roe-v1",
            )

    def validate_prices(self, *, ordered=True):
        if (
            ordered
            and self.targets != sorted(set(self.targets), reverse=not self.long)
            or not (
                self.stop < min(self.entry, self.limit)
                and all(t > max(self.entry, self.limit) for t in self.targets)
                if self.long
                else self.stop > max(self.entry, self.limit)
                and all(t < min(self.entry, self.limit) for t in self.targets)
            )
        ):
            raise AqlaError("SL, entry and TPs have invalid direction/ordering after rounding")

    def initial_size(self):
        self.budget = min(
            self.account.equity * self.policy.risk_percent / 100,
            self.account.equity * self.policy.max_total_risk_percent / 100 - self.reserved_risk,
        )
        self.cost = self.limit * self.policy.cost_reserve_bps / 10000
        self.distance = abs(self.limit - self.stop) + self.cost
        self.position_price = max(
            self.entry, self.limit, self.market.mid if self.kind == "limit" else self.entry
        )
        if self.managed and (not self.long):
            self.position_price = max(self.position_price, self.stop)
        if self.signal.entry_timing == "deferred":
            self.position_price = max(
                self.position_price, self.entry * (1 + self.policy.max_slippage_percent / 100)
            )
        maximum = min(
            self.budget / self.distance,
            self.policy.max_notional_usdc / self.position_price,
            self.account.available_margin * margin_leverage(self.leverage, self.market) / self.position_price,
        )
        if self.policy.sizing_mode == "fixed_notional":
            if self.policy.fixed_notional_usdc is None:
                raise AqlaError("Fixed-notional sizing requires its approved amount")
            maximum = min(maximum, self.policy.fixed_notional_usdc / self.position_price)
        self.quantity = quantity_grid(
            min(maximum, self.market.max_quantity) if self.market.max_quantity else maximum, self.market
        )

    def select_tier(self):
        tier_price = self.position_price if self.managed and (not self.long) else self.limit
        tier = next(
            (
                tier
                for tier in self.market.leverage_tiers
                if tier.notional_floor <= self.quantity * tier_price < tier.notional_cap
            ),
            None,
        )
        if tier is None:
            raise AqlaError("Planned notional is outside the supported leverage brackets")
        self.market = self.market.model_copy(
            update={"max_leverage": min(self.market.max_leverage, tier.max_leverage)}
        )
        self.leverage = effective_leverage(self.signal, self.market, self.policy, self.backup)
        return tier_price

    def converge_backup_tiers(self):
        for _ in range(len(self.market.leverage_tiers) + 1):
            self.select_tier()
            self.derive_backup_prices()
            if "stop_loss" in self.fields:
                self.stop = self.derived_stop
            if "take_profits" in self.fields:
                self.targets = self.derived_targets
            self.validate_prices(ordered=False)
            self.distance = abs(self.limit - self.stop) + self.cost
            if self.managed and (not self.long):
                self.position_price = max(self.position_price, self.stop)
            reduced = quantity_grid(
                min(
                    self.quantity,
                    self.budget / self.distance,
                    self.policy.max_notional_usdc / self.position_price,
                    self.account.available_margin
                    * margin_leverage(self.leverage, self.market)
                    / (self.position_price if self.managed and (not self.long) else self.limit),
                ),
                self.market,
            )
            if reduced == self.quantity:
                break
            self.quantity = reduced
        else:
            raise AqlaError("Backup ROE leverage brackets did not converge")

    def adjust_tiers(self):
        if not self.market.leverage_tiers:
            return
        if self.backup is not None and self.backup.uses_roe(self.fields):
            self.converge_backup_tiers()
        else:
            tier_price = self.select_tier()
            self.quantity = quantity_grid(
                min(
                    self.quantity,
                    self.account.available_margin * margin_leverage(self.leverage, self.market) / tier_price,
                ),
                self.market,
            )

    def finalize_size(self):
        from aqla_agent.percent_targets import prices

        if (
            self.quantity <= 0
            or self.quantity < self.market.min_quantity
            or self.quantity * self.limit < self.market.min_notional
        ):
            raise AqlaError("Insufficient risk/margin budget or exchange minimum notional")
        if self.source_tp is not None and self.signal.take_profit_unit == "roe_percent":
            final_leverage = str(margin_leverage(self.leverage, self.market))
            if self.source_tp["margin_leverage"] != final_leverage:
                self.targets = prices(
                    self.entry, self.signal.side, self.market, "roe_percent", self.selected, final_leverage
                )
                self.source_tp.update(
                    margin_leverage=final_leverage, preview_prices=[str(value) for value in self.targets]
                )
        self.targets, self.tp_sizes = executable_targets(
            self.quantity, self.targets, self.policy, self.market
        )
        if self.source_tp is not None:
            self.source_tp["values"] = self.source_tp["values"][: len(self.targets)]
            self.source_tp["preview_prices"] = self.source_tp["preview_prices"][: len(self.targets)]

    def plan_metadata(self, values):
        if self.policy.entry_execution != "standard":
            values["entry_execution"] = self.policy.entry_execution
        if self.policy.take_profit_execution != "standard":
            values["take_profit_execution"] = self.policy.take_profit_execution
        if self.signal.entry_timing == "deferred":
            values.update(
                entry_timing="deferred",
                entry_gate={
                    "origin_mid": str(self.market.mid),
                    "direction": "down"
                    if self.market.mid > self.entry
                    else "up"
                    if self.market.mid < self.entry
                    else "at_entry",
                },
            )
        if self.signal.source_exchange is not None:
            values["source_exchange"] = self.signal.source_exchange
        if self.source_tp is not None:
            values["source_tp"] = self.source_tp
        if self.source_reference is not None:
            values["source_entry_reference"] = self.source_reference

    def finish(self):
        trade_id = digest([*self.binding, sorted(self.signal.source_refs)])[:32]
        values = {
            "trade_id": trade_id,
            "source_refs": self.signal.source_refs,
            "policy_revision": self.revision,
            "policy": self.policy,
            "signal_leverage": int(Decimal(self.signal.leverage).to_integral_value(rounding=ROUND_FLOOR))
            if self.signal.leverage is not None
            else None,
            "signal_leverage_original": self.signal.leverage,
            "effective_leverage": self.leverage,
            "source_symbol": self.signal.symbol,
            "backup_fields": self.fields,
            "backup_values": self.backup.model_copy(deep=True)
            if self.backup is not None and self.fields
            else None,
            "backup_profile_revision": self.backup_revision if self.fields else None,
            "network": self.binding[0],
            "account": self.binding[1],
            "principal": self.principal,
            "market": self.market,
            "side": self.signal.side,
            "entry_kind": self.kind,
            "entry_price": self.entry,
            "entry_limit": self.limit,
            "stop_loss": self.stop,
            "take_profits": self.targets,
            "quantity": self.quantity,
            "source_take_profit_count": len(self.signal.take_profits),
            "tp_quantities": self.tp_sizes,
            "risk_usdc": self.quantity * self.distance,
            "deadline_at": self.source_time + self.policy.max_signal_age_seconds * 1000,
            "created_at": self.stamp,
        }
        self.plan_metadata(values)
        return TradePlan(plan_id=digest(values), **values)

    def build(self):
        self.require_backups()
        self.validate_observations()
        self.select_entry()
        self.derive_backup_prices()
        self.select_stop()
        self.select_targets()
        self.validate_prices()
        self.initial_size()
        self.adjust_tiers()
        self.finalize_size()
        return self.finish()


def make_plan(
    signal: Interpretation,
    market: Market,
    account: AccountSnapshot,
    policy: Policy,
    revision: int,
    source_time: int,
    binding: tuple[str, str],
    reserved_risk=Decimal(0),
    stamp=None,
    principal=None,
    backup: BackupValues | None = None,
    backup_revision=None,
    maker_price: Decimal | None = None,
    *,
    _managed=False,
) -> TradePlan:
    if signal.entry == "split" or signal.entry == "range" and policy.entry_range_choice == "split":
        return make_entry_plan(
            signal,
            market,
            account,
            policy,
            revision,
            source_time,
            binding,
            reserved_risk,
            stamp,
            principal,
            backup,
            backup_revision,
            maker_price,
        )
    stamp = now_ms() if stamp is None else stamp
    with localcontext() as context:
        context.prec = 50
        return PlanBuilder(
            signal,
            market,
            account,
            policy,
            revision,
            source_time,
            binding,
            reserved_risk,
            stamp,
            principal,
            backup,
            backup_revision,
            maker_price,
            _managed,
        ).build()


def entry_targets(quantity, targets, policy, market, leg_quantities=()):
    # Reuse the configured adaptive rule at every stage, including the first protected fill.
    count = min(
        len(executable_targets(size, targets, policy, market)[0]) for size in (quantity, *leg_quantities)
    )
    return executable_targets(quantity, targets[:count], policy, market)


def select_source_entries(signal, market, policy, backup, maker_price):
    if signal.kind != "new_signal":
        raise AqlaError("Only a new setup can allocate two original entries")
    legs = (
        signal.entry_legs
        if signal.entry == "split"
        else [
            SourceEntryLeg(kind="limit", price=value, timing=signal.entry_timing)
            for value in signal.entry_prices
        ]
    )
    if len(legs) != 2:
        raise AqlaError("Both original entries are required")
    selections = []
    backup_entry = False
    for index, leg in enumerate(legs):
        if leg.kind == "unspecified":
            if backup and backup.entry_mode == "source_or_market":
                backup_entry = True
            elif policy.entry_mode != "source_or_market":
                raise AqlaError("Unstated entry form needs the configured market-entry fallback")
        single = signal.model_copy(
            update={
                "entry": leg.kind,
                "entry_legs": [],
                "entry_prices": [leg.price] if leg.price is not None else [],
                "entry_timing": leg.timing,
            }
        )
        selected = entry_prices(single, market, policy, maker_price=maker_price)
        selections.append((index, leg, selected))
    if signal.entry == "range" and selections[0][2].price == selections[1][2].price:
        raise AqlaError("Source zone collapses to one native tick; two distinct entries cannot be invented")
    return selections, backup_entry, legs


def allocate_source_entries(base, selections, legs):
    weights = [leg.weight_percent or Decimal(50) for leg in legs]
    quantities = [quantity_grid(base.quantity * weights[0] / 100, base.market)]
    quantities.append(base.quantity - quantities[0])
    planned = []
    for (index, leg, selected), size in zip(selections, quantities, strict=True):
        if size <= 0 or size < base.market.min_quantity or size * selected.limit < base.market.min_notional:
            raise ExchangeMinimum(
                "A source entry leg is below the exchange minimum; neither leg may be dropped or enlarged"
            )
        if not (
            base.stop_loss < selected.price < min(base.take_profits)
            if base.side == "long"
            else base.stop_loss > selected.price > max(base.take_profits)
        ):
            raise AqlaError("Both source entries must have valid common SL/TP structure")
        planned.append(
            EntryLegPlan(
                index=index,
                kind=selected.kind,
                price=selected.price,
                limit=selected.limit,
                quantity=size,
                timing=leg.timing,
            )
        )
    # Market intent first; otherwise the near limit fills before the far limit.
    planned.sort(
        key=lambda leg: (leg.kind != "market", -leg.limit if base.side == "long" else leg.limit, leg.index)
    )
    return planned


def make_entry_plan(
    signal,
    market,
    account,
    policy,
    revision,
    source_time,
    binding,
    reserved_risk,
    stamp,
    principal,
    backup,
    backup_revision,
    maker_price,
):
    selections, backup_entry, legs = select_source_entries(signal, market, policy, backup, maker_price)
    # Size conservatively at the worst allowed entry. Never use averaging to enlarge risk.
    worst = (max if signal.side == "long" else min)(row[2].limit for row in selections)
    anchor = signal.model_copy(
        update={"entry": "limit", "entry_prices": [worst], "entry_legs": [], "entry_timing": "standard"}
    )
    base = make_plan(
        anchor,
        market,
        account,
        policy,
        revision,
        source_time,
        binding,
        reserved_risk,
        stamp,
        principal,
        backup,
        backup_revision,
        maker_price,
        _managed=True,
    )
    planned = allocate_source_entries(base, selections, legs)
    values = base.model_dump()
    if backup_entry and "entry" not in base.backup_fields:
        values.update(
            backup_fields=[*base.backup_fields, "entry"],
            backup_values=backup.model_copy(deep=True),
            backup_profile_revision=backup_revision,
        )
    targets, quantities = entry_targets(
        base.quantity, base.take_profits, policy, base.market, [leg.quantity for leg in planned]
    )
    count = len(targets)
    values.update(
        entry_legs=planned,
        entry_price=sum(leg.price * leg.quantity for leg in planned) / base.quantity,
        take_profits=targets,
        tp_quantities=quantities,
    )
    if base.source_tp:
        values["source_tp"] = {
            **base.source_tp,
            "values": base.source_tp["values"][:count],
            "preview_prices": base.source_tp["preview_prices"][:count],
        }
    values.pop("plan_id")
    return TradePlan(plan_id=digest(values), **values)


# Named compatibility exports share the same implementation.
__all__ = [
    "EntryPrices",
    "SOURCE_NUMBERS",
    "backup_prices",
    "effective_leverage",
    "entry_prices",
    "entry_targets",
    "executable_targets",
    "latest_sources",
    "make_entry_plan",
    "make_plan",
    "management_steps",
    "number_is_ordinal",
    "numeric_evidence_values",
    "price_grid",
    "quantity_grid",
    "quoted_leverage_numbers",
    "quoted_numbers",
    "selected_targets",
    "slippage_price",
    "split_targets",
    "verify_entry_shape",
    "verify_evidence",
    "verify_setup_references",
    "verify_update_shape",
]
