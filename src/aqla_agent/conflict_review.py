"""Read-only closure proof. A flat position alone never proves ownership."""

from decimal import Decimal, InvalidOperation

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest, now_ms

from .manual_accounting import native_flat_closure


def unsubmitted_entry(engine, snapshot, trade):
    if (trade.get("lifecycle_version") != 2 or not engine.entry_not_sent(snapshot, trade)
            or trade.get("fill_events") or trade.get("orders") or trade.get("exit_orders") or trade.get("close_intents")):
        return False
    entries = trade.get("entry_orders", [])
    if not entries or any(row["kind"] != "original" for row in entries):
        return False
    identities = {row["operation_id"] for row in entries}
    cloids = {row["cloid"] for row in entries}
    def never_sent(operation):
        return operation.get("phase") == "rejected" and operation.get("outcome") == "not_sent" and not operation.get("legs")
    for row in entries:
        operation = snapshot["operations"].get(row["operation_id"])
        if operation is None and row.get("phase") != "pending" and not row.get("not_sent") or operation is not None and not never_sent(operation):
            return False
    for identity, operation in snapshot["operations"].items():
        if operation["phase"] in ("prepared", "sending", "unknown"):
            return False
        if identity not in identities and any(row.get("c") in cloids for row in operation.get("wire_action", {}).get("orders", [])):
            return False
    return True




async def observe_conflict(engine, trade_id):
    snapshot = engine.state.snapshot()
    trade = snapshot["trades"].get(trade_id)
    if not trade or trade["status"] != "conflicted":
        raise AqlaError("Select one current conflicted trade ID")
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(snapshot, plan)
    if unsubmitted_entry(engine, snapshot, trade):
        statuses = {row["cloid"]: (await engine.order_status(row["cloid"])).get("status") for row in trade["entry_orders"]}
        signed = engine.position_size(await engine.positions(plan.market), plan.market)
        reasons = [] if all(value == "unknownOid" for value in statuses.values()) else ["unexpected_native_order_without_local_request"]
        evidence = {"trade_hash": digest(trade), "operations_hash": digest(snapshot["operations"]), "fills": {}, "closed_at": None, "basis": "never_submitted_entry"}
        facts = {"trade_id": trade_id, "account_key": plan.account_key or getattr(engine.state, "account_key", plan.exchange + ":" + plan.network),
            "network": plan.network, "account": plan.account, "market": plan.market.symbol, "position_size": str(signed),
            "order_statuses": statuses, "reasons": reasons, "resolvable": not reasons, "pnl_complete": False, "fill_count": 0,
            "foreign_position_will_be_untouched": True}
        facts["fingerprint"] = digest([facts, evidence])
        return facts, evidence
    reasons = []
    identities = {} if trade.get("lifecycle_version") == 2 else {trade["entry_cloid"]: {"kind": "entry"}}
    identities.update({row["cloid"]: {"kind": "entry", "entry_role": row["kind"], "entry_leg": row["index"]}
        for row in trade.get("entry_orders", []) if not row.get("not_sent")
        and not (row.get("phase") == "pending" and row["operation_id"] not in snapshot["operations"])})
    identities.update({row["cloid"]: row for row in [*trade["orders"], *trade.get("exit_orders", [])]})
    statuses, order_ids = {}, {}
    for identity, role in identities.items():
        observation = await engine.order_status(identity)
        detail = observation.get("order", {})
        statuses[identity] = detail.get("status", observation.get("status", "unknown"))
        order = detail.get("order", {})
        oid = order.get("oid")
        if order.get("cloid") != identity or order.get("coin") != plan.market.symbol or oid is None or oid in order_ids:
            reasons.append("owned_order_identity_unconfirmed")
        if oid is not None:
            order_ids[oid] = role
    if any(value not in TERMINAL_ORDER for value in statuses.values()):
        reasons.append("owned_orders_not_terminal")
    if any(row["phase"] in ("prepared", "sending", "unknown") for row in snapshot["operations"].values()):
        reasons.append("unknown_operation")
    if any(identity not in snapshot["operations"] for identity in trade.get("close_intents", {})):
        reasons.append("unsent_close_intention")
    from .accounting import entry_quantity_evidence
    try:
        entry = await engine.order_status(trade["entry_cloid"])
        confirmed = entry_quantity_evidence(entry)
        if trade.get("lifecycle_version") == 2:
            amounts = []
            for row in trade["entry_orders"]:
                if row.get("not_sent") or row.get("phase") == "pending" and row["operation_id"] not in snapshot["operations"]:
                    amounts.append("0")
                else:
                    amounts.append(entry_quantity_evidence(await engine.order_status(row["cloid"])))
            confirmed = str(sum(map(Decimal, amounts))) if all(value is not None for value in amounts) else None
        if confirmed is None or Decimal(confirmed) != Decimal(trade["filled_quantity"]):
            reasons.append("entry_quantity_not_reconciled")
    except (InvalidOperation, ValueError, TypeError):
        reasons.append("entry_quantity_not_reconciled")
    preconditions = list(reasons)
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    if signed:
        reasons.append("position_not_flat")
    rows = [row for row in await engine.fill_history_for_plan(plan) if row.get("coin") == plan.market.symbol]
    owned = dict(trade.get("fill_events", {}))
    # First try exact own closure; mixed account proof retains foreign fills separately.
    def sequence(row):
        stamp, identity = row.get("time"), str(row.get("tid", ""))
        return (stamp if type(stamp) is int else -1, int(identity) if identity.isdigit() else -1)
    history = sorted(rows, key=sequence)
    balance, entered = Decimal(0), Decimal(0)
    observed_ids, last_exit = set(), None
    sign = Decimal(1) if plan.side == "long" else Decimal(-1)
    close_dir, close_side = ("Close Long", "A") if plan.side == "long" else ("Close Short", "B")
    try:
        for raw in history:
            if any(raw.get(key) is None for key in ("oid", "tid", "time", "sz", "px")) or type(raw["time"]) is not int or raw["time"] < plan.created_at:
                reasons.append("fill_history_incomplete")
                break
            amount, price = Decimal(str(raw["sz"])), Decimal(str(raw["px"]))
            if not amount.is_finite() or amount <= 0 or not price.is_finite() or price <= 0:
                reasons.append("fill_history_incomplete")
                break
            identity = digest([raw["oid"], raw["tid"], raw["time"], raw["sz"], raw["px"]])
            if identity in observed_ids:
                continue
            observed_ids.add(identity)
            role = order_ids.get(raw["oid"])
            fact = {key: raw.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin")}
            if role:
                fact.update(kind=role["kind"], index=role.get("index"))
                if trade.get("lifecycle_version") == 2 and role["kind"] == "entry":
                    fact.update(entry_role=role.get("entry_role", "original"), entry_leg=role.get("entry_leg", 0))
                if identity in owned and owned[identity] != fact:
                    reasons.append("immutable_fill_changed")
                    break
                # startPosition, where available, also excludes intervening
                # foreign exposure before an original own order's fill.
                if raw.get("startPosition") is not None and Decimal(str(raw["startPosition"])) != sign * balance:
                    reasons.append("foreign_exposure_in_history")
                    break
                if role["kind"] == "entry":
                    entered += amount
                    balance += amount
                else:
                    balance -= amount
                    last_exit = raw["time"]
            else:
                if (plan.market.product != "perp" or raw.get("dir") != close_dir or raw.get("side") != close_side
                        or raw.get("startPosition") is None or Decimal(str(raw["startPosition"])) != sign * balance
                        or balance <= 0 or amount > balance):
                    reasons.append("foreign_or_unproved_fill")
                    break
                fact.update(kind="manual_close", index=None)
                # Original native proof retained; never synthesize an order ID,
                # fee, PnL or receipt to fill a gap.
                fact.update(manual_proof={key: raw[key] for key in ("dir", "side", "startPosition")})
                if identity in owned and owned[identity] != fact:
                    reasons.append("immutable_fill_changed")
                    break
                balance -= amount
                last_exit = raw["time"]
            owned[identity] = fact
            if balance < 0:
                reasons.append("foreign_exposure_in_history")
                break
        if entered != Decimal(trade["filled_quantity"]):
            reasons.append("entry_quantity_not_reconciled")
        if not entered or balance != abs(signed) or balance or last_exit is None:
            reasons.append("own_closure_not_proved")
        if set(trade.get("fill_events", {})) - observed_ids:
            reasons.append("fill_history_incomplete")
    except (InvalidOperation, ValueError, TypeError):
        reasons.append("fill_history_incomplete")
    closure = native_flat_closure(trade, plan, rows, order_ids) if reasons else None
    if closure:
        reasons, owned, last_exit = preconditions, closure["fills"], closure["closed_at"]
    reasons = sorted(set(reasons))
    evidence = {"trade_hash": digest(trade), "operations_hash": digest(snapshot["operations"]), "fills": owned, "closed_at": last_exit}
    if closure:
        evidence.update({key: closure[key] for key in ("basis", "account_position_proof")})
    facts = {"trade_id": trade_id, "account_key": plan.account_key or getattr(engine.state, "account_key", plan.exchange + ":" + plan.network),
        "network": plan.network, "account": plan.account, "market": plan.market.symbol,
        "position_size": str(signed), "order_statuses": statuses, "reasons": reasons,
        "resolvable": not reasons, "pnl_complete": False, "fill_count": len(owned)}
    facts["fingerprint"] = digest([facts, evidence, rows])
    return facts, evidence


def commit_conflict(engine, data, trade_id, review, evidence):
    current = data["trades"][trade_id]
    if (not review["resolvable"] or current["status"] != "conflicted" or digest(current) != evidence["trade_hash"]
            or digest(engine.state.snapshot()["operations"]) != evidence["operations_hash"]):
        raise AqlaError("Trade changed during ownership review")
    engine.assert_plan_binding(engine.state.snapshot(), TradePlan.model_validate(current["plan"]))
    if evidence.get("basis") == "never_submitted_entry":
        if not unsubmitted_entry(engine, engine.state.snapshot(), current):
            raise AqlaError("Unsubmitted-entry proof changed; no ownership release")
        for row in current["entry_orders"]:
            row.update(not_sent=True, confirmed_filled="0", phase="terminal")
        if wave := current.get("waverider"):
            wave.update(finished=True, phase="finished")
        current.update(status="rejected", reserved_risk="0", entries_disabled=True, cleanup_complete=True,
                       unsent_reason="Market occupied by foreign exposure; this own entry was never submitted",
                       ownership_release={"basis": "never_submitted_entry", "review_fingerprint": review["fingerprint"], "confirmed_at": now_ms()})
        data["notifications"].setdefault("ownership-resolved:" + review["fingerprint"],
            {"trade_id": trade_id, "change": {"status": "rejected"}, "observed_at": now_ms()})
        return {"resolved": True, "trade_id": trade_id, "own_orders_sent": 0, "foreign_position_untouched": True, "pnl_complete": False}
    current.update(status="closed", remaining_quantity="0", reserved_risk="0", cleanup_complete=True,
        fill_events=evidence["fills"], closed_at=evidence["closed_at"],
        manual_closure={"confirmed_at": now_ms(), "review_fingerprint": review["fingerprint"], "pnl_complete": False,
            "order_statuses": review["order_statuses"], "basis": evidence.get("basis", "proved_original_account_closure"),
            **({"account_position_proof": evidence["account_position_proof"]} if "account_position_proof" in evidence else {})})
    from .monthly import accounting_facts
    current.update(accounting_facts(current))
    data["notifications"].setdefault("ownership-resolved:" + review["fingerprint"],
        {"trade_id": trade_id, "change": {"status": "closed"}, "observed_at": now_ms()})
    return {"resolved": True, "trade_id": trade_id, "pnl_complete": False}
