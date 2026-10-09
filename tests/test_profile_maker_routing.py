"""The real MCP profile gate and Router reach each native venue's wire adapter."""
import copy
from dataclasses import asdict
from decimal import Decimal

import httpx
import pytest

from aqla_agent.router import Router
from aqla_agent.setup import SetupBinding, write_password_hash
from aqla_hyperliquid_mcp.server import close_server, create_server

pytest_plugins = ["test_aster_adapter", "test_lighter_adapter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["aster", "lighter"])
@pytest.mark.parametrize("entry,tp", [("maker_only", "standard"), ("standard", "maker_only"), ("maker_only", "maker_only")])
async def test_mcp_profile_router_independent_native_entry_and_tp(setup, signal, aster, lighter, tmp_path, monkeypatch, venue, entry, tp):
    settings, state, _, exchange, _ = setup
    adapter, boundary = (aster if venue == "aster" else lighter)[:2]
    original_handle = boundary.handle
    def fresh_spread(request):
        response = original_handle(request)
        payload = response.json()
        if request.url.path.endswith("bookTicker"):
            payload.update(bidPrice="99.9", askPrice="100.1")
        elif request.url.path.endswith("orderBookDetails"):
            for row in payload["order_book_details"]:
                row.update(best_bid="99.9", best_ask="100.1")
        return httpx.Response(response.status_code, json=payload)
    monkeypatch.setattr(adapter.client, "_transport", httpx.MockTransport(fresh_spread))
    signer = lighter[2]
    monkeypatch.setattr(signer, "ORDER_TIME_IN_FORCE_POST_ONLY", 2, raising=False)
    verifier = tmp_path / "synthetic-profile-verifier.json"
    write_password_hash(verifier, "synthetic-profile-fixture")
    settings.setup_password_hash_file, settings.telegram_owner_id = verifier, "12345"
    monkeypatch.setattr("aqla_hyperliquid_mcp.server.Router", lambda *args: Router(*args, adapters={venue: adapter}))
    server = create_server(settings, state=state, exchange=exchange)
    binding = SetupBinding("12345", "12345", "profile-fixture-gateway", "profile-fixture-session")
    gate = server._aqla_setup_gate
    generation = gate.bind(binding)
    identity = "d" * 64
    gate.register(binding, identity, None, generation)
    auth = server.aqla_authority.sign("operator", "native-profile-command", setup={"identity": identity, "binding": asdict(binding)})
    parameters = {**state.snapshot()["profiles"]["standard"]["parameters"], "entry_execution": entry, "take_profit_execution": tp}
    result = await server.call_tool("control", {"action": "set_profile", "context_auth": auth,
        "expected_revision": state.snapshot()["configuration_revision"],
        "profile": {"id": "standard", "name": "Standard", "parameters": parameters}})
    assert not result.is_error
    pipeline = {"id": "legacy", "name": "Fixture source", "sources": ["channel"], "enabled": True,
                "targets": [{"exchange": venue, "profile_id": "standard"}]}
    result = await server.call_tool("control", {"action": "set_pipeline", "pipeline": pipeline, "context_auth": auth,
                                               "expected_revision": state.snapshot()["configuration_revision"]})
    assert not result.is_error
    source, interpretation = signal
    router = server.aqla_trading
    preview = await router.preview(source.event_id, interpretation)
    plan = preview["targets"][0]["plan"]
    assert plan["exchange"] == venue
    assert plan.get("entry_execution", "standard") == entry and plan.get("take_profit_execution", "standard") == tp
    trade = await router.execute(plan["plan_id"])
    if venue == "aster":
        entry_native = next(row for path, row in boundary.posts if path == "order" and row["reduceOnly"] == "false")
        assert (entry_native["timeInForce"] == "GTX") == (entry == "maker_only")
        if entry == "maker_only":
            boundary.fill(entry_native["newClientOrderId"])
    else:
        orders = [row for kind, row in signer.calls if kind == "order"] + [row for kind, value in signer.calls if kind == "group" for row in value["orders"]]
        entry_native = next(row for row in orders if not row.get("ReduceOnly", row.get("reduce_only")))
        assert (entry_native.get("TimeInForce", entry_native.get("time_in_force")) == 2) == (entry == "maker_only")
        if entry == "maker_only":
            filled = copy.deepcopy(entry_native)
            filled["TimeInForce" if "MarketIndex" in filled else "time_in_force"] = 0
            boundary.order(filled)
    trade = await router.engines[venue].refresh_trade(trade["trade_id"])
    assert trade["status"] == "open" and Decimal(trade["filled_quantity"]) > 0
    if venue == "aster":
        targets = [row for path, row in boundary.posts if path == "order" and row["reduceOnly"] == "true" and row["type"] != "STOP_MARKET"]
        assert targets and all((row.get("timeInForce") == "GTX") == (tp == "maker_only") for row in targets)
        stops = [row for path, row in boundary.posts if path == "order" and row["type"] == "STOP_MARKET"]
        assert stops and all(row["reduceOnly"] == "true" for row in stops)
    else:
        targets = [row for kind, row in signer.calls if kind == "order" and row["reduce_only"] and row["order_type"] != signer.ORDER_TYPE_STOP_LOSS]
        assert targets and all((row["time_in_force"] == 2) == (tp == "maker_only") for row in targets)
        groups = [value["orders"] for kind, value in signer.calls if kind == "group"]
        stops = [row for rows in groups for row in rows if row["Type"] == signer.ORDER_TYPE_STOP_LOSS]
        stops += [row for kind, row in signer.calls if kind == "order" and row["order_type"] == signer.ORDER_TYPE_STOP_LOSS]
        assert stops and all(row.get("TimeInForce", row.get("time_in_force")) == 0 for row in stops)
    assert state.snapshot()["trades"][trade["trade_id"]]["plan"] == plan
    await close_server(server)
    # The unused fixture adapter is outside Router ownership.
    await (lighter[0] if venue == "aster" else aster[0]).close()
