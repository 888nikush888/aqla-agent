"""Expired reduce-only target requests need positive absence evidence before replacement."""
from __future__ import annotations

from aqla_hyperliquid_mcp.models import digest, now_ms


async def expired_target(exchange, record, observations):
    action = record["wire_action"]
    orders = action.get("orders", [])
    expires = record.get("expires_after")
    stamp = now_ms()
    if (record["phase"] not in ("sending", "unknown") or action.get("type") != "order" or not orders
            or type(expires) is not int or not 10_000 <= stamp - expires <= 86_400_000
            or any(not row.get("r") or row.get("t") != {"limit": {"tif": "Gtc"}} for row in orders)
            or any(row != {"status": "unknownOid"} for row in observations)):
        return None
    cloids = {row.get("c") for row in orders}
    if None in cloids or len(cloids) != len(orders):
        return None
    trades = exchange.state.read("trades")["trades"]
    selected = [(key, trade) for key, trade in trades.items()
                if trade.get("initial_targets", {}).get("operation_id") == record["operation_id"]]
    if len(selected) != 1:
        return None
    trade_id, trade = selected[0]
    plan = trade["plan"]
    own = {row["cloid"] for row in trade["orders"] if row["kind"] == "tp" and not row.get("retired")}
    if (plan.get("exchange", "hyperliquid") != "hyperliquid" or plan["market"]["product"] != "perp"
            or plan["market"].get("dex")
            or plan["account"].lower() != exchange.account.lower() or own != cloids
            or any(row["a"] != plan["market"]["asset_id"] for row in orders)
            or any(trade.get("order_observations", {}).get(cloid, {}).get("oid") is not None for cloid in cloids)):
        return None
    user = exchange.account
    open_orders = await exchange.info("frontendOpenOrders", user=user)
    history = await exchange.info("historicalOrders", user=user)
    start = min(record["created_at"], record["nonce"]) - 60_000
    fills = await exchange.info("userFillsByTime", user=user, startTime=start, endTime=stamp)
    known_fills = {tuple(row.get(key) for key in ("oid", "tid", "time", "sz", "px")) for row in trade["fill_events"].values()}
    if (not isinstance(open_orders, list) or not isinstance(history, list) or not isinstance(fills, list)
            or len(history) >= 2000 or len(fills) >= 2000
            or any(row.get("cloid") in cloids for row in open_orders)
            or any(row.get("order", {}).get("cloid") in cloids for row in history)
            or any(row.get("coin") == plan["market"]["symbol"] and
                   tuple(row.get(key) for key in ("oid", "tid", "time", "sz", "px")) not in known_fills for row in fills)):
        return None
    repeated = [await exchange.info("orderStatus", user=user, oid=cloid) for cloid in sorted(cloids)]
    if any(row != {"status": "unknownOid"} for row in repeated):
        return None
    evidence = {"kind": "expired_target_absence", "observed_at": now_ms(), "expires_after": expires,
                "cloids": sorted(cloids), "symbol": plan["market"]["symbol"], "fill_start": start,
                "order_status": repeated, "open_orders": open_orders, "historical_orders": history,
                "fills": fills}
    def complete(data):
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
    if not exchange.state.update(complete):
        return None
    return evidence
