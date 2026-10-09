"""Additive native fill evidence; financial fill facts and hashes stay unchanged."""


def native_role(venue, row):
    if venue == "hyperliquid":
        return "taker" if row.get("crossed") is True else "maker" if row.get("crossed") is False else "unknown"
    if venue in ("aster", "lighter"):
        return row.get("execution_role") if row.get("execution_role") in ("maker", "taker") else "unknown"
    return "unknown"


def merge_role(roles, conflicts, identity, role):
    previous = roles.get(identity, "unknown")
    if identity in conflicts:
        roles[identity] = "unknown"
    elif previous in ("maker", "taker") and role in ("maker", "taker") and previous != role:
        conflicts[identity] = sorted({previous, role})
        roles[identity] = "unknown"
    elif role in ("maker", "taker") or identity not in roles:
        roles[identity] = role


def observed_roles(trade, *, kind=None):
    roles = trade.get("fill_execution_roles", {})
    return sorted({roles.get(identity, "unknown") for identity, fill in trade.get("fill_events", {}).items()
                   if kind is None or fill.get("kind") == kind})
