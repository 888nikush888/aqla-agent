"""Instrument the actual range transport without Google access or credentials."""
import copy
from types import SimpleNamespace

import pytest
from test_sheets import Google

from aqla_hyperliquid_mcp.models import AqlaError
from aqla_sheets_mcp import client as adapter
from aqla_sheets_mcp.client import PREFIX, SheetsClient
from aqla_sheets_mcp.journal import fingerprint, record


@pytest.fixture
def transport():
    google = Google()
    client = SheetsClient(SimpleNamespace(spreadsheet_id="memory-test-only"), session=google, scope="testnet:ci-account")
    client.layout()
    google.calls.clear()
    return google, client


def signal(identity, revision=1):
    return {**record("Signale", identity, "channel", 1, "now", "", "", False, "pending", "", "", "", "", "", "X" * 16000, "source"), "revision": revision}


def rules(revision, paused=False):
    return {**record("Regeln", "aktuell", "testnet", "ci-account", 1, paused, "[]", "null"), "revision": revision}


def trade(identity, net=1):
    return {**record("Trades", identity, "channel", "BTC", "long", "closed", "now", 100, 100, 1, 1, 0, 90, "[]", 10, 0, net, 0, net, "[]"), "revision": 1}


def preload(google, table, records):
    google.tables[PREFIX + table][1:] = [[*row["values"], row["fingerprint"], row.get("revision", 0)] for row in records]
    google.properties[PREFIX + table]["gridProperties"]["rowCount"] = max(1000, len(records) + 1)


def body_ranges(google):
    ranges = []
    for method, url, kwargs in google.calls:
        if method == "GET" and url.endswith("/values:batchGet"):
            for query in kwargs["params"]["ranges"]:
                title, first_col, first_row, last_col, last_row = Google.coordinates(query)
                if first_row > 1 and first_col == 1 and last_col > 1:
                    ranges.append((title, first_row, last_row))
    return ranges


def test_equal_content_advances_revision_and_rejects_intermediate_state(transport):
    google, client = transport
    client.sync([rules(1)])
    client.sync([rules(3)])
    assert google.tables[PREFIX + "Regeln"][1][-1] == 3
    assert client.sync([rules(2, True)])["accepted"] == {}
    row = client.read("Regeln")["rows"][0]
    assert row["Pause"] is False
    assert row["_verified"] is True
    # Older identical content may be acknowledged, but cannot lower the maximum.
    client.sync([rules(1)])
    assert google.tables[PREFIX + "Regeln"][1][-1] == 3


def test_lost_metadata_reply_retries_without_duplicate_or_revision_regression(transport):
    google, client = transport
    client.sync([rules(1)])
    google.lose_reply = True
    with pytest.raises(AqlaError):
        client.sync([rules(3)])
    assert client.sync([rules(3)])["accepted"]
    assert len(google.tables[PREFIX + "Regeln"]) == 2
    assert google.writes == 2
    assert google.tables[PREFIX + "Regeln"][1][-1] == 3


def test_one_row_upsert_and_page_do_not_download_historical_source_bodies(transport):
    google, client = transport
    records = [signal(str(index)) for index in range(2000)]
    preload(google, "Signale", records)
    # ID lookup must follow the current sheet order after a prior sort.
    google.tables[PREFIX + "Signale"][1:] = list(reversed(google.tables[PREFIX + "Signale"][1:]))
    incoming = copy.deepcopy(records[123])
    incoming["revision"] = 2
    incoming["values"][-2] = "=literal changed text"
    incoming["fingerprint"] = fingerprint(incoming["values"])
    assert client.sync([incoming])["accepted"]
    assert body_ranges(google) == [(PREFIX + "Signale", 1878, 1878)]
    google.calls.clear()
    page = client.read("Signale", offset=1876, limit=1)
    assert page["rows"][0]["ID"] == "123"
    assert page["rows"][0]["Text"] == "=literal changed text"
    assert page["total"] == 2000 and page["total_complete"]
    assert page["unverified_rows_scope"] == "page"
    assert body_ranges(google) == [(PREFIX + "Signale", 1878, 1878)]


def test_index_chunks_detect_duplicates_and_fail_closed_when_budget_is_exceeded(transport, monkeypatch):
    google, client = transport
    monkeypatch.setattr(adapter, "INDEX_CHUNK_ROWS", 3)
    monkeypatch.setattr(adapter, "MAX_INDEX_REQUESTS", 2)
    preload(google, "Regeln", [rules(1)])
    google.properties[PREFIX + "Regeln"]["gridProperties"]["rowCount"] = 8
    with pytest.raises(AqlaError, match="ID scan exceeded"):
        client.sync([rules(2)])
    assert google.writes == 0
    assert body_ranges(google) == []
    google.properties[PREFIX + "Regeln"]["gridProperties"]["rowCount"] = 7
    google.tables[PREFIX + "Regeln"].extend([[], [], [], copy.deepcopy(google.tables[PREFIX + "Regeln"][1])])
    with pytest.raises(AqlaError, match="Duplicate"):
        client.read("Regeln")


def test_filtered_pages_report_incomplete_totals_and_bounded_body_scans(transport, monkeypatch):
    google, client = transport
    monkeypatch.setattr(adapter, "TEXT_CHUNK_ROWS", 2)
    monkeypatch.setattr(adapter, "MAX_BODY_REQUESTS", 2)
    preload(google, "Signale", [signal(str(index)) for index in range(8)])
    result = client.read("Signale", limit=1, filters={"Quelle": "channel"})
    assert len(result["rows"]) == 1
    assert result["scanned_rows"] == 4
    assert result["matched_rows_scanned"] == 4
    assert result["total"] is None and result["total_complete"] is False
    assert result["complete"] is False and result["scan_truncated"] is True
    assert body_ranges(google) == [(PREFIX + "Signale", 2, 3), (PREFIX + "Signale", 4, 5)]
    # Unfiltered pages can continue without rescanning prior bodies.
    google.calls.clear()
    first_page = client.read("Signale", limit=8)
    assert first_page["complete"] is False and first_page["next_offset"] == 4
    assert first_page["total"] == 8 and first_page["total_complete"] is True
    assert body_ranges(google) == [(PREFIX + "Signale", 2, 3), (PREFIX + "Signale", 4, 5)]
    google.calls.clear()
    page = client.read("Signale", offset=4, limit=8)
    assert [row["ID"] for row in page["rows"]] == ["4", "5", "6", "7"]
    assert page["total"] == 8 and page["next_offset"] is None
    assert page["complete"] is True
    assert body_ranges(google) == [(PREFIX + "Signale", 6, 7), (PREFIX + "Signale", 8, 9)]


def test_complete_filtered_counts_and_statistics_include_all_scanned_rows(transport):
    google, client = transport
    preload(google, "Trades", [trade("one", 1), trade("two", -2), trade("three", 0)])
    page = client.read("Trades", offset=1, limit=1, filters={"Quelle": "channel"})
    assert page["complete"] is True and page["total_complete"] is True
    assert page["total"] == 3 and page["next_offset"] == 2
    assert page["rows"][0]["ID"] == "two"
    result = client.statistics()
    assert result["complete"] is True and result["scan_truncated"] is False
    assert result["scanned_trades"] == result["total_trades"] == 3
    group = result["groups"]["gesamt"]
    assert (group["wins"], group["losses"], group["break_even"]) == (1, 1, 1)
    assert group["net_before_funding_usd"] == -1


def test_statistics_exclude_edited_rows_and_label_partial_aggregates(transport, monkeypatch):
    google, client = transport
    monkeypatch.setattr(adapter, "BODY_CHUNK_ROWS", 2)
    monkeypatch.setattr(adapter, "MAX_BODY_REQUESTS", 2)
    preload(google, "Trades", [trade(str(index)) for index in range(6)])
    google.tables[PREFIX + "Trades"][2][17] = 10000
    result = client.statistics()
    assert result["complete"] is False and result["scan_truncated"] is True
    assert result["scanned_trades"] == 4 and result["total_trades"] == 6
    assert result["unverified_rows_excluded"] == 1
    assert result["groups"]["gesamt"]["net_before_funding_usd"] == 3
    assert body_ranges(google) == [(PREFIX + "Trades", 2, 3), (PREFIX + "Trades", 4, 5)]


def test_append_never_overwrites_a_body_with_a_removed_id(transport):
    google, client = transport
    google.tables[PREFIX + "Signale"].append(["", "manual data with no ID"])
    with pytest.raises(AqlaError, match="without an ID"):
        client.sync([signal("new")])
    assert google.writes == 0
    assert google.tables[PREFIX + "Signale"][1][1] == "manual data with no ID"


def test_partial_sync_prioritizes_delivery_order_instead_of_physical_row_order(transport, monkeypatch):
    google, client = transport
    monkeypatch.setattr(adapter, "TEXT_CHUNK_ROWS", 1)
    monkeypatch.setattr(adapter, "MAX_BODY_REQUESTS", 1)
    preload(google, "Signale", [signal(str(index)) for index in range(5)])
    requested = [signal("new"), signal("4", 2), signal("0", 2)]
    first = client.sync(requested)
    assert first["accepted"] == {"Signale:new": requested[0]["fingerprint"]}
    assert client.sync(requested[1:])["accepted"] == {"Signale:4": requested[1]["fingerprint"]}


def test_missing_range_response_is_not_misreported_as_empty_data(transport):
    google, client = transport
    original = google.request
    def truncated(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/values:batchGet"):
            data = response.json()
            data["valueRanges"] = data["valueRanges"][:-1]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)
        return response
    google.request = truncated
    with pytest.raises(AqlaError, match="incomplete range"):
        client.read("Signale")
