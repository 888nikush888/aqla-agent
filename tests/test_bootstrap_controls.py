from dataclasses import asdict

import pytest
from test_notifications import call

from aqla_agent.pipelines import Parameters
from aqla_agent.setup import SetupBinding
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("profile", "limits"), ("limits", "profile")])
async def test_fresh_setup_accepts_explicit_rules_and_limits_in_either_order(setup, order):
    settings, state, fake, exchange, _ = setup
    settings.policy = None
    settings.telegram_owner_id = "12345"
    def empty(value):
        value["policy"], value["policy_revision"] = None, 0
        value["profiles"]["standard"].update(parameters=None, revision=0)
        value["venue_limits"]["hyperliquid"] = None
        value["account_limits"] = {}
    state.update(empty)
    server = create_server(settings, state=state, exchange=exchange)
    gate = server._aqla_setup_gate
    gate.configured = True  # Explicit isolated native lease fixture, never production password bypass.
    binding = SetupBinding("12345", "12345", "fixture-gateway", "fixture-session")
    generation = gate.bind(binding)
    lease = "a" * 64
    gate.register(binding, lease, None, generation)
    token = server.aqla_authority.sign("operator", "bootstrap-fixture", setup={"identity": lease, "binding": asdict(binding)})
    flags = state.snapshot()["account_enabled"]
    for step in order:
        revision = state.snapshot()["configuration_revision"]
        if step == "profile":
            await call(server, "control", action="set_profile", profile={"id": "standard", "name": "Standard", "parameters": Parameters().model_dump(mode="json")},
                       expected_revision=revision, context_auth=token)
        else:
            await call(server, "control", action="set_account_limits", account_key="hyperliquid:testnet",
                       limits={"max_open_trades": 3, "max_total_risk_percent": "2"}, expected_revision=revision, context_auth=token)
    final = state.snapshot()
    assert final["policy"]["risk_percent"] == "0.5"
    assert final["policy"]["max_total_risk_percent"] == "2"
    assert final["policy"]["max_open_trades"] == 3
    assert final["policy_revision"] == final["profiles"]["standard"]["revision"] == 1
    assert final["account_enabled"] == flags and not fake.actions
    await close_server(server)
