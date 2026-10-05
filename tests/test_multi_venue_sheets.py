from decimal import Decimal

from test_sheets import Google

from aqla_hyperliquid_mcp.models import AccountSnapshot, Interpretation, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan
from aqla_sheets_mcp.client import META_HEADERS, PREFIX, SheetsClient
from aqla_sheets_mcp.journal import HEADERS, LEGACY_HEADERS, fingerprint, project, record


def test_header_extension_preserves_existing_rows_ids_and_metadata(setup):
    from types import SimpleNamespace

    google = Google()
    client = SheetsClient(SimpleNamespace(spreadsheet_id="unit-test-only"), session=google)
    client.layout()
    previous = ["historic-id", "channel", "BTC", "long", "closed", "now", 100, 100, 1, 1, 0, 90, "[]", 10, 0, 1, 0, 1, "[]"]
    google.tables[PREFIX + "Trades"] = [[*LEGACY_HEADERS["Trades"], *META_HEADERS], [*previous, fingerprint(previous), 7]]
    client.layout()
    assert google.tables[PREFIX + "Trades"][0] == [*HEADERS["Trades"], *META_HEADERS]
    assert google.tables[PREFIX + "Trades"][1][:len(previous)] == previous
    assert google.tables[PREFIX + "Trades"][1][-2:] == [fingerprint(previous), 7]
    assert len(google.tables[PREFIX + "Trades"]) == 2
    assert PREFIX + "Pipelines" in google.tables


def test_statistics_do_not_sum_usdc_usdt_or_testnet_mainnet():
    from types import SimpleNamespace

    google = Google()
    client = SheetsClient(SimpleNamespace(spreadsheet_id="unit-test-only"), session=google)
    rows = []
    for identity, currency, network in (("one", "USDC", "mainnet"), ("two", "USDT", "mainnet"), ("three", "USDC", "testnet")):
        row = record("Trades", identity, "channel", "BTC", "long", "closed", "now", 100, 100, 1, 1, 0, 90, "[]", 10, 0, 1, 0, 1, "[]")
        row["values"][-7:] = ["aster" if currency == "USDT" else "hyperliquid", network, "account", "pipeline", "profile", 1, currency]
        row["fingerprint"] = fingerprint(row["values"])
        rows.append(row)
    client.sync(rows)
    result = client.statistics()
    assert len(result["groups"]) == 3
    assert all(group["net_before_funding_usd"] == 1 for group in result["groups"].values())


def test_lighter_margin_fraction_rounding_reduces_size_and_aster_tiers_cap_signal():
    signal = Interpretation(kind="new_signal", source_refs=["unit-test-source"], symbol="BTC", side="long", leverage=20, entry="market", stop_loss="90", take_profits=["110"])
    market = Market(symbol="BTC", asset_id=1, product="perp", mid=100, size_decimals=5, quantity_step="0.00001", price_tick="0.1", max_leverage=50,
                    margin_leverage_scale=10000, leverage_tiers=[{"notional_floor": "0", "notional_cap": "10000", "max_leverage": 3}])
    account = AccountSnapshot(equity="10000", available_margin="10")
    plan = make_plan(signal, market, account, Policy(leverage_mode="signal", max_notional_usdc="10000"), 1, now_ms(), ("testnet", "unit-test"))
    assert plan.effective_leverage == 3
    assert plan.quantity * plan.entry_limit <= Decimal(10) * Decimal(10000) / 3334


def test_pipeline_and_profile_information_is_projected_without_credentials(setup):
    data = setup[1].snapshot()
    rows = project(data)
    assert rows["Pipelines:legacy:hyperliquid"]["values"][3:5] == ["hyperliquid", "standard"]
    assert len([row for row in rows.values() if row["table"] == "Handelsparameter" and row["record_id"].startswith("limits:")]) == 6
