"""Read-only closure proof. A flat position alone never proves ownership."""
from decimal import Decimal, InvalidOperation

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest


async def observe_conflict(engine, trade_id):
    snapshot = engine.state.snapshot()
    trade = snapshot["trades"].get(trade_id)
    if not trade or trade["status"] != "conflicted":
        raise AqlaError("Select one current conflicted trade ID")
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(snapshot, plan)
    reasons = []
    identities = {trade["entry_cloid"]: {"kind": "entry"}}
    identities.update({row["cloid"]: row for row in [*trade["orders"], *trade.get("exit_orders", [])]})
    statuses, order_ids = {}, {}
    for identity, role in identities.items():
        observation = await engine.exchange.order_status(identity)
        detail = observation.get("order", {})
        statuses[identity] = detail.get("status", observation.get("status", "unknown"))
        oid = detail.get("order", {}).get("oid")
        if oid is not None:
            order_ids[oid] = role
    if any(value not in TERMINAL_ORDER for value in statuses.values()):
        reasons.append("owned_orders_not_terminal")
    if any(row["phase"] in ("prepared", "sending", "unknown") for row in snapshot["operations"].values()):
        reasons.append("unknown_operation")
    if any(identity not in snapshot["operations"] for identity in trade.get("close_intents", {})):
        reasons.append("unsent_close_intention")
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    if signed:
        reasons.append("position_not_flat")
    rows = [row for row in await engine._fills(plan) if row.get("coin") == plan.market.symbol]
    owned = dict(trade.get("fill_events", {}))
    # Native fills form the complete chronological account-market path. Unknown
    # openings/reopenings or quantities outside this path are never adopted.
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
    reasons = sorted(set(reasons))
    evidence = {"trade_hash": digest(trade), "operations_hash": digest(snapshot["operations"]), "fills": owned, "closed_at": last_exit}
    facts = {"trade_id": trade_id, "account_key": plan.account_key or getattr(engine.state, "account_key", plan.exchange + ":" + plan.network),
        "network": plan.network, "account": plan.account, "market": plan.market.symbol,
        "position_size": str(signed), "order_statuses": statuses, "reasons": reasons,
        "resolvable": not reasons, "pnl_complete": False, "fill_count": len(owned)}
    facts["fingerprint"] = digest([facts, evidence, rows])
    return facts, evidence
