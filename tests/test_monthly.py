
import copy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aqla_agent.funding import bounded_history, hyperliquid_funding, payment
from aqla_agent.messages import monthly_message, telegram_view
from aqla_agent.monthly import (
    SUM_LABEL,
    accounting_facts,
    entry_quantity_evidence,
    fill_identity,
    month_bounds,
    monthly_report,
)
from aqla_agent.reporting import Reporting
from aqla_hyperliquid_mcp.models import AqlaError, Market, digest, require_first


def millis(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def trade(identity="trade", *, side="long", currency="USDC", close="2026-10-06T12:00:00+02:00", leverage=20):
    start = millis("2026-10-05T12:00:00+02:00")
    end = millis(close)
    market = Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, max_leverage=100, mid="100", quote_token=currency)
    fills = [{"oid": 1, "tid": 1, "time": start, "sz": "1", "px": "100", "kind": "entry"},
             {"oid": 1, "tid": 2, "time": start + 100, "sz": "1", "px": "102", "kind": "entry"},
             {"oid": 2, "tid": 3, "time": end - 100, "sz": ".5", "px": "105", "kind": "tp"},
             {"oid": 3, "tid": 4, "time": end, "sz": "1.5", "px": "110", "kind": "sl"}]
    fills = [{**row, "coin": "BTC", "fee": ".1", "feeToken": currency} for row in fills]
    row = {"trade_id": identity, "status": "closed", "chat_id": "-1001", "entry_cloid": "entry", "remaining_quantity": "0",
           "entry_fill_quantity_confirmed": "2",
           "order_observations": {"entry": {"status": "filled"}}, "fill_events": {fill_identity(row): row for row in fills},
           "plan": {"market": market.model_dump(mode="json"), "effective_leverage": leverage, "side": side, "exchange": "hyperliquid",
                    "network": "mainnet", "profile_id": "standard", "created_at": start - 100}}
    row.update(accounting_facts(row))
    row["funding_events"] = {}
    row["funding_coverage"] = {"start": start, "end": end, "complete": True,
                               "fill_fingerprint": digest(row["fill_events"]), "payments_fingerprint": digest({})}
    return row


def data(*trades):
    return {"trades": {row["trade_id"]: row for row in trades}, "source_names": {"-1001": "VIP Gold"},
            "profiles": {"standard": {"name": "Standard"}}, "notifications": {}, "events": {}, "operations": {}, "paused": False}


@pytest.mark.parametrize("side,sign", [("long", 1), ("short", -1)])
@pytest.mark.parametrize("funding", ["-1", "0", "2"])
def test_actual_partial_entries_and_exits_roe(side, sign, funding):
    row = trade(side=side)
    row["funding_events"] = {"f": {"amount": funding}}
    row["funding_coverage"]["payments_fingerprint"] = digest(row["funding_events"])
    result = monthly_report(data(row), "2026-10")
    value = result["rows"][0]
    assert Decimal(value["entry_notional"]) == 202
    assert Decimal(value["initial_margin"]) == Decimal("10.1")
    assert Decimal(value["gross_pnl"]) == Decimal("15.5") * sign
    assert Decimal(value["fees"]) == Decimal(".4")
    assert Decimal(value["net_pnl"]) == Decimal("15.5") * sign - Decimal(".4") + Decimal(funding)
    assert Decimal(value["roe_percent"]) == Decimal(value["net_pnl"]) / Decimal("10.1") * 100
    assert result["summary"]["roe_sum_label"] == SUM_LABEL


def test_currency_separation_and_arithmetic_sum():
    first, second = trade("hl"), trade("aster", currency="USDT", side="short")
    second["plan"]["exchange"] = "aster"
    result = monthly_report(data(first, second), "2026-10")
    assert result["summary"]["net_by_currency"] == {"USDC": "15.1", "USDT": "-15.9"}
    assert result["summary"]["wins"] == result["summary"]["losses"] == 1
    assert Decimal(result["summary"]["win_rate_percent"]) == 50
    assert Decimal(result["summary"]["roe_sum_percent"]) == sum(Decimal(row["roe_percent"]) for row in result["rows"])


@pytest.mark.parametrize("damage", ["fee", "currency", "funding", "exit", "entry", "mixed", "margin", "stale_funding"])
def test_missing_evidence_is_not_zero_and_excluded(damage):
    row = trade()
    if damage == "fee":
        require_first(iter(row["fill_events"].values()))["fee"] = None
    elif damage == "currency":
        require_first(iter(row["fill_events"].values()))["feeToken"] = "HYPE"
    elif damage == "funding":
        row.pop("funding_coverage")
    elif damage == "exit":
        row["fill_events"].pop(list(row["fill_events"])[-1])
    elif damage == "entry":
        row["order_observations"]["entry"]["status"] = "open"
        row.pop("entry_accounting")
    elif damage == "mixed":
        row["external_conflict"] = "Manual trade"
    elif damage == "margin":
        row["entry_accounting"]["initial_margin"] = "99"
    else:
        row["funding_events"]["late"] = {"amount": "1000"}
    result = monthly_report(data(row), "2026-10")
    assert result["rows"][0]["roe_percent"] is None
    assert result["rows"][0]["net_pnl"] is None
    assert result["rows"][0]["issues"]
    assert result["summary"]["excluded"] == 1
    assert result["summary"]["roe_sum_percent"] == "0"




def test_delayed_entry_fills_do_not_freeze_partial_margin():
    row = trade()
    row.pop("entry_accounting")
    row["entry_fill_quantity_confirmed"] = "3"
    assert "entry_accounting" not in accounting_facts(row)
    assert not monthly_report(data(row), "2026-10")["rows"][0]["complete"]
    assert entry_quantity_evidence({"order": {"status": "canceled", "order": {"origSz": "10", "sz": "8"}}}) == "2"
    assert entry_quantity_evidence({"order": {"status": "filled", "order": {"origSz": "10", "sz": "0"}}}) == "10"
    assert entry_quantity_evidence({"order": {"status": "open", "order": {"origSz": "10", "sz": "8"}}}) is None


def test_leverage_cap_uses_effective_margin_without_moving_prices():
    high, capped = trade("a", leverage=80), trade("b", leverage=20)
    source_prices = copy.deepcopy(capped["fill_events"])
    rows = monthly_report(data(high, capped), "2026-10")["rows"]
    assert Decimal(rows[0]["roe_percent"]) == Decimal(rows[1]["roe_percent"]) * 4
    assert capped["fill_events"] == source_prices




@pytest.mark.parametrize("entry_offset,included", [(-1, True), (0, False)])
def test_open_partial_entries_use_observed_time_at_berlin_month_end(entry_offset, included):
    row = trade("partial-open")
    end = month_bounds("2026-10")[1]
    row.update(status="open", remaining_quantity="2")
    row["order_observations"]["entry"]["status"] = "open"
    for field in ("entry_accounting", "entry_fill_quantity_confirmed", "closed_at"):
        row.pop(field)
    fills = [{**fill, "time": end + entry_offset} for fill in row["fill_events"].values() if fill["kind"] == "entry"]
    row["fill_events"] = {fill_identity(fill): fill for fill in fills}
    before = copy.deepcopy(row)
    report = monthly_report(data(row), "2026-10")
    assert len(report["open_trades"]) == int(included)
    assert report["rows"] == report["undated_closed_trades"] == []
    assert report["summary"]["complete"] == report["summary"]["trades"] == 0
    if included:
        opened = report["open_trades"][0]
        assert opened["entry_at"] == end - 1 and opened["complete"] is False
        assert all(opened[field] is None for field in ("entry_notional", "margin_leverage", "initial_margin", "gross_pnl", "fees", "funding", "net_pnl", "roe_percent"))
    assert row == before


@pytest.mark.parametrize("fills", ["empty", "zero", "undated"])
def test_monthly_open_entries_require_positive_dated_own_fills(fills):
    row = trade("unfilled-open")
    row.update(status="opening", remaining_quantity="0")
    row["order_observations"]["entry"]["status"] = "open"
    for field in ("entry_accounting", "entry_fill_quantity_confirmed", "closed_at"):
        row.pop(field)
    row["fill_events"] = {} if fills == "empty" else {identity: {**fill, **({"sz": "0"} if fills == "zero" else {"time": None})}
        for identity, fill in row["fill_events"].items() if fill["kind"] == "entry"}
    report = monthly_report(data(row), "2026-10")
    assert report["open_trades"] == report["rows"] == report["undated_closed_trades"] == []


@pytest.mark.parametrize("month", ["2026-13", "2026-0", "x", "2026-10-01", None, "1900-10"])
def test_month_validation(month):
    with pytest.raises(AqlaError):
        month_bounds(month)






































def report_at(snapshot, revision):
    return {**monthly_report(snapshot, "2026-10"), "revision": revision}


def values_writes(google):
    return [kwargs["json"] for method, url, kwargs in google.calls if method == "POST" and url.endswith("/values:batchUpdate")]


def written_rows(google):
    return sum(len(update["values"]) for call in values_writes(google) for update in call["data"])
































@pytest.mark.asyncio
async def test_bounded_history_splits_saturation_and_refuses_ambiguous_pages():
    calls = []
    async def fetch(start, end):
        calls.append((start, end))
        return [{"n": number} for number in range(start, end + 1)]
    assert len(await bounded_history(fetch, 0, 3, limit=3)) == 4
    assert calls == [(0, 3), (0, 1), (2, 3)]
    with pytest.raises(AqlaError, match="budget"):
        await bounded_history(fetch, 0, 3, limit=3, budget=1)
    with pytest.raises(AqlaError, match="millisecond"):
        await bounded_history(fetch, 0, 0, limit=1)


@pytest.mark.asyncio
async def test_hyperliquid_actual_payment_contract_and_hip3():
    market = Market(symbol="xyz:GOLD", asset_id=110001, product="perp", size_decimals=2, max_leverage=20, mid="100")
    seen = []
    async def info(kind, **args):
        seen.append((kind, args))
        return [{"time": 1500, "hash": "tx", "delta": {"coin": "xyz:GOLD", "type": "funding", "usdc": "-.3", "szi": "2"}}]
    result = await hyperliquid_funding(SimpleNamespace(account="owner", info=info), market, 1000, 2000)
    assert result[0]["amount"] == "-0.3" and result[0]["signed_size"] == "2"
    assert seen == [("userFunding", {"user": "owner", "startTime": 1000, "endTime": 2000})]






def test_telegram_month_summary_only_after_publication_and_no_fabricated_numbers():
    snapshot = data(trade())
    report = monthly_report(snapshot, "2026-10")
    report["url"] = "https://docs.google.com/spreadsheets/d/ci/edit#gid=1"
    snapshot["notifications"]["monthly:2026-10"] = {"monthly_report": report}
    view = telegram_view(snapshot, {})
    text = "\n".join(view["messages"][0]["texts"])
    assert SUM_LABEL in text and "/aqla → Journal & Berichte" in text
    assert view["messages"][0]["notification_ids"] == ["monthly:2026-10"]
    assert "USDC" in text
    assert monthly_message(monthly_report(data(), "2026-10")).count("nicht berechenbar") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed,wrong_size", [(False, False), (True, False), (False, True)])
async def test_funding_attribution_matches_owned_fills_and_refuses_manual_quantity(mixed, wrong_size, monkeypatch):
    snapshot = data(trade())
    row = snapshot["trades"]["trade"]
    async def info(*args, **kwargs):
        rows = list(row["fill_events"].values())
        if mixed:
            rows += [{**rows[0], "oid": 999, "tid": 999}]
        return rows
    async def payments(market, start, end):
        return [payment("fund", start + 500, "-1", "USDC", "BTC", size="3" if wrong_size else "2")]
    adapter = SimpleNamespace(info=info, funding_payments=payments, account="owner")
    fake_state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda fn: fn(snapshot))
    engine = SimpleNamespace(state=fake_state, exchange=adapter, assert_plan_binding=lambda *_: None)
    monkeypatch.setattr("aqla_agent.reporting.TradePlan.model_validate", lambda plan: plan)
    reporting = Reporting(fake_state, SimpleNamespace(engines={"hyperliquid": engine}))
    await reporting.refresh("trade")
    assert row["funding_coverage"]["complete"] == (not mixed and not wrong_size)
    report = monthly_report(snapshot, "2026-10")
    assert report["rows"][0]["complete"] == (not mixed and not wrong_size)
    if not mixed and not wrong_size:
        assert report["rows"][0]["funding"] == "-1"
        before = copy.deepcopy(row["funding_events"])
        await reporting.refresh("trade")
        assert row["funding_events"] == before


@pytest.mark.asyncio
async def test_historical_backfill_retains_ids_orders_and_does_not_adopt_manual_exit(monkeypatch):
    row = trade()
    original = copy.deepcopy(row)
    row.pop("entry_accounting")
    row.pop("closed_at")
    row.pop("entry_fill_quantity_confirmed")
    row["fill_events"] = {}
    row["orders"] = [{"kind": "tp", "cloid": "tp"}, {"kind": "sl", "cloid": "sl"}]
    snapshot = data(row)
    fake_state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda fn: fn(snapshot))
    async def status(cloid):
        oid = {"entry": 1, "tp": 2, "sl": 3}[cloid]
        return {"order": {"status": "filled", "order": {"oid": oid, "origSz": "2", "sz": "0"}}}
    async def info(*args, **kwargs):
        values = list(original["fill_events"].values())
        return values + [{**values[-1], "oid": 999, "tid": 999}]
    async def payments(*args):
        return []
    market = Market.model_validate(row["plan"]["market"])
    monkeypatch.setattr("aqla_agent.reporting.TradePlan.model_validate", lambda plan: SimpleNamespace(market=market, created_at=plan["created_at"]))
    adapter = SimpleNamespace(account="owner", order_status=status, info=info, funding_payments=payments)
    engine = SimpleNamespace(exchange=adapter, state=fake_state, assert_plan_binding=lambda *_: None)
    await Reporting(fake_state, SimpleNamespace(engines={"hyperliquid": engine})).refresh("trade")
    assert row["entry_accounting"]["initial_margin"] == original["entry_accounting"]["initial_margin"]
    assert row["closed_at"] == original["closed_at"]
    assert set(row["fill_events"]) == set(original["fill_events"])
    assert row["funding_coverage"]["complete"] is False  # Additional foreign fill blocks funding attribution.
    assert row["plan"] == original["plan"] and row["trade_id"] == original["trade_id"]


@pytest.mark.asyncio
async def test_reporting_timeout_and_fairness_do_not_starve_other_closed_trades(monkeypatch):
    first, second = trade("old"), trade("new")
    first["reporting_checked_at"] = 1
    second["reporting_checked_at"] = 2
    first["funding_coverage"] = second["funding_coverage"] = {}
    snapshot = data(first, second)
    fake_state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda fn: fn(snapshot))
    reporter = Reporting(fake_state, SimpleNamespace(engines={}), max_jobs=1)
    async def timeout(coroutine, **_):
        coroutine.close()
        raise TimeoutError
    monkeypatch.setattr("aqla_agent.reporting.asyncio.wait_for", timeout)
    await reporter.tick()
    assert first["reporting_checked_at"] > second["reporting_checked_at"]
    assert not first["funding_coverage"]["complete"]
    await reporter.tick()
    assert second["reporting_checked_at"] > 2


@pytest.mark.asyncio
async def test_saved_verified_evidence_survives_temporary_exchange_outage(monkeypatch):
    snapshot = data(trade())
    row = snapshot["trades"]["trade"]
    before = copy.deepcopy(row["funding_coverage"])
    async def unavailable(*args, **kwargs):
        raise OSError("Exchange unavailable")
    fake_state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda fn: fn(snapshot))
    adapter = SimpleNamespace(account="owner", info=unavailable)
    engine = SimpleNamespace(state=fake_state, exchange=adapter, assert_plan_binding=lambda *_: None)
    monkeypatch.setattr("aqla_agent.reporting.TradePlan.model_validate", lambda plan: plan)
    await Reporting(fake_state, SimpleNamespace(engines={"hyperliquid": engine})).refresh("trade")
    assert row["funding_coverage"] == before
    assert monthly_report(snapshot, "2026-10")["rows"][0]["complete"]


@pytest.mark.asyncio
async def test_reporting_uses_the_trade_market_for_native_fill_history(monkeypatch):
    snapshot = data(trade())
    row = snapshot["trades"]["trade"]
    calls = []
    async def selected(market, start, end):
        assert market == Market.model_validate(row["plan"]["market"])
        assert start == row["plan"]["created_at"] and end == row["closed_at"]
        calls.append(market)
        return list(row["fill_events"].values())
    async def general(*args, **kwargs):
        pytest.fail("Native historical reads must select the immutable trade market")
    async def payments(*args):
        return []
    adapter = SimpleNamespace(fill_history=selected, info=general, funding_payments=payments, account="owner")
    fake_state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda fn: fn(snapshot))
    engine = SimpleNamespace(state=fake_state, exchange=adapter, assert_plan_binding=lambda *_: None)
    monkeypatch.setattr("aqla_agent.reporting.TradePlan.model_validate", lambda plan: plan)
    original_facts = copy.deepcopy(row["fill_events"])
    await Reporting(fake_state, SimpleNamespace(engines={"hyperliquid": engine})).refresh("trade")
    assert len(calls) == 1 and row["funding_coverage"]["complete"] is True
    assert row["fill_events"] == original_facts






def test_entry_freezes_only_after_remainder_terminal_and_lighter_rounding():
    row = trade(leverage=3)
    row.pop("entry_accounting")
    row["plan"]["market"]["margin_leverage_scale"] = 10000
    row["order_observations"]["entry"]["status"] = "open"
    assert "entry_accounting" not in accounting_facts(row)
    row["order_observations"]["entry"]["status"] = "canceled"
    facts = accounting_facts(row)["entry_accounting"]
    assert Decimal(facts["margin_leverage"]) == Decimal(10000) / 3334
    assert Decimal(facts["initial_margin"]) == Decimal(202) / (Decimal(10000) / 3334)
    row["entry_accounting"] = facts
    assert "entry_accounting" not in accounting_facts(row)
