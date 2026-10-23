"""Independent-target fixture, shared without importing test cases."""
import pytest

from aqla_agent.pipelines import Parameters, Pipeline, TradingProfile, route_control
from aqla_agent.router import Router


@pytest.fixture
def routed(setup):
    settings, state, fake, exchange, _ = setup
    boundaries = {"hyperliquid": fake}
    router = Router(settings, state, exchange)
    profiles = [TradingProfile(id=name, name=name.upper(), parameters=Parameters(risk_percent=risk, max_notional_quote=notional))
                for name, risk, notional in (("a", "0.25", "500"), ("b", "1", "1000"))]
    for selected in profiles:
        state.update(lambda data, selected=selected: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=selected))
    pipeline = Pipeline(id="source-one", name="Quelle Eins", sources=["channel"], enabled=True,
                        targets=[{"exchange": "hyperliquid", "profile_id": "a"}])
    state.update(lambda data: route_control(data, "set_pipeline", expected_revision=data["configuration_revision"], pipeline=pipeline))
    return router, boundaries
