from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan, price_grid, quoted_numbers, slippage_price


def test_sizing_respects_risk_and_leverage_does_not_multiply_quantity(signal):
    _, interpretation = signal
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    account = AccountSnapshot(equity="10000", available_margin="10000")
    one = make_plan(interpretation, market, account, Policy(leverage=2), 1, now_ms(), ("testnet", "account"))
    two = make_plan(interpretation, market, account, Policy(leverage=10), 1, now_ms(), ("testnet", "account"))
    assert one.quantity == two.quantity
    assert one.risk_usdc <= 50
    assert sum(one.tp_quantities) == one.quantity
    assert one.quantity * one.entry_limit <= 500


def test_no_second_deadline_after_restart(signal):
    _, interpretation = signal
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    with pytest.raises(AqlaError, match="deadline expired"):
        make_plan(interpretation, market, AccountSnapshot(equity="1000", available_margin="1000"), Policy(), 1,
                  now_ms() - 301000, ("testnet", "account"))


def test_integer_and_significant_price_grids():
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20)
    assert price_grid(Decimal("123456.5"), market) == 123456
    assert price_grid(Decimal("1234.56"), market) == Decimal("1234.5")
    assert price_grid(Decimal("1234.56"), market, up=True) == Decimal("1234.6")


def test_general_decimal_numbers_have_no_channel_role_patterns():
    assert Decimal("98.5") in set(quoted_numbers("Absicherung bitte auf 98,5 ziehen"))
    assert Decimal("1234.56") in set(quoted_numbers("1.234,56"))
    assert Decimal("100000") in set(quoted_numbers("Targets: 100,000"))
    assert Decimal("1234567.89") in set(quoted_numbers("1\u202f234\u202f567,89"))
    assert set(quoted_numbers("0,001")) == {Decimal("0.001")}


@pytest.mark.parametrize("quote,expected", [("Stop Loss 90.", "90"), ("Entry 100.5.", "100.5"),
    ('Ziel "1.234,56."', "1234.56")])
def test_quote_evidence_accepts_sentence_periods_without_changing_decimal_values(quote, expected):
    assert Decimal(expected) in set(quoted_numbers(quote))
    assert set(quoted_numbers("100.BTC")) == set()


def test_user_policy_does_not_accept_extra_autonomy_fields():
    with pytest.raises(ValueError):
        Policy.model_validate({"risk_percent": "0.5", "autonomous_risk_increase": True})


@pytest.mark.parametrize("buy", [True, False])
@pytest.mark.parametrize("reference", ["100.0001", "1234.56", "100000.2"])
def test_slippage_rounding_stays_inside_exact_bound(reference, buy):
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid=reference, max_leverage=20)
    percent = Decimal("0.3")
    bound = market.mid * (1 + (1 if buy else -1) * percent / 100)
    limit = slippage_price(market.mid, percent, market, buy)
    assert limit <= bound if buy else limit >= bound
    assert limit == price_grid(bound, market, up=not buy)


@pytest.mark.parametrize("side", ["long", "short"])
def test_market_entry_plan_uses_inward_slippage_grid(signal, side):
    _, interpretation = signal
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100.0001", max_leverage=20)
    if side == "short":
        interpretation = interpretation.model_copy(update={"side": "short", "stop_loss": Decimal(110), "take_profits": [Decimal(90), Decimal(80)]})
    plan = make_plan(interpretation, market, AccountSnapshot(equity="10000", available_margin="10000"), Policy(), 1, now_ms(), ("testnet", "account"))
    bound = market.mid * (Decimal("1.003") if side == "long" else Decimal("0.997"))
    assert plan.entry_limit <= bound if side == "long" else plan.entry_limit >= bound
