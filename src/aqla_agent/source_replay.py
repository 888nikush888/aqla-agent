"""Isolated, order-free MCP fixture and grader for native historical source replay."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .decision_evaluation import CRITICAL_FIELDS, complete_fields, equal, write_private_json


def canonical_symbol(value):
    if value is None:
        return None
    value = value.upper().removesuffix(".P").lstrip("$")
    if value in ("GOLD", "XAU", "XAUUSD", "XAUUSDT", "XAUUSDC"):
        return "XYZ:GOLD"
    if ":" not in value:
        for suffix in ("/USDT", "/USDC", "USDT", "USDC"):
            if value.endswith(suffix):
                return value[:-len(suffix)]
    return value


def mismatches(interpretation, expected):
    actual = complete_fields(interpretation)
    fields = ("kind", "source_refs") if expected["kind"] not in ("new_signal", "trade_update") else (
        field for field in CRITICAL_FIELDS if field != "evidence")
    fields = list(fields)
    if expected.get("entry") == "split" or actual.get("entry") == "split":
        fields.append("entry_legs")
    errors = []
    for field in fields:
        left, right = actual.get(field), expected.get(field)
        if field == "symbol":
            left, right = canonical_symbol(left), canonical_symbol(right)
        elif field == "source_refs":
            left, right = sorted(left or []), sorted(right or [])
        elif field == "source_exchange":
            left, right = (left or "").casefold(), (right or "").casefold()
        elif field == "entry_legs":
            from aqla_hyperliquid_mcp.models import SourceEntryLeg
            left = [SourceEntryLeg.model_validate(row) for row in left or []]
            right = [SourceEntryLeg.model_validate(row) for row in right or []]
        if not equal(left, right):
            errors.append(field)
    return errors


def serve_fixture(path):
    """Expose production read/check tools only; broker HTTP always fails locally."""
    import httpx

    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.models import AqlaError, Settings
    from aqla_hyperliquid_mcp.server import create_server
    from aqla_hyperliquid_mcp.state import State

    from . import source_assistance
    from .decisions import validate_interpretation

    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    packet = fixture["packet"]
    directory = Path(path).parent
    assert not packet.get("trades"), "Owned trade fixtures require explicit isolated reconstruction"
    settings = Settings(network="testnet", account="0x" + "1" * 40,
                        state_path=directory / "state.sqlite3", sources=sorted({m["chat_id"] for m in packet["messages"]}))
    state = State(settings)
    originals = fixture.get("context_messages") or [{"event_id": m["event_id"], "source_id": m["source_id"],
                  "source": {k: v for k, v in m.items() if k not in ("event_id", "source_id")}} for m in packet["messages"]]
    rows = {m["event_id"]: {**m, "status": "pending" if m["event_id"] == packet["anchor_event_id"] else "context",
                           "interpretation": None} for m in originals}
    state.update(lambda data: data["events"].update(rows))

    def blocked(request):
        raise httpx.ConnectError("All exchange transports are disabled in historical replay", request=request)

    exchange = Hyperliquid(settings, state, httpx.AsyncClient(transport=httpx.MockTransport(blocked)))
    server = create_server(settings, state=state, exchange=exchange)
    allowed = {"source_context", "source_context_batch", "source_check_interpretation"}
    for tool in asyncio.run(server.list_tools()):
        if tool.name not in allowed:
            server.remove_tool(tool.name)
    assert {t.name for t in asyncio.run(server.list_tools())} == allowed
    check = source_assistance.check_interpretation
    checks = []

    def bounded(context, interpretation):
        if len(checks) >= 3:
            raise AqlaError("Historical evaluation permits at most three drafts; stop without executing")
        result = check(context, interpretation)
        submitted_valid = True
        try:
            validate_interpretation(source_assistance.context_packet(context), interpretation)
        except AqlaError:
            submitted_valid = False
        checks.append({**result, "submitted_valid": submitted_valid})
        write_private_json(directory / "checks.private.json", checks)
        return result

    source_assistance.check_interpretation = bounded
    try:
        server.run(transport="stdio")
    finally:
        source_assistance.check_interpretation = check
        assert not state.snapshot()["plans"] and not state.snapshot()["operations"] and not state.snapshot()["trades"]
        state.close()


if __name__ == "__main__":
    serve_fixture(sys.argv[1])
