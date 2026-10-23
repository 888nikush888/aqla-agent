"""Immutable entry/exit facts; rapid closures cannot erase entry notifications."""
from __future__ import annotations

import copy
from decimal import Decimal

from aqla_hyperliquid_mcp.models import now_ms


def exit_reason(trade, fill) -> tuple[str, str]:
    if fill["kind"] == "sl":
        return "stop_loss", "Stop-Loss"
    if fill["kind"] == "tp":
        return "take_profit", "Take-Profit"
    if fill["kind"] == "close":
        match: dict = next((order for order in trade.get("exit_orders", []) if order.get("oid") == fill["oid"] or trade.get("order_observations", {}).get(order["cloid"], {}).get("oid") == fill["oid"]), {})
        reason = match.get("reason", "unconfirmed")
        label = {"operator_close": "Nutzerauftrag", "source_close": "Quellenauftrag", "protection_failure": "Schutzmaßnahme"}.get(reason, "Grund nicht vollständig belegt")
        return reason, label
    return "unconfirmed", "Grund nicht vollständig belegt"


def close_reason(trade):
    fills = list(trade.get("fill_events", {}).values())
    exits = [row for row in fills if row["kind"] != "entry"]
    entered = sum((Decimal(row["sz"]) for row in fills if row["kind"] == "entry"), Decimal(0))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
    if not entered or exited != entered or not exits:
        return {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt"}
    if any(not isinstance(row.get("time"), int) or row.get("oid") is None for row in exits):
        return {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt"}
    last_time = max(row["time"] for row in exits)
    latest = [row for row in exits if row["time"] == last_time]
    reasons = {exit_reason(trade, row) for row in latest}
    reason, label = reasons.pop() if len(reasons) == 1 else ("unconfirmed", "Grund nicht vollständig belegt")
    result = {"kind": reason, "label": label, "last_exit_time": last_time}
    if len(latest) == 1:
        result.update(last_exit_price=latest[0]["px"], last_exit_oid=latest[0]["oid"])
    return result


def record_lifecycle(data, trade_id, kind):
    trade = data["trades"][trade_id]
    if not trade.get("lifecycle_version"):
        return
    events = trade.setdefault("lifecycle_events", {})
    if kind in events:
        return
    if kind == "entry_fill":
        fills = [row for row in trade.get("fill_events", {}).values() if row["kind"] == "entry"]
        if not fills:
            return
        timestamp = min(row["time"] for row in fills)
    else:
        if trade["status"] != "closed":
            return
        reason = close_reason(trade)
        timestamp = reason.get("last_exit_time", trade.get("cleanup_observed_at", now_ms()))
        trade["close_reason"] = reason
    frozen = {key: copy.deepcopy(value) for key, value in trade.items() if key in (
        "trade_id", "event_id", "chat_id", "plan", "status", "filled_quantity", "remaining_quantity", "entry_vwap", "stop_price", "targets", "target_budgets", "tp_filled",
        "order_observations", "orders", "close_reason", "fill_events", "backup_finalization",
        "entry_equity_snapshot", "initial_sl_risk", "source_tp_override", "percent_finalization")}
    event = {"kind": kind, "time": timestamp, "observed_at": now_ms(), "facts": frozen}
    if kind == "entry_fill":
        frozen["entry_stop_confirmed"] = trade.get("entry_stop_confirmed", False)
        data["notifications"].pop("entry-waiting:" + trade_id, None)
    events[kind] = event
    data["notifications"].setdefault("lifecycle:" + trade_id + ":" + kind,
                                    {"trade_id": trade_id, "lifecycle": copy.deepcopy(event), "observed_at": event["observed_at"]})


def notification_sort_key(identity: str, notice) -> tuple[int, int, int, str]:
    event = notice.get("lifecycle", {})
    timestamp = event.get("time", notice.get("time", notice.get("observed_at", 0)))
    return (0 if identity.startswith("protection:") or notice.get("severity") == "urgent" else 1, timestamp if type(timestamp) is int else 0,
            int(event.get("kind", notice.get("kind")) == "closed"), identity)
