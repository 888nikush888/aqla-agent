"""Durable, sanitized notices for meaningful recovery transitions."""
import re

from aqla_hyperliquid_mcp.models import digest, now_ms


def safe_report_identifiers(row, fact):
    for key in ("trade_id", "operation_id", "account_key", "exchange", "market"):
        value = row.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128 and all(char.isalnum() or char in ":_-" for char in value):
            fact[key] = value


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
    safe_report_identifiers(row, fact)
    return fact


def complete_recovery_accounts(account_passes, observed_at):
    complete = set()
    for account, row in (account_passes or {}).items():
        if not isinstance(account, str) or not isinstance(row, dict):
            continue
        attempted, succeeded, duration = row.get("last_attempt_at"), row.get("last_success_at"), row.get("duration_ms")
        if (row.get("category") == "complete" and type(row.get("error_count")) is int and row["error_count"] == 0
                and type(attempted) is int and type(succeeded) is int and type(duration) is int
                and 0 <= attempted <= succeeded <= observed_at and 0 <= duration <= 120000
                and observed_at - succeeded <= 120000):
            complete.add(account)
    return complete


def record_recovery_transitions(data, status, active, complete, protected_accounts, observed_at):
    for key in sorted(set(status) | set(active)):
        old = status.get(key, {})
        observed = active.get(key, [])
        previous = old.get("reports", []) if old.get("active") else []
        # A timeout may replace all per-trade reports with one account error.
        # Keep previously observed facts until their whole account is positively
        # reconciled; new/worsening account and trade errors still get published.
        retained = [row for row in previous if row.get("account_key") not in complete]
        facts = sorted({digest(row): row for row in [*retained, *observed]}.values(), key=digest)
        signature = digest(facts)
        if old.get("signature") == signature:
            continue
        if not facts and not old.get("active"):
            continue
        generation = old.get("generation", 0) + 1
        identity = "recovery:" + digest([key, generation, signature])
        data["notifications"].setdefault(identity, {"recovery_reports": facts or old.get("reports", []),
            "resolved": not facts, "observed_at": observed_at, "severity": "urgent" if any(
                row["code"] in ("replacement_unconfirmed", "protection_unconfirmed") or row.get("account_key") in protected_accounts
                for row in facts) else "normal"})
        status[key] = {"signature": signature, "generation": generation, "active": bool(facts), "reports": facts}


def record_recovery_notices(data, reports, account_passes=None):
    # These are diagnostics captured from THIS recovery call, not the durable
    # last-known account_recovery projection. Missing/stale/partial observations
    # never prove that an earlier protection problem has disappeared.
    observed_at = now_ms()
    complete = complete_recovery_accounts(account_passes, observed_at)
    active = {}
    for row in reports:
        fact = safe_report(row)
        if fact:
            key = digest([fact.get("account_key", fact.get("exchange")), fact.get("trade_id"), fact.get("operation_id")])
            active.setdefault(key, []).append(fact)
    status = data.setdefault("source_status", {}).setdefault("recovery", {})
    protected_accounts = {row["account_key"] for old in status.values() if old.get("active")
                          for row in old.get("reports", []) if row.get("account_key")
                          and row.get("code") in ("replacement_unconfirmed", "protection_unconfirmed")}
    record_recovery_transitions(data, status, active, complete, protected_accounts, observed_at)


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
    data["notifications"].setdefault(identity, {"source_notice": {"code": code, "category": category, "sources": safe_sources}, "observed_at": now_ms()})
