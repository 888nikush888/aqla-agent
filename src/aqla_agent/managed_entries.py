"""Two source entries share one owner, reservation and protection lifecycle."""
from __future__ import annotations

from decimal import Decimal

from aqla_hyperliquid_mcp.models import (
    TERMINAL_ORDER,
    AqlaError,
    EntryLegPlan,
    ExchangeMinimum,
    TradePlan,
    digest,
    now_ms,
    require_first,
)
from aqla_hyperliquid_mcp.rules import (
    backup_prices,
    entry_targets,
    make_entry_plan,
    price_grid,
    quantity_grid,
    selected_targets,
    split_targets,
)

from .accounting import position_basis
from .entry import execution_limit, gate_state, leg_view, validate_entry_prices
from .observations import TemporaryObservation, read_observation, retry_due, retry_marker


def enabled(plan):
    return bool(plan.entry_legs or plan.policy.entry_range_choice == "split" or plan.policy.management_mode == "waverider")






def cap_quantity(plan, ceiling):
    quantity = quantity_grid(min(plan.quantity, Decimal(ceiling)), plan.market)
    if quantity == plan.quantity:
        return plan
    if quantity <= 0 or quantity < plan.market.min_quantity or quantity * plan.entry_limit < plan.market.min_notional:
        raise ExchangeMinimum("Rerouted quantity is below the exchange minimum")
    values = plan.model_dump()
    values.update(quantity=quantity, risk_usdc=plan.risk_usdc * quantity / plan.quantity)
    legs = []
    if plan.entry_legs:
        first = quantity_grid(quantity * plan.entry_legs[0].quantity / plan.quantity, plan.market)
        legs = [leg.model_copy(update={"quantity": size}) for leg, size in zip(plan.entry_legs, [first, quantity - first], strict=True)]
        if any(leg.quantity <= 0 or leg.quantity < plan.market.min_quantity or leg.quantity * leg.limit < plan.market.min_notional for leg in legs):
            raise ExchangeMinimum("Rerouted source entry leg is below the exchange minimum")
        values["entry_legs"] = legs
    values["take_profits"], values["tp_quantities"] = entry_targets(quantity, plan.take_profits, plan.policy, plan.market, [leg.quantity for leg in legs])
    if plan.source_tp:
        count = len(values["take_profits"])
        values["source_tp"] = {**plan.source_tp, "values": plan.source_tp["values"][:count],
                               "preview_prices": plan.source_tp["preview_prices"][:count]}
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
        row = require_first(row for row in data["trades"][trade_id]["entry_orders"] if row["cloid"] == cloid)
        row.update(values)
    engine.state.update(apply)


async def cancel(engine, trade_id, rows=None, *, permanent=True):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(engine.state.snapshot(), plan)
    if permanent:
        engine.patch_trade(trade_id, entries_disabled=True)
        if trade.get("waverider"):
            engine.patch_trade(trade_id, waverider={**trade["waverider"], "finished": True, "phase": "finished"})
    for row in rows if rows is not None else entry_orders(trade):
        if row.get("not_sent"):
            continue
        if row.get("phase") == "terminal" and trade.get("order_observations", {}).get(row["cloid"], {}).get("status") in TERMINAL_ORDER:
            continue
        operation = engine.state.read("operations")["operations"].get(row["operation_id"])
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
        if native.get("cloid") == order["cloid"] and raw.get("order", {}).get("status") == "open" and engine.stop_matches(raw, check, Decimal(0), Decimal(trade["stop_price"])):
            # Maker legacy checks exact size; a managed leg owns an explicit size.
            covered += Decimal(native.get("sz") or 0)
            seen.add(oid)
    return covered >= size > 0


def finalized_backup_targets(plan, reference, stop, targets):
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
    return targets


def finalize_prices(engine, trade_id):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    reference = source_reference(trade)
    if reference is None:
        return False
    source = trade.get("source_tp_override", plan.source_tp)
    fingerprint = digest(original_fills(trade))
    if trade.get("source_reference_fingerprint") == fingerprint:
        return True
    stop, targets = Decimal(trade["stop_price"]), list(map(Decimal, trade["targets"]))
    targets = finalized_backup_targets(plan, reference, stop, targets)
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




def current_reference(trade):
    if trade.get("lifecycle_version") == 2:
        size, price, _, _ = position_basis(trade)
        if size:
            return price
    return Decimal(trade.get("entry_vwap") or trade["plan"]["entry_price"])


def managed_position_risk(engine, trade, plan, size, average, stop):
    direction = Decimal(1) if plan.side == "long" else Decimal(-1)
    own_risk = size * (max(Decimal(0), (average - stop) * direction) + average * plan.policy.cost_reserve_bps / 10000)
    if plan.policy.management_mode == "waverider":
        pools = engine.waves.pools
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
    return own_risk, risk


async def validate_actual(engine, trade_id):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
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
    own_risk, risk = managed_position_risk(engine, trade, plan, size, average, stop)
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
    engine.patch_trade(trade_id, current_entry_vwap=str(average), managed_entry_validated=True)


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


async def restore_targets(engine, trade_id):
    """Restore only confirmed owned protection before optional market-price reads."""
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    if trade.get("suspended_wave_targets") or any(row["phase"] in ("prepared", "sending", "unknown") for row in engine.state.read("operations")["operations"].values()):
        return False
    plan = TradePlan.model_validate(trade["plan"])
    await engine.capture_fills(trade_id)
    remaining = await engine.observe_owned_remaining(trade_id, engine.position_size(await engine.positions(plan.market), plan.market))
    engine.patch_trade(trade_id, remaining_quantity=str(remaining))
    if not remaining:
        return True
    for order in trade["orders"]:
        if order["kind"] != "tp" or order.get("retired"):
            continue
        raw = await engine.order_status(order["cloid"])
        if raw.get("order", {}).get("status") not in ({"open"} | TERMINAL_ORDER):
            return False
        if raw["order"]["status"] in TERMINAL_ORDER:
            def retire(data, cloid=order["cloid"]):
                require_first(row for row in data["trades"][trade_id]["orders"] if row["cloid"] == cloid)["retired"] = True
            engine.state.update(retire)
    engine.patch_trade(trade_id, managed_target_fingerprint=None)
    if not await sync_targets(engine, trade_id):
        return False
    await engine.ensure_protection(trade_id)
    if engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["status"] != "open":
        return False
    engine.patch_trade(trade_id, protection_repair_pending=None)
    return True


def working_target_allocation(trade, budgets):
    working = [Decimal(0)] * len(budgets)
    prices_match = True
    for order in trade["orders"]:
        fact = trade.get("order_observations", {}).get(order["cloid"], {})
        if order["kind"] != "tp" or order.get("retired") or fact.get("status") != "open":
            continue
        filled = sum((Decimal(row["sz"]) for row in trade["fill_events"].values() if row["oid"] == fact.get("oid")), Decimal(0))
        working[order["index"]] += max(Decimal(0), Decimal(order["quantity"]) - filled)
        prices_match &= Decimal(order["price"]) == Decimal(trade["targets"][order["index"]])
    return working, prices_match


async def replace_managed_target_generation(engine, trade_id, trade, budgets, completed, fingerprint):
    generation = trade.get("managed_target_generation", 0) + 1
    engine.patch_trade(trade_id, completed_target_indices=sorted(completed), managed_target_generation=generation,
        target_budgets=[str(value + Decimal(spent)) for value, spent in zip(budgets, trade["tp_filled"], strict=True)])
    if any(row["kind"] == "tp" and not row.get("retired") for row in trade["orders"]):
        identity = f"managed-targets:{trade_id}:{generation}:{fingerprint}"
        result = await engine.replace_targets(trade_id, list(map(Decimal, trade["targets"])), identity)
        if result.get("phase") not in ("acknowledged", "reconciled") and not result.get("no_unfilled_targets"):
            raise AqlaError("Managed target generation has no confirmed accepted outcome")


async def sync_targets(engine, trade_id):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
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
    working, prices_match = working_target_allocation(trade, budgets)
    if prices_match and working == budgets:
        engine.patch_trade(trade_id, completed_target_indices=sorted(completed), managed_target_fingerprint=fingerprint)
        return True
    if any(budgets[index] < plan.market.min_quantity or budgets[index] * Decimal(trade["targets"][index]) < plan.market.min_notional for index in active):
        raise ExchangeMinimum("Retained core targets are below native minimums")
    await replace_managed_target_generation(engine, trade_id, trade, budgets, completed, fingerprint)
    engine.patch_trade(trade_id, managed_target_fingerprint=fingerprint)
    return True


def pending_wave_retry(trade, wave, plan):
    wave_row = next((row for row in trade["entry_orders"] if row["kind"] == "waverider" and row["phase"] == "pending" and not row.get("not_sent")
                     and row["operation_id"] == wave.get("operation_id")), None)
    wave_retry = wave_row is not None and plan.policy.waverider_lifetime == "until_target"
    return wave_row, wave_retry


async def retain_retry(engine, trade_id, error):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    previous = trade.get("managed_entry_retry")
    wave = trade.get("waverider", {})
    wave_row, wave_retry = pending_wave_retry(trade, wave, plan)
    if previous and (previous.get("completed") or previous.get("wave_cycle") != (wave.get("cycle", 0) if wave_retry else None)):
        previous = None
    deadline = (previous["deadline_at"] if previous else now_ms() + 300000) if wave_retry else plan.deadline_at
    marker = {**retry_marker(previous, error, deadline, "execute"), "completed": False}
    if wave_retry:
        marker["wave_cycle"] = wave.get("cycle", 0)
    engine.patch_trade(trade_id, managed_entry_retry=marker, entry_diagnostic=error.diagnostic())
    if marker["exhausted"]:
        if wave_retry:
            reset_phase = engine.waves.reset_phase
            await cancel(engine, trade_id, [wave_row], permanent=False)
            engine.patch_trade(trade_id, managed_entry_retry={**marker, "completed": True},
                waverider={**reset_phase(wave, "waiting_rebound"), "cycle": wave.get("cycle", 0) + 1})
            return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        await cancel(engine, trade_id)
        if not Decimal(trade["filled_quantity"]):
            engine.patch_trade(trade_id, status="rejected", reserved_risk="0", unsent_reason="Original managed entry read budget exhausted")
    return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]


async def start(engine, plan, trade):
    marker = trade.get("managed_entry_retry")
    if marker and not marker.get("completed") and not retry_due(marker):
        return trade
    try:
        return await _start(engine, plan, trade)
    except TemporaryObservation as error:
        return await retain_retry(engine, plan.trade_id, error)


def validate_managed_configuration_retry(data, current, retry, prior):
    if (not retry_due(retry) or retry["attempts"] >= 5 or current.get("orders")
            or Decimal(current["filled_quantity"]) or Decimal(current["remaining_quantity"])
            or current.get("fill_events") or prior["phase"] != "rejected"
            or prior.get("outcome") != "not_sent" or prior.get("legs")
            or any(item["phase"] != "pending" and not item.get("not_sent") for item in current["entry_orders"])
            or any(item["operation_id"] in data["operations"] for item in current["entry_orders"])):
        raise AqlaError("Managed configuration retry requires a bounded, completely unsent entry")


def rearm_managed_configuration(data, plan, identity):
    current = data["trades"][plan.trade_id]
    retry = current["managed_entry_retry"]
    prior = data["operations"][identity]
    validate_managed_configuration_retry(data, current, retry, prior)
    replacement = f"leverage:{plan.trade_id}:observation:{retry['attempts'] + 1}"
    if replacement in data["operations"]:
        raise AqlaError("Configuration retry already has a durable request; reconcile it")
    retry.update(attempts=retry["attempts"] + 1, attempt_inflight=True)
    current.setdefault("leverage_attempts", []).append(identity)
    current["leverage_operation_id"] = replacement
    return replacement


async def configure_managed_leverage(engine, plan, trade, identity):
    first = require_first(row for row in trade["entry_orders"] if not row.get("not_sent"))
    view = leg_view(plan, first)
    if view.entry_timing == "deferred":
        quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
        state = gate_state(view, quote)
        if state == "waiting":
            return None, trade
        if state == "missed":
            await cancel(engine, plan.trade_id)
            engine.patch_trade(plan.trade_id, status="cancelled", reserved_risk="0", unsent_reason="Deferred source entry was missed")
            return None, engine.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
    latest_account, latest_quote = None, None
    async def guard():
        nonlocal latest_account, latest_quote
        latest_account, latest_quote = await engine.live_entry_guard(view, dispatch_operation=identity, account=latest_account, audit=latest_quote is None)
    def invalidate_account():
        nonlocal latest_account
        latest_account = None
    guard.invalidate_account = invalidate_account
    def final_guard(data):
        engine.validate_admission(data, view, latest_account, owned=True,
            allowed_prepared=(identity,), allowed_sending=(identity,))
        latest_quote.fresh()
    existing = await engine.exchange.mutate(identity, {"type": "updateLeverage", "asset": plan.market.asset_id,
        "isCross": plan.policy.margin_mode == "cross", "leverage": plan.effective_leverage},
        deadline=plan.deadline_at, async_prepare_guard=guard, prepare_guard=final_guard)
    return existing, None


async def first_managed_entry_gate(engine, plan, trade):
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    first = require_first(row for row in trade["entry_orders"] if not row.get("not_sent"))
    first_view = leg_view(plan, first)
    if first_view.entry_timing == "deferred" and gate_state(first_view, quote) == "waiting":
        return trade
    if entry_missed(trade, quote):
        await cancel(engine, plan.trade_id)
        engine.patch_trade(plan.trade_id, status="rejected", reserved_risk="0", unsent_reason="Source target or stop was reached before the first entry")
        return engine.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]


async def reject_managed_configuration(engine, plan, existing):
    engine.raise_temporary_not_sent(existing)
    if existing["phase"] == "rejected" and (existing.get("outcome") == "rejected" or existing.get("outcome") == "not_sent" and not existing.get("legs")):
        await cancel(engine, plan.trade_id)
        engine.patch_trade(plan.trade_id, status="rejected", reserved_risk="0", unsent_reason=existing.get("result", {}).get("error", "Configuration guard rejected before transport"))
        return engine.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
    engine.patch_trade(plan.trade_id, status="unknown" if existing["phase"] == "unknown" else "protecting", waiting_error="Leverage configuration requires exact reconciliation")
    return engine.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]


async def _start(engine, plan, trade):
    gated = await first_managed_entry_gate(engine, plan, trade)
    if gated is not None:
        return gated
    identity = trade.get("leverage_operation_id", "leverage:" + plan.trade_id)
    existing = engine.state.read("operations")["operations"].get(identity)
    parent = trade.get("configuration_parent_id", identity)
    configuration = engine.state.read("operations")["operations"].get(parent, {})
    if configuration.get("configuration_retry_needed") and configuration["phase"] in ("prepared", "sending", "unknown"):
        return await engine.repair_unsent_configuration(plan, parent)
    if existing and existing["phase"] == "rejected" and existing.get("outcome") == "not_sent" and not existing.get("legs"):
        try:
            engine.raise_temporary_not_sent(existing)
        except TemporaryObservation:
            marker = trade.get("managed_entry_retry")
            if not marker or marker.get("attempt_inflight"):
                raise
            def rearm(data):
                return rearm_managed_configuration(data, plan, identity)
            identity = engine.state.update(rearm)
            existing = None
    if existing is None:
        existing, waiting = await configure_managed_leverage(engine, plan, trade, identity)
        if waiting is not None:
            return waiting
    if existing["phase"] not in ("acknowledged", "reconciled"):
        return await reject_managed_configuration(engine, plan, existing)
    await dispatch_next(engine, plan.trade_id)
    current = engine.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
    if not sent_managed_entries(current):
        return current
    return await refresh(engine, plan.trade_id, dispatch=False)


async def dispatch_next(engine, trade_id):
    """Only a send or cancellation with possible fills requires another reconciliation."""
    try:
        return await _dispatch_next(engine, trade_id)
    except TemporaryObservation as error:
        current = await retain_retry(engine, trade_id, error)
        return "cancelled" if current["managed_entry_retry"]["exhausted"] else "unchanged"


def entry_missed(trade, quote):
    plan = trade["plan"]
    invalid = quote.stop_reference <= Decimal(trade["stop_price"]) if plan["side"] == "long" else quote.stop_reference >= Decimal(trade["stop_price"])
    reference = Decimal(trade["filled_quantity"]) > 0 or not plan.get("source_tp") and "take_profits" not in plan.get("backup_fields", [])
    reached = (quote.bid >= Decimal(trade["targets"][0]) if plan["side"] == "long" else quote.ask <= Decimal(trade["targets"][0]))
    return invalid or reference and (reached or bool(trade.get("entry_tp1_reached_at")))


async def validate_managed_entry_market(engine, plan, trade, view):
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


async def managed_entry_price_gate(engine, trade_id, plan, trade, row, view):
    ended = engine.waves.ended
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    if row["kind"] == "original" and entry_missed(trade, quote):
        await cancel(engine, trade_id, [item for item in trade["entry_orders"] if item["kind"] == "original"], permanent=plan.policy.management_mode != "waverider")
        return "cancelled"
    if view.entry_timing == "deferred":
        state = gate_state(view, quote)
        if state == "waiting":
            return "unchanged"
        if state == "missed":
            await cancel(engine, trade_id)
            return "cancelled"
    targets = [value for index, value in enumerate(trade["targets"]) if index not in trade.get("completed_target_indices", [])]
    validate_entry_prices(view, quote, stop=trade["stop_price"], targets=targets)
    if row["kind"] == "waverider" and ended(plan, trade, quote):
        await cancel(engine, trade_id, [row], permanent=False)
        return "cancelled"


def validate_managed_dispatch(engine, trade_id, plan, row, latest_quote):
    ended = engine.waves.ended
    current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    if row["kind"] == "original" and entry_missed(current, latest_quote):
        raise AqlaError("Original entry target/stop was reached before dispatch")
    if row["kind"] == "waverider" and ended(plan, current, latest_quote):
        raise AqlaError("Waverider endpoint was reached before reentry")
    if current.get("entries_disabled") or current["status"] in ("closing", "cleanup", "closed", "conflicted", "rejected", "cancelled"):
        raise AqlaError("Managed entry was terminated before dispatch")


def validate_managed_transport(engine, data, trade_id, plan, row, view, latest_account, latest_quote):
    ended = engine.waves.ended
    if row["kind"] == "waverider":
        live_permission = engine.waves.live_permission
        if not live_permission(engine, plan):
            raise AqlaError("Waverider authorization changed before reentry transport")
    engine.validate_admission(data, view, latest_account, owned=True,
        allowed_prepared=(row["operation_id"],), allowed_sending=(row["operation_id"],), waverider=row["kind"] == "waverider")
    latest_quote.fresh()
    if row["kind"] == "original" and entry_missed(data["trades"][trade_id], latest_quote):
        raise AqlaError("Original entry target/stop was reached before transport")
    if row["kind"] == "waverider" and ended(plan, data["trades"][trade_id], latest_quote):
        raise AqlaError("Waverider endpoint was reached before reentry transport")
    if data["trades"][trade_id].get("entries_disabled"):
        raise AqlaError("Managed entry was terminated before transport")


def managed_entry_guards(engine, trade_id, plan, row, view):
    latest_account, latest_quote = None, None
    async def guard():
        nonlocal latest_account, latest_quote
        latest_account, latest_quote = await engine.live_entry_guard(view, dispatch_operation=row["operation_id"], account=latest_account, audit=latest_quote is None, waverider=row["kind"] == "waverider")
        validate_managed_dispatch(engine, trade_id, plan, row, latest_quote)
    def invalidate_account():
        nonlocal latest_account
        latest_account = None
    guard.invalidate_account = invalidate_account
    def final_guard(data):
        validate_managed_transport(engine, data, trade_id, plan, row, view, latest_account, latest_quote)
    return guard, final_guard


def stage_managed_entry(data, trade_id, row, protective, stop, final_guard):
    final_guard(data)
    current = data["trades"][trade_id]
    owned = require_first(item for item in current["entry_orders"] if item["cloid"] == row["cloid"])
    if owned["phase"] == "dispatching" and row["operation_id"] in data["operations"]:
        return
    if owned["phase"] != "pending" or current.get("entries_disabled"):
        raise AqlaError("Managed entry no longer owns its reservation")
    owned["phase"] = "dispatching"
    current["orders"].append({"kind": "sl", "cloid": protective["c"], "quantity": protective["s"], "price": str(stop)})
    current["status"] = "opening" if not Decimal(current["filled_quantity"]) else "protecting"


def prepare_managed_retry(engine, data, trade_id, row, protective):
    current = data["trades"][trade_id]
    generation = 1 + sum(item["index"] == row["index"] and item["kind"] == row["kind"] for item in current["entry_orders"])
    identity = f"entry:{trade_id}:leg:{row['index']}:observation:{generation}"
    replacement = {key: value for key, value in row.items() if key not in ("not_sent", "confirmed_filled")}
    replacement.update(operation_id=identity, cloid=engine.exchange.cloid(identity), phase="pending")
    current["entry_orders"].insert(current["entry_orders"].index(require_first(item for item in current["entry_orders"] if item["cloid"] == row["cloid"])) + 1, replacement)
    if row["kind"] == "waverider":
        current["waverider"]["operation_id"] = identity
    for order in current["orders"]:
        if order["cloid"] == protective["c"]:
            order.update(retired=True, not_sent=True)


async def complete_managed_dispatch(engine, trade_id, row, protective, marker, view, result):
    if result.get("outcome") == "not_sent":
        patch_row(engine, trade_id, row["cloid"], not_sent=True, phase="terminal", confirmed_filled="0")
        def prepare_retry(data):
            prepare_managed_retry(engine, data, trade_id, row, protective)
        try:
            engine.raise_temporary_not_sent(result)
        except TemporaryObservation:
            engine.state.update(prepare_retry)
            raise
        await cancel(engine, trade_id)
    elif result["phase"] == "unknown":
        engine.patch_trade(trade_id, status="unknown")
    elif any(item["index"] == 1 and item["disposition"] == "rejected" for item in result.get("legs", [])):
        await cancel(engine, trade_id)
    if marker:
        engine.patch_trade(trade_id, managed_entry_retry={**marker, "completed": True})
    if row["kind"] == "waverider" and view.entry_execution == "maker_only" and result["phase"] in ("acknowledged", "reconciled"):
        await cancel(engine, trade_id, [row], permanent=False)
    return "unknown" if result["phase"] == "unknown" else "sent"


async def wave_entry_reservation_valid(engine, trade_id, trade, plan, row):
    ended = engine.waves.ended
    live_permission = engine.waves.live_permission
    wave = trade["waverider"]
    if (not live_permission(engine, plan) or ended(plan, trade) or wave.get("phase") != "pending_reenter"
            or wave.get("cycle", 0) != row.get("cycle", 0)):
        await cancel(engine, trade_id, [row], permanent=False)
        return False
    return True


async def managed_entry_prior_status(engine, trade_id, trade, row):
    marker = trade.get("managed_entry_retry")
    if marker and not marker.get("completed") and not retry_due(marker):
        return "unchanged"
    prior = [item for item in trade["entry_orders"] if item is not row and item["kind"] == "original"
             and not item.get("not_sent") and item["phase"] != "pending"]
    if any(item["phase"] != "terminal" for item in prior):
        return "unchanged"
    if prior and not Decimal(trade["filled_quantity"]):
        await cancel(engine, trade_id)
        return "cancelled"
    return row, marker


async def next_managed_entry_reservation(engine, trade_id, trade, plan):
    if trade.get("entries_disabled"):
        return "unchanged"
    pending = [row for row in trade["entry_orders"] if row["phase"] == "pending" and not row.get("not_sent")]
    if not pending:
        return "unchanged"
    row = pending[0]
    if now_ms() >= plan.deadline_at and not (row["kind"] == "waverider" and plan.policy.waverider_lifetime == "until_target"):
        return "unchanged"
    if row["kind"] == "waverider" and not await wave_entry_reservation_valid(engine, trade_id, trade, plan, row):
        return "cancelled"
    return await managed_entry_prior_status(engine, trade_id, trade, row)


async def _dispatch_next(engine, trade_id):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    selected = await next_managed_entry_reservation(engine, trade_id, trade, plan)
    if isinstance(selected, str):
        return selected
    row, marker = selected
    view = leg_view(plan, row)
    await validate_managed_entry_market(engine, plan, trade, view)
    gated = await managed_entry_price_gate(engine, trade_id, plan, trade, row, view)
    if gated is not None:
        return gated
    guard, final_guard = managed_entry_guards(engine, trade_id, plan, row, view)
    entry = engine.order(view, view.quantity, execution_limit(view), cloid=row["cloid"])
    if view.entry_timing == "deferred" and view.entry_execution == "standard":
        entry["t"] = {"limit": {"tif": "Ioc"}}
    stop = Decimal(trade["stop_price"])
    protective = engine.order(view, view.quantity, price_grid(stop * (Decimal(".95") if plan.side == "long" else Decimal("1.05")), plan.market, up=plan.side == "short"),
        reduce=True, trigger=("sl", stop), cloid=engine.exchange.cloid(row["operation_id"], 1))
    def stage(data):
        stage_managed_entry(data, trade_id, row, protective, stop, final_guard)
    # Stage the leg atomically with its request; failed early reads leave it retryable.
    result = await engine.exchange.mutate(row["operation_id"], {"type": "order", "orders": [entry, protective], "grouping": "normalTpsl"},
        deadline=None if row["kind"] == "waverider" and plan.policy.waverider_lifetime == "until_target" else plan.deadline_at,
        async_prepare_guard=guard, prepare_guard=stage)
    return await complete_managed_dispatch(engine, trade_id, row, protective, marker, view, result)


async def capture_partial_managed_entries(engine, trade_id, trade, total, cost):
    for row in trade["entry_orders"]:
        status = trade.get("order_observations", {}).get(row["cloid"], {}).get("status")
        oid = trade.get("order_observations", {}).get(row["cloid"], {}).get("oid")
        quantity = sum((Decimal(fill["sz"]) for fill in trade["fill_events"].values() if fill["kind"] == "entry" and fill["oid"] == oid), Decimal(0))
        if status == "open" and quantity:
            await cancel(engine, trade_id, [row], permanent=False)
            total, cost = await engine.capture_fills(trade_id)
            trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    return trade, total, cost


def managed_original_entry_ended(plan, trade, reached, invalid, has_target_reference):
    return now_ms() >= plan.deadline_at or (reached or trade.get("entry_tp1_reached_at")) and has_target_reference or invalid or any(fill["kind"] in ("tp", "sl", "close") for fill in trade["fill_events"].values())


async def cancel_reached_managed_entries(engine, trade_id, trade, plan, quote, total, cost, remaining, reached, invalid, has_target_reference, new_source_tp, source_tp_fingerprint):
    if managed_original_entry_ended(plan, trade, reached, invalid, has_target_reference):
        await cancel(engine, trade_id, [row for row in trade["entry_orders"] if row["kind"] == "original"], permanent=False)
        if new_source_tp:
            await cancel(engine, trade_id, [row for row in trade["entry_orders"] if row["kind"] == "waverider"], permanent=False)
        ended = engine.waves.ended
        if plan.policy.management_mode != "waverider" or ended(plan, trade, quote) or invalid or any(fill["kind"] in ("sl", "close") for fill in trade["fill_events"].values()):
            await cancel(engine, trade_id)
        total, cost = await engine.capture_fills(trade_id)
        signed = engine.position_size(await engine.positions(plan.market), plan.market)
        remaining = await engine.observe_owned_remaining(trade_id, signed)
        trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        engine.patch_trade(trade_id, source_tp_observed_fingerprint=source_tp_fingerprint)
    return trade, total, cost, remaining


async def refresh_managed_entry_lifetime(engine, trade_id, trade, plan, quote, total, cost, remaining):
    reached = (quote.bid >= Decimal(trade["targets"][0]) if plan.side == "long" else quote.ask <= Decimal(trade["targets"][0]))
    if total:
        queue_evidence = engine.waves.queue_evidence
        queue_evidence(engine, trade_id, quote)
    invalid = quote.stop_reference <= Decimal(trade["stop_price"]) if plan.side == "long" else quote.stop_reference >= Decimal(trade["stop_price"])
    # A pre-fill percentage preview is not evidence that a real TP has been reached.
    has_target_reference = bool(total or not plan.source_tp and "take_profits" not in plan.backup_fields)
    source_tp_fingerprint = digest([row for row in trade["fill_events"].values() if row["kind"] == "tp"])
    new_source_tp = any(row["kind"] == "tp" for row in trade["fill_events"].values()) and source_tp_fingerprint != trade.get("source_tp_observed_fingerprint")
    trade, total, cost, remaining = await cancel_reached_managed_entries(engine, trade_id, trade, plan, quote, total, cost, remaining, reached, invalid, has_target_reference, new_source_tp, source_tp_fingerprint)
    return trade, total, cost, remaining


def record_initial_managed_risk(engine, trade_id, plan, current):
    originals_done = all(row.get("not_sent") or row["phase"] == "terminal" for row in current["entry_orders"] if row["kind"] == "original")
    if originals_done and not current.get("initial_sl_risk"):
        reference = source_reference(current)
        original_quantity = sum((Decimal(row["sz"]) for row in original_fills(current)), Decimal(0))
        amount = plan.risk_usdc if plan.policy.management_mode == "waverider" else original_quantity * abs(reference - Decimal(current["stop_price"]))
        engine.patch_trade(trade_id, initial_sl_risk={"amount": str(amount), "entry": str(reference), "stop": current["stop_price"],
            "quantity": str(original_quantity), "currency": plan.market.quote_token, "entry_fingerprint": digest(original_fills(current))})


async def resume_managed_wave_targets(engine, trade_id, plan):
    resume_exit = engine.waves.resume_exit
    if not await resume_exit(engine, trade_id):
        await engine.ensure_protection(trade_id)
        return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    await engine.capture_fills(trade_id)
    remaining = await engine.observe_owned_remaining(trade_id, engine.position_size(await engine.positions(plan.market), plan.market))
    engine.patch_trade(trade_id, remaining_quantity=str(remaining))
    if not remaining:
        return await refresh(engine, trade_id, dispatch=False)


async def refresh_wave_inventory(engine, trade_id, trade, plan, quote):
    execute = engine.waves.execute
    pools = engine.waves.pools
    tick = engine.waves.tick
    inventory = pools(trade, plan.policy.waverider_flex_percent)
    excess = quantity_grid(max(Decimal(0), inventory["flex"] - inventory["rights"]), plan.market)
    if excess:
        await execute(engine, trade_id, trade["waverider"], "retire", excess, quote, Decimal(0))
        return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    await tick(engine, trade_id, quote)


async def refresh_managed_protection(engine, trade_id, plan, current, trade, quote):
    if current.get("suspended_wave_targets"):
        result = await resume_managed_wave_targets(engine, trade_id, plan)
        if result is not None:
            return result
    pending = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("pending_stop_update")
    if pending and pending["phase"] in ("waiting_fill", "replacing", "unknown"):
        await engine.resume_pending_stop(trade_id)
        if engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("pending_stop_update", {}).get("phase") in ("waiting_fill", "replacing", "unknown"):
            return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    finalize_prices(engine, trade_id)
    await validate_actual(engine, trade_id)
    if not await sync_targets(engine, trade_id):
        await cancel(engine, trade_id)
        return await engine.close_position(trade_id, Decimal(100), "managed-target-complete:" + trade_id)
    await engine.ensure_protection(trade_id)
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    stops = [(row, await engine.order_status(row["cloid"])) for row in trade["orders"] if row["kind"] == "sl" and not row.get("retired")]
    if not protected(engine, trade, stops):
        raise AqlaError("No confirmed stop covers the managed position")
    if plan.policy.management_mode == "waverider":
        return await refresh_wave_inventory(engine, trade_id, trade, plan, quote)


async def refresh_managed_position(engine, trade_id, trade, plan, total, cost, remaining, quote, dispatch):
    engine.patch_trade(trade_id, filled_quantity=str(total), entry_vwap=str(cost / total), remaining_quantity=str(remaining),
                  status="open" if trade["status"] == "open" else "protecting")
    current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    record_initial_managed_risk(engine, trade_id, plan, current)
    try:
        result = await refresh_managed_protection(engine, trade_id, plan, current, trade, quote)
        if result is not None:
            return result
    except TemporaryObservation as error:
        engine.patch_trade(trade_id, status="protecting", protection_observation_pending=error.diagnostic())
        raise
    except AqlaError as error:
        engine.patch_trade(trade_id, execution_incident="Managed protection failed: " + str(error))
        await cancel(engine, trade_id)
        if getattr(engine.exchange, "fail_closed_protection", False):
            engine.state.pause_entries("Managed protection could not be confirmed")
            return await engine.close_position(trade_id, Decimal(100), "protection-failure:" + trade_id)
        raise
    engine.patch_trade(trade_id, protection_observation_pending=None)
    if (
        (dispatch and engine.state.read('trades', entries={'trades': [trade_id]})['trades'][trade_id]['status'] == 'open')
        and (await dispatch_next(engine, trade_id) != 'unchanged')
    ):
        return await refresh(engine, trade_id, dispatch=False)
    return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]


def sent_managed_entries(trade):
    return [row for row in trade["entry_orders"] if row["phase"] != "pending" and not row.get("not_sent")]


async def refresh(engine, trade_id, *, dispatch=True):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    if trade["status"] == "cleanup":
        return await engine.finish_cleanup(trade_id)
    plan = TradePlan.model_validate(trade["plan"])
    if trade["status"] in ("closed", "cancelled", "rejected", "conflicted"):
        return trade
    sent = sent_managed_entries(trade)
    if not sent and not trade.get("entries_disabled"):
        if now_ms() >= plan.deadline_at:
            await cancel(engine, trade_id)
            engine.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Original signal deadline expired")
            return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        return await start(engine, plan, trade)
    total, cost = await engine.capture_fills(trade_id)
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    trade, total, cost = await capture_partial_managed_entries(engine, trade_id, trade, total, cost)
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    remaining = await engine.observe_owned_remaining(trade_id, signed)
    if remaining and trade.get("protection_repair_pending"):
        if not await restore_targets(engine, trade_id):
            return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    trade, total, cost, remaining = await refresh_managed_entry_lifetime(engine, trade_id, trade, plan, quote, total, cost, remaining)
    if remaining:
        return await refresh_managed_position(engine, trade_id, trade, plan, total, cost, remaining, quote, dispatch)
    elif total:
        await cancel(engine, trade_id)
        await engine.capture_fills(trade_id)
        if entry_final(engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]):
            engine.begin_cleanup(trade_id, "closed")
            return await engine.finish_cleanup(trade_id)
    elif entry_final(engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]):
        engine.begin_cleanup(trade_id, "rejected")
        return await engine.finish_cleanup(trade_id)
    return engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]

# Named compatibility exports share the same implementation.
__all__ = ['all_order_rows', 'cancel', 'cap_quantity', 'current_reference', 'dispatch_next', 'enabled', 'entry_final', 'entry_missed', 'entry_orders', 'entry_targets', 'finalize_prices', 'initialize_trade', 'leg_view', 'make_entry_plan', 'original_fills', 'patch_row', 'pending_quantity', 'position_basis', 'protected', 'refresh', 'restore_targets', 'retain_retry', 'source_reference', 'start', 'sync_targets', 'target_allocations', 'validate_actual']
