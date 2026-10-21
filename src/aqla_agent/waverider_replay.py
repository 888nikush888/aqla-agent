"""Offline executable-depth comparison. This module never constructs a broker."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, TradePlan, digest
from aqla_hyperliquid_mcp.rules import backup_prices, quantity_grid, slippage_price

from .entry import PriceSnapshot, gate_state, validate_entry_prices
from .managed_entries import leg_view, target_allocations
from .waverider import VERSION, advance, profile_hash, reset_phase, software_hash


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
    target_prices = list(plan.take_profits)
    target_budgets, target_spent = None, [Decimal(0)] * len(target_prices)
    stop = plan.stop_loss
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
        if original_events:
            while original_cursor < len(original_events) and original_events[original_cursor]["time"] <= stamp:
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
        endpoint = plan.take_profits[target_end - 1]
        reached_end = quote.bid >= endpoint if sign == 1 else quote.ask <= endpoint
        target_one = quote.bid >= plan.take_profits[0] if sign == 1 else quote.ask <= plan.take_profits[0]
        active = stamp < plan.deadline_at and not stopped and not reached_end
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
                    if amount < size:
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
        active = stamp < plan.deadline_at and not stopped and not (quote.bid >= endpoint if sign == 1 else quote.ask <= endpoint)
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
                remaining_risk = plan.risk_usdc - losses - risk_fees - max(Decimal(0), (core_cost - core * stop) * sign) - max(Decimal(0), (flex_cost - flex * stop) * sign) - reserved_risk
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
        if grew and losses + risk_fees + stop_risk > plan.risk_usdc:
            raise AqlaError("Replay exceeded the original lifetime loss budget")
        mark = quote.bid if sign == 1 else quote.ask
        equity = cash + own * mark * sign - fees - own * mark * rate
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append((stamp, str(equity)))
    return {"net_pnl": str(equity), "max_drawdown": str(drawdown), "waves": cycles, "valuation": "conservative_mark_to_market", "equity": curve}


def evaluate(dataset):
    families = [case["family"] for case in dataset["cases"]]
    if len(set(families)) != len(families) or set(families) & set(dataset.get("development_families", [])):
        raise AqlaError("Development/holdout families overlap or a setup is counted twice")
    rows, errors = [], 0
    profiles = set()
    for case in dataset["cases"]:
        plan = TradePlan.model_validate(case["plan"])
        profiles.add(profile_hash(plan.policy, plan.base_parameters))
        try:
            plan = TradePlan.model_validate(case["plan"])
            rows.append({"id": digest(case["family"])[:16], "cohort": f"{plan.exchange}:{plan.network}:{plan.market.quote_token}",
                         "baseline": simulate(case, wave=False), "waverider": simulate(case, wave=True)})
        except (AqlaError, ValueError, KeyError, ArithmeticError, TypeError):
            errors += 1
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
    aggregate = next(iter(cohorts.values())) if len(cohorts) == 1 else {key: None for key in ("net_pnl", "baseline_net_pnl", "max_drawdown", "baseline_max_drawdown")}
    report = {"version": VERSION, "software_hash": software_hash(), "profile_hash": next(iter(profiles)) if len(profiles) == 1 else "mixed",
        "cases": len(rows), "critical_errors": errors, "waves": sum(row["waverider"]["waves"] for row in rows),
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
            observations = [{"quote": row["quote"], "fee_rate": row["facts"].get("fee_rate"), "atr": row["facts"].get("atr")}
                            for row in records if "stop_reference" in row["quote"]]
            entries = [json.loads(row[0]) for row in db.execute("SELECT payload FROM original_fills WHERE setup=?", (identity,))]
            cases.append({"id": identity, "family": meta["family"], "plan": meta["plan"], "latency_ms": 1000,
                          "original_entry_events": entries, "observations": observations})
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
        passed = (report["cases"] >= MIN_CASES and report["waves"] >= MIN_WAVES and not report["critical_errors"]
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
