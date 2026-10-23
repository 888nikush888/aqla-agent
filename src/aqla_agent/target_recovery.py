"""Expired reduce-only target requests need positive absence evidence before replacement."""
from __future__ import annotations

from aqla_hyperliquid_mcp.models import digest, now_ms


def absence_proved(operations, order):
    record = operations.get(order.get("absence_operation"), {})
    return (record.get("phase") == "reconciled" and record.get("outcome") == "absent_expired"
            and any(row.get("kind") == "expired_target_absence" and order["cloid"] in row.get("cloids", [])
                    and row.get("observed_at", 0) >= row.get("expires_after", 0) + 10_000
                    for row in record.get("evidence", [])))


def expired_target_cloids(record, observations, stamp):
    action = record["wire_action"]
    orders = action.get("orders", [])
    expires = record.get("expires_after")
    if (record["phase"] not in ("sending", "unknown") or action.get("type") != "order" or not orders
            or type(expires) is not int or not 10_000 <= stamp - expires <= 86_400_000
            or any(not row.get("r") or row.get("t") != {"limit": {"tif": "Gtc"}} for row in orders)
            or any(row != {"status": "unknownOid"} for row in observations)):
        return None
    cloids = {row.get("c") for row in orders}
    if None in cloids or len(cloids) != len(orders):
        return None
    return cloids


def target_trade_matches(exchange, trade, plan, cloids, orders):
    own = {row["cloid"] for row in trade["orders"] if row["kind"] == "tp" and not row.get("retired")}
    if (plan.get("exchange", "hyperliquid") != "hyperliquid" or plan["market"]["product"] != "perp"
            or plan["market"].get("dex")
            or plan["account"].lower() != exchange.account.lower() or own != cloids
            or any(row["a"] != plan["market"]["asset_id"] for row in orders)
            or any(trade.get("order_observations", {}).get(cloid, {}).get("oid") is not None for cloid in cloids)):
        return None
    return True


def expired_target_trade(exchange, record, cloids):
    orders = record["wire_action"].get("orders", [])
    trades = exchange.state.read("trades")["trades"]
    selected = [(key, trade) for key, trade in trades.items()
                if trade.get("initial_targets", {}).get("operation_id") == record["operation_id"]]
    if len(selected) != 1:
        return None
    trade_id, trade = selected[0]
    plan = trade["plan"]
    if target_trade_matches(exchange, trade, plan, cloids, orders) is None:
        return None
    return trade_id, trade, plan


def unknown_target_fills(trade, plan, fills):
    known_fills = {tuple(row.get(key) for key in ("oid", "tid", "time", "sz", "px")) for row in trade["fill_events"].values()}
    return any(row.get("coin") == plan["market"]["symbol"] and
                   tuple(row.get(key) for key in ("oid", "tid", "time", "sz", "px")) not in known_fills for row in fills)


def target_history_absent(trade, plan, cloids, open_orders, history, fills):
    if (not isinstance(open_orders, list) or not isinstance(history, list) or not isinstance(fills, list)
            or len(history) >= 2000 or len(fills) >= 2000
            or any(row.get("cloid") in cloids for row in open_orders)
            or any(row.get("order", {}).get("cloid") in cloids for row in history)
            or unknown_target_fills(trade, plan, fills)):
        return None
    return True


def complete_target_absence(data, record, trade_id, trade, cloids, evidence):
    current = data["operations"][record["operation_id"]]
    owned = data["trades"][trade_id]
    if (current["phase"] not in ("sending", "unknown") or current["request_hash"] != record["request_hash"]
            or owned.get("initial_targets", {}).get("operation_id") != record["operation_id"]
            or digest(owned["fill_events"]) != digest(trade["fill_events"])):
        return False
    current.update(phase="reconciled", outcome="absent_expired", evidence=[evidence])
    for row in owned["orders"]:
        if row["cloid"] in cloids:
            row.update(retired=True, absence_operation=record["operation_id"])
    owned.setdefault("initial_target_history", {})[record["operation_id"]] = owned.pop("initial_targets")
    owned["managed_target_generation"] = owned.get("managed_target_generation", 0) + 1
    owned.pop("managed_target_fingerprint", None)
    owned["protection_repair_pending"] = record["operation_id"]
    return True


async def expired_target(exchange, record, observations):
    stamp = now_ms()
    expires = record.get("expires_after")
    cloids = expired_target_cloids(record, observations, stamp)
    if cloids is None:
        return None
    selected = expired_target_trade(exchange, record, cloids)
    if selected is None:
        return None
    trade_id, trade, plan = selected
    user = exchange.account
    open_orders = await exchange.info("frontendOpenOrders", user=user)
    history = await exchange.info("historicalOrders", user=user)
    start = min(record["created_at"], record["nonce"]) - 60_000
    fills = await exchange.info("userFillsByTime", user=user, startTime=start, endTime=stamp)
    if target_history_absent(trade, plan, cloids, open_orders, history, fills) is None:
        return None
    repeated = [await exchange.info("orderStatus", user=user, oid=cloid) for cloid in sorted(cloids)]
    if any(row != {"status": "unknownOid"} for row in repeated):
        return None
    evidence = {"kind": "expired_target_absence", "observed_at": now_ms(), "expires_after": expires,
                "cloids": sorted(cloids), "symbol": plan["market"]["symbol"], "fill_start": start,
                "order_status": repeated, "open_orders": open_orders, "historical_orders": history,
                "fills": fills}
    def complete(data):
        return complete_target_absence(data, record, trade_id, trade, cloids, evidence)
    if not exchange.state.update(complete):
        return None
    return evidence
