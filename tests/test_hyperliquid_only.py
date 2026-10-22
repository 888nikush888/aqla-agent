import copy
import importlib.util
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aqla_agent.pipelines import Pipeline, migrate, route_control
from aqla_hyperliquid_mcp.models import AqlaError, ExecutionSettings, Settings


@pytest.mark.parametrize("exchange", ["aster", "lighter"])
def test_removed_exchanges_cannot_be_configured_or_dispatched(setup, exchange):
    settings, state, *_ = setup
    assert importlib.util.find_spec("aqla_agent." + exchange) is None
    with pytest.raises(ValidationError):
        ExecutionSettings(exchange=exchange)
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "venues": {exchange: {"network": "mainnet"}}})
    with pytest.raises(ValidationError):
        Pipeline(id="removed", name="Removed", sources=["channel"], targets=[{"exchange": exchange, "profile_id": "standard"}])
    before = state.snapshot()
    with pytest.raises(AqlaError, match="Hyperliquid"):
        state.update(lambda data: route_control(data, "set_exchange_enabled", exchange=exchange, enabled=True, expected_revision=data["configuration_revision"]))
    assert state.snapshot() == before


@pytest.mark.parametrize("exchange", ["aster", "lighter"])
def test_migration_retires_configuration_without_rewriting_financial_history(setup, exchange):
    data = setup[1].snapshot()
    key = exchange + ":mainnet"
    data["execution_accounts"][key] = {"exchange": exchange, "network": "mainnet", "account": "historical"}
    data["account_enabled"][key] = True
    data["account_limits"][key] = {"max_open_trades": 2, "max_total_risk_percent": "1"}
    data["trades"]["historical"] = {"plan": {"exchange": exchange}, "status": "closed", "fill_events": {"original": {"fee": "0.23"}}, "funding_events": {"actual": {"amount": "-0.12"}}}
    data["operations"]["historical"] = {"exchange": exchange, "phase": "acknowledged", "nonce": 17, "wire_action": {"orders": [{"c": "historical-original"}]}}
    data["pipelines"]["legacy"]["targets"][0]["fallback_exchanges"] = [exchange]
    financial = {name: copy.deepcopy(data[name]) for name in ("trades", "operations", "bindings", "nonces", "plans", "profiles")}
    assert migrate(data)
    assert all(data[name] == value for name, value in financial.items())
    assert key not in data["execution_accounts"] and key not in data["account_enabled"]
    assert data["retired_execution_configuration"]["execution_accounts"][key]["exchange"] == exchange
    assert data["retired_execution_configuration"]["account_enabled"][key] is True
    assert "fallback_exchanges" not in data["pipelines"]["legacy"]["targets"][0]
    after = copy.deepcopy(data)
    assert not migrate(data) and data == after


@pytest.mark.parametrize("exchange", ["aster", "lighter"])
@pytest.mark.parametrize("work", ["position", "unknown_operation", "terminal_with_remaining"])
def test_migration_cannot_abandon_removed_account_with_active_work(setup, exchange, work):
    data = setup[1].snapshot()
    if work == "terminal_with_remaining":
        data["trades"]["active"] = {"plan": {"exchange": exchange}, "status": "closed", "remaining_quantity": "1"}
    elif work == "position":
        data["trades"]["active"] = {"plan": {"exchange": exchange}, "status": "open"}
    else:
        data["operations"]["unknown"] = {"exchange": exchange, "phase": "unknown"}
    before = copy.deepcopy(data)
    with pytest.raises(AqlaError, match="unsupported account"):
        migrate(data)
    assert data == before


@pytest.mark.asyncio
async def test_retired_account_report_uses_immutable_cached_finance_without_an_adapter(setup):
    from test_monthly import trade

    from aqla_agent.monthly import report_row
    from aqla_agent.reporting import Reporting

    row = trade("historical", currency="USDT")
    row["plan"]["exchange"] = "aster"
    state = setup[1]
    state.update(lambda data: data.update(trades={"historical": row}, retired_execution_configuration={"execution_accounts": {"aster:mainnet": {}}}))
    before = state.snapshot()
    def forbidden(*_):
        pytest.fail("Archived reporting tried to create an execution adapter")
    result = await Reporting(state, SimpleNamespace(engine_for_plan=forbidden)).refresh("historical")
    assert result["complete"] == report_row(before, row)["complete"]
    assert state.snapshot() == before
