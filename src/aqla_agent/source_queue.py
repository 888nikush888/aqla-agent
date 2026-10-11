"""Pure source revision selection and conservative queue maintenance."""
from aqla_hyperliquid_mcp.models import AqlaError, SourceEvent, digest

UNFINISHED = {"pending", "interpreted", "needs_reconciliation"}


def is_source_row(row):
    """Opaque legacy history is retained, never admitted as a current source."""
    if not isinstance(row, dict) or not isinstance(row.get("event_id"), str) or not isinstance(row.get("source_id"), str):
        return False
    try:
        SourceEvent.model_validate(row.get("source"))
    except (ValueError, TypeError):
        return False
    return True


def binding_trade_id(binding):
    return binding.get("trade_id") if isinstance(binding, dict) else binding if isinstance(binding, str) else None


def version(row):
    return row["source"].get("edited_at") or row["source"]["sent_at"]


def content(row):
    source = row["source"]
    media = source.get("media", "text")
    if source.get("text", "").strip() and media in ("text", "caption"):
        media = "body"
    return [source.get(key) for key in ("text", "reply_to_id", "thread_id", "forward_from", "album_id")] + [media]


def groups(events, refs=None):
    result = {}
    for row in events.values():
        if is_source_row(row) and (refs is None or row["source_id"] in refs):
            result.setdefault(row["source_id"], []).append(row)
    return result


def current_rows(rows):
    maximum = max(map(version, rows))
    return [row for row in rows if version(row) == maximum]


def anchor_row(rows):
    return min(rows, key=lambda row: (not row.get("queue_anchor", False),
               row.get("status") not in ("interpreted", "executed", "updated"),
               row["source"].get("received_at", 0), row["event_id"]))


def canonical_sources(events, refs=None):
    result = {}
    for ref, rows in groups(events, refs).items():
        current = current_rows(rows)
        if len({digest(content(row)) for row in current}) > 1:
            raise AqlaError("Source revisions have conflicting content at the same timestamp")
        # Persisted anchor and acted status survive sorted-key SQLite restarts.
        result[ref] = anchor_row(current)
    return result


def recovery_ids(snapshot, event_rows=None):
    identities = {row["event_id"] for row in (event_rows if event_rows is not None else snapshot["events"].values())
                  if is_source_row(row) and row.get("status") == "needs_reconciliation"}
    for trade in snapshot.get("trades", {}).values():
        if trade.get("status") not in ("closed", "cancelled", "rejected"):
            identities.update(trade.get("plan", {}).get("source_revisions", {}).values())
            if trade.get("event_id"):
                identities.add(trade["event_id"])
        pending = trade.get("pending_stop_update")
        if pending and pending.get("phase") in ("waiting_fill", "replacing", "unknown"):
            identities.add(pending["event_id"])
            identities.update(pending.get("source_revisions", {}).values())
    for operation_id, operation in snapshot.get("operations", {}).items():
        if operation.get("phase") in ("prepared", "sending", "unknown"):
            request = operation.get("request", {})
            identities.update(request.get("source_revisions", {}).values())
            if request.get("event_id"):
                identities.add(request["event_id"])
            trade = snapshot.get("trades", {}).get(operation.get("trade_id"), {})
            identities.update(trade.get("plan", {}).get("source_revisions", {}).values())
            binding = snapshot.get("bindings", {}).get(operation_id, {})
            identities.update(snapshot.get("trades", {}).get(binding_trade_id(binding), {}).get("plan", {}).get("source_revisions", {}).values())
            if operation_id.startswith("update:") and operation_id[7:] in snapshot["events"]:
                identities.add(operation_id[7:])
            for saved in snapshot.get("plans", {}).values():
                plan = saved.get("plan", {})
                if operation_id in ("entry:" + plan.get("trade_id", ""), "leverage:" + plan.get("trade_id", "")):
                    identities.update(plan.get("source_revisions", {}).values())
    binding_roots = [snapshot.get("bindings", {})]
    binding_roots.extend(snapshot.get("account_bindings", {}).values())
    binding_roots.extend(snapshot.get("venue_bindings", {}).values())
    for bindings in binding_roots:
        for identity, binding in bindings.items():
            trade = snapshot.get("trades", {}).get(binding_trade_id(binding), {})
            if trade and trade.get("status") not in ("closed", "cancelled", "rejected"):
                if identity in snapshot["events"]:
                    identities.add(identity)
                identities.update(trade.get("plan", {}).get("source_revisions", {}).values())
    for operation_id, operation in snapshot.get("operations", {}).items():
        if operation.get("phase") in ("prepared", "sending", "unknown"):
            tokens = set(operation_id.split(":"))
            for saved in [*snapshot.get("trades", {}).values(), *snapshot.get("plans", {}).values()]:
                plan = saved.get("plan", {})
                if plan.get("trade_id") in tokens:
                    identities.update(plan.get("source_revisions", {}).values())
                    if saved.get("event_id"):
                        identities.add(saved["event_id"])
    return identities


def repair_queue(snapshot):
    """Mutate only unfinished unbound versions; callers supply an atomic transaction."""
    protected = recovery_ids(snapshot)
    for rows in groups(snapshot["events"]).values():
        current = current_rows(rows)
        conflict = len({digest(content(row)) for row in current}) > 1
        anchor = anchor_row(current)
        signature = digest(sorted(row["event_id"] for row in current)) if conflict else None
        for row in rows:
            if row["event_id"] in protected or row.get("status") not in ("pending", "interpreted"):
                continue
            if row not in current or not conflict and row is not anchor:
                row.update(status="superseded",
                           superseded_reason="older_revision" if version(row) < version(anchor) else "equivalent_revision")
                if conflict:
                    row.update(successor_conflict_signature=signature, successor_candidates=sorted(item["event_id"] for item in current))
                    row.pop("successor_event_id", None)
                else:
                    row["successor_event_id"] = anchor["event_id"]
                row.pop("source_quarantine", None)
            elif conflict:
                row["source_quarantine"] = {"signature": signature, "reason": "conflicting current content"}
            else:
                row.pop("source_quarantine", None)
                row["queue_anchor"] = True


def queue_overview(snapshot, limit=50, *, event_rows=None):
    events = snapshot["events"]
    event_rows = [row for row in (events.values() if event_rows is None else event_rows) if is_source_row(row)]
    recovery = recovery_ids(snapshot, event_rows)
    grouped = {}
    for row in event_rows:
        grouped.setdefault(row["source_id"], []).append(row)
    ready, blocked, signatures = [], [], []
    recovery_work = {identity for identity in recovery if identity in events and is_source_row(events[identity]) and events[identity].get("status") in UNFINISHED}
    historical_conflicts = []
    for ref, rows in grouped.items():
        current = current_rows(rows)
        if len(rows) > len(current):
            historical = {}
            for row in rows:
                if version(row) < version(current[0]):
                    historical.setdefault(version(row), []).append(row)
            for stamp, previous in historical.items():
                if len(previous) > 1 and len({digest(content(row)) for row in previous}) > 1:
                    historical_conflicts.append({"source_id": ref, "version": stamp, "signature": digest(sorted(row["event_id"] for row in previous))})
        if len(current) > 1 and len({digest(content(row)) for row in current}) > 1:
            unfinished = [row["event_id"] for row in current if row.get("status") in UNFINISHED]
            if unfinished:
                blocked.extend(unfinished)
                signatures.append(digest(sorted(row["event_id"] for row in current)))
        elif anchor_row(current).get("status") in ("pending", "interpreted") and anchor_row(current)["event_id"] not in recovery:
            ready.append(anchor_row(current)["event_id"])
    operations = [identity for identity, operation in snapshot.get("operations", {}).items() if operation.get("phase") in ("prepared", "sending", "unknown")]
    return {"ready_total": len(ready), "ready": ready[:limit],
            "raw_unfinished_total": sum(row.get("status") in UNFINISHED for row in event_rows),
            "superseded_total": sum(row.get("status") == "superseded" for row in event_rows),
            "blocked_total": len(signatures), "blocked_revision_total": len(blocked), "blocked": blocked[:limit],
            "blocked_signature": digest(sorted(signatures)) if signatures else None,
            "recovery_total": len(recovery_work) + len(operations), "recovery_source_total": len(recovery_work), "recovery": sorted(recovery_work)[:limit],
            "recovery_operation_total": len(operations), "recovery_operations": sorted(operations)[:limit],
            "historical_conflict_total": len(historical_conflicts), "historical_conflicts": historical_conflicts[:limit]}
