import asyncio
import copy
import json
import sqlite3
from decimal import Decimal

import pytest

from aqla_agent.waverider import collect_evidence, software_hash
from aqla_agent.waverider_replay import evaluate, export_replay, simulate
from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Interpretation, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan


def replay_case(prices, *, currency="USDC", venue="hyperliquid"):
    stamp = 1000000
    signal = Interpretation(kind="new_signal", source_refs=["source"], symbol="BTC", side="long", entry="market", stop_loss=90, take_profits=[110, 120])
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
