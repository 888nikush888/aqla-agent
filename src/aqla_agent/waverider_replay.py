"""Offline executable-depth comparison. This module never constructs a broker."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
from bisect import bisect_right
from decimal import Decimal
from pathlib import Path

from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    Interpretation,
    TradePlan,
    digest,
    require_first,
)
from aqla_hyperliquid_mcp.rules import backup_prices, make_plan, quantity_grid, slippage_price

from .entry import PriceSnapshot, gate_state, validate_entry_prices
from .managed_entries import cap_quantity, leg_view, target_allocations
from .observations import TemporaryObservation
from .waverider import (
    COST_MODEL,
    VERSION,
    advance,
    comparable_performance,
    cycle_observation,
    profile_hash,
    qualification_evidence,
    reset_phase,
    retained_targets_fit,
    software_hash,
)


class ReplayEvidenceError(AqlaError):
    """Missing evidence is an incomplete measurement, never a trading loss or a pass."""


class ReplayAdmissionRejected(AqlaError):
    """A valid production admission refusal does not become a zero-PnL performance case."""


def replan_case(case, parameters):
    """Compare the current profile inside the original approved budget, without an account read."""
    from .pipelines import AccountLimits, Parameters

    original = TradePlan.model_validate(case["plan"])
    if "interpretation" not in case:
        raise ReplayEvidenceError("Current-profile replay needs the original evidenced interpretation")
    signal = Interpretation.model_validate(case["interpretation"])
    if set(signal.source_refs) != set(original.source_refs):
        raise ReplayEvidenceError("Replanning source references differ from the original plan")
    profile = Parameters.model_validate(parameters)
    policy = profile.policy(
        AccountLimits(
            max_open_trades=original.policy.max_open_trades,
            max_total_risk_percent=original.policy.max_total_risk_percent,
        )
    )
    policy.risk_percent = min(policy.risk_percent, original.policy.risk_percent)
    policy.max_notional_usdc = min(policy.max_notional_usdc, original.policy.max_notional_usdc)
    policy.max_signal_age_seconds = min(policy.max_signal_age_seconds, original.policy.max_signal_age_seconds)
    # A budget witness is derived from the immutable plan, not a claimed historical balance.
    account = AccountSnapshot(
        equity=original.risk_usdc * 100 / policy.risk_percent,
        available_margin=original.quantity
        * max(original.entry_limit, original.stop_loss)
        / (original.effective_leverage or 1),
        observed_at=original.created_at,
    )
    source_time = original.deadline_at - original.policy.max_signal_age_seconds * 1000
    try:
        plan = make_plan(
            signal,
            original.market,
            account,
            policy,
            original.policy_revision,
            source_time,
            (original.network, original.account),
            stamp=original.created_at,
            principal=original.principal,
            backup=original.backup_values,
            backup_revision=original.backup_profile_revision,
        )
        plan = cap_quantity(plan, original.quantity)
        if plan.risk_usdc > original.risk_usdc:
            plan = cap_quantity(plan, plan.quantity * original.risk_usdc / plan.risk_usdc)
    except AqlaError as error:
        raise ReplayAdmissionRejected(str(error)) from error
    if (
        plan.risk_usdc > original.risk_usdc
        or plan.quantity > original.quantity
        or plan.deadline_at > original.deadline_at
    ):
        raise AqlaError("Replanning exceeded the original risk, quantity or deadline")
    plan.exchange = original.exchange
    plan.base_parameters = profile.model_dump(mode="json")
    plan.plan_id = digest({key: value for key, value in plan.model_dump().items() if key != "plan_id"})
    return {
        **case,
        "plan": plan.model_dump(mode="json"),
        "original_entry_events": [],
        "initial_fills": [],
        "replanning": {
            "original_plan_hash": digest(case["plan"]),
            "budget": "original_approved_plan",
            "entry_path": "counterfactual",
            "profile_hash": profile_hash(plan.policy, plan.base_parameters),
        },
    }


def funding_schedule(case, plan):
    rows = case.get("funding", [])
    previous = -1
    expected_kind = "oracle" if plan.exchange == "hyperliquid" else None
    for row in rows:
        stamp, rate, price = int(row["time"]), Decimal(row["rate"]), Decimal(row["reference_price"])
        if (
            stamp <= previous
            or not rate.is_finite()
            or not price.is_finite()
            or price <= 0
            or row["reference_kind"] != expected_kind
            or row["market"] != plan.market.symbol
            or row["network"] != plan.network
            or row["exchange"] != plan.exchange
            or row["currency"] != plan.market.quote_token
            or row.get("settled") is not True
        ):
            raise ReplayEvidenceError(
                "Funding needs ordered native settlements with the venue's correct price basis"
            )
        previous = stamp
    return rows


def validate_replay_window(case, plan):
    records = case["observations"]
    if len(records) < 5:
        raise ReplayEvidenceError("Native replay has too few executable observations")
    stamps = [row["quote"]["observed_at"] for row in records]
    until_target = plan.policy.waverider_lifetime == "until_target"
    if (
        abs(stamps[0] - plan.created_at) > 2000
        or not until_target
        and abs(stamps[-1] - plan.deadline_at) > 2000
        or any(not 0 < right - left <= 2000 for left, right in zip(stamps, stamps[1:], strict=False))
    ):
        raise ReplayEvidenceError("Native observations do not continuously cover the original setup window")
    if until_target:
        validate_endpoint_coverage(records, plan)
    return records, stamps


def validate_endpoint_coverage(records, plan):
    endpoint = (
        len(plan.take_profits)
        if plan.policy.waverider_until_tp == "last"
        else min(plan.policy.waverider_until_tp, len(plan.take_profits))
    )
    target = plan.take_profits[endpoint - 1]
    if not any(
        (Decimal(row["quote"]["bid"]) >= target or Decimal(row["quote"]["stop_reference"]) <= plan.stop_loss)
        if plan.side == "long"
        else (
            Decimal(row["quote"]["ask"]) <= target
            or Decimal(row["quote"]["stop_reference"]) >= plan.stop_loss
        )
        for row in records
    ):
        raise ReplayEvidenceError(
            "Until-target replay needs observations through its TP or stop; the entry deadline is insufficient"
        )


def validate_replay_quotes(records):
    for record in records:
        try:
            quote = PriceSnapshot.model_validate(record["quote"])
            quote.fresh(as_of=quote.observed_at)
            if quote.stop_reference_kind != "mark":
                raise ReplayEvidenceError("The native stop reference uses another venue's price kind")
        except TemporaryObservation as error:
            raise ReplayEvidenceError(
                "Native book or stop reference exceeds the unchanged two-second freshness limit"
            ) from error


def validate_entry_fees(case, plan, stamps):
    for event in case.get("original_entry_events", case.get("initial_fills", [])):
        if (
            event.get("fee") is None
            or event.get("feeToken") != plan.market.quote_token
            or not Decimal(event["fee"]).is_finite()
            or not stamps[0] <= event["time"] <= stamps[-1]
        ):
            raise ReplayEvidenceError(
                "Original entries need actual fees in the native quote currency and covered time window"
            )


def validate_fee_binding(row, plan, previous_end):
    if (
        row["exchange"] != plan.exchange
        or row["network"] != plan.network
        or row["account"] != plan.account
        or row["market"] != plan.market.symbol
        or row["currency"] != plan.market.quote_token
        or row["liquidity"] != "taker"
        or not row.get("evidence_hash")
        or row["valid_until"] <= row["valid_from"]
        or previous_end is not None
        and row["valid_from"] != previous_end
    ):
        raise ReplayEvidenceError(
            "Historical taker fee evidence has a gap, overlap or another account/market"
        )


def validate_fee_provenance(row):
    if row["source"] == "native_account_snapshot":
        if row["valid_from"] < row["observed_at"] or row["valid_until"] - row["observed_at"] > 60000:
            raise ReplayEvidenceError(
                "A current fee snapshot cannot be backdated into historical fee evidence"
            )
    elif row["source"] != "native_account_fee_history":
        raise ReplayEvidenceError("Unverified or maker-only rates cannot certify account taker fees")
    rate = Decimal(row["rate"])
    if not rate.is_finite() or not 0 <= rate <= Decimal(".1"):
        raise ReplayEvidenceError("Historical taker fee rate is invalid")


def validate_replay_fees(case, plan, records, stamps):
    schedules = case.get("fee_schedule", [])
    if not schedules:
        raise ReplayEvidenceError("Historical account taker fee schedule is missing")
    previous_end = None
    for row in schedules:
        validate_fee_binding(row, plan, previous_end)
        validate_fee_provenance(row)
        previous_end = row["valid_until"]
    starts = [row["valid_from"] for row in schedules]
    for record, stamp in zip(records, stamps, strict=True):
        index = bisect_right(starts, stamp) - 1
        if (
            index < 0
            or stamp >= schedules[index]["valid_until"]
            or Decimal(record["fee_rate"]) != Decimal(schedules[index]["rate"])
        ):
            raise ReplayEvidenceError("An executable observation has no matching historical taker fee")


def validate_replay_funding(case, plan, stamps):
    coverage = case.get("funding_coverage", {})
    if (
        coverage.get("start", stamps[0] + 1) > stamps[0]
        or coverage.get("end", stamps[-1] - 1) < stamps[-1]
        or coverage.get("source") != "native_settled_funding_history"
        or not coverage.get("evidence_hash")
    ):
        raise ReplayEvidenceError("Settled funding coverage is not proved for the complete replay window")
    rows = funding_schedule(case, plan)
    if plan.exchange == "hyperliquid":
        # Native settlement timestamps can be a few milliseconds after the hour.
        expected = set(range((stamps[0] // 3600000 + 1), stamps[-1] // 3600000 + 1))
        actual = [row["time"] // 3600000 for row in rows if stamps[0] < row["time"] <= stamps[-1]]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise ReplayEvidenceError("Hourly native funding settlements are missing or duplicated")


def validate_replay_profile(plan):
    if plan.base_parameters is None:
        raise ReplayEvidenceError("Replay has no explicit base profile binding")
    from .pipelines import Parameters

    base = Parameters.model_validate(plan.base_parameters)
    effective = Parameters.from_policy(plan.policy)
    for field in Parameters.model_fields:
        if field == "management_mode":
            continue
        if field in ("risk_percent", "max_notional_quote") and getattr(effective, field) <= getattr(
            base, field
        ):
            continue
        if getattr(effective, field) != getattr(base, field):
            raise ReplayEvidenceError("Saved plan differs from its claimed base profile: " + field)
    if plan.entry_execution == "maker_only" or plan.take_profit_execution == "maker_only":
        raise ReplayEvidenceError(
            "Maker execution needs native queue-position evidence; IOC depth is insufficient"
        )


def validate_native_evidence(case, plan):
    """Native provenance alone cannot certify costs, continuity or the active profile."""
    records, stamps = validate_replay_window(case, plan)
    validate_replay_quotes(records)
    validate_entry_fees(case, plan, stamps)
    validate_replay_fees(case, plan, records, stamps)
    validate_replay_funding(case, plan, stamps)
    validate_replay_profile(plan)


class ReplaySimulation:
    """One causal comparison ledger; no transport or live state is constructed."""

    def __init__(self, case, *, wave):
        self.plan = TradePlan.model_validate(case["plan"])
        self.sign = Decimal(1) if self.plan.side == "long" else Decimal(-1)
        self.core, self.flex, self.core_cost, self.flex_cost, self.cash, self.fees, self.risk_fees = (
            Decimal(0) for _ in range(7)
        )
        self.peak, self.drawdown, self.losses, self.cycles = (Decimal(0), Decimal(0), Decimal(0), 0)
        self.entries = self.plan.entry_legs or [self.plan]
        self.completed, self.entered, self.pending = (set(), set(), None)
        self.state = {"phase": "waiting_rise"}
        self.stopped, self.previous = (False, -1)
        self.latency = max(1000, int(case["latency_ms"]))
        self.initial_quote = None
        self.records = case["observations"]
        self.curve = []
        self.original_events = sorted(
            case.get("original_entry_events", case.get("initial_fills", [])),
            key=lambda row: (row["time"], str(row.get("tid", ""))),
        )
        self.original_cursor = 0
        self.settlements = funding_schedule(case, self.plan)
        self.funding_cursor, self.funding, self.funding_spent = (0, Decimal(0), Decimal(0))
        self.target_prices = list(self.plan.take_profits)
        self.target_budgets, self.target_spent = (None, [Decimal(0)] * len(self.target_prices))
        self.stop = self.plan.stop_loss
        self.reached_end = False
        self.venue_average, self.original_quantity, self.original_cost = (Decimal(0), Decimal(0), Decimal(0))
        if len(self.records) < 5:
            raise AqlaError("Replay case has too few executable observations")
        self.wave = wave
        self.grew = False
        self.equity = Decimal(0)

    def fill(self, quote, *, buy, limit, quantity, stamp):
        # Reserve half of displayed depth for competing flow; charge the worst bound.
        amount, _ = quote.executable(buy=buy, limit=limit, quantity=quantity * 2, as_of=stamp)
        amount = quantity_grid(min(quantity, amount / 2), self.plan.market)
        return (amount, limit)

    def read_observation(self, record):
        quote = PriceSnapshot.model_validate(record["quote"])
        stamp = quote.observed_at
        if stamp <= self.previous or quote.market != self.plan.market.symbol:
            raise AqlaError("Replay observations must be strictly ordered and market-bound")
        quote.fresh(as_of=stamp)
        self.previous = stamp
        if self.initial_quote is None:
            self.initial_quote = stamp
        if record.get("fee_rate") is None or record.get("atr") is None:
            raise AqlaError("Replay account fee or volatility evidence is unavailable")
        rate, volatility = (Decimal(record["fee_rate"]), Decimal(record["atr"]))
        if (
            not rate.is_finite()
            or not 0 <= rate <= Decimal(".1")
            or (not volatility.is_finite())
            or (volatility <= 0)
        ):
            raise AqlaError("Replay lacks finite observed account fees/volatility")
        if not quote.bids or not quote.asks:
            raise AqlaError("Candles or best quotes alone cannot qualify Waverider")
        return (quote, stamp, rate, volatility)

    def settle_events(self, stamp, rate):
        self.grew = False
        while (
            self.original_cursor < len(self.original_events)
            and self.original_events[self.original_cursor]["time"] <= stamp
            or (
                self.funding_cursor < len(self.settlements)
                and self.settlements[self.funding_cursor]["time"] <= stamp
            )
        ):
            entry_time = (
                self.original_events[self.original_cursor]["time"]
                if self.original_cursor < len(self.original_events)
                else float("inf")
            )
            funding_time = (
                self.settlements[self.funding_cursor]["time"]
                if self.funding_cursor < len(self.settlements)
                else float("inf")
            )
            if entry_time == funding_time:
                raise ReplayEvidenceError("Entry/funding ordering at the same timestamp is not proved")
            if funding_time < entry_time:
                event = self.settlements[self.funding_cursor]
                amount = (
                    -(self.core + self.flex)
                    * self.sign
                    * Decimal(event["reference_price"])
                    * Decimal(event["rate"])
                )
                self.cash += amount
                self.funding += amount
                self.funding_spent += max(Decimal(0), -amount)
                self.funding_cursor += 1
                continue
            self.seed_entry(rate)

    def seed_entry(self, rate):
        event = self.original_events[self.original_cursor]
        self.original_cursor += 1
        if event.get("kind") != "entry" or event.get("entry_role", "original") != "original":
            raise AqlaError("Only proved original entries may seed both comparison paths")
        amount, price = (Decimal(event["sz"]), Decimal(event["px"]))
        if amount <= 0 or price <= 0:
            raise AqlaError("Original entry evidence has invalid size/price")
        if event.get("fee") is not None and event.get("feeToken") not in (None, self.plan.market.quote_token):
            raise AqlaError("Original entry fee currency differs from the comparison currency")
        part = amount * self.plan.policy.waverider_flex_percent / 100 if self.wave else Decimal(0)
        self.venue_average = (self.venue_average * (self.core + self.flex) + amount * price) / (
            self.core + self.flex + amount
        )
        self.core, self.flex = (self.core + amount - part, self.flex + part)
        self.core_cost, self.flex_cost = (
            self.core_cost + (amount - part) * price,
            self.flex_cost + part * price,
        )
        self.cash -= amount * price * self.sign
        paid = Decimal(event["fee"]) if event.get("fee") is not None else amount * price * rate
        self.fees += paid
        self.risk_fees += max(Decimal(0), paid)
        source_index = event.get("entry_leg", 0)
        self.entered.update(
            (
                index
                for index, leg in enumerate(self.entries)
                if (leg.index if self.plan.entry_legs else 0) == source_index
            )
        )
        self.state["last_original_fill"] = event["time"]
        self.original_quantity, self.original_cost, self.grew = (
            self.original_quantity + amount,
            self.original_cost + amount * price,
            True,
        )

    def try_original_entry(self, quote, stamp, rate):
        target_one = (
            quote.bid >= self.plan.take_profits[0]
            if self.sign == 1
            else quote.ask <= self.plan.take_profits[0]
        )
        if (
            not self.original_events
            and (not target_one)
            and (stamp < self.plan.deadline_at)
            and (not self.stopped)
        ):
            for index, leg in enumerate(self.entries):
                if index in self.entered or (index and index - 1 not in self.entered):
                    continue
                if stamp < self.initial_quote + self.latency or (
                    index and stamp < self.state.get("last_original_fill", stamp) + self.latency
                ):
                    continue
                if self.try_entry_leg(index, leg, quote, stamp, rate):
                    break

    def try_entry_leg(self, index, leg, quote, stamp, rate):
        kind = leg.kind if self.plan.entry_legs else self.plan.entry_kind
        limit = leg.limit if self.plan.entry_legs else self.plan.entry_limit
        size = leg.quantity
        if kind == "market" or (quote.ask <= limit if self.sign == 1 else quote.bid >= limit):
            view = (
                leg_view(
                    self.plan,
                    {
                        "index": leg.index,
                        "quantity": str(leg.quantity),
                        "entry_gate": {"origin_mid": str(self.records[0]["quote"]["mid"])},
                    },
                )
                if self.plan.entry_legs
                else self.plan
            )
            if view.entry_timing == "deferred" and gate_state(view, quote, as_of=stamp) == "missed":
                self.stopped = True
                return False
            try:
                validate_entry_prices(view, quote, stop=self.stop, targets=self.target_prices, as_of=stamp)
            except AqlaError:
                return False
            amount, price = self.fill(quote, buy=self.sign == 1, limit=limit, quantity=size, stamp=stamp)
            if amount <= 0:
                return False
            part = amount * self.plan.policy.waverider_flex_percent / 100 if self.wave else Decimal(0)
            self.venue_average = (self.venue_average * (self.core + self.flex) + amount * price) / (
                self.core + self.flex + amount
            )
            self.core, self.flex = (self.core + amount - part, self.flex + part)
            self.core_cost, self.flex_cost = (
                self.core_cost + (amount - part) * price,
                self.flex_cost + part * price,
            )
            self.cash -= amount * price * self.sign
            self.fees += amount * price * rate
            self.risk_fees += amount * price * rate
            self.entered.add(index)
            self.state["last_original_fill"] = stamp
            self.original_quantity, self.original_cost, self.grew = (
                self.original_quantity + amount,
                self.original_cost + amount * price,
                True,
            )
            return True

    def sync_targets(self, quote, stamp):
        target_end = (
            len(self.plan.take_profits)
            if self.plan.policy.waverider_until_tp == "last"
            else min(self.plan.policy.waverider_until_tp, len(self.plan.take_profits))
        )
        within_lifetime = (
            stamp < self.plan.deadline_at or self.plan.policy.waverider_lifetime == "until_target"
        )
        if self.original_quantity:
            reference = self.original_cost / self.original_quantity
            if self.plan.source_tp and self.plan.source_tp["unit"] == "roe_percent":
                from .percent_targets import prices

                self.target_prices = prices(
                    reference,
                    self.plan.side,
                    self.plan.market,
                    "roe_percent",
                    self.plan.source_tp["values"],
                    self.plan.source_tp["margin_leverage"],
                )
            elif "take_profits" in self.plan.backup_fields:
                _, self.target_prices = backup_prices(
                    reference,
                    self.plan.side,
                    self.plan.market,
                    percentages=self.plan.backup_values.fallback_take_profit_percentages[
                        : len(self.target_prices)
                    ],
                    take_profit_unit=self.plan.backup_values.fallback_take_profit_unit,
                    leverage=self.plan.effective_leverage,
                )
        target_one = (
            quote.bid >= self.target_prices[0] if self.sign == 1 else quote.ask <= self.target_prices[0]
        )
        endpoint = self.target_prices[target_end - 1]
        self.reached_end = self.reached_end or (
            quote.bid >= endpoint if self.sign == 1 else quote.ask <= endpoint
        )
        active = within_lifetime and (not self.stopped) and (not self.reached_end)
        return (target_one, active)

    def manage_targets(self, quote, stamp, rate, active):
        own = self.core + self.flex
        if not own:
            return
        allocations, self.completed = target_allocations(
            self.plan, own, self.target_budgets, self.target_spent, self.completed
        )
        if allocations:
            self.target_budgets = [
                amount + spent for amount, spent in zip(allocations, self.target_spent, strict=True)
            ]
        if self.pending and self.pending["action"] == "reduce" and not active:
            self.pending = None
        trigger = self.triggered_target(quote, own)
        if trigger is None:
            return
        stop_hit, index, amount = trigger
        limit = slippage_price(
            quote.bid if self.sign == 1 else quote.ask,
            self.plan.policy.max_slippage_percent,
            self.plan.market,
            buy=self.sign == -1,
        )
        filled, price = self.fill(quote, buy=self.sign == -1, limit=limit, quantity=amount, stamp=stamp)
        if not filled:
            return
        self.record_target_fill(filled, price, rate, own, index)
        self.tighten_stop()
        if stop_hit:
            self.stopped = True
        self.pending = None

    def triggered_target(self, quote, own):
        stop_hit = quote.stop_reference <= self.stop if self.sign == 1 else quote.stop_reference >= self.stop
        indices = [
            i
            for i, target in enumerate(self.target_prices)
            if i not in self.completed and (quote.bid >= target if self.sign == 1 else quote.ask <= target)
        ]
        if stop_hit or (indices and (not (self.pending and self.pending["action"] == "reduce"))):
            index = indices[0] if indices else None
            amount = own if stop_hit else min(own, self.target_budgets[index] - self.target_spent[index])
            return stop_hit, index, amount

    def record_target_fill(self, filled, price, rate, own, index):
        ratio = filled / own
        basis = (self.core_cost + self.flex_cost) / own
        self.losses += max(Decimal(0), -(price - basis) * filled * self.sign)
        self.core, self.flex = (self.core * (1 - ratio), self.flex * (1 - ratio))
        self.core_cost, self.flex_cost = (self.core_cost * (1 - ratio), self.flex_cost * (1 - ratio))
        self.cash += filled * price * self.sign
        self.fees += filled * price * rate
        self.risk_fees += filled * price * rate
        if index is not None:
            self.target_spent[index] += filled
            if self.target_spent[index] >= self.target_budgets[index]:
                self.completed.add(index)

    def tighten_stop(self):
        if self.plan.policy.stop_management == "tp_ladder":
            if 3 in self.completed and all(i in self.completed for i in range(4)):
                proposed = self.target_prices[1]
            elif 2 in self.completed and all(i in self.completed for i in range(3)):
                proposed = self.target_prices[0]
            elif 0 in self.completed:
                proposed = self.venue_average
            else:
                proposed = self.stop
            self.stop = max(self.stop, proposed) if self.sign == 1 else min(self.stop, proposed)
        elif self.plan.policy.break_even_after_tp and all(
            i in self.completed for i in range(self.plan.policy.break_even_after_tp)
        ):
            self.stop = (
                max(self.stop, self.venue_average) if self.sign == 1 else min(self.stop, self.venue_average)
            )

    def pending_action(self, quote, stamp, rate, active, target_one):
        if not (self.pending and stamp >= self.pending["at"] + self.latency and active):
            return
        action = self.pending["action"]
        if action == "reduce" and self.flex:
            self.reduce_wave(quote, stamp, rate)
        elif action == "reenter":
            self.reenter_wave(quote, stamp, rate, target_one)
        self.pending = None

    def reduce_wave(self, quote, stamp, rate):
        basis = self.flex_cost / self.flex
        limit = slippage_price(
            quote.bid if self.sign == 1 else quote.ask,
            self.plan.policy.max_slippage_percent,
            self.plan.market,
            buy=self.sign == -1,
        )
        if (limit - basis) * self.sign > self.pending["cost"]:
            feasible = retained_targets_fit(
                self.plan,
                self.core,
                self.target_budgets,
                self.target_spent,
                self.completed,
                self.target_prices,
            )
            if not feasible:
                self.pending = None
                self.state = reset_phase(self.state, "waiting_rise")
            else:
                amount, price = self.fill(
                    quote, buy=self.sign == -1, limit=limit, quantity=self.flex, stamp=stamp
                )
                if amount:
                    self.flex, self.flex_cost = (self.flex - amount, self.flex_cost - amount * basis)
                    self.cash += amount * price * self.sign
                    self.fees += amount * price * rate
                    self.risk_fees += amount * price * rate
                    self.state = {
                        **reset_phase(self.state, "waiting_rebound"),
                        "sale_price": str(price),
                        "sold_quantity": str(amount),
                    }
                else:
                    self.state = {
                        **reset_phase(self.state, "waiting_rise"),
                        "cycle": self.state.get("cycle", 0) + 1,
                    }
        else:
            self.state["phase"] = "waiting_retrace"

    def reenter_wave(self, quote, stamp, rate, target_one):
        limit = slippage_price(
            quote.ask if self.sign == 1 else quote.bid,
            self.plan.policy.max_slippage_percent,
            self.plan.market,
            buy=self.sign == 1,
        )
        size = Decimal(self.state.get("sold_quantity", "0"))
        reserved_quantity = sum(
            (
                leg.quantity
                for i, leg in enumerate(self.entries)
                if i not in self.entered and stamp < self.plan.deadline_at and (not target_one)
            )
        )
        reserved_risk = sum(
            (
                leg.quantity
                * (
                    abs((leg.limit if self.plan.entry_legs else self.plan.entry_limit) - self.stop)
                    + (leg.limit if self.plan.entry_legs else self.plan.entry_limit)
                    * self.plan.policy.cost_reserve_bps
                    / 10000
                )
                for i, leg in enumerate(self.entries)
                if i not in self.entered and stamp < self.plan.deadline_at and (not target_one)
            ),
            Decimal(0),
        )
        open_reserve = (self.core_cost + self.flex_cost) * self.plan.policy.cost_reserve_bps / 10000
        remaining_risk = (
            self.plan.risk_usdc
            - self.losses
            - self.risk_fees
            - self.funding_spent
            - open_reserve
            - max(Decimal(0), (self.core_cost - self.core * self.stop) * self.sign)
            - max(Decimal(0), (self.flex_cost - self.flex * self.stop) * self.sign)
            - reserved_risk
        )
        maximum = quantity_grid(
            max(Decimal(0), remaining_risk)
            / (abs(limit - self.stop) + limit * self.plan.policy.cost_reserve_bps / 10000),
            self.plan.market,
        )
        size = min(size, maximum, self.plan.quantity - self.core - self.flex - reserved_quantity)
        if size > 0 and (Decimal(self.state["sale_price"]) - limit) * self.sign > self.pending["cost"]:
            amount, price = self.fill(quote, buy=self.sign == 1, limit=limit, quantity=size, stamp=stamp)
            if amount:
                self.venue_average = (self.venue_average * (self.core + self.flex) + amount * price) / (
                    self.core + self.flex + amount
                )
                self.flex, self.flex_cost = (self.flex + amount, self.flex_cost + amount * price)
                self.cash -= amount * price * self.sign
                self.fees += amount * price * rate
                self.risk_fees += amount * price * rate
                self.cycles += 1
                self.grew = True
                self.state = reset_phase(self.state, "waiting_rise")
            else:
                self.state = {
                    **reset_phase(self.state, "waiting_rebound"),
                    "cycle": self.state.get("cycle", 0) + 1,
                }
        else:
            self.state["phase"] = "waiting_rebound"

    def observe_wave(self, quote, stamp, rate, volatility, active):
        if self.wave and active and self.core + self.flex and (not self.pending):
            phase = self.state.get("phase", "waiting_rise")
            size = quantity_grid(
                self.flex if phase != "waiting_rebound" else Decimal(self.state.get("sold_quantity", "0")),
                self.plan.market,
            )
            if size > 0:
                try:
                    observed = cycle_observation(
                        self.plan,
                        quote,
                        size,
                        reentering=phase == "waiting_rebound",
                        fee_rate=rate,
                        as_of=stamp,
                    )
                except AqlaError:
                    observed = None
                if observed is not None:
                    price, cost, tick_size = observed
                    basis = (
                        self.flex_cost / self.flex
                        if self.flex
                        else Decimal(self.state.get("sale_price", str(price)))
                    )
                    self.state, action = advance(
                        self.state,
                        side=self.plan.side,
                        price=price,
                        basis=basis,
                        volatility=volatility,
                        cycle_cost=cost,
                        tick=tick_size,
                        stamp=stamp,
                    )
                    if action:
                        self.pending = {"action": action, "at": stamp, "cost": cost}

    def value(self, quote, stamp, rate):
        own = self.core + self.flex
        if own > self.plan.quantity or self.core_cost + self.flex_cost > self.plan.policy.max_notional_usdc:
            raise AqlaError("Replay exceeded original quantity/notional constraints")
        stop_risk = max(Decimal(0), (self.core_cost - self.core * self.stop) * self.sign) + max(
            Decimal(0), (self.flex_cost - self.flex * self.stop) * self.sign
        )
        if self.grew and self.losses + self.risk_fees + self.funding_spent + stop_risk > self.plan.risk_usdc:
            raise AqlaError("Replay exceeded the original lifetime loss budget")
        mark = quote.bid if self.sign == 1 else quote.ask
        equity = self.cash + own * mark * self.sign - self.fees - own * mark * rate
        self.peak = max(self.peak, equity)
        self.drawdown = max(self.drawdown, self.peak - equity)
        self.curve.append((stamp, str(equity)))
        self.equity = equity

    def result(self):
        return {
            "net_pnl": str(self.equity),
            "max_drawdown": str(self.drawdown),
            "waves": self.cycles,
            "fees": str(self.fees),
            "funding": str(self.funding),
            "funding_spent": str(self.funding_spent),
            "original_entries_filled": sorted(self.entered),
            "original_quantity": str(self.original_quantity),
            "remaining_quantity": str(self.core + self.flex),
            "valuation": "conservative_mark_to_market",
            "equity": self.curve,
        }

    def run(self):
        for record in self.records:
            quote, stamp, rate, volatility = self.read_observation(record)
            self.settle_events(stamp, rate)
            self.try_original_entry(quote, stamp, rate)
            target_one, active = self.sync_targets(quote, stamp)
            self.manage_targets(quote, stamp, rate, active)
            self.pending_action(quote, stamp, rate, active, target_one)
            self.observe_wave(quote, stamp, rate, volatility, active)
            self.value(quote, stamp, rate)
        return self.result()


def simulate(case, *, wave):
    return ReplaySimulation(case, wave=wave).run()


def cohort_results(rows):
    cohorts = {}
    for cohort in sorted({row["cohort"] for row in rows}):
        summary = {}
        for mode in ("baseline", "waverider"):
            changes = {}
            for row in rows:
                if row["cohort"] != cohort:
                    continue
                previous = Decimal(0)
                for stamp, value in row[mode]["equity"]:
                    amount = Decimal(value)
                    changes[stamp] = changes.get(stamp, Decimal(0)) + amount - previous
                    previous = amount
            peak, equity, maximum = Decimal(0), Decimal(0), Decimal(0)
            for stamp in sorted(changes):
                equity += changes[stamp]
                peak = max(peak, equity)
                maximum = max(maximum, peak - equity)
            summary.update(
                {
                    ("baseline_" if mode == "baseline" else "") + "net_pnl": str(equity),
                    ("baseline_" if mode == "baseline" else "") + "max_drawdown": str(maximum),
                }
            )
        cohorts[cohort] = summary
    return cohorts


def evaluate(dataset):
    families = [case["family"] for case in dataset["cases"]]
    if len(set(families)) != len(families) or set(families) & set(dataset.get("development_families", [])):
        raise AqlaError("Development/holdout families overlap or a setup is counted twice")
    source_refs = [ref for case in dataset["cases"] for ref in set(case["plan"]["source_refs"])]
    if len(source_refs) != len(set(source_refs)):
        raise AqlaError("A source is reused across supposedly independent setup families")
    rows, failures, errors, data_errors, admission_rejections = [], [], 0, 0, 0
    profiles = set()
    for case in dataset["cases"]:
        try:
            if dataset.get("target_parameters") is not None:
                case = replan_case(case, dataset["target_parameters"])
            plan = TradePlan.model_validate(case["plan"])
            profiles.add(profile_hash(plan.policy, plan.base_parameters))
            if dataset.get("provenance") == "recorded_native_depth":
                validate_native_evidence(case, plan)
            rows.append(
                {
                    "id": digest(case["family"])[:16],
                    "cohort": f"{plan.exchange}:{plan.network}:{plan.market.quote_token}",
                    "baseline": simulate(case, wave=False),
                    "waverider": simulate(case, wave=True),
                }
            )
        except ReplayAdmissionRejected as error:
            admission_rejections += 1
            failures.append(
                {"id": digest(case["family"])[:16], "kind": "admission_rejected", "reason": str(error)}
            )
        except ReplayEvidenceError as error:
            data_errors += 1
            failures.append(
                {"id": digest(case["family"])[:16], "kind": "missing_evidence", "reason": str(error)}
            )
        except (ValueError, KeyError, ArithmeticError, TypeError) as error:
            errors += 1
            failures.append(
                {
                    "id": digest(case["family"])[:16],
                    "kind": "safety_or_invalid_input",
                    "error_type": type(error).__name__,
                }
            )
    return evaluation_report(dataset, rows, failures, profiles, errors, data_errors, admission_rejections)


def evaluation_report(dataset, rows, failures, profiles, errors, data_errors, admission_rejections):
    cohorts = cohort_results(rows)
    aggregate = (
        require_first(iter(cohorts.values()))
        if len(cohorts) == 1
        else {key: None for key in ("net_pnl", "baseline_net_pnl", "max_drawdown", "baseline_max_drawdown")}
    )
    report = {
        "version": VERSION,
        "software_hash": software_hash(),
        "profile_hash": require_first(iter(profiles)) if len(profiles) == 1 else "mixed",
        "cost_model": COST_MODEL,
        "cases_requested": len(dataset["cases"]),
        "cases": len(rows),
        "critical_errors": errors,
        "data_errors": data_errors,
        "admission_rejections": admission_rejections,
        "failures": failures,
        "waves": sum(row["waverider"]["waves"] for row in rows),
        **aggregate,
        "cohorts": cohorts,
        "aggregate_monetary_totals_unavailable": len(cohorts) != 1,
        "allowed_venues": sorted({cohort.split(":", 1)[0] for cohort in cohorts}),
        "data_kind": "executable_depth"
        if dataset.get("provenance") == "recorded_native_depth"
        else "synthetic",
        "holdout_hash": digest(dataset) if dataset.get("partition") == "holdout" else "",
        "results": rows,
    }
    return report


def export_replay(path, *, maximum=30):
    """Reserve the earliest unseen families; never select cases by their result."""
    cases, families, profile = [], set(), None
    with sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True) as db:
        setups = db.execute(
            "SELECT s.setup,s.payload,MIN(o.stamp) FROM setups s JOIN observations o ON s.setup=o.setup GROUP BY s.setup ORDER BY MIN(o.stamp),s.setup"
        ).fetchall()
        for identity, raw, _ in setups:
            meta = json.loads(raw)
            if meta.get("software_hash") != software_hash() or meta["family"] in families:
                continue
            plan = TradePlan.model_validate(meta["plan"])
            current = profile_hash(plan.policy, plan.base_parameters)
            if profile is not None and profile != current:
                continue
            profile = current
            records = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM observations WHERE setup=? ORDER BY stamp", (identity,)
                )
            ]
            observations = [
                {
                    "quote": row["quote"],
                    "fee_rate": row["facts"].get("fee_rate"),
                    "atr": row["facts"].get("atr"),
                    "facts": row["facts"],
                }
                for row in records
                if "stop_reference" in row["quote"]
            ]
            entries = [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM original_fills WHERE setup=?", (identity,))
            ]
            fees = {
                row["facts"]["fee_evidence"]["valid_from"]: dict(row["facts"]["fee_evidence"])
                for row in records
                if row["facts"].get("fee_evidence")
            }
            fee_schedule = [fees[key] for key in sorted(fees)]
            for left, right in zip(fee_schedule, fee_schedule[1:], strict=False):
                left["valid_until"] = min(left["valid_until"], right["valid_from"])
            cases.append(
                {
                    "id": identity,
                    "family": meta["family"],
                    "plan": meta["plan"],
                    "latency_ms": 1000,
                    "original_entry_events": entries,
                    "observations": observations,
                    "fee_schedule": fee_schedule,
                }
            )
            families.add(meta["family"])
            if len(cases) >= maximum:
                break
    return {
        "partition": "holdout",
        "provenance": "recorded_native_depth",
        "development_families": [],
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Offline Waverider versus Standard, with no exchange transport"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dataset", type=Path)
    inputs.add_argument("--from-replay", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--signing-key-file", type=Path)
    args = parser.parse_args()
    dataset = (
        export_replay(args.from_replay)
        if args.from_replay
        else json.loads(args.dataset.read_text(encoding="utf-8"))
    )
    report = evaluate(dataset)
    from .decision_evaluation import write_private_json

    write_private_json(args.output, report)
    if args.qualification:
        if args.signing_key_file is None:
            raise SystemExit("Qualification needs an explicit private integration key file")
        passed = (
            qualification_evidence(report)
            and report["profile_hash"] != "mixed"
            and all(comparable_performance(group) for group in report["cohorts"].values())
        )
        if not passed:
            raise SystemExit("Qualification not passed; live permission was not written")
        payload = {key: value for key, value in report.items() if key != "results"}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(
            args.signing_key_file.read_bytes().strip(), body, hashlib.sha256
        ).hexdigest()
        write_private_json(args.qualification, payload)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("cases", "critical_errors", "waves", "net_pnl", "baseline_net_pnl", "data_kind")
            }
        )
    )


if __name__ == "__main__":
    main()
