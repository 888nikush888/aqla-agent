from decimal import Decimal

import pytest
from oracles.rules_9ce4972 import make_plan as original_plan

from aqla_hyperliquid_mcp.models import AccountSnapshot, Market, Policy
from aqla_hyperliquid_mcp.rules import make_plan


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("entry", ["market", "limit", "range", "unspecified"])
@pytest.mark.parametrize("choice", ["near", "far", "midpoint"])
@pytest.mark.parametrize("timing", ["immediate", "deferred"])
@pytest.mark.parametrize("native_grid", [False, True])
def test_complete_plan_identity_and_risk_match_frozen_rules(signal, side, entry, choice, timing, native_grid):
    stamp = 1234567890000
    interpretation = signal[1].model_copy(update={"side": side, "entry": entry, "entry_timing": timing,
        "entry_prices": [Decimal(99), Decimal(101)] if entry == "range" else [Decimal(100)] if entry == "limit" else [],
        "stop_loss": Decimal(90 if side == "long" else 110),
        "take_profits": [Decimal(110), Decimal(120)] if side == "long" else [Decimal(90), Decimal(80)]})
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100.0001", max_leverage=20,
        **({"price_tick": ".1", "quantity_step": ".01", "native_symbol": "BTCUSDT"} if native_grid else {}))
    account = AccountSnapshot(equity="10000", available_margin="10000", observed_at=stamp)
    policy = Policy(entry_mode="source_or_market", entry_range_choice=choice)
    args = (interpretation, market, account, policy, 7, stamp - 1, ("testnet", "account"))
    expected = original_plan(*args, reserved_risk=Decimal(5), stamp=stamp)
    actual = make_plan(*args, reserved_risk=Decimal(5), stamp=stamp)
    assert actual.model_dump(mode="json", exclude_none=True) == expected.model_dump(mode="json", exclude_none=True)
    assert actual.plan_id == expected.plan_id and actual.trade_id == expected.trade_id
    assert actual.risk_usdc <= 50 and sum(actual.tp_quantities) == actual.quantity
