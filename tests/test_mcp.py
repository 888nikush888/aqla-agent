import copy
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.mark.parametrize("chats", [1, 10, 100])
@pytest.mark.parametrize("count", [1000, 10000, 100000])
def test_reader_overview_single_scan_matches_complete_original_output(chats, count):
    from aqla_hyperliquid_mcp.server import reader_overview
    from aqla_hyperliquid_mcp.trades import OPEN, UNFINISHED

    class CountedEvents(dict):
        scans = 0
        def values(self):
            for value in super().values():
                self.scans += 1
                yield value
    events = CountedEvents({str(index): {"event_id": str(index), "source_id": f"source-{index}",
                                       "source": {"chat_id": str(index % chats), "message_id": index + 1, "sent_at": index, "text": "body"},
                                       "status": "pending" if index % 13 == 0 else "report"} for index in range(count)})
    snapshot = {"events": events, "trades": {"active": {"status": "open", "plan": {"source_refs": [f"source-{index}" for index in range(0, count, 997)]}}},
                "sources": [str(index) for index in range(chats)], "cursors": {"0": 7}, "context_cutoffs": {"0": 4},
                "context_ids": {"0": [1, 2, 4]}, "source_aliases": {"alias": "0"}, "notifications": {"notice": {"error": "fact"}}}
    before = copy.deepcopy(snapshot)
    started = time.perf_counter()
    actual = reader_overview(snapshot, 50)
    elapsed = time.perf_counter() - started
    assert events.scans == count
    events.scans = 0
    started = time.perf_counter()
    active_refs = {ref for trade in snapshot["trades"].values() if trade["status"] in OPEN for ref in trade["plan"]["source_refs"]}
    tracked = {}
    for row in events.values():
        tracked.setdefault(row["source"]["chat_id"], []).append(row["source"]["message_id"])
    for chat, values in tracked.items():
        recent = sorted(set(values))[-50:]
        active = [row["source"]["message_id"] for row in events.values() if row["source"]["chat_id"] == chat and row["source_id"] in active_refs]
        tracked[chat] = sorted(set([*recent, *active]))
    expected = {"sources": snapshot["sources"], "cursors": snapshot["cursors"], "context_cutoffs": snapshot["context_cutoffs"],
                "context_ids": snapshot["context_ids"], "source_aliases": snapshot["source_aliases"], "tracked_ids": tracked,
                "pending": [row["event_id"] for row in events.values() if row["status"] in UNFINISHED][:50], "notification_pending": 1}
    old_elapsed = time.perf_counter() - started
    assert events.scans == count * (chats + 2)
    assert actual["source_queue"]["ready_total"] == sum(index % 13 == 0 for index in range(count))
    assert actual["source_queue"]["raw_unfinished_total"] == actual["source_queue"]["ready_total"]
    assert actual["source_queue"]["blocked_total"] == 0
    expected["source_queue"] = actual["source_queue"]
    assert actual == expected and snapshot == before
    print(f"reader overview events={count} chats={chats} old_scans={events.scans} new_scans={count} old_seconds={old_elapsed:.6f} new_seconds={elapsed:.6f}")


@pytest.mark.asyncio
async def test_real_mcp_registration_auth_and_tool_filter(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert "trade_execute" in names
    assert "hl_exchange" not in names
    result = await server.call_tool("status", {})
    assert result.is_error is not True
    with pytest.raises(ToolError, match="operator authority"):
        await server.call_tool("control", {"action": "pause"})
    token = server.aqla_authority.sign("operator", "native-telegram-message")
    accepted = await server.call_tool("control", {"action": "pause", "context_auth": token})
    assert accepted.is_error is not True
    assert state.snapshot()["paused"] is True
    await close_server(server)


@pytest.mark.asyncio
async def test_full_mcp_schemas_reject_arbitrary_python_attributes(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange, profile="full")
    names = {tool.name for tool in await server.list_tools()}
    assert {"hl_info", "hl_exchange"}.issubset(names)
    result = await server.call_tool("hl_info", {"request": {"type": "allMids"}})
    assert result.is_error is not True
    with pytest.raises(ToolError, match="union_tag_invalid"):
        await server.call_tool("hl_exchange", {"operation_id": "invalid", "request": {"operation": "post"}})
    await close_server(server)
