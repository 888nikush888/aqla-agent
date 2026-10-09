"""Actual Sheets HTTP request vectors against a stateful, credential-free boundary."""
import copy
import json
import math
from decimal import Decimal
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_monthly import trade
from test_presentation import CLOCK, state_data
from test_sheets import Google

from aqla_agent.presentation import PresentationScope
from aqla_agent.presentation import build_presentation as projection
from aqla_hyperliquid_mcp.auth import Authority
from aqla_hyperliquid_mcp.models import AqlaError, GoogleSheetsSettings, Settings, digest
from aqla_sheets_mcp.client import SheetsClient
from aqla_sheets_mcp.journal import (
    HEADERS,
    PRE_PRESENTATION_HEADERS,
    JournalRecord,
    fingerprint,
    project,
    record,
)
from aqla_sheets_mcp.presentation import TITLES, WORKBOOK_TITLE, chart_specs, publish, selection
from aqla_sheets_mcp.server import create_server


def build_presentation(source, scope=None, clock=None):
    return projection(source, scope or {"currency": "USDT"}, clock=clock)


class PresentationGoogle(Google):
    def __init__(self):
        super().__init__()
        self.title = "Original workbook"
        self.charts, self.formats, self.filters, self.validations = {}, {}, {}, {}
        self.lose_suffix = None
        self.batch_writes = 0
        self.post_count = 0
        self.lose_post = None
        self.normalize_charts = False

    def _native_chart(self, chart):
        chart = copy.deepcopy(chart)
        if not self.normalize_charts:
            return chart
        spec, basic = chart["spec"], chart["spec"]["basicChart"]
        spec.pop("backgroundColor", None)
        spec.pop("backgroundColorStyle", None)  # Native Sheets omits default white.
        series = basic["series"][0]
        region = series["series"]["sourceRange"]["sources"][0]
        rows = self.tables[self._props(region["sheetId"])["title"]]
        values = [row[region["startColumnIndex"]] for row in rows[1:region["endRowIndex"]] if len(row) > region["startColumnIndex"]]
        numeric = any(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
        domain = basic["domains"][0]["domain"]["sourceRange"]["sources"][0]
        labels = any(len(row) > domain["startColumnIndex"] and row[domain["startColumnIndex"]] != ""
                     for row in rows[1:domain["endRowIndex"]])
        axes = {axis["position"]: copy.deepcopy(axis) for axis in basic["axis"]}
        for position in ("BOTTOM_AXIS", "LEFT_AXIS"):
            axes.setdefault(position, {"position": position})
            axes[position]["viewWindowOptions"] = {}
            if not numeric:
                axes[position].pop("title", None)
        basic["axis"] = [axes[position] for position in ("BOTTOM_AXIS", "LEFT_AXIS")]
        if not numeric:
            series.pop("color", None)
            series.pop("colorStyle", None)
            if basic["chartType"] == "BAR" and labels:
                basic.pop("series")
                basic["axis"] = [axes["LEFT_AXIS"]]
        else:
            rgb = series.get("colorStyle", {}).get("rgbColor", series.get("color"))
            if rgb:
                quantized = {key: math.floor(value * 255) / 255 for key, value in rgb.items()}
                series["color"] = quantized
                series["colorStyle"] = {"rgbColor": copy.deepcopy(quantized)}
        return chart

    def request(self, method, url, **kwargs):
        if method == "GET" and not url.endswith("/values:batchGet"):
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            data = {"properties": {"title": self.title}, "sheets": [{"properties": copy.deepcopy(props),
                    "charts": copy.deepcopy(self.charts.get(props["sheetId"], [])),
                    "basicFilter": copy.deepcopy(self.filters.get(props["sheetId"], {})),
                    "data": [{"startRow": 3, "startColumn": 1, "rowData": [{"values": [
                        {"dataValidation": copy.deepcopy(self.validations.get((props["sheetId"], col), {}))}
                        for col in range(1, 11)]}]}],
                    "conditionalFormats": copy.deepcopy(self.formats.get(props["sheetId"], []))} for props in self.properties.values()],
                    "developerMetadata": copy.deepcopy(self.metadata)}
        elif method == "POST" and url.endswith(":batchUpdate") and not url.endswith("/values:batchUpdate"):
            self.calls.append((method, url, copy.deepcopy(kwargs)))
            for operation in kwargs["json"]["requests"]:
                if "createDeveloperMetadata" in operation:
                    row = copy.deepcopy(operation["createDeveloperMetadata"]["developerMetadata"])
                    row["metadataId"] = max([row.get("metadataId", 0) for row in self.metadata] + [0]) + 1
                    self.metadata.append(row)
                elif "updateDeveloperMetadata" in operation:
                    update = operation["updateDeveloperMetadata"]
                    identity = update["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                    next(row for row in self.metadata if row["metadataId"] == identity).update(update["developerMetadata"])
                elif "addSheet" in operation:
                    prop = copy.deepcopy(operation["addSheet"]["properties"])
                    assert prop["title"] not in self.properties
                    assert prop["sheetId"] not in {row["sheetId"] for row in self.properties.values()}
                    prop.setdefault("hidden", False)
                    prop.setdefault("index", len(self.properties))
                    self.properties[prop["title"]] = prop
                    self.tables[prop["title"]] = []
                    self._index(prop, prop["index"])
                elif "updateSheetProperties" in operation:
                    update = copy.deepcopy(operation["updateSheetProperties"]["properties"])
                    identity = update.pop("sheetId")
                    prop = next(row for row in self.properties.values() if row["sheetId"] == identity)
                    if "title" in update and update["title"] != prop["title"]:
                        old, new = prop["title"], update["title"]
                        self.properties[new] = self.properties.pop(old)
                        self.tables[new] = self.tables.pop(old)
                    if "index" in update:
                        self._index(prop, update.pop("index"))
                    if "gridProperties" in update:
                        prop["gridProperties"].update(update.pop("gridProperties"))
                    prop.update(update)
                elif "updateSpreadsheetProperties" in operation:
                    self.title = operation["updateSpreadsheetProperties"]["properties"]["title"]
                elif "addChart" in operation:
                    chart = self._native_chart(operation["addChart"]["chart"])
                    assert not any(chart["chartId"] == row["chartId"] for rows in self.charts.values() for row in rows)
                    identity = chart["position"]["overlayPosition"]["anchorCell"]["sheetId"]
                    assert self._props(identity)["gridProperties"]["columnCount"] >= 34
                    self.charts.setdefault(identity, []).append(chart)
                elif "updateChartSpec" in operation:
                    update = operation["updateChartSpec"]
                    previous = next(row for rows in self.charts.values() for row in rows if row["chartId"] == update["chartId"])
                    previous["spec"] = self._native_chart({**previous, "spec": update["spec"]})["spec"]
                elif "updateEmbeddedObjectPosition" in operation:
                    update = operation["updateEmbeddedObjectPosition"]
                    next(row for rows in self.charts.values() for row in rows if row["chartId"] == update["objectId"])["position"] = copy.deepcopy(update["newPosition"])
                elif "deleteConditionalFormatRule" in operation:
                    update = operation["deleteConditionalFormatRule"]
                    self.formats[update["sheetId"]].pop(update["index"])
                elif "addConditionalFormatRule" in operation:
                    update = operation["addConditionalFormatRule"]
                    identity = update["rule"]["ranges"][0]["sheetId"]
                    self.formats.setdefault(identity, []).insert(update["index"], copy.deepcopy(update["rule"]))
                elif "insertDimension" in operation:
                    dimensions = operation["insertDimension"]["range"]
                    props = self._props(dimensions["sheetId"])
                    extra = dimensions["endIndex"] - dimensions["startIndex"]
                    for row in self.tables[props["title"]]:
                        row[dimensions["startIndex"]:dimensions["startIndex"]] = [""] * extra
                    props["gridProperties"]["columnCount"] += extra
                elif "updateCells" in operation:
                    update = operation["updateCells"]
                    title = self._props(update["start"]["sheetId"])["title"]
                    if not self.tables[title]:
                        self.tables[title].append([])
                    self.tables[title][0] = [cell["userEnteredValue"]["stringValue"] for cell in update["rows"][0]["values"]]
                elif "setBasicFilter" in operation:
                    content = operation["setBasicFilter"]["filter"]
                    self.filters[content["range"]["sheetId"]] = copy.deepcopy(content)
                elif "setDataValidation" in operation:
                    content = operation["setDataValidation"]
                    self.validations[content["range"]["sheetId"], content["range"]["startColumnIndex"]] = copy.deepcopy(content["rule"])
                else:
                    assert set(operation) <= {"repeatCell", "setBasicFilter", "updateDimensionProperties", "setDataValidation", "mergeCells", "unmergeCells"}
            self.batch_writes += 1
            data = {}
        else:
            if method == "GET":
                for query in kwargs["params"]["ranges"]:
                    title, _, _, end_col, end_row = self.coordinates(query)
                    prop = self.properties[title]
                    assert end_col <= prop["gridProperties"]["columnCount"]
                    assert end_row <= prop["gridProperties"]["rowCount"]
            try:
                response = super().request(method, url, **kwargs)
            except Exception as error:
                raise AqlaError(f"Fake boundary: {type(error).__name__}: {error}") from error
            data = response.json()
        if method == "POST":
            self.post_count += 1
        if method == "POST" and (self.lose_post == self.post_count or self.lose_suffix and url.endswith(self.lose_suffix)):
            self.lose_suffix = None
            self.lose_post = None
            raise OSError("Accepted write; reply lost")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    def _props(self, identity):
        return next(row for row in self.properties.values() if row["sheetId"] == identity)

    def _index(self, prop, index):
        ordered = sorted((row for row in self.properties.values() if row is not prop), key=lambda row: row["index"])
        ordered.insert(index, prop)
        for position, row in enumerate(ordered):
            row["index"] = position


@pytest.fixture
def workbook():
    google = PresentationGoogle()
    for identity, title in [(0, "Tabellenblatt1"), *[(index, "aqla Testnet " + name) for index, name in enumerate(list(HEADERS)[:9], 1)],
                            *[(index, "aqla Mainnet " + name) for index, name in enumerate(HEADERS, 10)]]:
        width = 26 if identity == 0 else len(HEADERS[title.split(" ", 2)[2]]) + 2
        google.properties[title] = {"sheetId": identity, "title": title, "index": identity, "hidden": False,
                                    "gridProperties": {"rowCount": 1000, "columnCount": width}}
        google.tables[title] = [] if identity == 0 else [[*HEADERS[title.split(" ", 2)[2]], "_digest", "_revision"],
                                                       ["stable-raw-id", "preserve-body", *[""] * (width - 4), "raw-digest", 7]]
    google.metadata = [{"metadataId": 1, "metadataKey": "aqla.scope.mainnet", "metadataValue": "testnet:ci-primary"}]
    client = SheetsClient(SimpleNamespace(spreadsheet_id="memory-dashboard", tab_namespace="mainnet"), session=google, scope="testnet:ci-primary")
    source = state_data(trade("aster", currency="USDT"))
    source["trades"]["aster"]["plan"]["exchange"] = "aster"
    return google, client, source


def writes(google):
    return [(url, kwargs) for method, url, kwargs in google.calls if method == "POST"]


def raw_content(google):
    return {title: (props["sheetId"], copy.deepcopy(google.tables[title])) for title, props in google.properties.items() if title.startswith("aqla ")}


def test_real_http_migration_preserves_21_tab_ids_bodies_metadata_and_noops(workbook):
    google, client, source = workbook
    before, metadata = raw_content(google), copy.deepcopy(google.metadata)
    snapshot = build_presentation(source, clock=CLOCK)
    assert publish(client, snapshot, clock=0)["status"] == "published"
    assert google.properties["Dashboard"]["sheetId"] == 0 and google.title == WORKBOOK_TITLE
    assert [row["title"] for row in sorted(google.properties.values(), key=lambda row: row["index"])][:5] == list(TITLES)
    assert all(row["hidden"] for title, row in google.properties.items() if title.startswith("aqla "))
    assert raw_content(google) == before and google.metadata[:1] == metadata
    assert len(google.charts[0]) == 4
    assert [row["spec"]["basicChart"]["chartType"] for row in google.charts[0]] == ["LINE", "COLUMN", "BAR", "BAR"]
    google.calls.clear()
    assert publish(client, snapshot, clock=60)["status"] == "unchanged"
    assert writes(google) == []
    google.calls.clear()
    assert publish(client, snapshot, clock=61)["status"] == "throttled" and google.calls == []
    assert SheetsClient(client.settings, session=google, scope=client.scope).prefix == "aqla Mainnet "


def test_google_omitted_zero_defaults_and_float32_colors_do_not_cause_rewrites(workbook):
    google, client, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    publish(client, snapshot, clock=0)
    chart = google.charts[0][0]
    chart["position"]["overlayPosition"].pop("offsetXPixels")
    chart["position"]["overlayPosition"]["anchorCell"].pop("columnIndex")
    chart["spec"]["basicChart"]["series"][0]["colorStyle"]["rgbColor"]["red"] = .119999997
    chart["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0].pop("startRowIndex")
    google.calls.clear()
    assert publish(client, snapshot, clock=60)["status"] == "unchanged" and writes(google) == []


def test_native_styles_default_axes_and_8bit_colors_publish_after_verified_data_without_rewrites(workbook):
    google, client, source = workbook
    google.normalize_charts = True
    snapshot = build_presentation(source, clock=CLOCK)
    assert publish(client, snapshot, clock=0)["status"] == "published"
    for index, chart in enumerate(google.charts[0]):
        spec, basic = chart["spec"], chart["spec"]["basicChart"]
        assert "backgroundColorStyle" not in spec and "backgroundColor" not in spec
        assert {axis["position"] for axis in basic["axis"]} == {"LEFT_AXIS", "BOTTOM_AXIS"}
        rgb = basic["series"][0]["colorStyle"]["rgbColor"]
        assert rgb == {"red": 30 / 255, "green": 91 / 255, "blue": 173 / 255}
        assert any(axis.get("title") == ("R" if index == 3 else "USDT") for axis in basic["axis"])
    posts = writes(google)
    values_index = max(index for index, (url, _) in enumerate(posts) if url.endswith("/values:batchUpdate"))
    charts_index = next(index for index, (_, kwargs) in enumerate(posts)
                        if any("addChart" in request for request in kwargs["json"].get("requests", [])))
    assert charts_index > values_index
    assert all("backgroundColorStyle" in request["addChart"]["chart"]["spec"]
               for _, kwargs in posts for request in kwargs["json"].get("requests", []) if "addChart" in request)
    journal_id = google.properties["Journal"]["sheetId"]
    dimensions = [request["updateDimensionProperties"] for _, kwargs in posts
                  for request in kwargs["json"].get("requests", []) if "updateDimensionProperties" in request]
    assert any(row["range"] == {"sheetId": journal_id, "dimension": "COLUMNS", "startIndex": 19, "endIndex": 22}
               and row["properties"] == {"hiddenByUser": True} for row in dimensions)
    assert any(row["range"] == {"sheetId": journal_id, "dimension": "ROWS", "startIndex": 1, "endIndex": 2}
               and row["properties"] == {"pixelSize": 48} for row in dimensions)
    assert google.tables["Journal"][1][20] == "aster"  # Hidden linked identity is retained.
    google.calls.clear()
    assert publish(client, snapshot, clock=60)["status"] == "unchanged" and writes(google) == []


def test_native_empty_bar_omits_series_and_numeric_axis_only_without_net_r_evidence(workbook):
    google, client, source = workbook
    google.normalize_charts = True
    source["trades"]["aster"]["execution_incident"] = True
    source["source_ranks"] = {identity: {"tier": "C", "origin": "manual"}
                              for identity in ["-1001", *[f"synthetic-{index}" for index in range(13)]]}
    snapshot = build_presentation(source, clock=CLOCK)
    assert len(snapshot.channels) == 14 and all(row.net_r is None for row in snapshot.channels)
    assert snapshot.net_pnl.value == Decimal("15.1") and snapshot.funding.value == 0
    assert publish(client, snapshot, clock=0)["status"] == "published"
    chart = google.charts[0][3]
    assert "series" not in chart["spec"]["basicChart"]
    assert chart["spec"]["basicChart"]["axis"] == [{"position": "LEFT_AXIS", "viewWindowOptions": {}}]
    assert chart["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0]["startColumnIndex"] == 32
    assert "R je Setup" in chart["spec"]["title"] and chart["spec"]["hiddenDimensionStrategy"] == "SHOW_ALL"
    assert all(google.tables["Dashboard"][index][33] == "" for index in range(1, 15))
    google.calls.clear()
    assert publish(client, snapshot, clock=60)["status"] == "unchanged" and writes(google) == []
    # A missing default axis or a moved domain is damage, rather than canonical empty-series output.
    chart["spec"]["basicChart"]["axis"] = []
    assert publish(client, snapshot, clock=120)["status"] == "published"
    chart["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0]["startColumnIndex"] = 30
    assert publish(client, snapshot, clock=180)["status"] == "published"


def test_known_zero_net_r_requires_series_and_unit_axis_and_can_repair_missing_series(workbook):
    google, client, source = workbook
    google.normalize_charts = True
    trade = source["trades"]["aster"]
    trade["funding_events"] = {"synthetic-funding": {"amount": "-15.1"}}
    trade["funding_coverage"]["payments_fingerprint"] = digest(trade["funding_events"])
    snapshot = build_presentation(source, clock=CLOCK)
    assert snapshot.net_pnl.value == 0 and snapshot.channels[0].net_r == 0
    publish(client, snapshot, clock=0)
    assert google.tables["Dashboard"][1][33] == 0
    channel = google.charts[0][3]
    channel["spec"]["basicChart"].pop("series")
    channel["spec"]["basicChart"]["axis"] = [{"position": "LEFT_AXIS"}]
    assert publish(client, snapshot, clock=60)["status"] == "published"
    assert channel["spec"]["basicChart"]["series"][0]["targetAxis"] == "BOTTOM_AXIS"
    assert next(row for row in channel["spec"]["basicChart"]["axis"] if row["position"] == "BOTTOM_AXIS")["title"] == "R"


def test_charts_created_before_cells_are_reapplied_after_data_and_confirmed(workbook):
    google, client, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    publish(client, snapshot, clock=0)
    saved = google.tables["Dashboard"]
    google.tables["Dashboard"] = []
    google.normalize_charts = True
    google.charts[0] = [google._native_chart(chart) for chart in google.charts[0]]
    google.tables["Dashboard"] = saved
    assert all("title" not in axis for chart in google.charts[0] for axis in chart["spec"]["basicChart"]["axis"])
    metadata = next(row for row in google.metadata if row["metadataKey"] == "aqla.presentation.v1")
    pending = json.loads(metadata["metadataValue"])
    pending["confirmed"] = False
    metadata["metadataValue"] = json.dumps(pending)
    before = raw_content(google)
    google.calls.clear()
    assert publish(client, snapshot, clock=60)["status"] == "published"
    assert json.loads(metadata["metadataValue"])["confirmed"] is True
    assert raw_content(google) == before and len(google.charts[0]) == 4
    assert not any(url.endswith("/values:batchUpdate") for url, _ in writes(google))
    updates = [request["updateChartSpec"] for _, kwargs in writes(google)
               for request in kwargs["json"].get("requests", []) if "updateChartSpec" in request]
    assert {row["chartId"] for row in updates} == {104800, 104801, 104802, 104803}
    assert all(any(axis.get("title") == ("R" if index == 3 else "USDT")
                   for axis in chart["spec"]["basicChart"]["axis"]) for index, chart in enumerate(google.charts[0]))
    google.calls.clear()
    assert publish(client, snapshot, clock=120)["status"] == "unchanged" and writes(google) == []


@pytest.mark.parametrize("damage", ["series", "unit", "source", "color", "hidden", "title", "placement"])
def test_normalized_nonempty_native_charts_still_repair_financial_semantic_damage(workbook, damage):
    google, client, source = workbook
    google.normalize_charts = True
    snapshot = build_presentation(source, clock=CLOCK)
    before = raw_content(google)
    publish(client, snapshot, clock=0)
    chart = google.charts[0][0]
    basic = chart["spec"]["basicChart"]
    if damage == "series":
        basic.pop("series")
    elif damage == "unit":
        next(axis for axis in basic["axis"] if axis["position"] == "LEFT_AXIS")["title"] = "USDT+USDC"
    elif damage == "source":
        basic["series"][0]["series"]["sourceRange"]["sources"][0]["startColumnIndex"] = 29
    elif damage == "color":
        basic["series"][0]["colorStyle"]["rgbColor"]["red"] = .9
    elif damage == "hidden":
        chart["spec"]["hiddenDimensionStrategy"] = "SKIP_HIDDEN_COLUMNS"
    elif damage == "title":
        chart["spec"]["title"] = "Kontoequity"
    else:
        chart["position"]["overlayPosition"]["anchorCell"]["rowIndex"] = 24
    assert publish(client, snapshot, clock=60)["status"] == "published"
    assert raw_content(google) == before
    google.calls.clear()
    assert publish(client, snapshot, clock=120)["status"] == "unchanged" and writes(google) == []


@pytest.mark.parametrize("lost", [":batchUpdate", "/values:batchUpdate"])
def test_lost_reply_restart_reuses_sheets_charts_and_raw_rows(workbook, lost):
    google, client, source = workbook
    before = raw_content(google)
    snapshot = build_presentation(source, clock=CLOCK)
    google.lose_suffix = lost
    with pytest.raises(AqlaError, match="remain pending"):
        publish(client, snapshot, clock=0)
    restarted = SheetsClient(client.settings, session=google, scope=client.scope)
    assert publish(restarted, snapshot, clock=60)["status"] == "published"
    assert len(google.charts[0]) == 4 and len(google.properties) == 25
    assert raw_content(google) == before
    google.calls.clear()
    assert publish(restarted, snapshot, clock=120)["status"] == "unchanged" and writes(google) == []


@pytest.mark.parametrize("lost_post", range(1, 12))
def test_each_publication_stage_recovers_after_accepted_write_and_lost_response(workbook, lost_post):
    google, client, source = workbook
    before = raw_content(google)
    snapshot = build_presentation(source, clock=CLOCK)
    google.lose_post = lost_post
    with pytest.raises(AqlaError, match="remain pending"):
        publish(client, snapshot, clock=0)
    restarted = SheetsClient(client.settings, session=google, scope=client.scope)
    assert publish(restarted, snapshot, clock=1)["status"] == "throttled"
    assert publish(restarted, snapshot, clock=61)["status"] in ("published", "unchanged")
    assert len(google.charts[0]) == 4 and raw_content(google) == before
    google.calls.clear()
    assert publish(restarted, snapshot, clock=121)["status"] == "unchanged" and writes(google) == []


def test_structure_partial_recreates_only_a_missing_owned_sheet_and_hides_new_backend(workbook):
    google, client, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    publish(client, snapshot, clock=0)
    journal_id = google.properties.pop("Journal")["sheetId"]
    google.tables.pop("Journal")
    google.properties["aqla Aster Mainnet Fills"] = {"sheetId": 777, "title": "aqla Aster Mainnet Fills", "index": 25,
                                                    "hidden": False, "gridProperties": {"rowCount": 1000, "columnCount": len(HEADERS["Fills"]) + 2}}
    google.tables["aqla Aster Mainnet Fills"] = [["existing native fill"]]
    before = raw_content(google)
    publish(client, snapshot, clock=60)
    assert google.properties["Journal"]["sheetId"] == journal_id and len(google.charts[0]) == 4
    assert google.properties["aqla Aster Mainnet Fills"]["hidden"] is True and raw_content(google) == before


def test_selection_is_read_only_validated_and_raw_formula_source_is_literal(workbook):
    google, client, source = workbook
    source["source_names"]["-1001"] = '=HYPERLINK("https://invalid.example")'
    snapshot = build_presentation(source, clock=CLOCK)
    publish(client, snapshot, clock=0)
    assert google.tables["Journal"][1][0].startswith("=HYPERLINK")
    assert all(kwargs["json"].get("valueInputOption") == "RAW" for url, kwargs in writes(google) if url.endswith("/values:batchUpdate"))
    google.calls.clear()
    selected = selection(client)
    assert selected == {"selection": snapshot.scope.model_dump(mode="json"), "valid": True}
    assert writes(google) == []
    google.tables["Dashboard"][3][4] = "testnet"
    assert selection(client)["selection"]["network"] == "testnet"
    before = copy.deepcopy(source)
    google.tables["Dashboard"][3][7] = "USDT+USDC"
    assert selection(client)["valid"] is False and source == before


def test_fresh_selector_uses_primary_usdc_and_only_reads(workbook):
    google, client, _ = workbook
    assert selection(client) == {"selection": PresentationScope().model_dump(mode="json"), "valid": True}
    assert selection(client)["selection"] == {"exchange": "all", "network": "mainnet", "currency": "USDC", "period": "current"}
    assert writes(google) == []


def test_partial_content_repair_and_shrinking_tail_only_touch_owned_ranges(workbook):
    google, client, source = workbook
    before = raw_content(google)
    first = build_presentation(source, clock=CLOCK)
    publish(client, first, clock=0)
    google.tables["Journal"][1][9] = 999
    google.charts[0][0]["position"]["overlayPosition"]["anchorCell"]["rowIndex"] = 24
    assert publish(client, first, clock=60)["status"] == "published"
    assert google.tables["Journal"][1][9] == 15.1 and len(google.charts[0]) == 4
    source["trades"].clear()
    source["sequence"] += 1
    second = build_presentation(source, clock=CLOCK)
    google.calls.clear()
    publish(client, second, clock=120)
    assert all(value == "" for value in google.tables["Journal"][1])
    assert raw_content(google) == before
    assert all(Google.coordinates(update["range"])[0] in TITLES for url, kwargs in writes(google) if url.endswith("/values:batchUpdate") for update in kwargs["json"]["data"])
    assert not any("deleteSheet" in op or "deleteEmbeddedObject" in op for _, kwargs in writes(google) for op in kwargs["json"].get("requests", []))


def test_filters_selectors_title_and_frozen_headers_repair_with_identical_fingerprint(workbook):
    google, client, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    publish(client, snapshot, clock=0)
    journal_id = google.properties["Journal"]["sheetId"]
    google.filters.pop(journal_id)
    google.validations.pop((0, 7))
    google.title = "Old journal name"
    google.properties["Kanäle"]["gridProperties"]["frozenRowCount"] = 0
    before = raw_content(google)
    assert publish(client, snapshot, clock=60)["status"] == "published"
    assert google.title == WORKBOOK_TITLE
    assert google.filters[journal_id]["range"]["endColumnIndex"] == 22
    assert google.validations[0, 7]["strict"] is True
    assert google.properties["Kanäle"]["gridProperties"]["frozenRowCount"] == 1
    assert raw_content(google) == before
    google.calls.clear()
    assert publish(client, snapshot, clock=120)["status"] == "unchanged"
    assert writes(google) == []


def test_later_chart_write_lost_reply_persists_minute_gate_before_mutation(workbook):
    google, client, source = workbook
    first = build_presentation(source, clock=CLOCK)
    publish(client, first, clock=0)
    second = build_presentation(source, {"currency": "USDT", "period": "all"}, CLOCK)
    google.lose_post = google.post_count + 3  # Durable pending metadata, dashboard cells, then native chart edit.
    with pytest.raises(AqlaError, match="remain pending"):
        publish(client, second, clock=60)
    assert google.charts[0][1]["spec"]["title"] == "Monatsergebnisse · USDT"
    restarted = SheetsClient(client.settings, session=google, scope=client.scope)
    google.calls.clear()
    assert publish(restarted, second, clock=61)["status"] == "throttled"
    assert writes(google) == []
    assert publish(restarted, second, clock=121)["status"] == "published"
    assert len(google.charts[0]) == 4


def test_default_publication_clock_is_restart_stable_wall_time(workbook, monkeypatch):
    import aqla_sheets_mcp.presentation as publisher

    google, client, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    monkeypatch.setattr(publisher.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(publisher.time, "monotonic", lambda: pytest.fail("Persisted monotonic clocks reset at reboot"))
    publish(client, snapshot)
    restarted = SheetsClient(client.settings, session=google, scope=client.scope)
    monkeypatch.setattr(publisher.time, "time", lambda: 1_800_000_001)
    google.calls.clear()
    assert publish(restarted, snapshot)["status"] == "throttled" and writes(google) == []


def test_foreign_scope_stale_revision_nonempty_adoption_and_injected_snapshot_rejected(workbook):
    google, client, source = workbook
    google.tables["Tabellenblatt1"] = [["human-owned data"]]
    snapshot = build_presentation(source, clock=CLOCK)
    with pytest.raises(AqlaError, match="nonempty"):
        publish(client, snapshot, clock=0)
    assert writes(google) == []
    google.tables["Tabellenblatt1"] = []
    publish(client, snapshot, clock=60)
    modified = snapshot.model_dump(mode="json")
    modified["net_pnl"]["value"] = "10000"
    with pytest.raises(ValueError, match="fingerprint"):
        publish(client, modified, clock=120)
    source["sequence"] -= 1
    source["trades"].clear()
    with pytest.raises(AqlaError, match="Stale"):
        publish(client, build_presentation(source, clock=CLOCK), clock=120)
    google.metadata[0]["metadataValue"] = "mainnet:foreign"
    with pytest.raises(AqlaError, match="different aqla"):
        selection(client)


def test_legacy_header_upgrade_keeps_ids_old_digest_and_revision_until_verified_refresh():
    google = PresentationGoogle()
    client = SheetsClient(SimpleNamespace(spreadsheet_id="legacy", tab_namespace="mainnet"), session=google)
    table, title = "Fills", "aqla Mainnet Fills"
    headers = PRE_PRESENTATION_HEADERS[table]
    original = ["fill-id", "trade-id", *[""] * (len(headers) - 2)]
    old_digest = fingerprint(original)
    google.properties[title] = {"sheetId": 12, "title": title, "index": 0, "gridProperties": {"rowCount": 1000, "columnCount": len(headers) + 2}}
    google.tables[title] = [[*headers, "_digest", "_revision"], [*original, old_digest, 7]]
    client.allowed_tables = {table}
    client.layout()
    assert google.tables[title][1] == [*original, "", old_digest, 7]
    google.calls.clear()
    client.layout()
    assert writes(google) == []
    row = {**record(table, *original), "revision": 7}
    row["values"][-1] = "maker"
    row["fingerprint"] = fingerprint(row["values"])
    assert client.sync([row])["accepted"] == {"Fills:fill-id": row["fingerprint"]}
    assert google.properties[title]["sheetId"] == 12
    assert google.tables[title][1][-3:] == ["maker", row["fingerprint"], 7]


async def test_raw_journal_execution_columns_use_exact_side_evidence_without_changing_fills(setup, signal):
    _, state, _, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    live = await trading.execute(plan["plan_id"])
    data = state.snapshot()
    trade = data["trades"][live["trade_id"]]
    fills = copy.deepcopy(trade["fill_events"])
    identity = next(iter(fills))
    trade["plan"].update(entry_execution="maker_only", take_profit_execution="maker_only")
    trade["fill_execution_roles"] = {identity: "maker", "not-an-existing-fill-id": "taker"}
    rows = project(data)
    fill = rows["Fills:" + identity]
    projected_trade = rows["Trades:" + live["trade_id"]]
    assert fill["values"][HEADERS["Fills"].index("Ausführungsrolle")] == "maker"
    assert projected_trade["values"][-2:] == ["maker_only", "maker_only"]
    assert len(projected_trade["values"]) == len(HEADERS["Trades"])
    assert rows.get("Fills:not-an-existing-fill-id") is None and trade["fill_events"] == fills
    JournalRecord.model_validate(fill)
    JournalRecord.model_validate(projected_trade)


async def test_native_tool_schema_and_internal_authentication(tmp_path, workbook):
    google, client, _ = workbook
    key = tmp_path / "integration.secret"
    key.write_text("test-internal-token-" + "x" * 50, encoding="utf-8")
    authority = Authority.from_file(key)
    account = "0x" + "1" * 40
    settings = Settings(network="testnet", account=account, integration_key_file=key,
                        google_sheets=GoogleSheetsSettings(enabled=True, spreadsheet_id="memory-" + "0" * 25, credentials_file=tmp_path / "not-read.secret"))
    server = create_server(settings, client=client)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["sync_presentation_internal"].input_schema["properties"]["snapshot"]["$ref"].endswith("PresentationSnapshot")
    assert tools["presentation_selection_internal"].annotations.read_only_hint is True
    for tool in ("sync_presentation_internal", "presentation_selection_internal"):
        args = {"token": "invalid"}
        if tool.startswith("sync"):
            source = state_data()
            source.update(account=account, trading_account=account)
            args["snapshot"] = build_presentation(source, clock=CLOCK).model_dump(mode="json")
        with pytest.raises(ToolError, match="Internal integration access denied"):
            await server.call_tool(tool, args)
    assert writes(google) == []
    result = await server.call_tool("presentation_selection_internal", {"token": authority.key.decode()})
    assert json.loads(result.content[0].text)["valid"] is True


def test_chart_units_ranges_and_positions_are_distinct(workbook):
    _, _, source = workbook
    snapshot = build_presentation(source, clock=CLOCK)
    charts = chart_specs(snapshot, 0)
    positions = [chart["position"]["overlayPosition"] for chart in charts]
    assert len({(row["anchorCell"]["rowIndex"], row["anchorCell"]["columnIndex"]) for row in positions}) == 4
    assert positions[0]["widthPixels"] < 6 * 90 and positions[0]["heightPixels"] < 16 * 22
    assert charts[0]["spec"]["basicChart"]["axis"][0]["title"] == "USDT"
    assert charts[3]["spec"]["basicChart"]["axis"][0]["title"] == "R"
    assert "Kontoequity" not in charts[0]["spec"]["title"]
    assert all(chart["spec"]["hiddenDimensionStrategy"] == "SHOW_ALL" for chart in charts)
