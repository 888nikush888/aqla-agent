"""Read-only closure proof. A flat position alone never proves ownership."""
from decimal import Decimal, InvalidOperation
from itertools import groupby

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest, now_ms


def native_flat_closure(trade, plan, rows, order_ids):
    """Prove a native account-flat boundary after every original own fill."""
    if plan.market.product != "perp":
        return None
    unique, own, entered = {}, dict(trade.get("fill_events", {})), Decimal(0)
    own_ids = set()
    try:
        for raw in rows:
            if any(raw.get(key) is None for key in ("oid", "tid", "time", "sz", "px")) or type(raw["time"]) is not int or raw["time"] < plan.created_at:
                return None
            identity = digest([raw["oid"], raw["tid"], raw["time"], raw["sz"], raw["px"]])
            if identity in unique:
                if unique[identity] != raw:
                    return None
                continue
            unique[identity] = raw
            if identity in own and (any(own[identity].get(key) != raw.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin"))
                    or any(raw.get(key) != value for key, value in own[identity].get("manual_proof", {}).items())):
                return None
            amount, price = Decimal(str(raw["sz"])), Decimal(str(raw["px"]))
            if not amount.is_finite() or amount <= 0 or not price.is_finite() or price <= 0:
                return None
            role = order_ids.get(raw["oid"])
            if role:
                expected_side = "B" if (plan.side == "long") == (role["kind"] == "entry") else "A"
                if raw.get("side") != expected_side:
                    return None
                fact = {key: raw.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin")}
                fact.update(kind=role["kind"], index=role.get("index"))
                if identity in own and own[identity] != fact:
                    return None
                own[identity] = fact
                own_ids.add(identity)
                if role["kind"] == "entry":
                    entered += amount
        if not entered or entered != Decimal(trade["filled_quantity"]) or set(trade.get("fill_events", {})) - unique.keys():
            return None
        first = min(unique[key]["time"] for key in own_ids)
        history = sorted((row for row in unique.values() if row["time"] >= first), key=lambda row: row["time"])
        balance, processed, path = None, set(), []
        for _, group in groupby(history, key=lambda row: row["time"]):
            pending = []
            for raw in group:
                start = Decimal(str(raw["startPosition"]))
                if not start.is_finite() or raw.get("side") not in ("A", "B"):
                    return None
                end = start + Decimal(str(raw["sz"])) * (1 if raw["side"] == "B" else -1)
                pending.append((raw, start, end))
            if balance is None:
                roots = {start for _, start, _ in pending} - {end for _, _, end in pending}
                if len(roots) != 1:
                    return None
                balance = roots.pop()
            while pending:
                matches = [item for item in pending if item[1] == balance]
                if len(matches) != 1:
                    return None
                raw, start, balance = matches[0]
                pending.remove(matches[0])
                identity = digest([raw["oid"], raw["tid"], raw["time"], raw["sz"], raw["px"]])
                processed.add(identity)
                path.append({key: raw.get(key) for key in ("oid", "tid", "time", "coin", "sz", "px", "startPosition", "side", "dir")})
                if balance == 0 and own_ids <= processed:
                    return {"fills": own, "closed_at": raw["time"], "account_position_proof": path,
                            "basis": "proved_account_flat_after_last_owned_fill"}
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return None
    return None


async def observe_conflict(engine, trade_id):
    snapshot = engine.state.snapshot()
    trade = snapshot["trades"].get(trade_id)
    if not trade or trade["status"] != "conflicted":
        raise AqlaError("Select one current conflicted trade ID")
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(snapshot, plan)
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
    from .monthly import entry_quantity_evidence
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
    rows = [row for row in await engine._fills(plan) if row.get("coin") == plan.market.symbol]
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
