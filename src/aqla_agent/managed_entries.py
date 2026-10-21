"""Two source entries share one owner, reservation and protection lifecycle."""
from __future__ import annotations

from decimal import Decimal

from aqla_hyperliquid_mcp.models import (
    TERMINAL_ORDER,
    AqlaError,
    EntryLegPlan,
    ExchangeMinimum,
    SourceEntryLeg,
    TradePlan,
    digest,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import (
    backup_prices,
    entry_prices,
    executable_targets,
    make_plan,
    price_grid,
    quantity_grid,
    selected_targets,
    split_targets,
)

from .entry import execution_limit, gate_state, validate_entry_prices
from .observations import TemporaryObservation, read_observation, retry_due, retry_marker


def enabled(plan):
    return bool(plan.entry_legs or plan.policy.entry_range_choice == "split" or plan.policy.management_mode == "waverider")


def make_entry_plan(signal, market, account, policy, revision, source_time, binding,
                    reserved_risk, stamp, principal, backup, backup_revision, maker_price):
    if signal.kind != "new_signal":
        raise AqlaError("Only a new setup can allocate two original entries")
    legs = signal.entry_legs if signal.entry == "split" else [
        SourceEntryLeg(kind="limit", price=value, timing=signal.entry_timing)
        for value in signal.entry_prices]
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
        single = signal.model_copy(update={"entry": leg.kind, "entry_legs": [],
            "entry_prices": [leg.price] if leg.price is not None else [], "entry_timing": leg.timing})
        selected = entry_prices(single, market, policy, maker_price=maker_price)
        selections.append((index, leg, selected))
    if signal.entry == "range" and selections[0][2].price == selections[1][2].price:
        raise AqlaError("Source zone collapses to one native tick; two distinct entries cannot be invented")
    # Size conservatively at the worst allowed entry. Never use averaging to enlarge risk.
    worst = (max if signal.side == "long" else min)(row[2].limit for row in selections)
    anchor = signal.model_copy(update={"entry": "limit", "entry_prices": [worst], "entry_legs": [], "entry_timing": "standard"})
    base = make_plan(anchor, market, account, policy, revision, source_time, binding, reserved_risk,
                     stamp, principal, backup, backup_revision, maker_price, _managed=True)
    weights = [leg.weight_percent or Decimal(50) for leg in legs]
    quantities = [quantity_grid(base.quantity * weights[0] / 100, base.market)]
    quantities.append(base.quantity - quantities[0])
    planned, count = [], len(base.take_profits)
    for (index, leg, selected), size in zip(selections, quantities, strict=True):
        if size <= 0 or size < base.market.min_quantity or size * selected.limit < base.market.min_notional:
            raise ExchangeMinimum("A source entry leg is below the exchange minimum; neither leg may be dropped or enlarged")
        if not (base.stop_loss < selected.price < min(base.take_profits) if base.side == "long"
                else base.stop_loss > selected.price > max(base.take_profits)):
            raise AqlaError("Both source entries must have valid common SL/TP structure")
        sizes = split_targets(size, len(base.take_profits), policy, base.market)
        if any(amount < base.market.min_quantity or amount * price < base.market.min_notional for amount, price in zip(sizes, base.take_profits, strict=True)):
            raise ExchangeMinimum("A TP leg on an individual entry is below the exchange minimum")
        planned.append(EntryLegPlan(index=index, kind=selected.kind, price=selected.price,
            limit=selected.limit, quantity=size, timing=leg.timing))
    # Market intent first; otherwise the near limit fills before the far limit.
    planned.sort(key=lambda leg: (leg.kind != "market", -leg.limit if base.side == "long" else leg.limit, leg.index))
    values = base.model_dump()
    if backup_entry and "entry" not in base.backup_fields:
        values.update(backup_fields=[*base.backup_fields, "entry"], backup_values=backup.model_copy(deep=True), backup_profile_revision=backup_revision)
    values.update(entry_legs=planned, entry_price=sum(leg.price * leg.quantity for leg in planned) / base.quantity,
                  take_profits=base.take_profits[:count])
    values["tp_quantities"] = executable_targets(base.quantity, values["take_profits"], policy, base.market)[1]
    if base.source_tp:
        values["source_tp"] = {**base.source_tp, "values": base.source_tp["values"][:count],
                               "preview_prices": base.source_tp["preview_prices"][:count]}
    values.pop("plan_id")
    return TradePlan(plan_id=digest(values), **values)


def cap_quantity(plan, ceiling):
    quantity = quantity_grid(min(plan.quantity, Decimal(ceiling)), plan.market)
    if quantity == plan.quantity:
        return plan
    if quantity <= 0 or quantity < plan.market.min_quantity or quantity * plan.entry_limit < plan.market.min_notional:
        raise ExchangeMinimum("Rerouted quantity is below the exchange minimum")
    values = plan.model_dump()
    values.update(quantity=quantity, risk_usdc=plan.risk_usdc * quantity / plan.quantity,
                  tp_quantities=split_targets(quantity, len(plan.take_profits), plan.policy, plan.market))
    if plan.entry_legs:
        first = quantity_grid(quantity * plan.entry_legs[0].quantity / plan.quantity, plan.market)
        legs = [leg.model_copy(update={"quantity": size}) for leg, size in zip(plan.entry_legs, [first, quantity - first], strict=True)]
        if any(leg.quantity <= 0 or leg.quantity < plan.market.min_quantity or leg.quantity * leg.limit < plan.market.min_notional for leg in legs):
            raise ExchangeMinimum("Rerouted source entry leg is below the exchange minimum")
        for leg in legs:
            sizes = split_targets(leg.quantity, len(plan.take_profits), plan.policy, plan.market)
            if any(size < plan.market.min_quantity or size * price < plan.market.min_notional for size, price in zip(sizes, plan.take_profits, strict=True)):
                raise ExchangeMinimum("Rerouted TP is below the exchange minimum")
        values["entry_legs"] = legs
    values.pop("plan_id")
    return TradePlan(plan_id=digest(values), **values)


def entry_orders(trade):
    """All identities, including retired generations, remain fill owners forever."""
    return trade.get("entry_orders") or [{"cloid": trade["entry_cloid"],
        "operation_id": trade["entry_operation_id"], "kind": "original", "index": 0,
        "quantity": trade["plan"]["quantity"]}]


def all_order_rows(trade):
    if trade.get("lifecycle_version") == 2:
        entries = [{"kind": "entry", "cloid": row["cloid"], "quantity": row["quantity"],
                    "price": row["limit"], "index": row["index"], "retired": bool(row.get("not_sent"))}
                   for row in trade["entry_orders"]]
    else:
        entries = [{"kind": "entry", "cloid": trade["entry_cloid"], "quantity": trade["plan"].get("quantity"), "price": trade["plan"].get("entry_limit")}]
    return [*entries, *trade.get("orders", []), *trade.get("exit_orders", [])]


def entry_final(trade):
    rows = entry_orders(trade)
    observations = trade.get("order_observations", {})
    return (all(row.get("not_sent") or observations.get(row["cloid"], {}).get("status") in TERMINAL_ORDER for row in rows)
            and all(row.get("not_sent") or row.get("confirmed_filled") is not None for row in rows)
            and not any(row.get("phase") == "pending" for row in rows))


def original_fills(trade):
    wave_oids = {trade.get("order_observations", {}).get(row["cloid"], {}).get("oid")
                 for row in entry_orders(trade) if row.get("kind") == "waverider"}
    return [row for row in trade.get("fill_events", {}).values() if row["kind"] == "entry" and row.get("entry_role") != "waverider" and row["oid"] not in wave_oids]


def source_reference(trade):
    rows = original_fills(trade)
    quantity = sum((Decimal(row["sz"]) for row in rows), Decimal(0))
    return sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in rows), Decimal(0)) / quantity if quantity else None


def pending_quantity(trade):
    observations = trade.get("order_observations", {})
    return sum((Decimal(row["quantity"]) - Decimal(row.get("confirmed_filled") or 0)
                for row in entry_orders(trade) if not row.get("not_sent")
                and observations.get(row["cloid"], {}).get("status") not in TERMINAL_ORDER), Decimal(0))


def leg_view(plan, row):
    leg = next((leg for leg in plan.entry_legs if leg.index == row["index"]), None)
    return plan.model_copy(update={"entry_legs": [], "entry_kind": row.get("entry_kind", leg.kind if leg else plan.entry_kind),
        "entry_price": Decimal(row.get("price", str(leg.price if leg else plan.entry_price))),
        "entry_limit": Decimal(row.get("limit", str(leg.limit if leg else plan.entry_limit))),
        "quantity": Decimal(row["quantity"]), "entry_timing": row.get("timing", leg.timing if leg else plan.entry_timing),
        "entry_gate": row.get("entry_gate", plan.entry_gate)})


def initialize_trade(trade, plan, quote, exchange):
    legs = plan.entry_legs or [EntryLegPlan(index=0, kind=plan.entry_kind, price=plan.entry_price,
        limit=plan.entry_limit, quantity=plan.quantity, timing=plan.entry_timing)]
    rows = []
    for position, leg in enumerate(legs):
        operation = trade["entry_operation_id"] if position == 0 else f"entry:{plan.trade_id}:leg:{leg.index}"
        origin = quote.mid if quote else plan.market.mid
        rows.append({"kind": "original", "index": leg.index, "quantity": str(leg.quantity),
            "price": str(leg.price), "limit": str(leg.limit), "entry_kind": leg.kind, "timing": leg.timing,
            "operation_id": operation, "cloid": exchange.cloid(operation), "phase": "pending",
            "entry_gate": {"origin_mid": str(origin)}})
    trade.update(lifecycle_version=2, entry_orders=rows, entry_cloid=rows[0]["cloid"],
                 entry_operation_id=rows[0]["operation_id"], status="waiting_entry" if rows[0]["timing"] == "deferred" else "preparing", entries_disabled=False,
                 original_risk_budget=str(plan.risk_usdc), original_quantity=str(plan.quantity))
    if plan.policy.management_mode == "waverider":
        trade["waverider"] = {"phase": "warming_up", "cycle": 0, "shadow": True, "finished": False}


def patch_row(engine, trade_id, cloid, **values):
    def apply(data):
        row = next(row for row in data["trades"][trade_id]["entry_orders"] if row["cloid"] == cloid)
        row.update(values)
    engine.state.update(apply)


async def cancel(engine, trade_id, rows=None, *, permanent=True):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(engine.state.snapshot(), plan)
    if permanent:
        engine._patch(trade_id, entries_disabled=True)
        if trade.get("waverider"):
            engine._patch(trade_id, waverider={**trade["waverider"], "finished": True, "phase": "finished"})
    for row in rows if rows is not None else entry_orders(trade):
        if row.get("not_sent"):
            continue
        if row.get("phase") == "terminal" and trade.get("order_observations", {}).get(row["cloid"], {}).get("status") in TERMINAL_ORDER:
            continue
        operation = engine.state.snapshot()["operations"].get(row["operation_id"])
        if operation is None and row["phase"] == "pending" or operation and operation.get("outcome") == "not_sent":
            patch_row(engine, trade_id, row["cloid"], not_sent=True, confirmed_filled="0", phase="terminal")
            continue
        status = await engine.order_status(row["cloid"])
        if status.get("order", {}).get("status") in TERMINAL_ORDER:
            continue
        if status.get("status") == "unknownOid":
            raise AqlaError("Entry outcome is unknown; its identity must be reconciled before any replacement")
        await engine.exchange.mutate("cancel-managed:" + row["operation_id"],
            {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": row["cloid"]}]})
        status = await engine.order_status(row["cloid"])
        if status.get("order", {}).get("status") not in TERMINAL_ORDER:
            raise AqlaError("Entry remainder termination is not proved; preserve existing protection")
    return {"managed_entries_cancelled": True}


def protected(engine, trade, status_rows):
    plan = TradePlan.model_validate(trade["plan"])
    size = Decimal(trade["remaining_quantity"])
    covered = Decimal(0)
    seen = set()
    for order, raw in status_rows:
        native = raw.get("order", {}).get("order", {})
        oid = native.get("oid")
        if oid is None or oid in seen or order.get("retired"):
            continue
        check = plan.model_copy(update={"entry_execution": "standard"})
        if native.get("cloid") == order["cloid"] and raw.get("order", {}).get("status") == "open" and engine._stop_matches(raw, check, Decimal(0), Decimal(trade["stop_price"])):
            # Maker legacy checks exact size; a managed leg owns an explicit size.
            covered += Decimal(native.get("sz") or 0)
            seen.add(oid)
    return covered >= size and size > 0


def finalize_prices(engine, trade_id):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    reference = source_reference(trade)
    if reference is None:
        return False
    source = trade.get("source_tp_override", plan.source_tp)
    fingerprint = digest(original_fills(trade))
    if trade.get("source_reference_fingerprint") == fingerprint:
        return True
    stop, targets = Decimal(trade["stop_price"]), list(map(Decimal, trade["targets"]))
    if plan.backup_values:
        derived_stop, derived_targets = backup_prices(reference, plan.side, plan.market,
            plan.backup_values.fallback_stop_loss_percent if "stop_loss" in plan.backup_fields else None,
            selected_targets(plan.backup_values.fallback_take_profit_percentages, plan.policy)[:len(targets)] if "take_profits" in plan.backup_fields else (),
            stop_unit=plan.backup_values.fallback_stop_loss_unit, take_profit_unit=plan.backup_values.fallback_take_profit_unit,
            leverage=plan.effective_leverage)
        if derived_stop is not None and (derived_stop > stop if plan.side == "long" else derived_stop < stop):
            # A stop change needs the ordinary confirmed replacement protocol.
            raise AqlaError("Actual source fills require a tighter Backup stop before more exposure")
        if "take_profits" in plan.backup_fields:
            targets = derived_targets
    if source and source["unit"] != "price":
        from .percent_targets import prices
        targets = prices(reference if source["unit"] == "roe_percent" else source.get("reference_entry") or reference, plan.side, plan.market, source["unit"],
            source["values"], source.get("margin_leverage") if source["unit"] == "roe_percent" else source.get("signal_leverage"))
    elif "source_tp_override" in trade:
        targets = list(map(Decimal, trade["targets"]))
    def save(data):
        row = data["trades"][trade_id]
        row.setdefault("entry_reference_history", {})[fingerprint] = {"reference_entry": str(reference),
            "stop_loss": str(stop), "take_profits": [str(value) for value in targets]}
        row.update(source_reference_fingerprint=fingerprint, source_entry_vwap=str(reference),
                   targets=[str(value) for value in targets])
        if set(plan.backup_fields) & {"stop_loss", "take_profits"}:
            row["backup_finalization"] = {"phase": "applied", "reference_entry": str(reference),
                "filled_quantity": str(sum((Decimal(fill["sz"]) for fill in original_fills(row)), Decimal(0))),
                "stop_loss": str(stop), "take_profits": [str(value) for value in targets], "risk": str(plan.risk_usdc)}
    engine.state.update(save)
    return True


def position_basis(trade):
    """Chronological average-cost ledger; turnover is never current exposure."""
    quantity, cost, realized, fees = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    direction = Decimal(1) if trade["plan"]["side"] == "long" else Decimal(-1)
    for row in sorted(trade.get("fill_events", {}).values(), key=lambda row: (row["time"], str(row.get("tid")), str(row["oid"]))):
        size, price = Decimal(row["sz"]), Decimal(row["px"])
        if row.get("fee") is not None:
            fees += Decimal(row["fee"])
        if row["kind"] == "entry":
            quantity, cost = quantity + size, cost + size * price
        else:
            if size > quantity or quantity <= 0:
                raise AqlaError("Own exit exceeds chronological entry evidence")
            average = cost / quantity
            realized += size * (price - average) * direction
            quantity, cost = quantity - size, cost - size * average
    return quantity, cost / quantity if quantity else Decimal(0), realized, fees


def current_reference(trade):
    if trade.get("lifecycle_version") == 2:
        size, price, _, _ = position_basis(trade)
        if size:
            return price
    return Decimal(trade.get("entry_vwap") or trade["plan"]["entry_price"])


async def validate_actual(engine, trade_id):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    size, average, _, _ = position_basis(trade)
    if size != Decimal(trade["remaining_quantity"]):
        raise AqlaError("Managed position differs from immutable own fills")
    account = await read_observation(lambda: engine.exchange.account_snapshot(plan.market), "account")
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    stop = Decimal(trade["stop_price"])
    pending = pending_quantity(trade)
    if size + pending > plan.quantity or size * average > plan.policy.max_notional_usdc:
        raise AqlaError("Managed entry exceeds the original quantity/notional limit")
    direction = Decimal(1) if plan.side == "long" else Decimal(-1)
    own_risk = size * (max(Decimal(0), (average - stop) * direction) + average * plan.policy.cost_reserve_bps / 10000)
    if plan.policy.management_mode == "waverider":
        from .waverider import pools
        inventory = pools(trade, plan.policy.waverider_flex_percent)
        own_risk = (inventory["core"] * max(Decimal(0), (inventory["core_basis"] - stop) * direction)
            + inventory["flex"] * max(Decimal(0), (inventory["flex_basis"] - stop) * direction)
            + size * average * plan.policy.cost_reserve_bps / 10000)
    risk = own_risk
    observed = trade.get("order_observations", {})
    risk += sum(((Decimal(row["quantity"]) - Decimal(row.get("confirmed_filled") or 0)) * (abs(Decimal(row["limit"]) - stop)
                + Decimal(row["limit"]) * plan.policy.cost_reserve_bps / 10000)
                for row in trade["entry_orders"] if not row.get("not_sent")
                and observed.get(row["cloid"], {}).get("status") not in TERMINAL_ORDER), Decimal(0))
    valid_stop = stop < quote.stop_reference if plan.side == "long" else stop > quote.stop_reference
    if not trade.get("managed_entry_validated"):
        valid_stop &= stop < average if plan.side == "long" else stop > average
    if not valid_stop or risk > plan.risk_usdc:
        raise AqlaError("Managed own fills exceed the original stop-risk structure")
    snapshot = engine.state.snapshot()
    limits = engine.state.limits(plan) if hasattr(engine.state, "limits") else plan.policy
    others = sum((Decimal(row["reserved_risk"]) for identity, row in snapshot["trades"].items()
                  if identity != trade_id and row["status"] in ("waiting_entry", "preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted")), Decimal(0))
    accepted_equity = Decimal(trade.get("entry_equity_snapshot", {}).get("equity", str(account.equity)))
    if own_risk > accepted_equity * plan.policy.risk_percent / 100 or own_risk + others > account.equity * limits.max_total_risk_percent / 100:
        raise AqlaError("Managed fills exceed the accepted individual or current shared account risk limit")
    engine._patch(trade_id, current_entry_vwap=str(average), managed_entry_validated=True)


def target_allocations(plan, quantity, previous=None, consumed=None, completed=()):
    spent = list(map(Decimal, consumed or [0] * len(plan.take_profits)))
    previous = previous or [None] * len(spent)
    completed = set(completed)
    for index, (amount, budget) in enumerate(zip(spent, previous, strict=True)):
        if budget is not None and Decimal(budget) > 0 and amount >= Decimal(budget):
            completed.add(index)
    weights = split_targets(Decimal("100000000"), len(spent), plan.policy, plan.market)
    active = [index for index in range(len(weights)) if index not in completed]
    if not active:
        return [], completed
    outstanding = [max(Decimal(0), Decimal(budget or 0) - amount) for budget, amount in zip(previous, spent, strict=True)]
    if sum(outstanding) > 0:
        weights = outstanding
    denominator = sum(weights[index] for index in active)
    budgets = [Decimal(0)] * len(weights)
    for index in active[:-1]:
        budgets[index] = quantity_grid(quantity * weights[index] / denominator, plan.market)
    budgets[active[-1]] = quantity - sum(budgets)
    return budgets, completed


async def sync_targets(engine, trade_id):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    remaining = Decimal(trade["remaining_quantity"])
    if remaining <= 0:
        raise AqlaError("Targets require a proved remaining position")
    fingerprint = digest([trade["source_reference_fingerprint"], str(remaining), trade["tp_filled"]])
    if trade.get("managed_target_fingerprint") == fingerprint:
        return True
    if not await engine.resume_target_update(trade_id):
        raise AqlaError("Previous target replacement must be reconciled before another allocation")
    budgets, completed = target_allocations(plan, remaining, trade.get("target_budgets"), trade["tp_filled"], trade.get("completed_target_indices", []))
    if not budgets:
        return False
    active = [index for index in range(len(budgets)) if index not in completed]
    working = [Decimal(0)] * len(budgets)
    prices_match = True
    for order in trade["orders"]:
        fact = trade.get("order_observations", {}).get(order["cloid"], {})
        if order["kind"] != "tp" or order.get("retired") or fact.get("status") != "open":
            continue
        filled = sum((Decimal(row["sz"]) for row in trade["fill_events"].values() if row["oid"] == fact.get("oid")), Decimal(0))
        working[order["index"]] += max(Decimal(0), Decimal(order["quantity"]) - filled)
        prices_match &= Decimal(order["price"]) == Decimal(trade["targets"][order["index"]])
    if prices_match and working == budgets:
        engine._patch(trade_id, completed_target_indices=sorted(completed), managed_target_fingerprint=fingerprint)
        return True
    if any(budgets[index] < plan.market.min_quantity or budgets[index] * Decimal(trade["targets"][index]) < plan.market.min_notional for index in active):
        raise ExchangeMinimum("Retained core targets are below native minimums")
    generation = trade.get("managed_target_generation", 0) + 1
    engine._patch(trade_id, completed_target_indices=sorted(completed), managed_target_generation=generation,
        target_budgets=[str(value + Decimal(spent)) for value, spent in zip(budgets, trade["tp_filled"], strict=True)])
    if any(row["kind"] == "tp" and not row.get("retired") for row in trade["orders"]):
        identity = f"managed-targets:{trade_id}:{generation}:{fingerprint}"
        result = await engine.replace_targets(trade_id, list(map(Decimal, trade["targets"])), identity)
        if result.get("phase") not in ("acknowledged", "reconciled") and not result.get("no_unfilled_targets"):
            raise AqlaError("Managed target generation has no confirmed accepted outcome")
    engine._patch(trade_id, managed_target_fingerprint=fingerprint)
    return True


async def retain_retry(engine, trade_id, error):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    previous = trade.get("managed_entry_retry")
    marker = {**retry_marker(previous, error, plan.deadline_at, "execute"), "completed": False}
    engine._patch(trade_id, managed_entry_retry=marker, entry_diagnostic=error.diagnostic())
    if marker["exhausted"]:
        await cancel(engine, trade_id)
        if not Decimal(trade["filled_quantity"]):
            engine._patch(trade_id, status="rejected", reserved_risk="0", unsent_reason="Original managed entry read budget exhausted")
    return engine.state.snapshot()["trades"][trade_id]


async def start(engine, plan, trade):
    marker = trade.get("managed_entry_retry")
    if marker and not marker.get("completed") and not retry_due(marker):
        return trade
    try:
        return await _start(engine, plan, trade)
    except TemporaryObservation as error:
        return await retain_retry(engine, plan.trade_id, error)


async def _start(engine, plan, trade):
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    first = next(row for row in trade["entry_orders"] if not row.get("not_sent"))
    first_view = leg_view(plan, first)
    if first_view.entry_timing == "deferred" and gate_state(first_view, quote) == "waiting":
        return trade
    if entry_missed(trade, quote):
        await cancel(engine, plan.trade_id)
        engine._patch(plan.trade_id, status="rejected", reserved_risk="0", unsent_reason="Source target or stop was reached before the first entry")
        return engine.state.snapshot()["trades"][plan.trade_id]
    identity = trade.get("leverage_operation_id", "leverage:" + plan.trade_id)
    existing = engine.state.snapshot()["operations"].get(identity)
    parent = trade.get("configuration_parent_id", identity)
    configuration = engine.state.snapshot()["operations"].get(parent, {})
    if configuration.get("configuration_retry_needed") and configuration["phase"] in ("prepared", "sending", "unknown"):
        return await engine.repair_unsent_configuration(plan, parent)
    if existing is None:
        first = next(row for row in trade["entry_orders"] if not row.get("not_sent"))
        view = leg_view(plan, first)
        if view.entry_timing == "deferred":
            quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
            state = gate_state(view, quote)
            if state == "waiting":
                return trade
            if state == "missed":
                await cancel(engine, plan.trade_id)
                engine._patch(plan.trade_id, status="cancelled", reserved_risk="0", unsent_reason="Deferred source entry was missed")
                return engine.state.snapshot()["trades"][plan.trade_id]
        async def guard():
            await engine.live_entry_guard(view, dispatch_operation=identity, audit=True)
        existing = await engine.exchange.mutate(identity, {"type": "updateLeverage", "asset": plan.market.asset_id,
            "isCross": plan.policy.margin_mode == "cross", "leverage": plan.effective_leverage},
            deadline=plan.deadline_at, async_prepare_guard=guard)
    if existing["phase"] not in ("acknowledged", "reconciled"):
        engine._patch(plan.trade_id, status="unknown" if existing["phase"] == "unknown" else "protecting", waiting_error="Leverage configuration requires exact reconciliation")
        return engine.state.snapshot()["trades"][plan.trade_id]
    await dispatch_next(engine, plan.trade_id)
    current = engine.state.snapshot()["trades"][plan.trade_id]
    if not any(row["phase"] != "pending" and not row.get("not_sent") for row in current["entry_orders"]):
        return current
    return await refresh(engine, plan.trade_id, dispatch=False)


async def dispatch_next(engine, trade_id):
    try:
        return await _dispatch_next(engine, trade_id)
    except TemporaryObservation as error:
        await retain_retry(engine, trade_id, error)
        return False


def entry_missed(trade, quote):
    plan = trade["plan"]
    invalid = quote.stop_reference <= Decimal(trade["stop_price"]) if plan["side"] == "long" else quote.stop_reference >= Decimal(trade["stop_price"])
    reference = Decimal(trade["filled_quantity"]) > 0 or not plan.get("source_tp") and "take_profits" not in plan.get("backup_fields", [])
    reached = (quote.bid >= Decimal(trade["targets"][0]) if plan["side"] == "long" else quote.ask <= Decimal(trade["targets"][0]))
    return invalid or reference and (reached or bool(trade.get("entry_tp1_reached_at")))


async def _dispatch_next(engine, trade_id):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    if trade.get("entries_disabled") or now_ms() >= plan.deadline_at:
        return False
    pending = [row for row in trade["entry_orders"] if row["phase"] == "pending" and not row.get("not_sent")]
    if not pending:
        return False
    row = pending[0]
    if row["kind"] == "waverider":
        from .waverider import live_permission
        wave = trade["waverider"]
        if (not live_permission(engine, plan) or wave.get("finished") or wave.get("phase") != "pending_reenter"
                or wave.get("cycle", 0) != row.get("cycle", 0)):
            await cancel(engine, trade_id, [row], permanent=False)
            return False
    marker = trade.get("managed_entry_retry")
    if marker and not marker.get("completed") and not retry_due(marker):
        return False
    prior = [item for item in trade["entry_orders"] if item is not row and item["kind"] == "original"
             and not item.get("not_sent") and item["phase"] != "pending"]
    if any(item["phase"] != "terminal" for item in prior):
        return False
    if prior and not Decimal(trade["filled_quantity"]):
        await cancel(engine, trade_id)
        return False
    view = leg_view(plan, row)
    current_market = await read_observation(lambda: engine.exchange.market(plan.market.symbol, plan.market.product), "market")
    identity_fields = ("asset_id", "size_decimals", "quote_token", "price_tick", "quantity_step", "margin_leverage_scale")
    if any(getattr(current_market, key) != getattr(plan.market, key) for key in identity_fields) or plan.effective_leverage > current_market.max_leverage:
        raise AqlaError("Market identity, precision or leverage changed while entry was unsent")
    sizes = split_targets(view.quantity, len(trade["targets"]), plan.policy, current_market)
    if (view.quantity < current_market.min_quantity or view.quantity * view.entry_limit < current_market.min_notional
            or current_market.max_quantity is not None and view.quantity > current_market.max_quantity
            or any(size < current_market.min_quantity or size * Decimal(price) < current_market.min_notional
                   for size, price in zip(sizes, trade["targets"], strict=True))):
        raise ExchangeMinimum("Exchange minimum/maximum order sizes changed while entry was unsent")
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    if row["kind"] == "original" and entry_missed(trade, quote):
        await cancel(engine, trade_id, [item for item in trade["entry_orders"] if item["kind"] == "original"], permanent=plan.policy.management_mode != "waverider")
        return False
    if view.entry_timing == "deferred":
        state = gate_state(view, quote)
        if state == "waiting":
            return False
        if state == "missed":
            await cancel(engine, trade_id)
            return False
    validate_entry_prices(view, quote, stop=trade["stop_price"], targets=trade["targets"])
    latest_account, latest_quote = None, None
    async def guard():
        nonlocal latest_account, latest_quote
        latest_account, latest_quote = await engine.live_entry_guard(view, dispatch_operation=row["operation_id"], audit=latest_account is None)
        current = engine.state.snapshot()["trades"][trade_id]
        if row["kind"] == "original" and entry_missed(current, latest_quote):
            raise AqlaError("Original entry target/stop was reached before dispatch")
        if current.get("entries_disabled") or current["status"] in ("closing", "cleanup", "closed", "conflicted", "rejected", "cancelled"):
            raise AqlaError("Managed entry was terminated before dispatch")
    def final_guard(data):
        engine.validate_admission(data, view, latest_account, owned=True,
            allowed_prepared=(row["operation_id"],), allowed_sending=(row["operation_id"],))
        latest_quote.fresh()
        if row["kind"] == "original" and entry_missed(data["trades"][trade_id], latest_quote):
            raise AqlaError("Original entry target/stop was reached before transport")
        if data["trades"][trade_id].get("entries_disabled"):
            raise AqlaError("Managed entry was terminated before transport")
    entry = engine.order(view, view.quantity, execution_limit(view), cloid=row["cloid"])
    if view.entry_timing == "deferred" and view.entry_execution == "standard":
        entry["t"] = {"limit": {"tif": "Ioc"}}
    stop = Decimal(trade["stop_price"])
    protective = engine.order(view, view.quantity, price_grid(stop * (Decimal(".95") if plan.side == "long" else Decimal("1.05")), plan.market, up=plan.side == "short"),
        reduce=True, trigger=("sl", stop), cloid=engine.exchange.cloid(row["operation_id"], 1))
    def stage(data):
        current = data["trades"][trade_id]
        owned = next(item for item in current["entry_orders"] if item["cloid"] == row["cloid"])
        if owned["phase"] != "pending" or current.get("entries_disabled"):
            raise AqlaError("Managed entry no longer owns its reservation")
        owned["phase"] = "dispatching"
        current["orders"].append({"kind": "sl", "cloid": protective["c"], "quantity": protective["s"], "price": str(stop)})
        current["status"] = "opening" if not Decimal(current["filled_quantity"]) else "protecting"
    engine.state.update(stage)
    result = await engine.exchange.mutate(row["operation_id"], {"type": "order", "orders": [entry, protective], "grouping": "normalTpsl"},
        deadline=plan.deadline_at, async_prepare_guard=guard, prepare_guard=final_guard)
    if result.get("outcome") == "not_sent":
        patch_row(engine, trade_id, row["cloid"], not_sent=True, phase="terminal", confirmed_filled="0")
        def prepare_retry(data):
            current = data["trades"][trade_id]
            generation = 1 + sum(item["index"] == row["index"] and item["kind"] == row["kind"] for item in current["entry_orders"])
            identity = f"entry:{trade_id}:leg:{row['index']}:observation:{generation}"
            replacement = {key: value for key, value in row.items() if key not in ("not_sent", "confirmed_filled")}
            replacement.update(operation_id=identity, cloid=engine.exchange.cloid(identity), phase="pending")
            current["entry_orders"].insert(current["entry_orders"].index(next(item for item in current["entry_orders"] if item["cloid"] == row["cloid"])) + 1, replacement)
            for order in current["orders"]:
                if order["cloid"] == protective["c"]:
                    order.update(retired=True, not_sent=True)
        try:
            engine.raise_temporary_not_sent(result)
        except TemporaryObservation:
            engine.state.update(prepare_retry)
            raise
        await cancel(engine, trade_id)
    elif result["phase"] == "unknown":
        engine._patch(trade_id, status="unknown")
    elif any(item["index"] == 1 and item["disposition"] == "rejected" for item in result.get("legs", [])):
        await cancel(engine, trade_id)
    if marker:
        engine._patch(trade_id, managed_entry_retry={**marker, "completed": True})
    if row["kind"] == "waverider" and view.entry_execution == "maker_only" and result["phase"] in ("acknowledged", "reconciled"):
        await cancel(engine, trade_id, [row], permanent=False)
    return True


async def refresh(engine, trade_id, *, dispatch=True):
    trade = engine.state.snapshot()["trades"][trade_id]
    if trade["status"] == "cleanup":
        return await engine.finish_cleanup(trade_id)
    plan = TradePlan.model_validate(trade["plan"])
    if trade["status"] in ("closed", "cancelled", "rejected", "conflicted"):
        return trade
    sent = [row for row in trade["entry_orders"] if row["phase"] != "pending" and not row.get("not_sent")]
    if not sent and not trade.get("entries_disabled"):
        if now_ms() >= plan.deadline_at:
            await cancel(engine, trade_id)
            engine._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Original signal deadline expired")
            return engine.state.snapshot()["trades"][trade_id]
        return await start(engine, plan, trade)
    total, cost = await engine.capture_fills(trade_id)
    trade = engine.state.snapshot()["trades"][trade_id]
    for row in trade["entry_orders"]:
        status = trade.get("order_observations", {}).get(row["cloid"], {}).get("status")
        oid = trade.get("order_observations", {}).get(row["cloid"], {}).get("oid")
        quantity = sum((Decimal(fill["sz"]) for fill in trade["fill_events"].values() if fill["kind"] == "entry" and fill["oid"] == oid), Decimal(0))
        if status == "open" and quantity:
            await cancel(engine, trade_id, [row], permanent=False)
            total, cost = await engine.capture_fills(trade_id)
            trade = engine.state.snapshot()["trades"][trade_id]
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    remaining = engine.owned_remaining(trade_id, signed)
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    reached = (quote.bid >= Decimal(trade["targets"][0]) if plan.side == "long" else quote.ask <= Decimal(trade["targets"][0]))
    if total:
        from .waverider import queue_evidence
        queue_evidence(engine, trade_id, quote)
    invalid = quote.stop_reference <= Decimal(trade["stop_price"]) if plan.side == "long" else quote.stop_reference >= Decimal(trade["stop_price"])
    # A pre-fill percentage preview is not evidence that a real TP has been reached.
    has_target_reference = bool(total or not plan.source_tp and "take_profits" not in plan.backup_fields)
    source_tp_fingerprint = digest([row for row in trade["fill_events"].values() if row["kind"] == "tp"])
    new_source_tp = any(row["kind"] == "tp" for row in trade["fill_events"].values()) and source_tp_fingerprint != trade.get("source_tp_observed_fingerprint")
    if now_ms() >= plan.deadline_at or (reached or trade.get("entry_tp1_reached_at")) and has_target_reference or invalid or any(fill["kind"] in ("tp", "sl", "close") for fill in trade["fill_events"].values()):
        await cancel(engine, trade_id, [row for row in trade["entry_orders"] if row["kind"] == "original"], permanent=False)
        if new_source_tp:
            await cancel(engine, trade_id, [row for row in trade["entry_orders"] if row["kind"] == "waverider"], permanent=False)
        if plan.policy.management_mode != "waverider" or now_ms() >= plan.deadline_at or invalid or any(fill["kind"] in ("sl", "close") for fill in trade["fill_events"].values()):
            await cancel(engine, trade_id)
        total, cost = await engine.capture_fills(trade_id)
        signed = engine.position_size(await engine.positions(plan.market), plan.market)
        remaining = engine.owned_remaining(trade_id, signed)
        trade = engine.state.snapshot()["trades"][trade_id]
        engine._patch(trade_id, source_tp_observed_fingerprint=source_tp_fingerprint)
    if remaining:
        engine._patch(trade_id, filled_quantity=str(total), entry_vwap=str(cost / total), remaining_quantity=str(remaining), status="protecting")
        current = engine.state.snapshot()["trades"][trade_id]
        originals_done = all(row.get("not_sent") or row["phase"] == "terminal" for row in current["entry_orders"] if row["kind"] == "original")
        if originals_done and not current.get("initial_sl_risk"):
            reference = source_reference(current)
            original_quantity = sum((Decimal(row["sz"]) for row in original_fills(current)), Decimal(0))
            amount = plan.risk_usdc if plan.policy.management_mode == "waverider" else original_quantity * abs(reference - Decimal(current["stop_price"]))
            engine._patch(trade_id, initial_sl_risk={"amount": str(amount), "entry": str(reference), "stop": current["stop_price"],
                "quantity": str(original_quantity), "currency": plan.market.quote_token, "entry_fingerprint": digest(original_fills(current))})
        try:
            if current.get("suspended_wave_targets"):
                from .waverider import resume_exit
                if not await resume_exit(engine, trade_id):
                    await engine.ensure_protection(trade_id)
                    return engine.state.snapshot()["trades"][trade_id]
                await engine.capture_fills(trade_id)
                trade = engine.state.snapshot()["trades"][trade_id]
                remaining = engine.owned_remaining(trade_id, engine.position_size(await engine.positions(plan.market), plan.market))
                engine._patch(trade_id, remaining_quantity=str(remaining))
                if not remaining:
                    return await refresh(engine, trade_id, dispatch=False)
            pending = engine.state.snapshot()["trades"][trade_id].get("pending_stop_update")
            if pending and pending["phase"] in ("waiting_fill", "replacing", "unknown"):
                await engine.resume_pending_stop(trade_id)
                if engine.state.snapshot()["trades"][trade_id].get("pending_stop_update", {}).get("phase") in ("waiting_fill", "replacing", "unknown"):
                    return engine.state.snapshot()["trades"][trade_id]
            finalize_prices(engine, trade_id)
            await validate_actual(engine, trade_id)
            if not await sync_targets(engine, trade_id):
                await cancel(engine, trade_id)
                return await engine.close_position(trade_id, Decimal(100), "managed-target-complete:" + trade_id)
            await engine.ensure_protection(trade_id)
            trade = engine.state.snapshot()["trades"][trade_id]
            stops = [(row, await engine.order_status(row["cloid"])) for row in trade["orders"] if row["kind"] == "sl" and not row.get("retired")]
            if not protected(engine, trade, stops):
                raise AqlaError("No confirmed stop covers the managed position")
            if plan.policy.management_mode == "waverider":
                from .waverider import execute, pools, tick
                inventory = pools(trade, plan.policy.waverider_flex_percent)
                excess = quantity_grid(max(Decimal(0), inventory["flex"] - inventory["rights"]), plan.market)
                if excess:
                    await execute(engine, trade_id, trade["waverider"], "retire", excess, quote, Decimal(0))
                    return engine.state.snapshot()["trades"][trade_id]
                await tick(engine, trade_id, quote)
        except TemporaryObservation as error:
            engine._patch(trade_id, protection_observation_pending=error.diagnostic())
            raise
        except AqlaError as error:
            engine._patch(trade_id, execution_incident="Managed protection failed: " + str(error))
            await cancel(engine, trade_id)
            if getattr(engine.exchange, "fail_closed_protection", False):
                engine.state.pause_entries("Managed protection could not be confirmed")
                return await engine.close_position(trade_id, Decimal(100), "protection-failure:" + trade_id)
            raise
        engine._patch(trade_id, protection_observation_pending=None)
        if dispatch and engine.state.snapshot()["trades"][trade_id]["status"] == "open":
            await dispatch_next(engine, trade_id)
            return await refresh(engine, trade_id, dispatch=False)
    elif total:
        await cancel(engine, trade_id)
        await engine.capture_fills(trade_id)
        if entry_final(engine.state.snapshot()["trades"][trade_id]):
            engine.begin_cleanup(trade_id, "closed")
            return await engine.finish_cleanup(trade_id)
    elif entry_final(engine.state.snapshot()["trades"][trade_id]):
        engine.begin_cleanup(trade_id, "rejected")
        return await engine.finish_cleanup(trade_id)
    return engine.state.snapshot()["trades"][trade_id]
