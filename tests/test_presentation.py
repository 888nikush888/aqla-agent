
import copy
from decimal import Decimal

import pytest
from test_monthly import data, millis, trade

from aqla_agent.presentation import PresentationScope, PresentationSnapshot, build_presentation
from aqla_agent.ranking import RankingPolicy, outcomes
from aqla_hyperliquid_mcp.models import digest, require_first

CLOCK = millis("2026-10-09T12:00:00+02:00")


def test_daily_and_multiday_periods_use_berlin_calendar_and_preserve_scope():
    value = state_data(trade(close="2026-10-09T01:00:00+02:00"), trade("older", close="2026-10-06T12:00:00+02:00"))
    today = build_presentation(value, {"period": "today"}, clock=CLOCK)
    week = build_presentation(value, {"period": "7d"}, clock=CLOCK)
    assert today.evaluated_trades == 1 and week.evaluated_trades == 2
    assert today.period_start == millis("2026-10-09T00:00:00+02:00")
    assert today.period_end_exclusive == millis("2026-10-10T00:00:00+02:00")


def test_internal_dashboard_projection_has_no_external_snapshot_transport_limit(monkeypatch):
    monkeypatch.setattr("aqla_agent.presentation.MAX_PRESENTATION_BYTES", 1)
    value = state_data(trade())
    with pytest.raises(ValueError, match="transport budget"):
        build_presentation(value, clock=CLOCK)
    snapshot = build_presentation(value, clock=CLOCK, transport_budget=False)
    assert snapshot.complete_trades == 1 and snapshot.net_pnl.value is not None


def state_data(*trades):
    result = data(*trades)
    result.update(network="testnet", account="ci-primary", trading_account="ci-primary", sequence=12,
                  source_aliases={}, source_ranks={}, ranking_history={}, ranking_policy=RankingPolicy().model_dump(mode="json"),
                  account_limits={}, pipelines={})
    for row in result["trades"].values():
        row["plan"].update(source_refs=["source:" + row["trade_id"]], stop_loss="90")
        row["filled_quantity"], row["entry_vwap"] = "2", "101"
        row["initial_sl_risk"] = {"amount": "22"}
    return result




def test_presentation_finance_oracle_separates_network_currency_and_missing_evidence():
    usdc, usdt, testnet, missing = trade("hl"), trade("aster", currency="USDT"), trade("testnet", currency="USDT"), trade("missing", currency="USDT")
    usdt["plan"]["exchange"] = missing["plan"]["exchange"] = "aster"
    testnet["plan"].update(exchange="aster", network="testnet")
    require_first(iter(missing["fill_events"].values()))["fee"] = None
    missing["funding_coverage"]["fill_fingerprint"] = digest(missing["fill_events"])
    source = state_data(usdc, usdt, testnet, missing)
    before = copy.deepcopy(source)
    result = build_presentation(source, {"currency": "USDT"}, clock=CLOCK)
    assert [row.trade_id for row in result.journal] == ["aster", "missing"]
    assert result.net_pnl.value == Decimal("15.1") and result.net_pnl.status == "partial"
    assert result.fees.value == Decimal(".4") and result.fees.known == 1 and result.fees.total == 2
    assert result.journal[1].fees is None and result.journal[1].net_pnl is None
    assert result.complete_trades == 1 and result.evaluated_trades == 2 and result.hit_rate_percent == 100
    assert result.equity_curve is False
    assert source == before
    usd = build_presentation(source, {"currency": "USDC"}, CLOCK)
    assert [row.trade_id for row in usd.journal] == ["hl"] and usd.net_pnl.value == Decimal("15.1")
    paper = build_presentation(source, {"network": "testnet", "currency": "USDT"}, CLOCK)
    assert [row.trade_id for row in paper.journal] == ["testnet"]


def test_presentation_whole_multileg_setup_net_r_matches_ranking_not_sum_of_legs():
    first, second = trade("leg-a", currency="USDT"), trade("leg-b", currency="USDC", side="short")
    first["plan"]["exchange"] = "aster"
    source = state_data(first, second)
    for row in source["trades"].values():
        row["plan"].update(setup_id="one-setup", source_refs=["same-source"])
    result = build_presentation(source, {"currency": "USDT"}, clock=CLOCK)
    assert result.net_pnl.value == Decimal("15.1") and result.fees.value == Decimal(".4")
    assert result.channels[0].net_r == (Decimal("15.1") / 22 + Decimal("-15.9") / 22) / 2
    assert result.channels[0].net_r == Decimal(outcomes(source)[0]["-1001"][0]["net_r"])
    assert result.channels[0].scope_setups == 1
    source["events"]["pending"] = {"source_id": "same-source", "targets": {"leg-c": {"status": "planned"}}}
    pending = build_presentation(source, {"currency": "USDT"}, clock=CLOCK)
    assert pending.channels[0].net_r is None and pending.channels[0].excluded_setups == 1


def test_presentation_actual_fill_roles_do_not_mutate_financial_fingerprints():
    row = trade("role", currency="USDT")
    row["plan"].update(exchange="aster", entry_execution="maker_only", take_profit_execution="maker_only")
    source = state_data(row)
    before = copy.deepcopy(row["fill_events"])
    ids = list(row["fill_events"])
    row["fill_execution_roles"] = {ids[0]: "maker", ids[1]: "taker"}
    result = build_presentation(source, {"currency": "USDT"}, clock=CLOCK)
    assert result.journal[0].execution_roles == {"maker": 1, "taker": 1, "unknown": 2}
    assert result.journal[0].entry_execution == "maker_only"
    assert row["fill_events"] == before and result.journal[0].complete


def test_presentation_rank_origin_empty_data_and_publisher_fingerprint_contract():
    source = state_data()
    source["source_ranks"] = {"channel": {"tier": "C", "origin": "manual", "metrics": {"score": "62", "drawdown_r": "1.2"}, "evidence": [1, 2]}}
    source["ranking_history"] = {"change": {"source_id": "channel", "time": CLOCK - 1}}
    first = build_presentation(source, clock=CLOCK)
    assert first.net_pnl.value is None and first.net_pnl.status == "empty"
    assert first.channels[0].origin == "manual" and first.channels[0].proof == 2
    assert first.channels[0].last_change == CLOCK - 1
    source["sequence"] += 1
    second = build_presentation(source, clock=CLOCK + 60_000)
    assert first.fingerprint == second.fingerprint
    tampered = first.model_dump(mode="json")
    tampered["open_trades"] = 999
    with pytest.raises(ValueError, match="fingerprint"):
        PresentationSnapshot.model_validate(tampered)


@pytest.mark.parametrize("field,value", [("network", "live"), ("exchange", "unknown"), ("period", "2026-13"), ("currency", "USDT+USDC"), ("period", "=NOW()")])
def test_presentation_selector_validation_never_infers_or_executes_cell_data(field, value):
    with pytest.raises(ValueError):
        PresentationScope.model_validate({field: value})


@pytest.mark.parametrize("exchange,currency", [("hyperliquid", "USDC"), ("aster", "USDT"), ("lighter", "USDC")])
@pytest.mark.parametrize("network", ["mainnet", "testnet"])
@pytest.mark.parametrize("side,funding", [("long", "-1"), ("short", "2")])
def test_all_venues_networks_short_and_signed_funding_match_monthly_oracle(exchange, currency, network, side, funding):
    from aqla_agent.monthly import report_row

    row = trade("oracle", currency=currency, side=side)
    row["plan"].update(exchange=exchange, network=network)
    row["funding_events"] = {"payment": {"amount": funding}}
    row["funding_coverage"]["payments_fingerprint"] = digest(row["funding_events"])
    source = state_data(row)
    snapshot = build_presentation(source, {"exchange": exchange if exchange == "hyperliquid" else "all", "currency": currency, "network": network}, CLOCK)
    authoritative = report_row(source, row)
    expected_net = (Decimal("15.5") if side == "long" else Decimal("-15.5")) - Decimal(".4") + Decimal(funding)
    assert snapshot.net_pnl.value == expected_net
    assert snapshot.fees.value == Decimal(".4")
    assert snapshot.journal[0].entry_price == Decimal("101")
    assert snapshot.journal[0].exit_price == Decimal("108.75")
    assert snapshot.journal[0].quantity == Decimal("2")
    assert snapshot.hit_rate_percent == (100 if side == "long" else 0)
    assert snapshot.net_pnl.value == Decimal(authoritative["net_pnl"])
    assert snapshot.fees.value == Decimal(authoritative["fees"]) and snapshot.funding.value == Decimal(funding)
    assert snapshot.cumulative_realized_net[-1].value == snapshot.net_pnl.value
    assert snapshot.channels[0].net_r == Decimal(outcomes(source)[0]["-1001"][0]["net_r"])


@pytest.mark.parametrize("missing", ["funding", "fee_currency", "entry_quantity"])
def test_missing_cost_fx_and_execution_evidence_never_becomes_zero(missing):
    row = trade("incomplete", currency="USDT")
    if missing == "funding":
        row.pop("funding_coverage")
    elif missing == "fee_currency":
        require_first(iter(row["fill_events"].values()))["feeToken"] = "BNB"
        row["funding_coverage"]["fill_fingerprint"] = digest(row["fill_events"])
    else:
        row.pop("entry_fill_quantity_confirmed")
    snapshot = build_presentation(state_data(row), {"currency": "USDT"}, clock=CLOCK)
    assert snapshot.net_pnl.value is None and snapshot.net_pnl.status == "missing"
    assert snapshot.complete_trades == 0 and snapshot.daily_pnl[0].value is None
    assert snapshot.channels[0].net_r is None
    if missing == "funding":
        assert snapshot.funding.value is None
    if missing == "fee_currency":
        assert snapshot.fees.value is None


def test_past_month_open_cohort_does_not_include_future_costs_and_current_uses_berlin():
    row = trade("future-close", currency="USDT", close="2026-11-06T12:00:00+01:00")
    snapshot = build_presentation(state_data(row), {"currency": "USDT"}, clock=CLOCK)
    assert snapshot.open_trades == 1 and snapshot.net_pnl.status == "empty"
    assert snapshot.journal[0].closed_at is None and snapshot.journal[0].exit_price is None
    assert snapshot.journal[0].fees is None and snapshot.journal[0].funding is None
    november = build_presentation(state_data(row), {"currency": "USDT"}, clock=millis("2026-10-31T23:30:00+00:00"))
    assert november.resolved_period == "2026-11" and november.net_pnl.value == Decimal("15.1")


def test_past_period_entry_prices_quantities_and_fill_roles_do_not_include_future_fills():
    row = trade("future-partial-entry", currency="USDT", close="2026-11-06T12:00:00+01:00")
    entries = [fill for fill in row["fill_events"].values() if fill["kind"] == "entry"]
    entries[1]["time"] = millis("2026-11-01T12:00:00+01:00")
    row.pop("entry_accounting")
    source = state_data(row)
    row["fill_execution_roles"] = dict.fromkeys(row["fill_events"], "maker")
    snapshot = build_presentation(source, {"currency": "USDT"}, CLOCK)
    shown = snapshot.journal[0]
    assert snapshot.open_trades == 1 and snapshot.net_pnl.value is None
    assert shown.entry_price == 100 and shown.quantity == 1
    assert shown.execution_roles == {"maker": 1, "taker": 0, "unknown": 0}
    assert shown.closed_at is None and shown.exit_price is None and shown.fees is None
    for fill in entries:
        fill["time"] = millis("2026-11-01T12:00:00+01:00")
    empty = build_presentation(source, {"currency": "USDT"}, CLOCK)
    assert empty.open_trades == 0 and empty.journal == [] and empty.net_pnl.value is None


def test_native_snapshot_transport_bounds_escaped_unicode_without_exporting_history():
    from aqla_agent.monthly import native_monthly_bytes

    source = state_data(trade("native-budget"))
    snapshot = build_presentation(source, clock=CLOCK)
    assert native_monthly_bytes(snapshot.model_dump(mode="json")) < 600_000
    source["source_names"]["-1001"] = "😀\"" * 100_000
    with pytest.raises(ValueError, match="transport budget"):
        build_presentation(source, clock=CLOCK)


def test_50k_event_history_and_source_text_are_never_copied_or_exported():
    class NoCopy(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("Historical event copying is forbidden")

    row = trade("bounded", currency="USDT")
    source = state_data(row)
    source["events"] = NoCopy({str(index): {"source": {"text": "private source text" * 200}} for index in range(50_000)})
    source["events"]["pending"] = {"source_id": "source:bounded", "targets": {"unopened": {"status": "planned"}}}
    snapshot = build_presentation(source, {"currency": "USDT"}, clock=CLOCK)
    assert snapshot.channels[0].net_r is None and snapshot.channels[0].excluded_setups == 1
    assert "private source text" not in snapshot.model_dump_json() and len(snapshot.model_dump_json()) < 10_000


def test_unfilled_cancelled_and_pending_trades_have_no_realized_zero_or_open_position_count():
    pending, cancelled = trade("pending", currency="USDT"), trade("cancelled", currency="USDT")
    for row, status in ((pending, "waiting_entry"), (cancelled, "cancelled")):
        row.update(status=status, fill_events={}, filled_quantity="0")
        row.pop("closed_at")
        row.pop("entry_accounting")
    snapshot = build_presentation(state_data(pending, cancelled), {"currency": "USDT"}, clock=CLOCK)
    assert len(snapshot.journal) == 2 and all(row.cohort == "unfilled" for row in snapshot.journal)
    assert snapshot.open_trades == 0 and snapshot.evaluated_trades == 0
    assert snapshot.net_pnl.value is None and snapshot.net_pnl.status == "empty"


def test_scoped_outcomes_reuse_only_current_build_rows_and_keep_cross_currency_family(monkeypatch):
    from oracles.ranking_ed233a2 import outcomes as original_outcomes

    from aqla_agent import monthly, presentation

    selected, sibling, unrelated = trade("selected"), trade("sibling", currency="USDT"), trade("unrelated", currency="USDT")
    source = state_data(selected, sibling, unrelated)
    for row in (selected, sibling):
        row["plan"].update(setup_id="shared-family", source_refs=["shared-source"])
    expected_r = Decimal(require_first(row["net_r"] for row in original_outcomes(source)[0]["-1001"]
                             if row["setup_id"] == "shared-family"))
    calls, original = [], monthly.report_row

    def counted(data, row):
        calls.append(row["trade_id"])
        return original(data, row)

    monkeypatch.setattr(monthly, "report_row", counted)
    monkeypatch.setattr(presentation, "report_row", counted)
    result = build_presentation(source, clock=CLOCK)
    assert calls == ["selected", "sibling"]
    assert result.channels[0].scope_setups == 1
    assert result.channels[0].net_r == expected_r
    coverage = sibling.pop("funding_coverage")
    calls.clear()
    changed = build_presentation(source, clock=CLOCK)
    assert calls == ["selected", "sibling"]
    assert changed.channels[0].net_r is None and changed.channels[0].excluded_setups == 1
    assert result.channels[0].net_r == expected_r
    sibling["funding_coverage"] = coverage
    pending = copy.deepcopy(sibling)
    pending.update(trade_id="pending", status="waiting_entry", fill_events={}, filled_quantity="0")
    source["trades"]["pending"] = pending
    waiting = build_presentation(source, clock=CLOCK)
    assert waiting.channels[0].net_r is None and waiting.channels[0].excluded_setups == 1
    assert original_outcomes(source)[1]["-1001"][0]["trade_ids"] == ["selected", "sibling"]


def test_empty_scope_skips_unrelated_financial_outcomes(monkeypatch):
    from aqla_agent import monthly

    source = state_data(*(trade(str(index), currency="USDT") for index in range(100)))
    monkeypatch.setattr(monthly, "report_row", lambda *_: pytest.fail("Unselected family accounting was evaluated"))
    result = build_presentation(source, clock=CLOCK)
    assert not result.journal and not result.channels


def test_rank_history_is_scanned_once_with_original_valid_time_semantics():
    class OneScan(dict):
        def values(self):
            assert not getattr(self, "scanned", False), "Rank history was rescanned per source"
            self.scanned = True
            return super().values()

    source = state_data()
    source["source_ranks"] = {identity: {"tier": "F"} for identity in ("one", "two", "empty")}
    source["ranking_history"] = OneScan({
        "old": {"source_id": "one", "time": CLOCK - 1}, "new": {"source_id": "one", "time": CLOCK},
        "invalid": {"source_id": "one", "time": "later"}, "missing": {"source_id": "two"},
        "negative": {"source_id": "two", "time": -10}, "zero": {"source_id": "two", "time": 0}})
    result = build_presentation(source, clock=CLOCK)
    assert {row.source_id: row.last_change for row in result.channels} == {"one": CLOCK, "two": 0, "empty": None}


def test_default_currency_shows_primary_history_with_mainnet_berlin_month():
    source = state_data(trade("primary"), trade("aster", currency="USDT"))
    snapshot = build_presentation(source, clock=CLOCK)
    assert snapshot.scope == PresentationScope(currency="USDC", network="mainnet", period="current")
    assert snapshot.resolved_period == "2026-10"
    assert [row.trade_id for row in snapshot.journal] == ["primary"]
    assert snapshot.net_pnl.value == Decimal("15.1")
    assert source["trades"]["aster"]["plan"]["market"]["quote_token"] == "USDT"
