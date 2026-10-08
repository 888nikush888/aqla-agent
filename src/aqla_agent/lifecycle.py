"""Immutable entry/exit facts; rapid closures cannot erase entry notifications."""
from __future__ import annotations

import copy
from decimal import Decimal

from aqla_hyperliquid_mcp.models import now_ms


def close_reason(trade):
    fills = list(trade.get("fill_events", {}).values())
    exits = [row for row in fills if row["kind"] != "entry"]
    entered = sum((Decimal(row["sz"]) for row in fills if row["kind"] == "entry"), Decimal(0))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
    if not entered or exited != entered or not exits:
        return {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt"}
    if any(not isinstance(row.get("time"), int) or row.get("oid") is None for row in exits):
        return {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt"}
    last = max(exits, key=lambda row: row["time"])
    kind = last["kind"]
    if kind == "sl":
        label, reason = "Stop-Loss", "stop_loss"
    elif kind == "tp":
        label, reason = "Take-Profit", "take_profit"
    elif kind == "close":
        match = next((order for order in trade.get("exit_orders", []) if order.get("oid") == last["oid"] or trade.get("order_observations", {}).get(order["cloid"], {}).get("oid") == last["oid"]), {})
        reason = match.get("reason", "unconfirmed")
        label = {"operator_close": "Nutzerauftrag", "source_close": "Quellenauftrag", "protection_failure": "Schutzmaßnahme"}.get(reason, "Grund nicht vollständig belegt")
    else:
        label, reason = "Grund nicht vollständig belegt", "unconfirmed"
    return {"kind": reason, "label": label, "last_exit_time": last["time"], "last_exit_price": last["px"], "last_exit_oid": last["oid"]}


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
