"""Bounded local model views; retained observations never prove historical absence."""
import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aqla_hyperliquid_mcp.models import AqlaError, digest

from .sources import resolve_source

BYTE_CAP = 8192
BERLIN = ZoneInfo("Europe/Berlin")
TRADE_STATUSES = frozenset({"waiting_entry", "entry_pending", "preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted", "closed", "cancelled", "rejected"})
EVENT_STATUSES = frozenset({"pending", "context", "report", "unrelated", "incomplete", "ambiguous", "interpreted", "planned", "executed", "updated", "rejected", "superseded", "failed", "executing", "update_pending"})


def size(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str).encode())


def aware_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return int(parsed.timestamp() * 1000)
    except (TypeError, AttributeError, ValueError) as error:
        raise AqlaError("Use an aware ISO timestamp with UTC or an explicit offset, for example 2026-10-11T17:39:00+02:00") from error


def timestamp(value):
    return datetime.fromtimestamp(value / 1000, UTC).astimezone(BERLIN).isoformat() if value is not None else None


def shrink(value, characters=200, items=8, depth=0):
    if depth >= 4 and isinstance(value, (dict, list)):
        return {"details_omitted": True, "kind": type(value).__name__, "items": len(value)}
    if isinstance(value, str) and len(value) > characters:
        return {"excerpt": value[:characters], "characters": len(value), "body_complete": False}
    if isinstance(value, list):
        return [shrink(row, characters, items, depth + 1) for row in value[:items]] + ([{"omitted_items": len(value) - items}] if len(value) > items else [])
    if isinstance(value, dict):
        result = {key: shrink(row, characters, items, depth + 1) for key, row in list(value.items())[:items]}
        if len(value) > items:
            result["omitted_fields"] = len(value) - items
        if result != value and "_verified" in value:
            result.update(_verified=False, full_record_fingerprint_verified=value["_verified"], body_complete=False)
        return result
    return value


def page(rows, offset, limit, metadata, *, max_bytes=BYTE_CAP):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AqlaError("Use offset >= 0 and limit 1–100")
    if offset > len(rows):
        raise AqlaError("Offset exceeds this retained result; restart at offset 0")
    result = {**metadata, "rows": [], "total": len(rows), "offset": offset, "next_offset": None,
              "retained_page_complete": True, "historical_complete": False, "absence_proves_never_received": False,
              "untrusted_data": True}
    for row in rows[offset:offset + limit]:
        candidate = {**result, "rows": [*result["rows"], row], "next_offset": offset + len(result["rows"]) + 1}
        if size(candidate) > max_bytes:
            if result["rows"]:
                break
            row = shrink(row, characters=48, items=4)
            candidate = {**result, "rows": [row], "next_offset": offset + 1, "body_complete": False}
            if size(candidate) > max_bytes:
                raise AqlaError("Record exceeds the compact byte budget; request its full details for the native result reader")
        result = candidate
    next_offset = offset + len(result["rows"])
    result["next_offset"] = next_offset if next_offset < len(rows) else None
    result["retained_page_complete"] = result["next_offset"] is None
    return result


def compact_view(value, tool, arguments, offset=0, limit=20, view_version=None):
    version = digest(value)
    if view_version is not None and view_version != version or offset and view_version is None:
        raise AqlaError("This live compact view requires the same view_version for continued pages. If it changed, restart at offset 0 or request the full view and page its immutable native output_id.")
    rows = []
    for name, item in value.items():
        entries = list(enumerate(item)) if isinstance(item, list) else list(item.items()) if isinstance(item, dict) else [(None, item)]
        rows.extend({"section": name, "key": key, "value": shrink(row)} for key, row in entries)
    result = page(rows, offset, limit, {"view": "compact", "view_version": version, "paging_basis": "live_snapshot",
        "continuation_requires_same_view_version": True, "full_details": {"tool": tool, "arguments": {**arguments, "view": "full"}},
        "full_body_available": True})
    while True:
        result["next_offset"] = offset + len(result["rows"]) if offset + len(result["rows"]) < len(rows) else None
        result["retained_page_complete"] = result["next_offset"] is None
        if result["next_offset"] is not None:
            pagination = {"view_offset": result["next_offset"], "view_limit": limit} if tool == "journal_read" else {"offset": result["next_offset"], "limit": limit}
            result["next_page"] = {"tool": tool, "arguments": {**arguments, **pagination, "view": "compact", "view_version": version}}
        if size(result) <= BYTE_CAP:
            break
        if len(result["rows"]) <= 1:
            raise AqlaError("Compact view metadata exceeds its byte budget; request the full view for immutable native output recovery")
        result["rows"].pop()
    return result


def source_attribution(snapshot, plan):
    revisions = plan.get("source_revisions", {})
    originals, missing, later, unbound_edits = {}, [], [], []
    for source_id in plan.get("source_refs", []):
        identity = revisions.get(source_id)
        original = snapshot["events"].get(identity)
        if original is None:
            missing.append({"source_id": source_id, "event_id": identity, "reason": "original_revision_not_retained" if identity else "legacy_plan_has_no_revision_binding"})
            unbound_edits.extend({"event_id": row["event_id"], "source_id": source_id, "source": row["source"],
                                  "relative_to_missing_original": "unknown", "is_edited_source": row["source"].get("edited_at") is not None}
                                 for row in snapshot["events"].values() if row["source_id"] == source_id)
        else:
            originals[source_id] = {"event_id": identity, "source": original["source"]}
        if original:
            version = original["source"].get("edited_at") or original["source"]["sent_at"]
            later.extend({"event_id": row["event_id"], "source_id": source_id, "source": row["source"]} for row in snapshot["events"].values()
                         if row["source_id"] == source_id and (row["source"].get("edited_at") or row["source"]["sent_at"]) > version)
    return {"source_revisions": copy.deepcopy(revisions), "original_sources": originals,
            "missing_original_sources": missing, "later_source_edits": later, "retained_revisions_with_missing_original": unbound_edits}


def source_lookup(snapshot, start, end, *, source=None, symbol=None, status=None, account=None, network=None,
                  offset=0, limit=20, cursor=None, time_field="sent_at"):
    lower, upper = aware_time(start), aware_time(end)
    if time_field not in ("sent_at", "received_at"):
        raise AqlaError("time_field must be sent_at or received_at")
    if lower >= upper:
        raise AqlaError("The aware interval must have start < end; end is exclusive")
    selector = resolve_source(snapshot, source) if source else None
    selector = snapshot.get("source_aliases", {}).get(selector, selector)
    query = {"start": lower, "end": upper, "source": selector, "symbol": symbol, "status": status,
             "account": account, "network": network, "time_field": time_field}
    valid_statuses = TRADE_STATUSES | EVENT_STATUSES | {row["status"] for group in (snapshot.get("events", {}), snapshot.get("trades", {})) for row in group.values()}
    if status and status not in valid_statuses:
        raise AqlaError("Unknown retained status. Available statuses: " + ", ".join(sorted(valid_statuses)))
    by_source, uncertain = {}, {}
    for trade in snapshot.get("trades", {}).values():
        for source_id in trade["plan"]["source_refs"]:
            by_source.setdefault(source_id, []).append(trade)
    for operation in snapshot.get("operations", {}).values():
        if operation["phase"] in ("prepared", "sending", "unknown"):
            for identity in operation["operation_id"].split(":"):
                if identity in snapshot.get("trades", {}):
                    uncertain[identity] = uncertain.get(identity, 0) + 1
    rows = []
    relevant = [event for event in snapshot.get("events", {}).values() if lower <= event["source"][time_field] < upper
                and (selector is None or event["source"]["chat_id"] == selector)]
    for event in sorted(relevant, key=lambda row: (row["source"][time_field], row["source"].get("edited_at") or 0, row["event_id"])):
        body = event["source"]
        if not lower <= body[time_field] < upper or selector and body["chat_id"] != selector:
            continue
        trades = by_source.get(event["source_id"], [])
        for trade in trades or [None]:
            plan = trade["plan"] if trade else {}
            event_symbol = (event.get("interpretation") or {}).get("symbol")
            if symbol and symbol.casefold() not in {str(event_symbol).casefold(), str(plan.get("market", {}).get("symbol")).casefold()}:
                continue
            if (status and status not in (event["status"], trade["status"] if trade else None)
                    or account and account.casefold() not in {str(plan.get("account")).casefold(), str(plan.get("account_key")).casefold()}
                    or network and plan.get("network") != network):
                continue
            entries = [fill for fill in (trade or {}).get("fill_events", {}).values() if fill.get("kind") == "entry"]
            rows.append({"event_id": event["event_id"], "source_id": event["source_id"], "revision_event_id": event["event_id"],
                "source_chat_id": body["chat_id"], "source_name": snapshot.get("source_names", {}).get(body["chat_id"]),
                "sent_at": timestamp(body["sent_at"]), "received_at": timestamp(body.get("received_at")), "edited_at": timestamp(body.get("edited_at")),
                "event_status": event["status"], "symbol": plan.get("market", {}).get("symbol", event_symbol),
                "body_excerpt": body.get("text", "")[:200], "body_complete": len(body.get("text", "")) <= 200,
                "source_body_sha256": hashlib.sha256(body.get("text", "").encode()).hexdigest(),
                "source_details": {"tool": "source_context", "arguments": {"event_id": event["event_id"], "view": "full"}},
                "trade_id": (trade or {}).get("trade_id"), "plan_id": plan.get("plan_id"), "trade_status": (trade or {}).get("status"),
                "account": plan.get("account"), "network": plan.get("network"), "exchange": plan.get("exchange", "hyperliquid") if plan else None,
                "quote_token": plan.get("market", {}).get("quote_token"),
                "plan_source_revision": plan.get("source_revisions", {}).get(event["source_id"]),
                "is_plan_source_revision": plan.get("source_revisions", {}).get(event["source_id"]) == event["event_id"] if plan else None,
                "confirmed_own_entry_fill_count": len(entries), "confirmed_own_entry_quantity": str(sum((Decimal(fill["sz"]) for fill in entries), Decimal(0))) if entries else None,
                "event_status_is_fill_proof": False, "uncertain_operation_count": uncertain.get((trade or {}).get("trade_id"), 0),
                "trade_details": {"tool": "trade_diagnostics", "arguments": {"trade_id": trade["trade_id"], "view": "full"}} if trade else None})
    retained_revisions = len({row["event_id"] for row in rows})
    missing = 0
    for trade in snapshot.get("trades", {}).values():
        plan = trade["plan"]
        if (selector and trade.get("chat_id") != selector or symbol and plan["market"]["symbol"].casefold() != symbol.casefold()
                or network and plan["network"] != network or account and account.casefold() not in {str(plan.get("account")).casefold(), str(plan.get("account_key")).casefold()}
                or status in TRADE_STATUSES - EVENT_STATUSES and trade["status"] != status):
            continue
        for source_id in plan["source_refs"]:
            original = plan.get("source_revisions", {}).get(source_id)
            if original not in snapshot.get("events", {}):
                missing += 1
                rows.append({"kind": "missing_original_source_revision", "source_id": source_id, "event_id": original,
                    "trade_id": trade["trade_id"], "plan_id": plan["plan_id"], "trade_status": trade["status"],
                    "source_time": None, "interval_membership": "unknown_original_timestamp_not_retained",
                    "status_filter_membership": "unknown_original_event_status" if status else None,
                    "account": plan.get("account"), "network": plan["network"], "exchange": plan.get("exchange", "hyperliquid"),
                    "trade_details": {"tool": "trade_diagnostics", "arguments": {"trade_id": trade["trade_id"], "view": "full"}}})
    binding = digest([query, rows])
    if cursor is not None:
        try:
            token = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if offset != 0 or token["binding"] != binding or type(token["offset"]) is not int:
                raise ValueError
            offset = token["offset"]
        except (ValueError, KeyError, TypeError) as error:
            raise AqlaError("Source lookup cursor is invalid or its interval, filters or matching retained projections changed; restart at offset 0") from error
    result = page(rows, offset, limit, {"interval": query, "status_filter_domain": "event_status_or_bound_trade_status",
        "retained_event_count": len(snapshot.get("events", {})), "pruned_event_count": len(snapshot.get("event_tombstones", {})),
        "matching_retained_source_revisions": retained_revisions, "undated_saved_source_bindings": missing,
        "cursor_basis": "query_and_matching_retained_projections", "retained_totals_scope": "current_snapshot"}, max_bytes=BYTE_CAP - 768)
    result["next_cursor"] = base64.urlsafe_b64encode(json.dumps({"binding": binding, "offset": result["next_offset"]}, separators=(",", ":")).encode()).decode() if result["next_offset"] is not None else None
    return result
