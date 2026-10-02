import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.server import close_server, create_server


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
