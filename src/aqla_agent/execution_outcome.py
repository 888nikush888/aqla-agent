"""Local execution facts. A consumed source or saved plan is never fill proof."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


class EntryOperations:
    """Index exact entry identities once; never associate SL/close or substrings."""

    def __init__(self, operations):
        self.by_id, self.by_trade = {}, {}
        records = operations.items() if isinstance(operations, dict) else ((None, row) for row in operations)
        for key, row in records:
            identity = row.get("operation_id", key)
            if not isinstance(identity, str):
                continue
            self.by_id[identity] = row
            if identity.startswith("entry:"):
                trade_id = identity.split(":", 2)[1]
                self.by_trade.setdefault(trade_id, []).append(row)

    def for_trade(self, trade):
        rows = self.by_trade.get(trade.get("trade_id"), [])
        current = self.by_id.get(trade.get("entry_operation_id"))
        return [*rows, current] if current is not None and all(current is not row for row in rows) else rows


def quantity(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def retry_view(row):
    """Expose bounded categories/times only, never an arbitrary provider error."""
    marker = row.get("observation_retry")
    if not isinstance(marker, dict):
        return None
    output = {key: marker[key] for key in ("attempts", "next_retry_at", "deadline_at")
              if type(marker.get(key)) is int and 0 <= marker[key] < 253402300800000}
    if type(marker.get("exhausted")) is bool:
        output["exhausted"] = marker["exhausted"]
    if marker.get("authority") in ("execute", "preview"):
        output["authority"] = marker["authority"]
    for key in ("code", "stage", "category", "exception_type"):
        value = marker.get(key)
        if isinstance(value, str) and len(value) <= 64 and all(char.isalnum() or char in "_-" for char in value):
            output[key] = value
    return output


def diagnostic_view(trade, operations=()):
    """Public error identity only; message/trace/account details never escape."""
    diagnostic = trade.get("entry_diagnostic")
    if not isinstance(diagnostic, dict):
        index = operations if isinstance(operations, EntryOperations) else EntryOperations(operations)
        result = index.by_id.get(trade.get("entry_operation_id"), {}).get("result")
        diagnostic = result.get("diagnostic") if isinstance(result, dict) else None
    if not isinstance(diagnostic, dict):
        return None
    output = {}
    for key in ("code", "stage", "category", "exception_type"):
        value = diagnostic.get(key)
        if isinstance(value, str) and len(value) <= 64 and all(char.isalnum() or char in "_-" for char in value):
            output[key] = value
    if type(diagnostic.get("retryable")) is bool:
        output["retryable"] = diagnostic["retryable"]
    return output or None


def operation_diagnostic_view(operation):
    """Sanitize an embedded diagnostic without rewriting the saved receipt."""
    result = operation.get("result")
    if not isinstance(result, dict) or "diagnostic" not in result:
        return operation
    return {**operation, "result": {**result, "diagnostic": diagnostic_view({"entry_diagnostic": result["diagnostic"]})}}


def trade_outcome(trade, operations=()):
    """Separate receipt-backed entry history from reliable current ownership."""
    operation_id = trade.get("entry_operation_id")
    index = operations if isinstance(operations, EntryOperations) else EntryOperations(operations)
    entry_ops = index.for_trade(trade)
    current = index.by_id.get(operation_id, {})
    outcome = current.get("outcome")
    fills = list(trade.get("fill_events", {}).values())
    entries = [row for row in fills if row.get("kind") == "entry"]
    valid = all(row.get("kind") in ("entry", "tp", "sl", "close") and quantity(row.get("sz")) is not None and quantity(row.get("sz")) > 0 for row in fills)
    entered = sum((quantity(row["sz"]) for row in entries), Decimal(0)) if valid else None
    exited = sum((quantity(row["sz"]) for row in fills if row.get("kind") != "entry"), Decimal(0)) if valid else None
    recorded = quantity(trade.get("filled_quantity"))
    remainder = quantity(trade.get("remaining_quantity"))
    consistent = entered is not None and exited <= entered and recorded == entered and remainder == entered - exited
    stage = trade.get("status")
    conflict = stage == "conflicted" or bool(trade.get("external_conflict"))
    uncertain = stage == "unknown" or any(row.get("phase") in ("prepared", "sending", "unknown") for row in entry_ops)
    missing = recorded is None or entered is None or (recorded > 0 and not entries)
    submission = ("unconfirmed" if uncertain else "not_sent" if outcome == "not_sent" else
                  "accepted" if outcome in ("accepted", "partial") else "sent_rejected" if outcome == "rejected" else "unknown")
    retry = retry_view(trade)
    if conflict:
        state = "conflicted"
    elif uncertain or missing or entries and not consistent:
        state = "unknown"
    elif entered and stage in ("closed", "cancelled", "rejected") and remainder != 0:
        state = "unknown"
    elif entered and consistent:
        state = "closed" if remainder == 0 and stage in ("closed", "cancelled", "rejected") else "filled"
    elif retry and not retry.get("exhausted"):
        state = "pending_observation"
    elif stage == "waiting_entry":
        state = "waiting_entry"
        submission = "not_sent" if not entry_ops else submission
    elif stage in ("rejected", "cancelled") and recorded == 0 and not entries:
        state = "not_sent" if submission == "not_sent" or not entry_ops and not trade.get("orders") else "no_entry_fill"
        if state == "not_sent":
            submission = "not_sent"
    else:
        state = "pending_entry" if stage in ("entry_pending", "preparing", "opening", "protecting") else "unknown"
    ownership_confirmed = consistent and not conflict and not uncertain and state not in ("unknown", "conflicted", "pending_entry")
    return {"state": state, "entry_submission": submission, "entry_fill_confirmed": True if entries and entered else None if missing or uncertain or state == "unknown" else False,
            "confirmed_entry_fill_count": len(entries) if valid else None,
            "confirmed_entry_quantity": str(entered) if entries and entered is not None else None,
            "known_own_remaining_quantity": str(remainder) if ownership_confirmed else None,
            "current_ownership_confirmed": ownership_confirmed,
            "historical_fill_evidence_missing": missing, "observation_retry": retry,
            "entry_diagnostic": diagnostic_view(trade, index)}


def event_outcomes(snapshot, event, operations=None):
    """Use exact immutable source revisions, preserving independent target scopes."""
    rows = []
    covered = set()
    targets = event.get("targets", {})
    operations = EntryOperations(snapshot.get("operations", {})) if operations is None else operations
    for identity, target in targets.items():
        trade = snapshot.get("trades", {}).get(target.get("trade_id"))
        saved = snapshot.get("plans", {}).get(target.get("plan_id"), {})
        plan = trade.get("plan", {}) if trade else saved.get("plan", {})
        facts = source_trade_outcome(event, trade, operations) if trade else {
            "state": "pending_observation" if (retry := retry_view(target) or retry_view(saved)) and not retry.get("exhausted") else
                     "unknown" if target.get("trade_id") else
                     "planned" if target.get("status") == "planned" else "not_sent" if target.get("status") == "rejected" else "unknown",
            "entry_fill_confirmed": False if not target.get("trade_id") and target.get("status") in ("planned", "rejected") else None,
            "observation_retry": retry}
        rows.append({"target_id": identity, "trade_id": target.get("trade_id"), "plan_id": target.get("plan_id"),
                     **{key: target.get(key, plan.get(key)) for key in ("exchange", "network", "account_key", "profile_id")},
                     "quote_token": plan.get("market", {}).get("quote_token"),
                     "source_revision_matches": revision_matches(event, plan),
                     **facts})
        covered.add(target.get("trade_id"))
    for trade in snapshot.get("trades", {}).values():
        plan = trade.get("plan", {})
        if trade.get("trade_id") in covered or plan.get("source_revisions", {}).get(event.get("source_id")) != event.get("event_id"):
            continue
        rows.append({"trade_id": trade["trade_id"], "plan_id": plan.get("plan_id"),
                     **{key: plan.get(key) for key in ("exchange", "network", "account_key", "profile_id")},
                     "quote_token": plan.get("market", {}).get("quote_token"),
                     "source_revision_matches": True, **source_trade_outcome(event, trade, operations)})
    covered_plans = {row.get("plan_id") for row in rows}
    candidates = sorted(((identity, saved) for identity, saved in snapshot.get("plans", {}).items()
                         if not targets and saved.get("event_id") == event.get("event_id")),
                        key=lambda item: (item[1].get("plan", {}).get("created_at", 0), item[0]), reverse=True)
    for identity, saved in candidates:
        plan = saved.get("plan", {})
        if targets or identity in covered_plans or plan.get("trade_id") in covered or saved.get("event_id") != event.get("event_id"):
            continue
        retry = retry_view(saved)
        stored_trade = snapshot.get("trades", {}).get(plan.get("trade_id"))
        rows.append({"plan_id": identity, "trade_id": plan.get("trade_id"),
                     **{key: plan.get(key) for key in ("exchange", "network", "account_key", "profile_id")},
                     "quote_token": plan.get("market", {}).get("quote_token"),
                     "source_revision_matches": revision_matches(event, plan),
                     **(source_trade_outcome(event, stored_trade, operations) if stored_trade else {
                         "state": "pending_observation" if retry and not retry.get("exhausted") else "not_sent" if retry and retry.get("exhausted") else "planned",
                         "entry_fill_confirmed": False, "observation_retry": retry})})
        covered.add(plan.get("trade_id"))
    return {"event_status_is_fill_proof": False, "event_status_meaning": "source_processing_or_consumed_binding",
            "targets": rows, "basis": "retained_local_receipts_not_live_exchange_inventory"}


def revision_matches(event, plan):
    revision = plan.get("source_revisions", {}).get(event.get("source_id"))
    return revision == event.get("event_id") if revision else None


def source_trade_outcome(event, trade, operations=()):
    history = trade_outcome(trade, operations)
    matches = revision_matches(event, trade.get("plan", {}))
    update = (event.get("interpretation") or {}).get("kind") == "trade_update"
    if update or matches is not True:
        return {"state": "update_only" if update else "source_revision_unconfirmed", "entry_fill_confirmed": None,
                "source_revision_matches": matches, "bound_trade_history": history}
    return history


def execution_counts(snapshot):
    active = {"waiting_entry", "entry_pending", "preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted"}
    operations = EntryOperations(snapshot.get("operations", {}))
    facts = [(row.get("status"), trade_outcome(row, operations)) for row in snapshot.get("trades", {}).values()]
    return {"known_own_open": sum(status in active and row["current_ownership_confirmed"] and quantity(row["known_own_remaining_quantity"]) > 0 for status, row in facts),
            "pending_entries": sum(status in active and row["state"] in ("waiting_entry", "pending_entry", "pending_observation") for status, row in facts),
            "unsettled_ownership": sum(status in active and row["state"] in ("conflicted", "unknown") for status, row in facts),
            "historical_unconfirmed": sum(status not in active and row["state"] == "unknown" for status, row in facts),
            "basis": "retained_local_receipts_not_live_exchange_inventory"}
