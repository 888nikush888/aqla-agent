"""Instrument the actual range transport without Google access or credentials."""
import base64
import copy
import json
from types import SimpleNamespace

import pytest
from test_sheets import Google

from aqla_hyperliquid_mcp.models import AqlaError
from aqla_sheets_mcp import client as adapter
from aqla_sheets_mcp.client import PREFIX, SheetsClient
from aqla_sheets_mcp.journal import HEADERS, fingerprint, record


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


def filtered_ids(google, client, table, filters, *, limit=100):
    """Follow legacy offsets before continuing the remaining physical index."""
    identities, offset, cursor = [], 0, None
    for _ in range(100):
        google.calls.clear()
        kwargs = {"scan_cursor": cursor} if cursor is not None else {}
        page = client.read(table, offset=offset, limit=limit, filters=filters, **kwargs)
        identities.extend(row["ID"] for row in page["rows"])
        chunk = adapter.TEXT_CHUNK_ROWS if table in adapter.TEXT_TABLES else adapter.BODY_CHUNK_ROWS
        assert sum(last - first + 1 for _, first, last in body_ranges(google)) <= chunk * adapter.MAX_BODY_REQUESTS
        body_calls = [kwargs for method, url, kwargs in google.calls
                      if method == "GET" and url.endswith("/values:batchGet")
                      and any(Google.coordinates(query)[1] == 1 and Google.coordinates(query)[2] > 1
                              and Google.coordinates(query)[3] > 1 for query in kwargs["params"]["ranges"])]
        assert len(body_calls) <= adapter.MAX_BODY_REQUESTS
        assert all(len(kwargs["params"]["ranges"]) <= 20 for kwargs in body_calls)
        if page["next_offset"] is not None:
            assert page.get("next_scan_cursor") is None
            offset = page["next_offset"]
        elif page.get("next_scan_cursor") is not None:
            offset, cursor = 0, page["next_scan_cursor"]
        else:
            return identities
    pytest.fail("Filtered pagination did not terminate")


def test_filtered_125_signal_ids_are_reachable_without_duplicates(transport):
    google, client = transport
    expected = [f"signal-{index:04d}" for index in range(125)]
    preload(google, "Signale", [signal(identity) for identity in expected])
    assert filtered_ids(google, client, "Signale", {"Quelle": "channel"}) == expected


def test_filtered_1255_trade_ids_match_complete_oracle(transport):
    google, client = transport
    expected = [f"trade-{index:04d}" for index in range(1255)]
    preload(google, "Trades", [trade(identity) for identity in expected])
    assert filtered_ids(google, client, "Trades", {"Quelle": "channel"}) == expected


def signal_cursor(google, client, *, count=125, filters=None):
    preload(google, "Signale", [signal(f"signal-{index:04d}") for index in range(count)])
    return client.read("Signale", offset=120, filters=filters or {"Quelle": "channel"})["next_scan_cursor"]


def test_filtered_legacy_prefix_is_exhausted_before_cursor_handoff(transport):
    google, client = transport
    expected = [f"signal-{index:04d}" for index in range(125)]
    preload(google, "Signale", [signal(identity) for identity in expected])
    first = client.read("Signale", filters={"Quelle": "channel"})
    assert [row["ID"] for row in first["rows"]] == expected[:100]
    assert first["next_offset"] == 100 and first["next_scan_cursor"] is None
    assert first["scan_complete"] is False
    second = client.read("Signale", offset=first["next_offset"], filters={"Quelle": "channel"})
    assert [row["ID"] for row in second["rows"]] == expected[100:120]
    assert second["next_offset"] is None and second["next_scan_cursor"] is not None
    third = client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=second["next_scan_cursor"])
    assert [row["ID"] for row in third["rows"]] == expected[120:]


def test_cursor_limit_does_not_skip_prefetched_matching_rows(transport):
    google, client = transport
    expected = [f"signal-{index:04d}" for index in range(1255)]
    preload(google, "Signale", [signal(identity) for identity in expected])
    assert filtered_ids(google, client, "Signale", {"Quelle": "channel"}) == expected
    cursor = client.read("Signale", offset=120, filters={"Quelle": "channel"})["next_scan_cursor"]
    google.calls.clear()
    page = client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)
    assert [row["ID"] for row in page["rows"]] == expected[120:220]
    assert page["scanned_rows"] == 100
    assert sum(last - first + 1 for _, first, last in body_ranges(google)) == 120
    page = client.read("Signale", limit=23, filters={"Quelle": "channel"}, scan_cursor=page["next_scan_cursor"])
    assert [row["ID"] for row in page["rows"]] == expected[220:243]


def test_filtered_offset_beyond_unscanned_prefix_is_rejected(transport):
    google, client = transport
    preload(google, "Signale", [signal(str(index)) for index in range(125)])
    with pytest.raises(AqlaError, match="start at offset 0.*next_offset.*next_scan_cursor"):
        client.read("Signale", offset=121, filters={"Quelle": "channel"})
    page = client.read("Signale", offset=120, filters={"Quelle": "channel"})
    assert page["rows"] == [] and page["next_offset"] is None
    assert page["next_scan_cursor"] is not None


def test_cursor_final_suffix_never_claims_global_total(transport):
    google, client = transport
    cursor = signal_cursor(google, client)
    page = client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)
    assert [row["ID"] for row in page["rows"]] == [f"signal-{index:04d}" for index in range(120, 125)]
    assert page["total"] is None and page["total_complete"] is False and page["next_offset"] is None
    assert page["complete"] is True and page["scan_complete"] is True and page["scan_truncated"] is False
    assert page["next_scan_cursor"] is None


@pytest.mark.parametrize("matching", [list(range(240, 250)), [0, 119, 120, 239, 240, 249]])
def test_cursor_empty_matching_page_has_continuation(transport, matching):
    google, client = transport
    records = [signal(f"signal-{index:04d}") for index in range(250)]
    source = HEADERS["Signale"].index("Quelle")
    for index, row in enumerate(records):
        row["values"][source] = "match" if index in matching else "other"
        row["fingerprint"] = fingerprint(row["values"])
    preload(google, "Signale", records)
    if matching[0] == 240:
        first = client.read("Signale", filters={"Quelle": "match"})
        assert first["rows"] == [] and first["next_scan_cursor"] is not None
        middle = client.read("Signale", filters={"Quelle": "match"}, scan_cursor=first["next_scan_cursor"])
        assert middle["rows"] == [] and middle["next_scan_cursor"] is not None
        assert middle["scan_complete"] is False and middle["scanned_rows"] == 120
    expected = [f"signal-{index:04d}" for index in matching]
    assert filtered_ids(google, client, "Signale", {"Quelle": "match"}, limit=1) == expected


@pytest.mark.parametrize("change", ["scope", "scope_key", "namespace", "spreadsheet", "table", "filter"])
def test_cursor_rejects_scope_namespace_table_or_filter_change(transport, change):
    google, client = transport
    cursor = signal_cursor(google, client)
    table, filters = "Signale", {"Quelle": "channel"}
    if change == "scope":
        client.scope = "testnet:another-account"
    elif change == "scope_key":
        client.scope_key = "aqla.scope.another"
    elif change == "namespace":
        client.prefix = "aqla Another "
    elif change == "spreadsheet":
        client.settings.spreadsheet_id = "another-memory-spreadsheet"
    elif change == "table":
        table = "Trades"
    else:
        filters = {"Quelle": "other"}
    with pytest.raises(AqlaError, match="^stale_cursor:.*restart at offset 0"):
        client.read(table, filters=filters, scan_cursor=cursor)


@pytest.mark.parametrize("change", ["sort", "insert", "delete", "id", "position", "worksheet", "digest", "revision", "header"])
def test_cursor_rejects_sort_insert_delete_or_digest_revision_change(transport, change):
    google, client = transport
    cursor = signal_cursor(google, client)
    rows = google.tables[PREFIX + "Signale"]
    if change == "sort":
        rows[1], rows[2] = rows[2], rows[1]
    elif change == "insert":
        incoming = signal("inserted")
        rows.insert(1, [*incoming["values"], incoming["fingerprint"], incoming["revision"]])
    elif change == "delete":
        del rows[1]
    elif change == "id":
        rows[1][0] = "replaced-id"
    elif change == "position":
        rows.insert(1, [])
    elif change == "worksheet":
        google.properties[PREFIX + "Signale"]["sheetId"] += 100
    elif change == "digest":
        rows[1][-2] = "different-metadata-digest"
    elif change == "revision":
        rows[1][-1] += 1
    else:
        rows[0][1], rows[0][2] = rows[0][2], rows[0][1]
    with pytest.raises(AqlaError, match="^stale_cursor:.*restart at offset 0"):
        client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)


def test_cursor_filter_mutation_changes_binding(transport):
    google, client = transport
    filters = {"Quelle": "channel"}
    cursor = signal_cursor(google, client, filters=filters)
    filters["Quelle"] = "other"
    with pytest.raises(AqlaError, match="^stale_cursor:"):
        client.read("Signale", filters=filters, scan_cursor=cursor)


def test_cursor_filter_key_order_is_compatible(transport):
    google, client = transport
    preload(google, "Signale", [signal(f"signal-{index:04d}") for index in range(125)])
    filters = {"Quelle": "channel", "ID": "signal-0120"}
    cursor = client.read("Signale", filters=filters)["next_scan_cursor"]
    page = client.read("Signale", filters=dict(reversed(list(filters.items()))), scan_cursor=cursor)
    assert [row["ID"] for row in page["rows"]] == ["signal-0120"]


@pytest.mark.parametrize("change", ["empty", "long", "encoding", "json", "primitive", "extra", "missing", "duplicate",
                                    "version", "bool_version", "negative", "bool_position", "float_position",
                                    "string_position", "end_position", "beyond_position", "digest", "digest_type"])
def test_cursor_rejects_malformed_version_and_position(transport, change):
    google, client = transport
    original = signal_cursor(google, client)
    payload = json.loads(base64.urlsafe_b64decode(original + "=" * (-len(original) % 4)))
    malformed = {"extra": {**payload, "extra": None}, "missing": {key: value for key, value in payload.items() if key != "v"},
                 "version": {**payload, "v": 2}, "bool_version": {**payload, "v": True},
                 "negative": {**payload, "next": -1}, "bool_position": {**payload, "next": True},
                 "float_position": {**payload, "next": 120.0}, "string_position": {**payload, "next": "120"},
                 "end_position": {**payload, "next": 125}, "beyond_position": {**payload, "next": 126},
                 "digest": {**payload, "binding": "not-a-digest"}, "digest_type": {**payload, "index": []},
                 "primitive": []}
    raw = json.dumps(malformed.get(change, payload))
    if change == "duplicate":
        raw = raw[:-1] + ', "v": 1}'
    elif change == "json":
        raw = "{invalid json"
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    token = {"empty": "", "long": "a" * 2049, "encoding": "!not-base64!"}.get(change, token)
    with pytest.raises(AqlaError, match="Invalid journal scan cursor.*restart at offset 0") as caught:
        client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=token)
    if len(token) > 30:
        assert token not in str(caught.value)


@pytest.mark.parametrize("offset,filters", [(1, {"Quelle": "channel"}), (0, {}), (0, None)])
def test_cursor_requires_filtered_zero_offset(transport, offset, filters):
    google, client = transport
    cursor = signal_cursor(google, client)
    with pytest.raises(AqlaError, match="requires offset 0 and nonempty filters"):
        client.read("Signale", offset=offset, filters=filters, scan_cursor=cursor)


@pytest.mark.parametrize("metadata_column,changed_value", [(-2, "changed-digest"), (-1, 2), (0, "changed-id"), (-1, True)])
def test_cursor_rejects_visible_changes_during_body_reads(transport, monkeypatch, metadata_column, changed_value):
    google, client = transport
    cursor = signal_cursor(google, client, count=250)
    original = google.request

    def changing_request(method, url, **kwargs):
        if url.endswith("/values:batchGet") and any(
            Google.coordinates(query)[1] == 1 and Google.coordinates(query)[2] > 1
            and Google.coordinates(query)[3] > 1 for query in kwargs["params"]["ranges"]
        ):
            # A prefetched row beyond the page's one emitted match is visible too.
            google.tables[PREFIX + "Signale"][136][metadata_column] = changed_value
        return original(method, url, **kwargs)

    monkeypatch.setattr(google, "request", changing_request)
    with pytest.raises(AqlaError, match="^stale_cursor:"):
        client.read("Signale", limit=1, filters={"Quelle": "channel"}, scan_cursor=cursor)


def test_cursor_preserves_unverified_row_reporting(transport):
    google, client = transport
    cursor = signal_cursor(google, client)
    google.tables[PREFIX + "Signale"][121][HEADERS["Signale"].index("Text")] = "manual edit without metadata change"
    page = client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)
    assert page["rows"][0]["ID"] == "signal-0120" and page["rows"][0]["_verified"] is False
    assert page["unverified_rows"] == 1 and page["unverified_rows_scope"] == "scan"
    assert page["total"] is None and page["total_complete"] is False


def test_cursor_lost_read_does_not_advance_client_position(transport, monkeypatch):
    google, client = transport
    cursor = signal_cursor(google, client)
    original = google.request
    lost = 0

    def losing_request(method, url, **kwargs):
        nonlocal lost
        response = original(method, url, **kwargs)
        if lost < 3 and url.endswith("/values:batchGet") and any(
            Google.coordinates(query)[1] == 1 and Google.coordinates(query)[2] > 1
            and Google.coordinates(query)[3] > 1 for query in kwargs["params"]["ranges"]
        ):
            lost += 1
            raise OSError("Body read reply lost")
        return response

    monkeypatch.setattr(google, "request", losing_request)
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)
    with pytest.raises(AqlaError, match="request failed"):
        client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)
    retry = client.read("Signale", filters={"Quelle": "channel"}, scan_cursor=cursor)
    assert lost == 3
    assert [row["ID"] for row in retry["rows"]] == [f"signal-{index:04d}" for index in range(120, 125)]
    assert retry["scan_complete"] is True
    assert google.writes == 0


def test_monthly_publisher_transport_tracks_content_extent_not_grid_capacity():
    from test_monthly import data, report_at, sheet_client, values_writes, written_rows

    from aqla_sheets_mcp.monthly import table_values

    google, client = sheet_client()
    report = report_at(data(), 1)
    expected, _ = table_values(report)
    assert client.publish_monthly(report)["status"] == "published"
    assert len(values_writes(google)) == 1 and written_rows(google) == len(expected)
    assert all(call["valueInputOption"] == "RAW" for call in values_writes(google))
    google.properties["aqla Mainnet Monatsbericht 2026-10"]["gridProperties"]["rowCount"] = 1_000_000
    google.calls.clear()
    assert client.publish_monthly(report)["status"] == "published"
    assert values_writes(google) == []
    queried = [Google.coordinates(query) for method, url, kwargs in google.calls
               if method == "GET" and url.endswith("/values:batchGet")
               for query in kwargs["params"]["ranges"] if "Monatsbericht" in query]
    assert max(item[4] for item in queried) == len(expected)


def test_monthly_legacy_grid_beyond_verification_budget_has_no_values_writes():
    from test_monthly import data, report_at, sheet_client, values_writes

    google, client = sheet_client()
    report = report_at(data(), 1)
    assert client.publish_monthly(report)["status"] == "published"
    google.metadata[:] = [item for item in google.metadata if not item["metadataKey"].startswith("aqla.monthly.")]
    google.properties["aqla Mainnet Monatsbericht 2026-10"]["gridProperties"]["rowCount"] = 20_001
    google.calls.clear()
    assert client.publish_monthly(report) == {"status": "blocked", "publication": None, "error_code": "budget"}
    assert values_writes(google) == []


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


def test_existing_migration_headers_are_batched_on_every_sync(transport):
    google, client = transport
    client.sync([rules(1)])
    google.calls.clear()
    assert client.sync([rules(1)])["accepted"]
    reads = [kwargs for method, _, kwargs in google.calls if method == "GET"]
    assert len(reads) == 5
    header_reads = [kwargs["params"]["ranges"] for method, url, kwargs in google.calls
                    if method == "GET" and url.endswith("/values:batchGet")
                    and all(Google.coordinates(query)[2] == Google.coordinates(query)[4] == 1 for query in kwargs["params"]["ranges"])]
    assert len(header_reads) == 2
    assert {Google.coordinates(query)[0] for query in header_reads[0]} == {PREFIX + table for table in HEADERS}
    assert body_ranges(google) == [(PREFIX + "Regeln", 2, 2)]


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
    incoming["values"][HEADERS["Signale"].index("Text")] = "=literal changed text"
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
