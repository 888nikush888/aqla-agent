import copy
import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_sheets import Google

from aqla_agent import monthly as jobs
from aqla_agent.funding import aster_funding, bounded_history, hyperliquid_funding, lighter_funding, payment
from aqla_agent.messages import monthly_message, telegram_view
from aqla_agent.monthly import (
    BERLIN,
    SUM_LABEL,
    accounting_facts,
    acknowledge_publication,
    due_at,
    entry_quantity_evidence,
    fill_identity,
    month_bounds,
    monthly_report,
    next_job,
)
from aqla_agent.reporting import Reporting
from aqla_hyperliquid_mcp.models import AqlaError, GoogleSheetsSettings, Market, digest
from aqla_sheets_mcp.client import SheetsClient


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
        next(iter(row["fill_events"].values()))["fee"] = None
    elif damage == "currency":
        next(iter(row["fill_events"].values()))["feeToken"] = "HYPE"
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


def test_berlin_month_boundaries_dst_spanning_trade_and_testnet_exclusion():
    assert month_bounds("2026-10")[1] - month_bounds("2026-10")[0] == (31 * 24 + 1) * 3_600_000
    assert month_bounds("2026-03")[1] - month_bounds("2026-03")[0] == (31 * 24 - 1) * 3_600_000
    assert datetime.fromtimestamp(due_at("2026-10") / 1000, BERLIN).isoformat() == "2026-11-01T09:00:00+01:00"
    first = trade("boundary", close="2026-11-01T00:00:00+01:00")
    archived = trade("testnet")
    archived["plan"]["network"] = "testnet"
    oct_report = monthly_report(data(first, archived), "2026-10")
    assert oct_report["rows"] == [] and len(oct_report["open_trades"]) == 1
    assert oct_report["open_trades"][0]["roe_percent"] is None
    nov_report = monthly_report(data(first, archived), "2026-11")
    assert len(nov_report["rows"]) == 1 and nov_report["open_trades"] == []
    assert next_job(data(), due_at("2026-10") - 1)["report"] is None


@pytest.mark.parametrize("month", ["2026-13", "2026-0", "x", "2026-10-01", None, "1900-10"])
def test_month_validation(month):
    with pytest.raises(AqlaError):
        month_bounds(month)


def test_publication_restart_revision_and_notification_dedup():
    snapshot = data(trade())
    clock = due_at("2026-10")
    report = next_job(snapshot, clock)["report"]
    assert report["revision"] == 1 and not snapshot["notifications"]
    url = "https://docs.google.com/spreadsheets/d/test/edit#gid=1"
    acknowledge_publication(snapshot, "2026-10", report["fingerprint"], url, 1, clock)
    assert list(snapshot["notifications"]) == ["monthly:2026-10"]
    restarted = copy.deepcopy(snapshot)
    restarted["notifications"].clear()
    assert next_job(restarted, clock)["report"] is None
    acknowledge_publication(restarted, "2026-10", report["fingerprint"], url, 1, clock)
    assert not restarted["notifications"]
    restarted["source_names"]["-1001"] = "New name"
    updated = next_job(restarted, clock)["report"]
    assert updated["revision"] == 2
    with pytest.raises(AqlaError, match="geändert"):
        acknowledge_publication(restarted, "2026-10", report["fingerprint"], url, 1, clock)
    acknowledge_publication(restarted, "2026-10", updated["fingerprint"], url, 2, clock)
    assert not restarted["notifications"]


def test_request_publication_and_catchup():
    snapshot = data()
    snapshot["monthly_requests"] = ["2026-09"]
    report = next_job(snapshot, millis("2026-10-07T10:00:00+02:00"))["report"]
    acknowledge_publication(snapshot, "2026-09", report["fingerprint"], "url", 1, millis("2026-10-07T10:00:00+02:00"))
    assert list(snapshot["notifications"])[0].startswith("monthly-request:2026-09:")
    assert not snapshot["monthly_requests"]
    assert next_job(snapshot, due_at("2026-12"))["report"]["month"] == "2026-10"


def fail_selected_month(snapshot, clock, code):
    selected = next_job(snapshot, clock)
    jobs.fail_publication(snapshot, {"job": selected["job"], "error_code": code}, clock)
    return selected


def test_failed_old_month_does_not_starve_later_month():
    snapshot = data()
    clock = due_at("2026-11")
    failed = fail_selected_month(snapshot, clock, "budget")
    assert failed["report"]["month"] == "2026-10"
    selected = next_job(snapshot, clock)
    assert selected["report"]["month"] == "2026-11"
    blocked = snapshot["monthly_jobs"]["2026-10"]
    assert blocked["status"] == "blocked" and blocked["last_error"] == "budget"
    assert blocked["attempt_count"] == 1 and blocked["last_attempt"] == clock
    assert blocked["next_retry_at"] is None


def test_monthly_retry_state_survives_restart(setup, monkeypatch):
    from aqla_hyperliquid_mcp.state import State

    settings, state, *_ = setup
    clock = due_at("2026-10")
    state.update(lambda snapshot: fail_selected_month(snapshot, clock, "temporary"))
    expected = state.snapshot()["monthly_jobs"]
    state.close()
    restarted = State(settings)
    try:
        assert restarted.snapshot()["monthly_jobs"] == expected
        assert next_job(restarted.snapshot(), clock + 59_999)["report"] is None
        assert restarted.update(lambda snapshot: next_job(snapshot, clock + 60_000))["job"]["attempt_count"] == 2
    finally:
        restarted.close()


def test_monthly_fingerprint_change_releases_block():
    snapshot = data(trade())
    clock = due_at("2026-10")
    blocked = fail_selected_month(snapshot, clock, "schema")
    assert next_job(snapshot, clock)["report"] is None
    snapshot["source_names"]["-1001"] = "Changed source evidence label"
    current = next_job(snapshot, clock)
    assert current["report"]["fingerprint"] != blocked["report"]["fingerprint"]
    assert current["job"]["attempt_count"] == 1
    with pytest.raises(AqlaError, match="stale monthly job"):
        jobs.fail_publication(snapshot, {"job": blocked["job"], "error_code": "budget"}, clock)
    assert snapshot["monthly_jobs"]["2026-10"]["status"] == "publishing"


def test_monthly_temporary_failure_uses_bounded_retry():
    snapshot, clock = data(), due_at("2026-10")
    for attempt, seconds in enumerate([60, 120, 240, 480, 960, 1920, 3600, 3600], start=1):
        selected = fail_selected_month(snapshot, clock, "temporary")
        job = snapshot["monthly_jobs"]["2026-10"]
        assert job["attempt_count"] == attempt and job["status"] == "retry"
        assert job["next_retry_at"] == clock + seconds * 1000
        assert next_job(snapshot, clock + seconds * 1000 - 1)["report"] is None
        assert selected["job"]["attempt_count"] == attempt
        clock += seconds * 1000


def test_monthly_retry_keeps_scheduled_notice_unique():
    snapshot, clock = data(), due_at("2026-10")
    fail_selected_month(snapshot, clock, "temporary")
    selected = next_job(snapshot, clock + 60_000)
    report = selected["report"]
    acknowledge_publication(snapshot, report["month"], report["fingerprint"], "url", report["revision"], clock + 60_000,
                            job=selected["job"])
    assert list(snapshot["notifications"]) == ["monthly:2026-10"]
    assert snapshot["monthly_jobs"]["2026-10"]["status"] == "published"
    assert snapshot["monthly_jobs"]["2026-10"]["last_error"] is None
    snapshot["notifications"].clear()
    assert next_job(snapshot, clock + 60_001)["report"] is None
    acknowledge_publication(snapshot, report["month"], report["fingerprint"], "url", report["revision"], clock + 60_001,
                            job=selected["job"])
    assert not snapshot["notifications"]


def sheet_client():
    settings = GoogleSheetsSettings(enabled=True, spreadsheet_id="ci-monthly-" + "0" * 25, tab_namespace="mainnet", credentials_file="ci-only.secret")
    google = Google()
    return google, SheetsClient(settings, session=google, scope="mainnet:ci-account")


def test_sheet_publication_lose_reply_restart_shrink_and_raw_names():
    google, client = sheet_client()
    snapshot = data(trade())
    snapshot["source_names"]["-1001"] = '=IMPORTXML("untrusted")'
    report = monthly_report(snapshot, "2026-10")
    google.lose_reply = True
    with pytest.raises(AqlaError):
        client.publish_monthly(report)
    published = client.publish_monthly(report)
    title = "aqla Mainnet Monatsbericht 2026-10"
    assert sum(name == title for name in google.tables) == 1
    assert published["url"].endswith("#gid=" + str(google.properties[title]["sheetId"]))
    assert any('=IMPORTXML("untrusted")' in row for row in google.tables[title])
    assert all(call[2]["json"]["valueInputOption"] == "RAW" for call in google.calls if call[1].endswith("/values:batchUpdate"))
    snapshot["trades"] = {}
    report = monthly_report(snapshot, "2026-10")
    client.publish_monthly(report)
    assert not any("trade" in row for row in google.tables[title])


def test_sheet_confirm_all_values_scope_and_fingerprint():
    google, client = sheet_client()
    report = monthly_report(data(), "2026-10")
    altered = copy.deepcopy(report)
    altered["summary"]["trades"] = 99
    with pytest.raises(AqlaError, match="fingerprint"):
        client.publish_monthly(altered)
    client.publish_monthly(report)
    google.metadata = [{"metadataKey": "aqla.scope.mainnet", "metadataValue": "mainnet:another-account"}]
    with pytest.raises(AqlaError, match="different"):
        client.publish_monthly(report)


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


@pytest.mark.asyncio
async def test_aster_actual_signed_income_contract():
    seen = []
    market = Market(symbol="BTC", native_symbol="BTCUSDT", asset_id=0, product="perp", size_decimals=2, max_leverage=20, mid="100", quote_token="USDT")
    async def read(path, args, **kwargs):
        seen.append((path, args, kwargs))
        return [{"tranId": "1", "time": 1500, "incomeType": "FUNDING_FEE", "symbol": "BTCUSDT", "income": "2", "asset": "USDT"}]
    result = await aster_funding(SimpleNamespace(read=read), market, 1000, 2000)
    assert result[0]["amount"] == "2" and result[0]["signed_size"] is None
    assert seen[0][0] == "income" and seen[0][1]["incomeType"] == "FUNDING_FEE" and seen[0][2] == {"private": True}


@pytest.mark.asyncio
async def test_lighter_actual_position_funding_pagination_short_and_repeat_cursor():
    market = Market(symbol="BTC", asset_id=1, product="perp", size_decimals=2, max_leverage=20, mid="100")
    calls = []
    async def read(path, args, **kwargs):
        calls.append(args)
        return {"position_fundings": [{"timestamp": 2, "market_id": 1, "funding_id": 9, "change": "1", "position_size": "2", "position_side": "short"}], "next_cursor": "next" if len(calls) == 1 else None}
    adapter = SimpleNamespace(account="123", read=read)
    result = await lighter_funding(adapter, market, 1000, 3000)
    assert len(result) == 1 and result[0]["signed_size"] == "-2" and result[0]["time"] == 2000
    assert calls[0]["start_timestamp"] == 1 and calls[1]["cursor"] == "next"
    async def repeated(*args, **kwargs):
        return {"position_fundings": [], "next_cursor": "same"}
    with pytest.raises(AqlaError, match="cursor repeated"):
        await lighter_funding(SimpleNamespace(account="123", read=repeated), market, 1000, 3000)


def test_telegram_month_summary_only_after_publication_and_no_fabricated_numbers():
    snapshot = data(trade())
    report = monthly_report(snapshot, "2026-10")
    report["url"] = "https://docs.google.com/spreadsheets/d/ci/edit#gid=1"
    snapshot["notifications"]["monthly:2026-10"] = {"monthly_report": report}
    view = telegram_view(snapshot, {})
    text = "\n".join(view["messages"][0]["texts"])
    assert SUM_LABEL in text and "Vollständige Trade-Tabelle" in text
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
    reporter = Reporting(fake_state, SimpleNamespace(engines={}))
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
async def test_actual_monthly_mcp_read_internal_authority_and_publication_ack(setup):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    baseline = state.snapshot()
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["monthly_report"].annotations.read_only_hint
    assert (await server.call_tool("monthly_report", {"month": "2026-09"})).is_error is not True
    assert state.snapshot() == baseline
    with pytest.raises(ToolError, match="denied"):
        await server.call_tool("integration", {"action": "monthly_request", "token": "forged", "month": "2026-09"})
    token = settings.integration_key_file.read_text()
    await server.call_tool("integration", {"action": "monthly_request", "token": token, "month": "2026-09"})
    assert state.snapshot()["monthly_requests"] == ["2026-09"]
    assert not state.snapshot()["notifications"]
    selected = state.update(next_job)
    report = selected["report"]
    settings.google_sheets.spreadsheet_id = "ci" + "0" * 25
    publication = {"month": "2026-09", "fingerprint": report["fingerprint"], "revision": report["revision"],
                   "url": "https://docs.google.com/spreadsheets/d/" + settings.google_sheets.spreadsheet_id + "/edit#gid=7"}
    with pytest.raises(ToolError, match="URL"):
        await server.call_tool("integration", {"action": "monthly_ack", "token": token, "month": "2026-09",
                               "publication": {**publication, "url": "https://forged.invalid"}, "job": selected["job"]})
    await server.call_tool("integration", {"action": "monthly_ack", "token": token, "month": "2026-09", "publication": publication, "job": selected["job"]})
    assert state.snapshot()["notifications"] and not fake.actions
    assert state.snapshot()["profiles"] == baseline["profiles"]
    assert (await server.call_tool("status", {})).is_error is not True
    await close_server(server)


@pytest.mark.asyncio
async def test_actual_monthly_failure_and_ack_reject_stale_generations(setup, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.server import close_server, create_server

    settings, state, fake, exchange, _ = setup
    clock = due_at("2026-11")
    monkeypatch.setattr(jobs, "now_ms", lambda: clock)
    settings.google_sheets.spreadsheet_id = "ci" + "0" * 25
    server = create_server(settings, state=state, exchange=exchange)
    token = settings.integration_key_file.read_text()

    async def call(action, **args):
        response = await server.call_tool("integration", {"action": action, "token": token, **args})
        return json.loads(response.content[0].text)

    try:
        first = await call("monthly_job")
        assert first["report"]["month"] == "2026-10"
        await call("monthly_failure", failure={"job": first["job"], "error_code": "budget"})
        assert state.snapshot()["monthly_jobs"]["2026-10"]["status"] == "blocked"
        later = await call("monthly_job")
        assert later["report"]["month"] == "2026-11"
        await call("monthly_request", month="2026-10")
        fresh = await call("monthly_job")
        assert fresh["job"]["request_generation"] == first["job"]["request_generation"] + 1
        publication = {"month": "2026-10", "fingerprint": first["report"]["fingerprint"], "revision": first["report"]["revision"],
                       "url": "https://docs.google.com/spreadsheets/d/" + settings.google_sheets.spreadsheet_id + "/edit#gid=7"}
        before = state.snapshot()
        with pytest.raises(ToolError, match="stale monthly job"):
            await call("monthly_failure", failure={"job": first["job"], "error_code": "schema"})
        with pytest.raises(ToolError, match="stale monthly job"):
            await call("monthly_ack", month="2026-10", job=first["job"], publication=publication)
        with pytest.raises(ToolError):
            await call("monthly_failure", failure={"job": fresh["job"], "error_code": "payload-or-secret", "detail": "not-persisted"})
        assert state.snapshot() == before
        await call("monthly_ack", month="2026-10", job=fresh["job"], publication=publication)
        assert state.snapshot()["monthly_jobs"]["2026-10"]["status"] == "published"
        with pytest.raises(ToolError, match="stale monthly job"):
            await call("monthly_failure", failure={"job": fresh["job"], "error_code": "temporary"})
        assert list(state.snapshot()["notifications"]) == ["monthly:2026-10"]
        assert fake.actions == []
    finally:
        await close_server(server)
