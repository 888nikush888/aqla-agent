from dataclasses import asdict

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_agent.setup import SetupBinding, write_password_hash
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.fixture
def setup_server(setup, tmp_path):
    settings, state, _, exchange, _ = setup
    verifier = tmp_path / "synthetic-setup-verifier.json"
    write_password_hash(verifier, "synthetic-fixture-only")
    settings.setup_password_hash_file = verifier
    settings.telegram_owner_id = "12345"
    server = create_server(settings, state=state, exchange=exchange, profile="full")
    now = [1000.0]
    gate = server._aqla_setup_gate
    gate.clock, gate._last_time = lambda: now[0], now[0]
    binding = SetupBinding("12345", "12345", "synthetic-test-gateway", "synthetic-test-session")
    generation = gate.bind(binding)
    identity = "b" * 64
    gate.register(binding, identity, 1300, generation)
    token = server.aqla_authority.sign("operator", "explicit-synthetic-grant", setup={"identity": identity, "binding": asdict(binding)})
    return server, state, now, token, binding


@pytest.mark.asyncio
async def test_no_verifier_never_silently_allows_owner_configuration(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    token = server.aqla_authority.sign("operator", "native-owner")
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("control", {"action": "resume", "context_auth": token})
    result = await server.call_tool("control", {"action": "pause", "context_auth": token})
    assert not result.is_error
    assert server._aqla_setup_gate.status()["configured"] is False
    await close_server(server)


@pytest.mark.asyncio
async def test_all_critical_controls_require_mcp_verified_live_lease(setup_server):
    server, state, now, token, _ = setup_server
    ordinary = server.aqla_authority.sign("operator", "owner-without-setup")
    for action in ("set_policy", "set_sources", "resume", "set_profile", "set_pipeline", "delete_profile", "delete_pipeline",
                   "set_exchange_limits", "set_exchange_enabled", "set_ranking_policy", "set_account_limits", "set_account_enabled", "set_source_rank"):
        with pytest.raises(ToolError, match="Setup grant"):
            await server.call_tool("control", {"action": action, "context_auth": ordinary})
    with pytest.raises(ToolError, match="operator authority"):
        await server.call_tool("control", {"action": "resume", "context_auth": server.aqla_authority.sign("source", "forged-request")})
    assert not (await server.call_tool("control", {"action": "resume", "context_auth": token})).is_error
    now[0] = 1300
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("control", {"action": "resume", "context_auth": token})
    assert state.snapshot()["paused"] is False
    await close_server(server)


@pytest.mark.asyncio
async def test_expiry_during_activation_await_is_checked_inside_state_commit(setup_server, monkeypatch):
    server, state, now, token, _ = setup_server
    before = state.snapshot()
    async def activation(_):
        now[0] = 1300
    monkeypatch.setattr(server.aqla_trading, "activate", activation)
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("control", {"action": "set_exchange_enabled", "exchange_id": "hyperliquid", "enabled": True,
                               "expected_revision": before["configuration_revision"], "context_auth": token})
    assert state.snapshot() == before
    await close_server(server)


@pytest.mark.asyncio
async def test_native_internal_grant_requires_private_binding_and_registered_generation(setup_server):
    server, _, now, token, binding = setup_server
    with pytest.raises(ToolError):
        await server.call_tool("integration", {"action": "setup_bind", "token": "source-forged-token", "setup_binding": asdict(binding)})
    await server.call_tool("integration", {"action": "setup_revoke", "token": server.aqla_authority.key.decode(), "setup_binding": {"owner_id": binding.owner_id}})
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("control", {"action": "resume", "context_auth": token})
    now[0] += 1
    assert not server._aqla_setup_gate.status(binding, "b" * 64)["active"]
    await close_server(server)


@pytest.mark.asyncio
async def test_full_public_raw_configuration_and_websocket_mutation_cannot_bypass_setup(setup_server):
    server, _, _, _, _ = setup_server
    ordinary = server.aqla_authority.sign("operator", "owner-without-setup")
    request = {"operation": "update_leverage", "leverage": 2, "name": "BTC", "is_cross": True}
    with pytest.raises(ToolError, match="operator authority"):
        await server.call_tool("hl_exchange", {"operation_id": "raw-forged", "request": request})
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("hl_exchange", {"operation_id": "raw-no-grant", "request": request, "context_auth": ordinary})
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("hl_stream", {"action": "post_exchange", "operation_id": "raw-ws-no-grant", "exchange_request": request, "context_auth": ordinary})
    await close_server(server)


@pytest.mark.asyncio
async def test_full_raw_await_expiry_sends_nothing_and_does_not_prepare_operation(setup_server, monkeypatch):
    server, state, now, token, _ = setup_server
    async def verify():
        now[0] = 1300
    monkeypatch.setattr(server.aqla_exchange, "verify_signer", verify)
    with pytest.raises(ToolError, match="Setup grant"):
        await server.call_tool("hl_action", {"operation_id": "expired-raw", "request": {"type": "order", "orders": [
            {"a": 0, "b": True, "p": "100", "s": "1", "r": False, "t": {"limit": {"tif": "Alo"}}}]}, "context_auth": token})
    assert "expired-raw" not in state.snapshot()["operations"]
    await close_server(server)


def test_public_raw_reduce_only_and_known_cancel_preserve_operator_safety_access():
    from aqla_agent.setup_actions import public_action_requires_setup
    from aqla_hyperliquid_mcp.catalog import SDK_ADAPTER
    from aqla_hyperliquid_mcp.models import AqlaError
    row = {"request": {"network": "testnet", "account": "synthetic"},
           "legs": [{"status": {"resting": {"oid": 42}}}]}
    data = {"operations": {"owned": row}, "trades": {}}
    close = SDK_ADAPTER.validate_python({"operation": "market_close", "coin": "BTC"})
    assert public_action_requires_setup(close, data) is False
    cancel = SDK_ADAPTER.validate_python({"operation": "cancel", "name": "BTC", "oid": 42})
    assert public_action_requires_setup(cancel, data, account="synthetic", network="testnet") is False
    with pytest.raises(AqlaError, match="known account order"):
        public_action_requires_setup(cancel, data, account="synthetic", network="mainnet")


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_manual_rank_cancels_old_unfilled_entry_and_keeps_owned_partial_protection(setup, signal, partial):
    from decimal import Decimal

    from aqla_agent.ranking import set_manual_rank
    from aqla_agent.router import Router
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    state.update(lambda data: data["ranking_policy"].update(enabled=True))
    await router.recover()
    source, interpretation = signal
    interpretation.entry = "limit"
    interpretation.entry_prices = ["100"]
    from aqla_hyperliquid_mcp.models import Evidence
    interpretation.evidence.append(Evidence(field="entry_prices.0", quote="Entry 100", source_id=source.source_id))
    fake.partial_limit_fraction = .5 if partial else 0
    plan = await router.preview(source.event_id, interpretation)
    opened = await router.execute(plan["plan_id"])
    original = state.snapshot()["trades"][opened["trade_id"]]
    entries = len([action for action in fake.actions if action["type"] == "order" and any(not order["r"] for order in action["orders"])])
    state.update(lambda data: set_manual_rank(data, "channel", "D", data["source_ranks"]["channel"]["revision"], "12345"))
    await router.recover()
    saved = state.snapshot()["trades"][opened["trade_id"]]
    if partial:
        assert Decimal(saved["filled_quantity"]) > 0
        assert saved["plan"] == original["plan"] and saved["status"] in ("open", "entry_pending")
        assert any(order["kind"] == "sl" and fake.orders[order["cloid"]]["status"] == "open" for order in saved["orders"])
    else:
        assert fake.orders[saved["entry_cloid"]]["status"] == "canceled"
        assert saved["status"] in ("cancelled", "rejected")
    assert len([action for action in fake.actions if action["type"] == "order" and any(not order["r"] for order in action["orders"])]) == entries
    await router.close()
