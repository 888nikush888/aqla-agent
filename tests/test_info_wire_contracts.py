"""Independent documented requests, checked at the actual MCP-to-HTTP boundary."""
import json
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.catalog import INFO_ADAPTER, validate_info
from aqla_hyperliquid_mcp.coverage import manifest
from aqla_hyperliquid_mcp.server import close_server, create_server

CASES = json.loads((Path(__file__).parent / "fixtures" / "info_contract_vectors.json").read_text(encoding="utf-8"))


def test_independent_requests_cover_every_manifest_info_contract():
    assert {case["request"]["type"] for case in CASES} == set(manifest()["expected_info_types"])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
async def test_documented_info_request_reaches_exact_http_wire(setup, case):
    settings, state, _, exchange, _ = setup
    sent = []
    def handler(request):
        assert request.url.path == "/info"
        body = json.loads(request.content)
        assert body == case["wire"]
        sent.append(body)
        return httpx.Response(200, json={"contract": case["id"], "verified": True})
    await exchange.client.aclose()
    exchange.client = httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(handler))
    server = create_server(settings, state=state, exchange=exchange, profile="full")
    try:
        result = await server.call_tool("hl_info", {"request": case["request"]})
        assert result.is_error is not True
        assert sent == [case["wire"]]
        assert case["id"] in str(result)
        bad = {**case["request"], "unrecognized_field": "must-never-reach-http"}
        with pytest.raises(ToolError):
            await server.call_tool("hl_info", {"request": bad})
        assert sent == [case["wire"]]
    finally:
        await close_server(server)


@pytest.mark.parametrize("payload", [
    {"type": "orderStatus", "user": "0x" + "1" * 40, "oid": 0},
    {"type": "orderStatus", "user": "0x" + "1" * 40, "oid": 2**64},
    {"type": "orderStatus", "user": "not-an-address", "oid": 12},
    {"type": "orderStatus", "user": "0x" + "1" * 40, "oid": "0xshort"},
    {"type": "l2Book", "coin": "BTC", "nSigFigs": 1},
    {"type": "l2Book", "coin": "BTC", "nSigFigs": 6},
    {"type": "l2Book", "coin": "BTC", "nSigFigs": 5, "mantissa": 3},
    {"type": "l2Book", "coin": "BTC", "mantissa": 1},
    {"type": "userFills", "user": "0x" + "1" * 40, "aggregateByTime": "yes"},
    {"type": "borrowLendReserveState", "token": -1},
    {"type": "settledOutcome", "outcome": -1},
    {"type": "userFillsByTime", "user": "0x" + "1" * 40, "startTime": 20, "endTime": 10},
    {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "unknown", "startTime": 0, "endTime": 1}},
    {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": 10, "endTime": 0}},
])
def test_documented_info_boundaries_reject_invalid_requests(payload):
    with pytest.raises(ValueError):
        validate_info(payload)


def test_required_fields_are_not_implicitly_supplied():
    for case in CASES:
        model = INFO_ADAPTER.validate_python(case["request"])
        for name, field in type(model).model_fields.items():
            if name != "type" and field.is_required():
                body = {key: value for key, value in case["request"].items() if key != name}
                with pytest.raises(ValueError):
                    validate_info(body)
