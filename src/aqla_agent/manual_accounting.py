"""Evidence-bound PnL for proved manual closures; native receipts stay immutable."""
from decimal import Decimal

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest

from .conflict_review import native_flat_closure
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


def attribute_manual_closure(trade, observed, payments):
    from .monthly import fill_identity
    plan = TradePlan.model_validate(trade["plan"])
    closure = trade.get("manual_closure", {})
    identities = {trade["entry_cloid"], *(row["cloid"] for row in [*trade["orders"], *trade.get("exit_orders", [])])}
    statuses = closure.get("order_statuses", {}) if isinstance(closure, dict) else {}
    if (trade["status"] != "closed" or plan.exchange != "hyperliquid" or plan.market.product != "perp"
            or closure.get("basis") not in ("proved_original_account_closure", "proved_account_flat_after_last_owned_fill")
            or set(statuses) != identities or any(value not in TERMINAL_ORDER for value in statuses.values())):
        raise AqlaError("Manual closure lacks verified terminal original orders")
    roles = {row["oid"]: row for row in trade["fill_events"].values() if row["kind"] != "manual_close"}
    proof = native_flat_closure(trade, plan, list(observed.values()), roles)
    if not proof or proof["closed_at"] != trade["closed_at"]:
        raise AqlaError("Manual closure position history is incomplete or changed")
    if closure.get("account_position_proof") and proof["account_position_proof"] != closure["account_position_proof"]:
        raise AqlaError("The stored native closure position proof changed")
    sign = Decimal(1 if plan.side == "long" else -1)
    own, gross, fees, exited = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    allocations, fee_evidence, timeline = {}, {}, []
    proportional = False
    entries = [row for row in observed.values() if roles.get(row["oid"], {}).get("kind") == "entry"]
    notional = sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in entries)
    entry = notional / Decimal(trade["filled_quantity"])
    if entry != Decimal(trade["entry_vwap"]) or notional != Decimal(trade["entry_accounting"]["entry_notional"]):
        raise AqlaError("Frozen own entry prices or margin evidence changed")
    for step in proof["account_position_proof"]:
        identity = fill_identity(step)
        raw = observed[identity]
        amount, start = Decimal(raw["sz"]), Decimal(raw["startPosition"])
        delta = amount * (1 if raw["side"] == "B" else -1)
        role = roles.get(raw["oid"])
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
        if part:
            if raw.get("fee") is None or raw.get("feeToken") != plan.market.quote_token:
                raise AqlaError("Attributed fill fees or currency are incomplete")
            fee = Decimal(raw["fee"])
            if not fee.is_finite():
                raise AqlaError("Attributed fill fee is not finite")
            allocated_fee = fee * part / amount
            fees += allocated_fee
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
            proportional |= part != amount
        timeline.append((raw["time"], own * sign, start + delta))
    if own or exited != Decimal(trade["filled_quantity"]):
        raise AqlaError("Complete own closure quantity is not proved")
    assigned = {}
    for fact in payments:
        stamp = fact["time"]
        if (fact["market"] != plan.market.symbol or fact["currency"] != plan.market.quote_token
                or not trade["entry_accounting"]["entry_at"] <= stamp <= trade["closed_at"]
                or any(time == stamp for time, _, _ in timeline)):
            raise AqlaError("Funding market, currency, interval or ordering is unconfirmed")
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
        allocation = {**fact, "amount": str(amount * own_size / account_size),
            "allocation": {"account_amount": fact["amount"], "account_signed_size": str(account_size), "own_signed_size": str(own_size)}}
        if fact["identity"] in assigned and assigned[fact["identity"]] != allocation:
            raise AqlaError("Funding identity changed within the historical response")
        assigned[fact["identity"]] = allocation
        proportional |= own_size != account_size
    if any(identity not in assigned or assigned[identity] != fact for identity, fact in trade.get("funding_events", {}).items()):
        raise AqlaError("Previously assigned funding evidence changed or disappeared")
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
