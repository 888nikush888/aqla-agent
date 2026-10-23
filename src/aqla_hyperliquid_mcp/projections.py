"""Pure bounded reader projection shared by the selective state view and its oracles."""
from typing import TypedDict

from aqla_agent.source_queue import is_source_row, queue_overview
from aqla_hyperliquid_mcp.models import OPEN

from .models import digest


class ReaderOverview(TypedDict):
    sources: list[str]
    cursors: dict[str, int]
    context_cutoffs: dict[str, int]
    context_ids: dict[str, list[int]]
    source_aliases: dict[str, str]
    tracked_ids: dict[str, list[int]]
    pending: list[str]
    notification_pending: int
    notification_signature: str
    source_queue: dict


def reader_overview(snapshot, context_limit: int) -> ReaderOverview:
    """Group recent and active IDs in one event scan; preserve the reader contract."""
    active_refs = {ref for trade in snapshot["trades"].values() if trade["status"] in OPEN for ref in trade["plan"]["source_refs"]}
    grouped: dict[str, set[int]] = {}
    active: dict[str, set[int]] = {}
    event_rows = []
    for row in snapshot["events"].values():
        if not is_source_row(row):
            continue
        event_rows.append(row)
        chat, identity = row["source"]["chat_id"], row["source"]["message_id"]
        grouped.setdefault(chat, set()).add(identity)
        if row["source_id"] in active_refs:
            active.setdefault(chat, set()).add(identity)

    queue = queue_overview(snapshot, event_rows=event_rows)
    pending = queue["ready"]
    tracked = {chat: sorted(set(sorted(identities)[-context_limit:]) | active.get(chat, set())) for chat, identities in grouped.items()}
    return {"sources": snapshot["sources"], "cursors": snapshot["cursors"], "context_cutoffs": snapshot.get("context_cutoffs", {}),
            "context_ids": snapshot.get("context_ids", {}), "source_aliases": snapshot.get("source_aliases", {}), "tracked_ids": tracked,
            "pending": pending, "source_queue": queue, "notification_pending": len(snapshot["notifications"]),
            "notification_signature": digest(snapshot["notifications"])}
