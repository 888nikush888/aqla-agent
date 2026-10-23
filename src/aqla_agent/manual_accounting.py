"""Evidence-bound PnL for proved manual closures; native receipts stay immutable."""

from decimal import Decimal, InvalidOperation
from itertools import groupby

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest

from .evidence import verified_coverage_matches


def closure_fingerprint(trade):
    return digest({key: value for key, value in trade.get("manual_closure", {}).items() if key != "pnl_complete"})


def verified_manual_accounting(trade):
    value = trade.get("manual_accounting")
    if not isinstance(value, dict) or not isinstance(trade.get("manual_closure"), dict):
        return None
    if (trade.get("status") != "closed" or not verified_coverage_matches(trade, start=trade.get("entry_accounting", {}).get("entry_at"), end=trade.get("closed_at"))
            or value.get("fingerprint") != digest({key: item for key, item in value.items() if key != "fingerprint"})
            or value.get("fill_fingerprint") != digest(trade.get("fill_events", {}))
            or value.get("closure_fingerprint") != closure_fingerprint(trade)
            or value.get("plan_fingerprint") != digest(trade["plan"])
            or value.get("entry_fingerprint") != digest(trade.get("entry_accounting", {}))
            or value.get("funding_fingerprint") != digest(trade.get("funding_events", {}))
            or value.get("closed_at") != trade.get("closed_at")):
        return None
    return value


def require_manual_terminal_orders(trade, plan, closure, identities, statuses):
    if (trade["status"] != "closed" or plan.exchange != "hyperliquid" or plan.market.product != "perp"
            or closure.get("basis") not in ("proved_original_account_closure", "proved_account_flat_after_last_owned_fill")
            or set(statuses) != identities or any(value not in TERMINAL_ORDER for value in statuses.values())):
        raise AqlaError("Manual closure lacks verified terminal original orders")


def manual_attribution_proof(trade, observed):
    plan = TradePlan.model_validate(trade["plan"])
    closure = trade.get("manual_closure", {})
    from .managed_entries import all_order_rows
    identities = {row["cloid"] for row in all_order_rows(trade)}
    statuses = closure.get("order_statuses", {}) if isinstance(closure, dict) else {}
    require_manual_terminal_orders(trade, plan, closure, identities, statuses)
    roles = {row["oid"]: row for row in trade["fill_events"].values() if row["kind"] != "manual_close"}
    proof = native_flat_closure(trade, plan, list(observed.values()), roles)
    if not proof or proof["closed_at"] != trade["closed_at"]:
        raise AqlaError("Manual closure position history is incomplete or changed")
    if closure.get("account_position_proof") and proof["account_position_proof"] != closure["account_position_proof"]:
        raise AqlaError("The stored native closure position proof changed")
    return plan, roles, proof


def manual_entry_price(trade, observed, roles):
    entries = [row for row in observed.values() if roles.get(row["oid"], {}).get("kind") == "entry"]
    notional = sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in entries)
    entry = notional / Decimal(trade["filled_quantity"])
    if entry != Decimal(trade["entry_vwap"]) or notional != Decimal(trade["entry_accounting"]["entry_notional"]):
        raise AqlaError("Frozen own entry prices or margin evidence changed")
    return entry


def manual_fill_share(raw, role, own, exited, sign, entry, amount, start, delta, gross):
    part = Decimal(0)
    if role and role["kind"] == "entry":
        if exited:
            raise AqlaError("Manual attribution requires final own entries before reductions")
        own += amount
        part = amount
    elif delta * sign > 0:
        if role:
            raise AqlaError("Own exit unexpectedly increased the account position")
    else:
        if own <= 0 or start * sign <= 0 or amount > abs(start) or own > abs(start):
            raise AqlaError("Manual reduction does not match the proved own/account quantities")
        if role:
            part = amount
        elif own == abs(start):
            part = amount
        elif amount == abs(start):
            part = own
        else:
            raise AqlaError("Partial reduction of mixed exposure has no unique own attribution")
        if part > own:
            raise AqlaError("Attributed exit exceeds own remaining quantity")
        own -= part
        exited += part
        gross += part * (Decimal(raw["px"]) - entry) * sign
    return own, exited, part, gross


def allocate_manual_fee(trade, raw, plan, identity, role, part, amount, fee_evidence, allocations):
    from .accounting import fill_identity
    if raw.get("fee") is None or raw.get("feeToken") != plan.market.quote_token:
        raise AqlaError("Attributed fill fees or currency are incomplete")
    fee = Decimal(raw["fee"])
    if not fee.is_finite():
        raise AqlaError("Attributed fill fee is not finite")
    allocated_fee = fee * part / amount
    if role and role["kind"] == "entry":
        old = trade.get("report_fee_evidence", {}).get(identity, trade["fill_events"][identity])
        if any(old.get(key) is not None and old[key] != raw.get(key) for key in ("fee", "feeToken")):
            raise AqlaError("An immutable own fee changed")
        fee_evidence[identity] = {"fee": raw["fee"], "feeToken": raw["feeToken"]}
    else:
        fact = {key: raw.get(key) for key in ("oid", "tid", "time", "coin", "px", "feeToken")}
        fact.update(sz=str(part), fee=str(allocated_fee), index=None,
            kind="manual_close_allocated" if part != amount else "manual_close",
            attribution={"native_fill_id": identity, "account_quantity": raw["sz"], "account_fee": raw["fee"]})
        allocations[fill_identity(fact)] = fact
    return allocated_fee


def attribute_manual_fills(trade, observed, plan, roles, proof):
    from .accounting import fill_identity
    sign = Decimal(1 if plan.side == "long" else -1)
    own, gross, fees, exited = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    allocations, fee_evidence, timeline = {}, {}, []
    proportional = False
    entry = manual_entry_price(trade, observed, roles)
    for step in proof["account_position_proof"]:
        identity = fill_identity(step)
        raw = observed[identity]
        amount, start = Decimal(raw["sz"]), Decimal(raw["startPosition"])
        delta = amount * (1 if raw["side"] == "B" else -1)
        role = roles.get(raw["oid"])
        own, exited, part, gross = manual_fill_share(raw, role, own, exited, sign, entry, amount, start, delta, gross)
        if part:
            fees += allocate_manual_fee(trade, raw, plan, identity, role, part, amount, fee_evidence, allocations)
            proportional |= part != amount
        timeline.append((raw["time"], own * sign, start + delta))
    if own or exited != Decimal(trade["filled_quantity"]):
        raise AqlaError("Complete own closure quantity is not proved")
    return gross, fees, exited, allocations, fee_evidence, timeline, proportional


def require_manual_funding_interval(trade, fact, plan, timeline):
    stamp = fact["time"]
    if (fact["market"] != plan.market.symbol or fact["currency"] != plan.market.quote_token
            or not trade["entry_accounting"]["entry_at"] <= stamp <= trade["closed_at"]
            or any(time == stamp for time, _, _ in timeline)):
        raise AqlaError("Funding market, currency, interval or ordering is unconfirmed")
    return stamp


def manual_funding_share(trade, fact, plan, timeline):
    stamp = require_manual_funding_interval(trade, fact, plan, timeline)
    previous = [item for item in timeline if item[0] < stamp]
    if not previous:
        raise AqlaError("Funding lacks a preceding own position")
    _, own_size, account_size = previous[-1]
    if (not own_size or not account_size or own_size * account_size <= 0 or abs(own_size) > abs(account_size)
            or fact["signed_size"] is None or Decimal(fact["signed_size"]) != account_size):
        raise AqlaError("Funding account/own position quantity is unconfirmed")
    amount = Decimal(fact["amount"])
    if not amount.is_finite():
        raise AqlaError("Funding payment is not finite")
    return own_size, account_size, amount


def attribute_manual_funding(trade, payments, plan, timeline):
    proportional = False
    assigned = {}
    for fact in payments:
        own_size, account_size, amount = manual_funding_share(trade, fact, plan, timeline)
        allocation = {**fact, "amount": str(amount * own_size / account_size),
            "allocation": {"account_amount": fact["amount"], "account_signed_size": str(account_size), "own_signed_size": str(own_size)}}
        if fact["identity"] in assigned and assigned[fact["identity"]] != allocation:
            raise AqlaError("Funding identity changed within the historical response")
        assigned[fact["identity"]] = allocation
        proportional |= own_size != account_size
    if any(identity not in assigned or assigned[identity] != fact for identity, fact in trade.get("funding_events", {}).items()):
        raise AqlaError("Previously assigned funding evidence changed or disappeared")
    return assigned, proportional


def attribute_manual_closure(trade, observed, payments):
    plan, roles, proof = manual_attribution_proof(trade, observed)
    gross, fees, exited, allocations, fee_evidence, timeline, proportional = attribute_manual_fills(trade, observed, plan, roles, proof)
    assigned, funding_proportional = attribute_manual_funding(trade, payments, plan, timeline)
    proportional |= funding_proportional
    result = {"gross_pnl": str(gross), "fees": str(fees), "exit_vwap": str(sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in allocations.values()) / exited),
        "exit_allocations": allocations, "closed_at": trade["closed_at"],
        "basis": "proportional_native_position_share" if proportional else "exact_native_own_position",
        "note": "Manuell geschlossen; Schließgebühr und Funding anteilig nach belegter Positionsmenge" if proportional else "Manuell geschlossen; eigene Ausführungen, Gebühren und Funding vollständig belegt",
        "fill_fingerprint": digest(trade["fill_events"]), "closure_fingerprint": closure_fingerprint(trade),
        "plan_fingerprint": digest(trade["plan"]), "entry_fingerprint": digest(trade["entry_accounting"]),
        "funding_fingerprint": digest(assigned)}
    result["fingerprint"] = digest(result)
    if trade.get("manual_accounting") and trade["manual_accounting"] != result:
        raise AqlaError("A previously verified manual attribution changed")
    return result, assigned, fee_evidence


def flat_fill_amount(raw, plan):
    if any(raw.get(key) is None for key in ("oid", "tid", "time", "sz", "px")) or type(raw["time"]) is not int or raw["time"] < plan.created_at:
        return None
    amount, price = Decimal(str(raw["sz"])), Decimal(str(raw["px"]))
    if not amount.is_finite() or amount <= 0 or not price.is_finite() or price <= 0:
        return None
    return amount


def retained_flat_fill_matches(fact, raw):
    return not (any(fact.get(key) != raw.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin"))
                    or any(raw.get(key) != value for key, value in fact.get("manual_proof", {}).items()))


def index_flat_fills(plan, rows, order_ids, own):
    unique, own_ids, entered = {}, set(), Decimal(0)
    for raw in rows:
        amount = flat_fill_amount(raw, plan)
        if amount is None:
            return None
        identity = digest([raw["oid"], raw["tid"], raw["time"], raw["sz"], raw["px"]])
        if identity in unique:
            if unique[identity] != raw:
                return None
            continue
        unique[identity] = raw
        if identity in own and not retained_flat_fill_matches(own[identity], raw):
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
    return unique, own_ids, entered


def native_position_batch(group):
    pending = []
    for raw in group:
        start = Decimal(str(raw["startPosition"]))
        if not start.is_finite() or raw.get("side") not in ("A", "B"):
            return None
        end = start + Decimal(str(raw["sz"])) * (1 if raw["side"] == "B" else -1)
        pending.append((raw, start, end))
    return pending


def trace_flat_boundary(history, own_ids, own):
    balance, processed, path = None, set(), []
    for _, group in groupby(history, key=lambda row: row["time"]):
        pending = native_position_batch(group)
        if pending is None:
            return None
        if balance is None:
            roots = {start for _, start, _ in pending} - {end for _, _, end in pending}
            if len(roots) != 1:
                return None
            balance = roots.pop()
        while pending:
            matches = [item for item in pending if item[1] == balance]
            if len(matches) != 1:
                return None
            raw, _, balance = matches[0]
            pending.remove(matches[0])
            identity = digest([raw["oid"], raw["tid"], raw["time"], raw["sz"], raw["px"]])
            processed.add(identity)
            path.append({key: raw.get(key) for key in ("oid", "tid", "time", "coin", "sz", "px", "startPosition", "side", "dir")})
            if balance == 0 and own_ids <= processed:
                return {"fills": own, "closed_at": raw["time"], "account_position_proof": path,
                        "basis": "proved_account_flat_after_last_owned_fill"}
    return None


def native_flat_closure(trade, plan, rows, order_ids):
    """Prove a native account-flat boundary after every original own fill."""
    if plan.market.product != "perp":
        return None
    own = dict(trade.get("fill_events", {}))
    try:
        collected = index_flat_fills(plan, rows, order_ids, own)
        if collected is None:
            return None
        unique, own_ids, entered = collected
        if not entered or entered != Decimal(trade["filled_quantity"]) or set(trade.get("fill_events", {})) - unique.keys():
            return None
        first = min(unique[key]["time"] for key in own_ids)
        history = sorted((row for row in unique.values() if row["time"] >= first), key=lambda row: row["time"])
        return trace_flat_boundary(history, own_ids, own)
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return None
