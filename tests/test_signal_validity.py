"""Long source validity never renews deadlines or relaxes execution observations."""
from decimal import Decimal

import pytest

from aqla_agent.journal_parameters import PARAMETERS
from aqla_agent.pipelines import AccountLimits, Parameters
from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan


@pytest.mark.parametrize("model", [Policy, Parameters])
def test_seven_day_validity_is_explicit_and_has_matching_menu_bounds(model):
    assert model().max_signal_age_seconds == 300
    selected = model(max_signal_age_seconds=604800)
    assert model.model_validate(selected.model_dump(mode="json")).max_signal_age_seconds == 604800
    schema = model.model_json_schema()["properties"]["max_signal_age_seconds"]
    assert schema["minimum"] == 1 and schema["maximum"] == 604800
    assert PARAMETERS["max_signal_age_seconds"][3] == "1 bis 604800"
    for invalid in (0, 604801):
        with pytest.raises(ValueError, match="max_signal_age_seconds"):
            model(max_signal_age_seconds=invalid)


@pytest.mark.parametrize("entry", ["market", "range"])
@pytest.mark.parametrize("remaining_ms", [1, 0, -1])
def test_week_old_original_entries_keep_exact_deadline_and_risk(signal, setup, entry, remaining_ms):
    _, interpretation = signal
    stamp = now_ms()
    source_time = stamp - 604800000 + remaining_ms
    interpretation = interpretation.model_copy(update={"entry": entry, "entry_prices": [Decimal(98), Decimal(100)]})
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    account = AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp)
    parameters = Parameters(risk_percent="0.75", max_notional_quote="650", entry_range_choice="split",
                            max_signal_age_seconds=604800)
    limits = AccountLimits(max_open_trades=4, max_total_risk_percent="3")
    policy = parameters.policy(limits)
    if remaining_ms <= 0:
        with pytest.raises(AqlaError, match="deadline expired"):
            make_plan(interpretation, market, account, policy, 1, source_time, ("testnet", "account"), stamp=stamp)
    else:
        plan = make_plan(interpretation, market, account, policy, 1, source_time, ("testnet", "account"), stamp=stamp)
        baseline = make_plan(interpretation, market, account, Policy(**{**policy.model_dump(), "max_signal_age_seconds": 300}),
                             1, stamp, ("testnet", "account"), stamp=stamp)
        assert plan.deadline_at == source_time + 604800000 == stamp + 1
        assert plan.quantity == baseline.quantity and plan.risk_usdc == baseline.risk_usdc
        assert plan.entry_legs == baseline.entry_legs and plan.tp_quantities == baseline.tp_quantities
        assert plan.policy.risk_percent == Decimal("0.75") and plan.policy.max_notional_usdc == 650
        assert plan.policy.max_open_trades == 4 and plan.policy.max_total_risk_percent == 3
        assert Policy.model_validate(baseline.policy.model_dump()).max_signal_age_seconds == 300
    assert not setup[2].actions


def test_week_long_validity_still_rejects_stale_account_observation(signal, setup):
    _, interpretation = signal
    stamp = now_ms()
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    account = AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp - 10001)
    with pytest.raises(AqlaError, match="Account observation is stale"):
        make_plan(interpretation, market, account, Policy(max_signal_age_seconds=604800),
                  1, stamp - 2 * 86400000, ("testnet", "account"), stamp=stamp)
    assert not setup[2].actions
