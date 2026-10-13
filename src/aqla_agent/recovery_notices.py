"""Durable, sanitized notices for meaningful recovery transitions."""
import re

from aqla_hyperliquid_mcp.models import digest, now_ms


def safe_report(row):
    if not (row.get("error") or row.get("replacement_unconfirmed") or row.get("market_conflict")):
        return None
    code = "market_conflict" if row.get("market_conflict") else "replacement_unconfirmed" if row.get("replacement_unconfirmed") else "reconciliation_unavailable"
    error = row.get("error", "")
    # Only fixed internal categories enter a public notice. RPC/exception text
    # is deliberately discarded, including credentials embedded in payloads.
    for needle, category in (("timed out", "recovery_timeout"), ("cancellation is unconfirmed", "owned_order_cancellation_unconfirmed"),
                             ("Rank-changed", "entry_cancellation_unconfirmed"), ("binding", "account_binding_mismatch"),
                             ("No confirmed effective stop loss", "protection_unconfirmed")):
        if isinstance(error, str) and needle in error:
            code = category
    fact = {"code": code}
    if type(row.get("old_stop_retained")) is bool:
        fact["old_stop_retained"] = row["old_stop_retained"]
    nested = row.get("operation")
    if isinstance(nested, dict) and "operation_id" not in row:
        row = {**row, "operation_id": nested.get("operation_id")}
    for key in ("trade_id", "operation_id", "account_key", "exchange", "market"):
        value = row.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128 and all(char.isalnum() or char in ":_-" for char in value):
            fact[key] = value
    return fact


def record_recovery_notices(data, reports):
    active = {}
    for row in reports:
        fact = safe_report(row)
        if fact:
            key = digest([fact.get("account_key", fact.get("exchange")), fact.get("trade_id"), fact.get("operation_id")])
            active.setdefault(key, []).append(fact)
    status = data.setdefault("source_status", {}).setdefault("recovery", {})
    for key in sorted(set(status) | set(active)):
        facts = sorted({digest(row): row for row in active.get(key, [])}.values(), key=digest)
        signature = digest(facts)
        old = status.get(key, {})
        if old.get("signature") == signature:
            continue
        if not facts and not old.get("active"):
            continue
        generation = old.get("generation", 0) + 1
        identity = "recovery:" + digest([key, generation, signature])
        data["notifications"].setdefault(identity, {"recovery_reports": facts or old.get("reports", []),
            "resolved": not facts, "observed_at": now_ms(), "severity": "urgent" if any(row["code"] in ("replacement_unconfirmed", "protection_unconfirmed") for row in facts) else "normal"})
        status[key] = {"signature": signature, "generation": generation, "active": bool(facts), "reports": facts}


def reader_notice_signature(status):
    """Retry counters/deadlines are diagnostics, never a new outage identity."""
    status = status or {}
    return digest([status.get("code", "reader_recovered"), sorted(set(status.get("sources", [])))])


def record_source_notice(data, code, version, sources=()):
    code = code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() and len(code) <= 80 else "source_status_changed"
    category = "conflicts" if code.startswith("source_conflict") else "reader"
    previous = data.setdefault("source_status", {}).get(category, {})
    if not previous and code in ("reader_recovered", "source_conflict_resolved"):
        return
    signature = digest([code, version])
    if previous.get("signature") == signature:
        return
    transition = previous.get("transition", 0) + 1
    data["source_status"][category] = {"signature": signature, "transition": transition}
    identity = "source-status:" + digest([category, transition])
    safe_sources = sorted({value for value in sources if isinstance(value, str) and re.fullmatch(r"-?[1-9]\d*|@[A-Za-z][A-Za-z0-9_]{3,31}", value)})
    data["notifications"].setdefault(identity, {"source_notice": {"code": code, "category": category, "sources": safe_sources}})
