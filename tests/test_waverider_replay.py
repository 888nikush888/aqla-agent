import asyncio
import copy
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aqla_agent.waverider import collect_evidence, software_hash
from aqla_agent.waverider_replay import (
    ReplayEvidenceError,
    evaluate,
    export_replay,
    replan_case,
    simulate,
    validate_native_evidence,
)
from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Interpretation, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan


def replay_case(prices, *, currency="USDC", venue="hyperliquid"):
    stamp = 1000000
    signal = Interpretation(kind="new_signal", source_refs=["source:" + venue + currency], symbol="BTC", side="long", entry="market", stop_loss=90, take_profits=[110, 120])
    plan = make_plan(signal, Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20, quote_token=currency),
        AccountSnapshot(equity=10000, available_margin=10000, observed_at=stamp), Policy(entry_range_choice="split"),
        1, stamp, ("testnet", "account"), stamp=stamp)
    plan.exchange = venue
    rows = [{"quote": {"market": "BTC", "bid": str(price), "ask": str(price), "mid": str(price), "stop_reference": str(price), "stop_reference_kind": "mark",
             "started_at": stamp + i * 1000, "exchange_time": stamp + i * 1000, "observed_at": stamp + i * 1000,
             "bids": [[str(price), "10000"]], "asks": [[str(price), "10000"]]}, "fee_rate": ".0004", "atr": "1"} for i, price in enumerate(prices)]
    return {"family": venue + currency, "plan": plan.model_dump(mode="json"), "latency_ms": 1000,
        "original_entry_events": [{"kind": "entry", "entry_role": "original", "entry_leg": 0, "time": stamp,
            "sz": str(plan.quantity), "px": "100", "fee": ".01", "feeToken": currency, "oid": 1}], "observations": rows}


def test_replay_keeps_existing_fills_and_does_not_close_for_a_price_gain():
    case = replay_case([100, 100, 105, 111, 121, 150])
    result = simulate(case, wave=False)
    assert Decimal(result["net_pnl"]) > 0
    assert result["waves"] == 0


def test_replay_counts_stop_gap_in_pnl_without_inventing_a_new_admission():
    result = simulate(replay_case([100, 100, 89, 89, 89]), wave=True)
    assert Decimal(result["net_pnl"]) < 0


def test_replay_cannot_restart_waves_after_tp1_and_a_price_rebound():
    case = replay_case([100, 100, 110, 105, 107, 106, 106, 106, 106, 104, 105, 105, 105, 105])
    case["plan"]["policy"]["waverider_lifetime"] = "until_target"
    normal, wave = simulate(case, wave=False), simulate(case, wave=True)
    assert wave["waves"] == 0 and wave["fees"] == normal["fees"]
    assert wave["remaining_quantity"] == normal["remaining_quantity"]


def test_quote_currencies_and_networks_are_never_added_as_one_balance():
    one = replay_case([100, 100, 102, 103, 104, 105])
    two = replay_case([100, 100, 102, 103, 104, 105], currency="USDT", venue="aster")
    result = evaluate({"partition": "holdout", "provenance": "synthetic", "cases": [one, two]})
    assert result["critical_errors"] == 0
    assert result["net_pnl"] is None and result["aggregate_monetary_totals_unavailable"]
    assert set(result["cohorts"]) == {"hyperliquid:testnet:USDC", "aster:testnet:USDT"}
    assert result["data_kind"] == "synthetic"


def test_reused_setup_family_cannot_count_twice_as_holdout():
    case = replay_case([100] * 5)
    with pytest.raises(AqlaError, match="counted twice"):
        evaluate({"cases": [case, copy.deepcopy(case)]})


def test_relabelling_the_same_source_cannot_create_an_independent_setup():
    case = replay_case([100] * 5)
    alias = copy.deepcopy(case)
    alias["family"] = "different-label"
    with pytest.raises(AqlaError, match="source is reused"):
        evaluate({"cases": [case, alias]})


def settlement(case, stamp, *, rate=".001", price="120", basis=None):
    plan = case["plan"]
    return {"time": stamp, "rate": rate, "reference_price": price,
            "reference_kind": basis or ("oracle" if plan["exchange"] == "hyperliquid" else "mark"),
            "market": plan["market"]["symbol"], "exchange": plan["exchange"], "network": plan["network"],
            "currency": plan["market"]["quote_token"], "settled": True}


@pytest.mark.parametrize("side,expected", [("long", -1), ("short", 1)])
def test_native_funding_uses_settlement_inventory_and_oracle_not_trade_mark(side, expected):
    case = replay_case([100] * 5)
    case["plan"]["side"] = side
    if side == "short":
        case["plan"].update(stop_loss="110", take_profits=["90", "80"])
    stamp = case["observations"][2]["quote"]["observed_at"]
    case["funding"] = [settlement(case, stamp, price="120")]
    result = simulate(case, wave=False)
    quantity = Decimal(case["original_entry_events"][0]["sz"])
    assert Decimal(result["funding"]) == expected * quantity * 120 * Decimal(".001")
    unfunded = simulate({**case, "funding": []}, wave=False)
    assert Decimal(result["net_pnl"]) - Decimal(unfunded["net_pnl"]) == Decimal(result["funding"])


def test_funding_between_books_does_not_charge_a_later_original_entry():
    case = replay_case([100] * 5)
    origin = case["plan"]["created_at"]
    case["original_entry_events"][0]["time"] = origin + 1500
    case["funding"] = [settlement(case, origin + 1200)]
    assert simulate(case, wave=False)["funding"] == "0.000"
    case["original_entry_events"][0]["time"] = origin + 1100
    assert Decimal(simulate(case, wave=False)["funding"]) < 0
    case["original_entry_events"][0]["time"] = origin + 1200
    with pytest.raises(ReplayEvidenceError, match="ordering"):
        simulate(case, wave=False)


def test_funding_credits_do_not_refill_the_lifetime_loss_budget():
    case = replay_case([100] * 6)
    origin = case["plan"]["created_at"]
    case["funding"] = [settlement(case, origin + 1000), settlement(case, origin + 3000, rate="-.001")]
    result = simulate(case, wave=True)
    assert Decimal(result["funding"]) == 0
    assert Decimal(result["funding_spent"]) == Decimal(case["plan"]["quantity"]) * 120 * Decimal(".001")


@pytest.mark.parametrize("venue,basis", [("hyperliquid", "mark"), ("aster", "oracle")])
def test_funding_basis_cannot_be_substituted_between_venues(venue, basis):
    case = replay_case([100] * 5, venue=venue)
    case["funding"] = [settlement(case, case["plan"]["created_at"] + 1000, basis=basis)]
    with pytest.raises(ReplayEvidenceError, match="price basis"):
        simulate(case, wave=True)


def native_case():
    from aqla_agent.pipelines import Parameters
    case = replay_case([100] * 5)
    plan = case["plan"]
    plan["deadline_at"] = case["observations"][-1]["quote"]["observed_at"]
    plan["base_parameters"] = Parameters.from_policy(Policy.model_validate(plan["policy"])).model_dump(mode="json")
    case["fee_schedule"] = [{"exchange": plan["exchange"], "network": plan["network"], "account": plan["account"],
                             "market": "BTC", "currency": "USDC", "liquidity": "taker", "rate": ".0004",
                             "valid_from": plan["created_at"], "valid_until": plan["deadline_at"] + 1,
                             "source": "native_account_fee_history", "evidence_hash": "test-evidence"}]
    case["funding"] = []
    case["funding_coverage"] = {"start": plan["created_at"], "end": plan["deadline_at"],
                                "source": "native_settled_funding_history", "evidence_hash": "test-evidence"}
    return case


def test_until_target_cannot_qualify_a_recording_ending_only_at_the_entry_deadline():
    from aqla_hyperliquid_mcp.models import TradePlan
    case = native_case()
    case["plan"]["policy"]["waverider_lifetime"] = "until_target"
    case["plan"]["base_parameters"]["waverider_lifetime"] = "until_target"
    plan = TradePlan.model_validate(case["plan"])
    with pytest.raises(ReplayEvidenceError, match="Until-target replay"):
        validate_native_evidence(case, plan)


@pytest.mark.parametrize("change", ["maker", "account", "backdated", "fee_gap", "funding_absent", "quote_gap"])
def test_native_evidence_rejects_incomplete_or_relabelled_cost_proofs(change):
    from aqla_hyperliquid_mcp.models import TradePlan
    case = native_case()
    validate_native_evidence(case, TradePlan.model_validate(case["plan"]))
    fee = case["fee_schedule"][0]
    if change == "maker":
        fee["liquidity"] = "maker"
    elif change == "account":
        fee["account"] = "another-account"
    elif change == "backdated":
        fee.update(source="native_account_snapshot", observed_at=fee["valid_from"] + 100000)
    elif change == "fee_gap":
        fee["valid_until"] -= 2
    elif change == "funding_absent":
        case.pop("funding_coverage")
    else:
        case["observations"][-1]["quote"]["observed_at"] += 3000
    with pytest.raises(ReplayEvidenceError):
        validate_native_evidence(case, TradePlan.model_validate(case["plan"]))


def test_native_provenance_alone_never_qualifies_and_all_cases_remain_visible():
    good, missing = native_case(), replay_case([100] * 5, venue="aster", currency="USDT")
    report = evaluate({"partition": "holdout", "provenance": "recorded_native_depth", "cases": [good, missing]})
    assert report["cases_requested"] == 2 and report["cases"] == 1
    assert report["data_errors"] == 1 and report["critical_errors"] == 0
    assert report["failures"][0]["kind"] == "missing_evidence"


def test_current_split_profile_is_replanned_at_original_risk_without_reusing_old_fills():
    from aqla_agent.pipelines import Parameters
    stamp = 1000000
    source = Interpretation(kind="new_signal", source_refs=["source"], symbol="BTC", side="long",
                            entry="range", entry_prices=[95, 105], stop_loss=90, take_profits=[110, 120])
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2, max_leverage=20)
    policy = Policy(entry_range_choice="near")
    old = make_plan(source, market, AccountSnapshot(equity=10000, available_margin=10000, observed_at=stamp),
                    policy, 1, stamp, ("testnet", "account"), stamp=stamp)
    case = replay_case([100, 100, 100, 94, 94, 94])
    case.update(plan=old.model_dump(mode="json"), interpretation=source.model_dump(mode="json"))
    original = copy.deepcopy(case)
    parameters = Parameters.from_policy(policy).model_dump(mode="json")
    parameters["entry_range_choice"] = "split"
    result = replan_case(case, parameters)
    plan = result["plan"]
    assert len(plan["entry_legs"]) == 2
    assert {Decimal(leg["price"]) for leg in plan["entry_legs"]} == {Decimal(95), Decimal(105)}
    assert Decimal(plan["quantity"]) <= old.quantity and Decimal(plan["risk_usdc"]) <= old.risk_usdc
    assert plan["deadline_at"] <= old.deadline_at
    assert not result["original_entry_events"] and case == original
    execution = simulate(result, wave=False)
    assert execution["original_entries_filled"] == [0, 1]
    assert Decimal(execution["original_quantity"]) == Decimal(plan["quantity"])


def test_current_profile_cannot_be_applied_by_only_relabelling_an_old_plan():
    from aqla_agent.pipelines import Parameters
    with pytest.raises(ReplayEvidenceError, match="original evidenced interpretation"):
        replan_case(replay_case([100] * 5), Parameters().model_dump(mode="json"))


def test_old_near_policy_cannot_hide_behind_a_split_base_profile_hash():
    from aqla_hyperliquid_mcp.models import TradePlan
    case = native_case()
    case["plan"]["policy"]["entry_range_choice"] = "near"
    with pytest.raises(ReplayEvidenceError, match="claimed base profile: entry_range_choice"):
        validate_native_evidence(case, TradePlan.model_validate(case["plan"]))


def test_duplicate_funding_is_rejected_instead_of_counted_twice():
    case = replay_case([100] * 5)
    event = settlement(case, case["plan"]["created_at"] + 1000)
    case["funding"] = [event, dict(event)]
    with pytest.raises(ReplayEvidenceError, match="ordered native settlements"):
        simulate(case, wave=True)


@pytest.mark.asyncio
async def test_current_native_fee_discount_is_applied_once_and_never_backdated(monkeypatch):
    from aqla_agent import waverider
    stamp = 1000000
    monkeypatch.setattr(waverider, "now_ms", lambda: stamp)
    async def info(kind, **_):
        if kind == "userFees":
            return {"userCrossRate": ".00045", "activeReferralDiscount": ".04"}
        assert kind == "candleSnapshot"
        return [{"t": stamp - (index + 1) * 60000, "T": stamp - index * 60000 - 1,
                 "h": "101", "l": "99", "c": "100"} for index in range(16)]
    exchange = SimpleNamespace(settings=SimpleNamespace(venue="hyperliquid", network="testnet"), account="account", info=info)
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2)
    facts = await waverider.market_facts(exchange, market)
    assert Decimal(facts["fee_rate"]) == Decimal(".000432")
    evidence = facts["fee_evidence"]
    assert evidence["liquidity"] == "taker" and evidence["source"] == "native_account_snapshot"
    assert evidence["valid_from"] == evidence["observed_at"] == stamp
    assert evidence["network"] == "testnet" and evidence["account"] == "account"


@pytest.mark.asyncio
async def test_standard_shadow_collection_is_read_only_and_stores_plan_once(setup, signal, monkeypatch):
    from aqla_agent import waverider
    from aqla_hyperliquid_mcp.models import TradePlan
    settings, state, boundary, exchange, trading = setup
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    source, value = signal
    raw = await trading.preview(source.event_id, value)
    trade = await trading.execute(raw["plan_id"])
    plan = TradePlan.model_validate(raw)
    async def facts(*_):
        return {"atr": "1", "fee_rate": ".0004", "observed_at": now_ms()}
    monkeypatch.setattr(waverider, "market_facts", facts)
    await asyncio.gather(*getattr(trading, "_waverider_capture_tasks", {}).values())
    trading._waverider_facts_cache = {}
    before = list(boundary.actions)
    await collect_evidence(trading, trade["trade_id"], await exchange.price_snapshot(plan.market))
    path = settings.state_path.parent / "waverider-replay.sqlite3"
    with sqlite3.connect(path) as db:
        metadata = json.loads(db.execute("SELECT payload FROM setups").fetchone()[0])
        rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM observations")]
    assert metadata["plan"]["plan_id"] == plan.plan_id
    assert metadata["software_hash"] == software_hash()
    assert all("plan" not in row for row in rows)
    assert boundary.actions == before
    assert not state.snapshot()["trades"][trade["trade_id"]].get("waverider")
    assert len(export_replay(path)["cases"]) == 1
