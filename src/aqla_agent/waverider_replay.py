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
from .waverider import COST_MODEL, VERSION, advance, profile_hash, reset_phase, software_hash


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
    policy = profile.policy(AccountLimits(max_open_trades=original.policy.max_open_trades,
                                         max_total_risk_percent=original.policy.max_total_risk_percent))
    policy.risk_percent = min(policy.risk_percent, original.policy.risk_percent)
    policy.max_notional_usdc = min(policy.max_notional_usdc, original.policy.max_notional_usdc)
    policy.max_signal_age_seconds = min(policy.max_signal_age_seconds, original.policy.max_signal_age_seconds)
    # A budget witness is derived from the immutable plan, not a claimed historical balance.
    account = AccountSnapshot(equity=original.risk_usdc * 100 / policy.risk_percent,
                              available_margin=original.quantity * max(original.entry_limit, original.stop_loss)
                              / (original.effective_leverage or 1), observed_at=original.created_at)
    source_time = original.deadline_at - original.policy.max_signal_age_seconds * 1000
    try:
        plan = make_plan(signal, original.market, account, policy, original.policy_revision, source_time,
                         (original.network, original.account), stamp=original.created_at, principal=original.principal,
                         backup=original.backup_values, backup_revision=original.backup_profile_revision)
        plan = cap_quantity(plan, original.quantity)
        if plan.risk_usdc > original.risk_usdc:
            plan = cap_quantity(plan, plan.quantity * original.risk_usdc / plan.risk_usdc)
    except AqlaError as error:
        raise ReplayAdmissionRejected(str(error)) from error
    if plan.risk_usdc > original.risk_usdc or plan.quantity > original.quantity or plan.deadline_at > original.deadline_at:
        raise AqlaError("Replanning exceeded the original risk, quantity or deadline")
    plan.exchange = original.exchange
    plan.base_parameters = profile.model_dump(mode="json")
    plan.plan_id = digest({key: value for key, value in plan.model_dump().items() if key != "plan_id"})
    return {**case, "plan": plan.model_dump(mode="json"), "original_entry_events": [], "initial_fills": [],
            "replanning": {"original_plan_hash": digest(case["plan"]), "budget": "original_approved_plan",
                           "entry_path": "counterfactual", "profile_hash": profile_hash(plan.policy, plan.base_parameters)}}


def funding_schedule(case, plan):
    rows = case.get("funding", [])
    previous = -1
    expected_kind = "oracle" if plan.exchange == "hyperliquid" else None
    for row in rows:
        stamp, rate, price = int(row["time"]), Decimal(row["rate"]), Decimal(row["reference_price"])
        if (stamp <= previous or not rate.is_finite() or not price.is_finite() or price <= 0
                or row["reference_kind"] != expected_kind or row["market"] != plan.market.symbol
                or row["network"] != plan.network or row["exchange"] != plan.exchange
                or row["currency"] != plan.market.quote_token or row.get("settled") is not True):
            raise ReplayEvidenceError("Funding needs ordered native settlements with the venue's correct price basis")
        previous = stamp
    return rows


def validate_native_evidence(case, plan):
    """Native provenance alone cannot certify costs, continuity or the active profile."""
    records = case["observations"]
    if len(records) < 5:
        raise ReplayEvidenceError("Native replay has too few executable observations")
    stamps = [row["quote"]["observed_at"] for row in records]
    until_target = plan.policy.waverider_lifetime == "until_target"
    if (abs(stamps[0] - plan.created_at) > 2000 or not until_target and abs(stamps[-1] - plan.deadline_at) > 2000
            or any(not 0 < right - left <= 2000 for left, right in zip(stamps, stamps[1:], strict=False))):
        raise ReplayEvidenceError("Native observations do not continuously cover the original setup window")
    if until_target:
        endpoint = len(plan.take_profits) if plan.policy.waverider_until_tp == "last" else min(plan.policy.waverider_until_tp, len(plan.take_profits))
        target = plan.take_profits[endpoint - 1]
        if not any((Decimal(row["quote"]["bid"]) >= target or Decimal(row["quote"]["stop_reference"]) <= plan.stop_loss)
                   if plan.side == "long" else (Decimal(row["quote"]["ask"]) <= target or Decimal(row["quote"]["stop_reference"]) >= plan.stop_loss)
                   for row in records):
            raise ReplayEvidenceError("Until-target replay needs observations through its TP or stop; the entry deadline is insufficient")
    for record in records:
        try:
            quote = PriceSnapshot.model_validate(record["quote"])
            quote.fresh(as_of=quote.observed_at)
            if quote.stop_reference_kind != "mark":
                raise ReplayEvidenceError("The native stop reference uses another venue's price kind")
        except TemporaryObservation as error:
            raise ReplayEvidenceError("Native book or stop reference exceeds the unchanged two-second freshness limit") from error
    for event in case.get("original_entry_events", case.get("initial_fills", [])):
        if (event.get("fee") is None or event.get("feeToken") != plan.market.quote_token
                or not Decimal(event["fee"]).is_finite() or not stamps[0] <= event["time"] <= stamps[-1]):
            raise ReplayEvidenceError("Original entries need actual fees in the native quote currency and covered time window")
    schedules = case.get("fee_schedule", [])
    if not schedules:
        raise ReplayEvidenceError("Historical account taker fee schedule is missing")
    previous_end = None
    for row in schedules:
        if (row["exchange"] != plan.exchange or row["network"] != plan.network or row["account"] != plan.account
                or row["market"] != plan.market.symbol or row["currency"] != plan.market.quote_token
                or row["liquidity"] != "taker" or not row.get("evidence_hash")
                or row["valid_until"] <= row["valid_from"]
                or previous_end is not None and row["valid_from"] != previous_end):
            raise ReplayEvidenceError("Historical taker fee evidence has a gap, overlap or another account/market")
        if row["source"] == "native_account_snapshot":
            if row["valid_from"] < row["observed_at"] or row["valid_until"] - row["observed_at"] > 60000:
                raise ReplayEvidenceError("A current fee snapshot cannot be backdated into historical fee evidence")
        elif row["source"] != "native_account_fee_history":
            raise ReplayEvidenceError("Unverified or maker-only rates cannot certify account taker fees")
        rate = Decimal(row["rate"])
        if not rate.is_finite() or not 0 <= rate <= Decimal(".1"):
            raise ReplayEvidenceError("Historical taker fee rate is invalid")
        previous_end = row["valid_until"]
    starts = [row["valid_from"] for row in schedules]
    for record, stamp in zip(records, stamps, strict=True):
        index = bisect_right(starts, stamp) - 1
        if index < 0 or stamp >= schedules[index]["valid_until"] or Decimal(record["fee_rate"]) != Decimal(schedules[index]["rate"]):
            raise ReplayEvidenceError("An executable observation has no matching historical taker fee")
    coverage = case.get("funding_coverage", {})
    if (coverage.get("start", stamps[0] + 1) > stamps[0] or coverage.get("end", stamps[-1] - 1) < stamps[-1]
            or coverage.get("source") != "native_settled_funding_history" or not coverage.get("evidence_hash")):
        raise ReplayEvidenceError("Settled funding coverage is not proved for the complete replay window")
    rows = funding_schedule(case, plan)
    if plan.exchange == "hyperliquid":
        # Native settlement timestamps can be a few milliseconds after the hour.
        expected = set(range((stamps[0] // 3600000 + 1), stamps[-1] // 3600000 + 1))
        actual = [row["time"] // 3600000 for row in rows if stamps[0] < row["time"] <= stamps[-1]]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise ReplayEvidenceError("Hourly native funding settlements are missing or duplicated")
    if plan.base_parameters is None:
        raise ReplayEvidenceError("Replay has no explicit base profile binding")
    from .pipelines import Parameters
    base = Parameters.model_validate(plan.base_parameters)
    effective = Parameters.from_policy(plan.policy)
    for field in Parameters.model_fields:
        if field == "management_mode":
            continue
        if field in ("risk_percent", "max_notional_quote") and getattr(effective, field) <= getattr(base, field):
            continue
        if getattr(effective, field) != getattr(base, field):
            raise ReplayEvidenceError("Saved plan differs from its claimed base profile: " + field)
    if plan.entry_execution == "maker_only" or plan.take_profit_execution == "maker_only":
        raise ReplayEvidenceError("Maker execution needs native queue-position evidence; IOC depth is insufficient")


def simulate(case, *, wave):
    plan = TradePlan.model_validate(case["plan"])
    sign = Decimal(1) if plan.side == "long" else Decimal(-1)
    core, flex, core_cost, flex_cost, cash, fees, risk_fees = (Decimal(0) for _ in range(7))
    peak, drawdown, losses, cycles = Decimal(0), Decimal(0), Decimal(0), 0
    entries = plan.entry_legs or [plan]
    completed, entered, pending = set(), set(), None
    state = {"phase": "waiting_rise"}
    stopped, previous = False, -1
    latency = max(1000, int(case["latency_ms"]))
    initial_quote = None
    records = case["observations"]
    curve = []
    original_events = sorted(case.get("original_entry_events", case.get("initial_fills", [])), key=lambda row: (row["time"], str(row.get("tid", ""))))
    original_cursor = 0
    settlements = funding_schedule(case, plan)
    funding_cursor, funding, funding_spent = 0, Decimal(0), Decimal(0)
    target_prices = list(plan.take_profits)
    target_budgets, target_spent = None, [Decimal(0)] * len(target_prices)
    stop = plan.stop_loss
    reached_end = False
    venue_average, original_quantity, original_cost = Decimal(0), Decimal(0), Decimal(0)
    if len(records) < 5:
        raise AqlaError("Replay case has too few executable observations")
    def fill(quote, *, buy, limit, quantity, stamp):
        amount, _ = quote.executable(buy=buy, limit=limit, quantity=quantity * 2, as_of=stamp)
        # Half the displayed depth is reserved for competing flow. Charge the worst bound.
        amount = quantity_grid(min(quantity, amount / 2), plan.market)
        return amount, limit
    for record in records:
        quote = PriceSnapshot.model_validate(record["quote"])
        stamp = quote.observed_at
        if stamp <= previous or quote.market != plan.market.symbol:
            raise AqlaError("Replay observations must be strictly ordered and market-bound")
        quote.fresh(as_of=stamp)
        previous = stamp
        if initial_quote is None:
            initial_quote = stamp
        if record.get("fee_rate") is None or record.get("atr") is None:
            raise AqlaError("Replay account fee or volatility evidence is unavailable")
        rate, volatility = Decimal(record["fee_rate"]), Decimal(record["atr"])
        if not rate.is_finite() or not 0 <= rate <= Decimal(".1") or not volatility.is_finite() or volatility <= 0:
            raise AqlaError("Replay lacks finite observed account fees/volatility")
        if not quote.bids or not quote.asks:
            raise AqlaError("Candles or best quotes alone cannot qualify Waverider")
        own = core + flex
        grew = False
        while (original_cursor < len(original_events) and original_events[original_cursor]["time"] <= stamp
               or funding_cursor < len(settlements) and settlements[funding_cursor]["time"] <= stamp):
            entry_time = original_events[original_cursor]["time"] if original_cursor < len(original_events) else float("inf")
            funding_time = settlements[funding_cursor]["time"] if funding_cursor < len(settlements) else float("inf")
            if entry_time == funding_time:
                raise ReplayEvidenceError("Entry/funding ordering at the same timestamp is not proved")
            if funding_time < entry_time:
                event = settlements[funding_cursor]
                amount = -(core + flex) * sign * Decimal(event["reference_price"]) * Decimal(event["rate"])
                cash += amount
                funding += amount
                funding_spent += max(Decimal(0), -amount)
                funding_cursor += 1
                continue
            event = original_events[original_cursor]
            original_cursor += 1
            if event.get("kind") != "entry" or event.get("entry_role", "original") != "original":
                raise AqlaError("Only proved original entries may seed both comparison paths")
            amount, price = Decimal(event["sz"]), Decimal(event["px"])
            if amount <= 0 or price <= 0:
                raise AqlaError("Original entry evidence has invalid size/price")
            if event.get("fee") is not None and event.get("feeToken") not in (None, plan.market.quote_token):
                raise AqlaError("Original entry fee currency differs from the comparison currency")
            part = amount * plan.policy.waverider_flex_percent / 100 if wave else Decimal(0)
            venue_average = (venue_average * (core + flex) + amount * price) / (core + flex + amount)
            core, flex = core + amount - part, flex + part
            core_cost, flex_cost = core_cost + (amount - part) * price, flex_cost + part * price
            cash -= amount * price * sign
            paid = Decimal(event["fee"]) if event.get("fee") is not None else amount * price * rate
            fees += paid
            risk_fees += max(Decimal(0), paid)
            source_index = event.get("entry_leg", 0)
            entered.update(index for index, leg in enumerate(entries) if (leg.index if plan.entry_legs else 0) == source_index)
            state["last_original_fill"] = event["time"]
            original_quantity, original_cost, grew = original_quantity + amount, original_cost + amount * price, True
        target_end = len(plan.take_profits) if plan.policy.waverider_until_tp == "last" else min(plan.policy.waverider_until_tp, len(plan.take_profits))
        target_one = quote.bid >= plan.take_profits[0] if sign == 1 else quote.ask <= plan.take_profits[0]
        within_lifetime = stamp < plan.deadline_at or plan.policy.waverider_lifetime == "until_target"
        if not original_events and not target_one and stamp < plan.deadline_at and not stopped:
            for index, leg in enumerate(entries):
                if index in entered or index and index - 1 not in entered:
                    continue
                if stamp < initial_quote + latency or index and stamp < state.get("last_original_fill", stamp) + latency:
                    continue
                kind = leg.kind if plan.entry_legs else plan.entry_kind
                limit = leg.limit if plan.entry_legs else plan.entry_limit
                size = leg.quantity
                if kind == "market" or (quote.ask <= limit if sign == 1 else quote.bid >= limit):
                    view = leg_view(plan, {"index": leg.index, "quantity": str(leg.quantity), "entry_gate": {"origin_mid": str(records[0]["quote"]["mid"])}}) if plan.entry_legs else plan
                    if view.entry_timing == "deferred" and gate_state(view, quote, as_of=stamp) == "missed":
                        stopped = True
                        continue
                    try:
                        validate_entry_prices(view, quote, stop=stop, targets=target_prices, as_of=stamp)
                    except AqlaError:
                        continue
                    amount, price = fill(quote, buy=sign == 1, limit=limit, quantity=size, stamp=stamp)
                    if amount <= 0:
                        continue
                    part = amount * plan.policy.waverider_flex_percent / 100 if wave else Decimal(0)
                    venue_average = (venue_average * (core + flex) + amount * price) / (core + flex + amount)
                    core, flex = core + amount - part, flex + part
                    core_cost, flex_cost = core_cost + (amount - part) * price, flex_cost + part * price
                    cash -= amount * price * sign
                    fees += amount * price * rate
                    risk_fees += amount * price * rate
                    entered.add(index)
                    state["last_original_fill"] = stamp
                    original_quantity, original_cost, grew = original_quantity + amount, original_cost + amount * price, True
                    break
        own = core + flex
        if original_quantity:
            reference = original_cost / original_quantity
            if plan.source_tp and plan.source_tp["unit"] == "roe_percent":
                from .percent_targets import prices
                target_prices = prices(reference, plan.side, plan.market, "roe_percent", plan.source_tp["values"], plan.source_tp["margin_leverage"])
            elif "take_profits" in plan.backup_fields:
                _, target_prices = backup_prices(reference, plan.side, plan.market,
                    percentages=plan.backup_values.fallback_take_profit_percentages[:len(target_prices)],
                    take_profit_unit=plan.backup_values.fallback_take_profit_unit, leverage=plan.effective_leverage)
        target_one = quote.bid >= target_prices[0] if sign == 1 else quote.ask <= target_prices[0]
        endpoint = target_prices[target_end - 1]
        reached_end = reached_end or (quote.bid >= endpoint if sign == 1 else quote.ask <= endpoint)
        active = within_lifetime and not stopped and not reached_end
        if own:
            allocations, completed = target_allocations(plan, own, target_budgets, target_spent, completed)
            if allocations:
                target_budgets = [amount + spent for amount, spent in zip(allocations, target_spent, strict=True)]
            if pending and pending["action"] == "reduce" and not active:
                pending = None
            stop_hit = quote.stop_reference <= stop if sign == 1 else quote.stop_reference >= stop
            indices = [i for i, target in enumerate(target_prices) if i not in completed
                       and (quote.bid >= target if sign == 1 else quote.ask <= target)]
            if stop_hit or indices and not (pending and pending["action"] == "reduce"):
                index = indices[0] if indices else None
                amount = own if stop_hit else min(own, target_budgets[index] - target_spent[index])
                limit = slippage_price(quote.bid if sign == 1 else quote.ask, plan.policy.max_slippage_percent, plan.market, buy=sign == -1)
                filled, price = fill(quote, buy=sign == -1, limit=limit, quantity=amount, stamp=stamp)
                if filled:
                    ratio = filled / own
                    basis = (core_cost + flex_cost) / own
                    losses += max(Decimal(0), -(price - basis) * filled * sign)
                    core, flex = core * (1 - ratio), flex * (1 - ratio)
                    core_cost, flex_cost = core_cost * (1 - ratio), flex_cost * (1 - ratio)
                    cash += filled * price * sign
                    fees += filled * price * rate
                    risk_fees += filled * price * rate
                    if index is not None:
                        target_spent[index] += filled
                        if target_spent[index] >= target_budgets[index]:
                            completed.add(index)
                    if plan.policy.stop_management == "tp_ladder":
                        if 3 in completed and all(i in completed for i in range(4)):
                            proposed = target_prices[1]
                        elif 2 in completed and all(i in completed for i in range(3)):
                            proposed = target_prices[0]
                        elif 0 in completed:
                            proposed = venue_average
                        else:
                            proposed = stop
                        stop = max(stop, proposed) if sign == 1 else min(stop, proposed)
                    elif plan.policy.break_even_after_tp and all(i in completed for i in range(plan.policy.break_even_after_tp)):
                        stop = max(stop, venue_average) if sign == 1 else min(stop, venue_average)
                    if stop_hit:
                        stopped = True
                    pending = None
        if pending and stamp >= pending["at"] + latency and active:
            action = pending["action"]
            if action == "reduce" and flex:
                basis = flex_cost / flex
                limit = slippage_price(quote.bid if sign == 1 else quote.ask, plan.policy.max_slippage_percent, plan.market, buy=sign == -1)
                if (limit - basis) * sign > pending["cost"]:
                    retained, _ = target_allocations(plan, core, target_budgets, target_spent, completed)
                    feasible = retained and all(size <= 0 or size >= plan.market.min_quantity and size * target_prices[i] >= plan.market.min_notional for i, size in enumerate(retained))
                    if not feasible:
                        pending = None
                        state = reset_phase(state, "waiting_rise")
                        continue
                    amount, price = fill(quote, buy=sign == -1, limit=limit, quantity=flex, stamp=stamp)
                    if amount:
                        flex, flex_cost = flex - amount, flex_cost - amount * basis
                        cash += amount * price * sign
                        fees += amount * price * rate
                        risk_fees += amount * price * rate
                        state = {**reset_phase(state, "waiting_rebound"), "sale_price": str(price), "sold_quantity": str(amount)}
            elif action == "reenter":
                limit = slippage_price(quote.ask if sign == 1 else quote.bid, plan.policy.max_slippage_percent, plan.market, buy=sign == 1)
                size = Decimal(state.get("sold_quantity", "0"))
                reserved_quantity = sum(leg.quantity for i, leg in enumerate(entries) if i not in entered and stamp < plan.deadline_at and not target_one)
                reserved_risk = sum((leg.quantity * (abs((leg.limit if plan.entry_legs else plan.entry_limit) - stop)
                    + (leg.limit if plan.entry_legs else plan.entry_limit) * plan.policy.cost_reserve_bps / 10000)
                    for i, leg in enumerate(entries) if i not in entered and stamp < plan.deadline_at and not target_one), Decimal(0))
                open_reserve = (core_cost + flex_cost) * plan.policy.cost_reserve_bps / 10000
                remaining_risk = plan.risk_usdc - losses - risk_fees - funding_spent - open_reserve - max(Decimal(0), (core_cost - core * stop) * sign) - max(Decimal(0), (flex_cost - flex * stop) * sign) - reserved_risk
                maximum = quantity_grid(max(Decimal(0), remaining_risk) / (abs(limit - stop) + limit * plan.policy.cost_reserve_bps / 10000), plan.market)
                size = min(size, maximum, plan.quantity - core - flex - reserved_quantity)
                if size > 0 and (Decimal(state["sale_price"]) - limit) * sign > pending["cost"]:
                    amount, price = fill(quote, buy=sign == 1, limit=limit, quantity=size, stamp=stamp)
                    if amount:
                        venue_average = (venue_average * (core + flex) + amount * price) / (core + flex + amount)
                        flex, flex_cost = flex + amount, flex_cost + amount * price
                        cash -= amount * price * sign
                        fees += amount * price * rate
                        risk_fees += amount * price * rate
                        cycles += 1
                        grew = True
                        state = reset_phase(state, "waiting_rise")
            pending = None
        if wave and active and core + flex and not pending:
            phase = state.get("phase", "waiting_rise")
            size = flex if phase != "waiting_rebound" else Decimal(state.get("sold_quantity", "0"))
            if size > 0:
                price = (quote.ask if sign == 1 else quote.bid) if phase == "waiting_rebound" else (quote.bid if sign == 1 else quote.ask)
                basis = flex_cost / flex if flex else Decimal(state.get("sale_price", str(price)))
                cost = price * (rate * 2 + plan.policy.max_slippage_percent / 50)
                state, action = advance(state, side=plan.side, price=price, basis=basis, volatility=volatility,
                    cycle_cost=cost, tick=plan.market.price_tick or Decimal(".0001"), stamp=stamp)
                if action:
                    pending = {"action": action, "at": stamp, "cost": cost}
        own = core + flex
        if own > plan.quantity or core_cost + flex_cost > plan.policy.max_notional_usdc:
            raise AqlaError("Replay exceeded original quantity/notional constraints")
        stop_risk = max(Decimal(0), (core_cost - core * stop) * sign) + max(Decimal(0), (flex_cost - flex * stop) * sign)
        if grew and losses + risk_fees + funding_spent + stop_risk > plan.risk_usdc:
            raise AqlaError("Replay exceeded the original lifetime loss budget")
        mark = quote.bid if sign == 1 else quote.ask
        equity = cash + own * mark * sign - fees - own * mark * rate
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append((stamp, str(equity)))
    return {"net_pnl": str(equity), "max_drawdown": str(drawdown), "waves": cycles, "fees": str(fees),
            "funding": str(funding), "funding_spent": str(funding_spent),
            "original_entries_filled": sorted(entered), "original_quantity": str(original_quantity), "remaining_quantity": str(own),
            "valuation": "conservative_mark_to_market", "equity": curve}


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
            rows.append({"id": digest(case["family"])[:16], "cohort": f"{plan.exchange}:{plan.network}:{plan.market.quote_token}",
                         "baseline": simulate(case, wave=False), "waverider": simulate(case, wave=True)})
        except ReplayAdmissionRejected as error:
            admission_rejections += 1
            failures.append({"id": digest(case["family"])[:16], "kind": "admission_rejected", "reason": str(error)})
        except ReplayEvidenceError as error:
            data_errors += 1
            failures.append({"id": digest(case["family"])[:16], "kind": "missing_evidence", "reason": str(error)})
        except (ValueError, KeyError, ArithmeticError, TypeError) as error:
            errors += 1
            failures.append({"id": digest(case["family"])[:16], "kind": "safety_or_invalid_input", "error_type": type(error).__name__})
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
            summary.update({("baseline_" if mode == "baseline" else "") + "net_pnl": str(equity),
                            ("baseline_" if mode == "baseline" else "") + "max_drawdown": str(maximum)})
        cohorts[cohort] = summary
    aggregate = require_first(iter(cohorts.values())) if len(cohorts) == 1 else {key: None for key in ("net_pnl", "baseline_net_pnl", "max_drawdown", "baseline_max_drawdown")}
    report = {"version": VERSION, "software_hash": software_hash(), "profile_hash": require_first(iter(profiles)) if len(profiles) == 1 else "mixed",
        "cost_model": COST_MODEL, "cases_requested": len(families), "cases": len(rows), "critical_errors": errors,
        "data_errors": data_errors, "admission_rejections": admission_rejections,
        "failures": failures, "waves": sum(row["waverider"]["waves"] for row in rows),
        **aggregate, "cohorts": cohorts, "aggregate_monetary_totals_unavailable": len(cohorts) != 1,
        "allowed_venues": sorted({cohort.split(":", 1)[0] for cohort in cohorts}),
        "data_kind": "executable_depth" if dataset.get("provenance") == "recorded_native_depth" else "synthetic",
        "holdout_hash": digest(dataset) if dataset.get("partition") == "holdout" else "", "results": rows}
    return report


def export_replay(path, *, maximum=30):
    """Reserve the earliest unseen families; never select cases by their result."""
    cases, families, profile = [], set(), None
    with sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True) as db:
        setups = db.execute("SELECT s.setup,s.payload,MIN(o.stamp) FROM setups s JOIN observations o ON s.setup=o.setup GROUP BY s.setup ORDER BY MIN(o.stamp),s.setup").fetchall()
        for identity, raw, _ in setups:
            meta = json.loads(raw)
            if meta.get("software_hash") != software_hash() or meta["family"] in families:
                continue
            plan = TradePlan.model_validate(meta["plan"])
            current = profile_hash(plan.policy, plan.base_parameters)
            if profile is not None and profile != current:
                continue
            profile = current
            records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM observations WHERE setup=? ORDER BY stamp", (identity,))]
            observations = [{"quote": row["quote"], "fee_rate": row["facts"].get("fee_rate"), "atr": row["facts"].get("atr"),
                             "facts": row["facts"]}
                            for row in records if "stop_reference" in row["quote"]]
            entries = [json.loads(row[0]) for row in db.execute("SELECT payload FROM original_fills WHERE setup=?", (identity,))]
            fees = {row["facts"]["fee_evidence"]["valid_from"]: dict(row["facts"]["fee_evidence"])
                    for row in records if row["facts"].get("fee_evidence")}
            fee_schedule = [fees[key] for key in sorted(fees)]
            for left, right in zip(fee_schedule, fee_schedule[1:], strict=False):
                left["valid_until"] = min(left["valid_until"], right["valid_from"])
            cases.append({"id": identity, "family": meta["family"], "plan": meta["plan"], "latency_ms": 1000,
                          "original_entry_events": entries, "observations": observations, "fee_schedule": fee_schedule})
            families.add(meta["family"])
            if len(cases) >= maximum:
                break
    return {"partition": "holdout", "provenance": "recorded_native_depth", "development_families": [], "cases": cases}


def main():
    parser = argparse.ArgumentParser(description="Offline Waverider versus Standard, with no exchange transport")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dataset", type=Path)
    inputs.add_argument("--from-replay", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--signing-key-file", type=Path)
    args = parser.parse_args()
    dataset = export_replay(args.from_replay) if args.from_replay else json.loads(args.dataset.read_text(encoding="utf-8"))
    report = evaluate(dataset)
    from .decision_evaluation import write_private_json
    write_private_json(args.output, report)
    if args.qualification:
        if args.signing_key_file is None:
            raise SystemExit("Qualification needs an explicit private integration key file")
        from .waverider import MIN_CASES, MIN_WAVES
        passed = (report["cases"] >= MIN_CASES and report["cases"] == report["cases_requested"]
            and report["waves"] >= MIN_WAVES and not report["critical_errors"] and not report["data_errors"]
            and report["data_kind"] == "executable_depth" and report["holdout_hash"] and report["profile_hash"] != "mixed"
            and all(Decimal(group["net_pnl"]) >= Decimal(group["baseline_net_pnl"])
                    and Decimal(group["max_drawdown"]) <= Decimal(group["baseline_max_drawdown"]) for group in report["cohorts"].values()))
        if not passed:
            raise SystemExit("Qualification not passed; live permission was not written")
        payload = {key: value for key, value in report.items() if key != "results"}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(args.signing_key_file.read_bytes().strip(), body, hashlib.sha256).hexdigest()
        write_private_json(args.qualification, payload)
    print(json.dumps({key: report[key] for key in ("cases", "critical_errors", "waves", "net_pnl", "baseline_net_pnl", "data_kind")}))


if __name__ == "__main__":
    main()
