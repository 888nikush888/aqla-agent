import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from aqla_agent.entry import BookSnapshot, PriceSnapshot
from aqla_agent.entry_watch import parse_book
from aqla_agent.pipelines import Parameters
from aqla_agent.waverider import VERSION, advance, atr, pools, qualification_valid, software_hash
from aqla_hyperliquid_mcp.models import AqlaError, Policy, now_ms


@pytest.mark.parametrize("side,sign", [("long", 1), ("short", -1)])
def test_confirmed_wave_and_reentry_require_three_distinct_observations(side, sign):
    state = {"phase": "waiting_rise"}
    for stamp, price in [(1000, 100), (2000, 102), (3000, 101.4), (4000, 101.4), (5000, 101.4)]:
        state, action = advance(state, side=side, price=Decimal(str(price if sign == 1 else 200 - price)), basis=Decimal(100),
            volatility=Decimal(1), cycle_cost=Decimal(".2"), tick=Decimal(".01"), stamp=stamp)
        if stamp < 5000:
            assert action is None
    assert action == "reduce"
    sale = Decimal("101.4") if sign == 1 else Decimal("98.6")
    state = {"phase": "waiting_rebound", "sale_price": str(sale)}
    for stamp, price in [(6000, 100.5), (7000, 101.1), (8000, 101.1), (9000, 101.1)]:
        state, action = advance(state, side=side, price=Decimal(str(price if sign == 1 else 200 - price)), basis=Decimal(100),
            volatility=Decimal(1), cycle_cost=Decimal(".2"), tick=Decimal(".01"), stamp=stamp)
        if stamp < 9000:
            assert action is None
    assert action == "reenter"


def test_same_quote_cannot_manufacture_confirmation():
    state = {"phase": "waiting_retrace", "extreme": "102"}
    for _ in range(10):
        state, action = advance(state, side="long", price=Decimal("101.4"), basis=Decimal(100), volatility=Decimal(1),
            cycle_cost=Decimal(".2"), tick=Decimal(".01"), stamp=1000)
        assert action is None
    assert state["confirmation_count"] == 1


def test_noise_and_expensive_round_trips_do_not_create_waves():
    state = {"phase": "waiting_rise"}
    for stamp in range(1000, 30000, 1000):
        state, action = advance(state, side="long", price=Decimal(100) + Decimal(stamp % 3) / 10, basis=Decimal(100),
            volatility=Decimal(1), cycle_cost=Decimal(2), tick=Decimal(".01"), stamp=stamp)
        assert action is None


def test_atr_uses_closed_candles_and_rejects_gaps():
    candles = [{"start": index * 60000, "end": index * 60000 + 59999, "high": "101", "low": "99", "close": "100"} for index in range(16)]
    candles[-1].update(high="100000", low="1")
    assert atr(candles, 15 * 60000) == 2
    with pytest.raises(AqlaError, match="gap"):
        atr([row for i, row in enumerate(candles) if i != 2], 16 * 60000)


def ledger(rows):
    return {"plan": {"side": "long"}, "fill_events": {str(i): {"kind": kind, "entry_role": role, "sz": str(size),
        "px": str(price), "fee": ".01", "feeToken": "USDC", "time": i, "tid": i, "oid": i}
        for i, (kind, role, size, price) in enumerate(rows)}}


def test_rebought_flex_keeps_its_own_cost_and_does_not_consume_core():
    data = ledger([("entry", "original", 2, 100), ("waverider", None, 1, 110), ("entry", "waverider", 1, 105)])
    result = pools(data, Decimal(50))
    assert result["core"] == result["flex"] == result["rights"] == 1
    assert result["flex_basis"] == 105
    assert result["spent"] == Decimal(".03")
    data["fill_events"]["3"] = {"kind": "waverider", "sz": "2", "px": "120", "fee": ".01", "time": 3, "tid": 3, "oid": 3}
    with pytest.raises(AqlaError, match="flexible inventory"):
        pools(data, Decimal(50))


def test_profitable_cycles_never_refill_the_loss_budget():
    data = ledger([("entry", "original", 2, 100), ("waverider", None, 1, 90),
                   ("entry", "waverider", 1, 80), ("waverider", None, 1, 120)])
    assert pools(data, Decimal(50))["spent"] == Decimal("10.04")


def test_source_tp_retires_rights_even_when_flex_is_out_of_market():
    data = ledger([("entry", "original", 2, 100), ("waverider", None, 1, 110), ("tp", None, Decimal(".5"), 120)])
    assert pools(data, Decimal(50))["rights"] == Decimal(".5")


@pytest.mark.parametrize("side", ["long", "short"])
def test_partial_targets_preserve_exact_inventory_after_multiple_waves(side):
    data = ledger([("entry", "original", ".1994", "2500.1"),
                   ("waverider", None, ".0997", "2539.9"),
                   ("entry", "waverider", ".0997", "2490.1"),
                   ("waverider", None, ".0997", "2589.9"),
                   ("entry", "waverider", ".0927", "2510.1"),
                   ("tp", None, ".0961", "2700.9")])
    data["plan"]["side"] = side
    result = pools(data, Decimal(50))
    assert result["core"] + result["flex"] == Decimal(".0963")
    data["fill_events"]["6"] = {"kind": "tp", "sz": ".09631", "px": "2850.9", "fee": ".01", "time": 6}
    with pytest.raises(AqlaError, match="flexible inventory"):
        pools(data, Decimal(50))
    data["fill_events"]["6"]["sz"] = ".0963"
    result = pools(data, Decimal(50))
    assert result["core"] == result["flex"] == result["rights"] == 0


@pytest.mark.parametrize("value", [0, 21, True, "TP1", "", -1])
def test_invalid_profile_endpoints_are_not_saved(value):
    with pytest.raises(ValueError):
        Parameters(waverider_until_tp=value)


def test_old_profile_wire_has_no_new_default_fields():
    assert "management_mode" not in Policy().model_dump()
    assert "waverider_flex_percent" not in Parameters().model_dump()
    assert "waverider_lifetime" not in Policy().model_dump() and "waverider_lifetime" not in Parameters().model_dump()
    assert "waverider_lifetime" not in repr(Policy())
    assert Parameters(management_mode="waverider", waverider_until_tp="last").management_mode == "waverider"


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("endpoint", [1, 2, "last"])
def test_endpoint_is_latched_by_actual_partial_fill_after_price_rebound(side, endpoint):
    from types import SimpleNamespace

    from aqla_agent.waverider import ended
    policy = Policy(management_mode="waverider", waverider_lifetime="until_target", waverider_until_tp=endpoint)
    plan = SimpleNamespace(policy=policy, side=side, deadline_at=0)
    trade = {"targets": ["110", "120"] if side == "long" else ["90", "80"], "tp_filled": ["0", "0"], "waverider": {}}
    quote = SimpleNamespace(bid=Decimal(100), ask=Decimal(100))
    assert not ended(plan, trade, quote)
    trade["tp_filled"][0 if endpoint == 1 else 1] = ".001"
    assert ended(plan, trade, quote)


def test_live_gate_requires_authenticated_complete_comparison(tmp_path):
    path = tmp_path / "qualification.json"
    key = b"test-only-integration-key"
    assert not qualification_valid(path, key)
    from aqla_agent.waverider import COST_MODEL
    payload = {"version": VERSION, "cost_model": COST_MODEL, "cases_requested": 30, "data_errors": 0,
               "critical_errors": 0, "cases": 30, "waves": 10, "net_pnl": "1", "baseline_net_pnl": "0",
               "max_drawdown": "1", "baseline_max_drawdown": "1", "data_kind": "executable_depth", "holdout_hash": "frozen-test", "software_hash": software_hash()}
    def save():
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        path.write_text(json.dumps({**payload, "signature": hmac.new(key, body, hashlib.sha256).hexdigest()}))
    save()
    assert qualification_valid(path, key)
    assert not qualification_valid(path, key, expected_cohort="hyperliquid:mainnet:USDC")
    assert not qualification_valid(path, b"another-key")
    payload["cases"] = 29
    save()
    assert not qualification_valid(path, key)
    payload["cases"] = 30
    payload["data_errors"] = 1
    save()
    assert not qualification_valid(path, key)
    payload["data_errors"] = 0
    payload["cases_requested"] = 31
    save()
    assert not qualification_valid(path, key)
    payload["cases_requested"] = 30
    payload.pop("cost_model")
    save()
    assert not qualification_valid(path, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "signature", "waiver", "software", "profile", "account", "network", "future", "old_plan", "scope_string"])
async def test_explicit_operator_authorization_is_separate_and_narrowly_bound(setup, signal, invalid):
    from aqla_agent.waverider import live_authority, live_permission, operator_authorized, profile_hash
    from aqla_hyperliquid_mcp.models import TradePlan
    settings, state, _, _, trading = setup
    state.update(lambda data: data["policy"].update(management_mode="waverider"))
    source, value = signal
    plan = TradePlan.model_validate(await trading.preview(source.event_id, value))
    identity = profile_hash(plan.policy, plan.base_parameters)
    path = settings.state_path.parent / f"waverider-operator-live-{identity}.json"
    key = settings.integration_key_file.read_bytes().strip()
    payload = {"kind": "waverider_operator_live_v1", "version": VERSION, "operator": "test-owner",
        "historical_quality_waived": True, "consent_sha256": hashlib.sha256(b"explicit test owner consent").hexdigest(),
        "issued_at": plan.created_at, "software_hash": software_hash(), "profile_hash": identity,
        "allowed_accounts": [f"{plan.exchange}:{plan.network}:{plan.account.lower()}"],
        "allowed_cohorts": [f"{plan.exchange}:{plan.network}:{plan.market.quote_token}"]}
    assert live_authority(trading, plan) is None
    if invalid == "waiver":
        payload["historical_quality_waived"] = False
    elif invalid == "software":
        payload["software_hash"] = "another-version"
    elif invalid == "profile":
        payload["profile_hash"] = "another-profile"
    elif invalid == "account":
        payload["allowed_accounts"] = [f"{plan.exchange}:{plan.network}:another-account"]
    elif invalid == "network":
        payload["allowed_cohorts"] = [f"{plan.exchange}:mainnet:{plan.market.quote_token}"]
    elif invalid == "scope_string":
        payload["allowed_accounts"] = payload["allowed_accounts"][0] + "another-account"
    elif invalid in ("future", "old_plan"):
        payload["issued_at"] += 60000 if invalid == "future" else 1
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    path.write_text(json.dumps({**payload, "signature": "invalid" if invalid == "signature" else signature}))
    assert not qualification_valid(path, key)
    assert operator_authorized(path, key, plan) is (invalid is None)
    assert not operator_authorized(path, key + b"wrong", plan)
    assert live_permission(trading, plan) is (invalid is None)
    assert live_authority(trading, plan) == ("operator" if invalid is None else None)
    path.unlink()
    assert not live_permission(trading, plan)


def test_depth_is_not_inferred_beyond_observed_levels():
    stamp = now_ms()
    quote = PriceSnapshot(market="BTC", bid=99, ask=100, mid=Decimal("99.5"), stop_reference=100,
        stop_reference_kind="mark", started_at=stamp, exchange_time=stamp, observed_at=stamp,
        bids=[(99, 1)], asks=[(100, 1), (101, 2)])
    assert quote.executable(buy=True, limit=Decimal(100), quantity=Decimal(10)) == (Decimal(1), Decimal(100))
    with pytest.raises(ValueError):
        quote.model_copy().model_validate({**quote.model_dump(), "asks": [(101, 2), (100, 1)]})


def test_native_book_ignores_another_coin_and_rejects_stale_data():
    from aqla_hyperliquid_mcp.models import Market
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2)
    data = {"channel": "l2Book", "data": {"coin": "ETH", "time": now_ms(), "levels": [[{"px": "99", "sz": "1"}], [{"px": "100", "sz": "1"}]]}}
    assert parse_book("hyperliquid", market, data) is None
    data["data"].update(coin="BTC", time=now_ms() - 3000)
    with pytest.raises(AqlaError, match="fresh"):
        parse_book("hyperliquid", market, data)


@pytest.mark.asyncio
async def test_shadow_round_trip_never_sends_a_real_or_test_order(setup, signal, monkeypatch):
    from aqla_agent import entry, waverider
    _, state, boundary, _, trading = setup
    state.update(lambda data: data["policy"].update(management_mode="waverider"))
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    before = list(boundary.actions)
    clock = [now_ms()]
    monkeypatch.setattr(entry, "now_ms", lambda: clock[0])
    monkeypatch.setattr(waverider, "now_ms", lambda: clock[0])
    async def facts(*_):
        return {"atr": "1", "fee_rate": ".0004", "observed_at": clock[0]}
    monkeypatch.setattr(waverider, "market_facts", facts)
    for price in [100, 104, 103, 103, 103, 100, 101, 101, 101]:
        clock[0] += 1000
        quote = BookSnapshot(market="BTC", bid=price, ask=price, mid=price, exchange_time=clock[0],
            started_at=clock[0], observed_at=clock[0], bids=[(price, 1000)], asks=[(price, 1000)])
        await waverider.tick(trading, trade["trade_id"], quote)
    current = state.snapshot()["trades"][trade["trade_id"]]
    # In-memory observations are checkpointed at transitions, including both paper actions.
    assert [row["action"] for row in current["waverider"]["paper_actions"]] == ["reduce", "reenter"]
    assert current["waverider"]["shadow"]
    assert boundary.actions == before
