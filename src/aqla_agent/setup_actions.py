"""Narrow classification of public raw exchange mutations; internal engines stay autonomous."""
from aqla_hyperliquid_mcp.models import AqlaError


def public_action_requires_setup(request, data, *, account_key=None, account=None, network=None):
    value = request.model_dump(mode="json", by_alias=True)
    kind = value.get("operation", value.get("type"))
    sdk = "operation" in value
    if kind == "market_close":
        return False  # The official SDK always builds a reduce-only order.
    if kind == "order":
        return not (value.get("reduce_only") is True if sdk else all(row["r"] is True for row in value["orders"]))
    if kind == "bulk_orders":
        return not (value["order_requests"] and all(row.get("reduce_only") is True for row in value["order_requests"]))
    if kind == "trailingStop":
        return value["reduceOnly"] is not True
    if kind == "twapOrder":
        return value["twap"]["r"] is not True
    cancel_kinds = {"cancel", "cancel_by_cloid", "bulk_cancel", "bulk_cancel_by_cloid", "cancelByCloid", "twapCancel"}
    if kind not in cancel_kinds:
        return True  # All configuration, leverage, margin, transfers, deploys and replacements.
    known = set()
    def bound(row, scope):
        if account_key and row.get("account_key", scope.get("account_key")) not in (None, account_key):
            return False
        if network and scope.get("network", network) != network:
            return False
        return not account or scope.get("account", account).lower() == account.lower()
    for row in data.get("operations", {}).values():
        if not bound(row, row.get("request", {})):
            continue
        for leg in row.get("legs", []):
            if leg.get("oid") is not None:
                known.add(str(leg["oid"]))
            status = leg.get("status", {})
            if isinstance(status, dict):
                for phase in ("resting", "filled"):
                    identity = status.get(phase, {}).get("oid")
                    if identity is not None:
                        known.add(str(identity))
        for order in row.get("wire_action", {}).get("orders", []):
            if order.get("c"):
                known.add(order["c"])
        if isinstance(row.get("result"), dict):
            result = row["result"].get("response", {}).get("data", {})
            if isinstance(result, dict):
                if result.get("oid") is not None:
                    known.add(str(result["oid"]))
                status = result.get("status", {})
                if isinstance(status, dict) and status.get("running", {}).get("twapId") is not None:
                    known.add(str(status["running"]["twapId"]))
    for trade in data.get("trades", {}).values():
        if not bound(trade, trade.get("plan", {})):
            continue
        if trade.get("entry_cloid"):
            known.add(trade["entry_cloid"])
        for order in [*trade.get("orders", []), *trade.get("entry_orders", []), *trade.get("exit_orders", [])]:
            for key in ("oid", "cloid"):
                if order.get(key) is not None:
                    known.add(str(order[key]))
    if sdk:
        rows = value.get("cancel_requests", [value])
        identities = [row.get("oid", row.get("cloid")) for row in rows]
    else:
        identities = [row.get("o", row.get("cloid")) for row in value.get("cancels", [])] if kind != "twapCancel" else [value["t"]]
    if not identities or any(str(identity) not in known for identity in identities):
        raise AqlaError("Public cancellation requires exact known account order identities")
    return False
