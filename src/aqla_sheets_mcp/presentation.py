"""Recoverable, bounded publication of derived views; raw journal tabs stay intact."""
from __future__ import annotations

import math
import time
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_agent.monthly import BERLIN
from aqla_agent.presentation import PresentationScope, PresentationSnapshot
from aqla_hyperliquid_mcp.models import AqlaError, Model, digest

from .client import column
from .journal import HEADERS, fit_cell

TITLES = ("Dashboard", "Journal", "Kanäle", "Parameter", "Monatsberichte")
META_KEY = "aqla.presentation.v1"
WORKBOOK_TITLE = "aqla Trade Journal"
MAX_ROWS = 10_000
BLUE = {"red": .12, "green": .36, "blue": .68}
DARK = {"red": .12, "green": .17, "blue": .24}
LIGHT = {"red": .96, "green": .97, "blue": .99}
GREEN = {"red": .12, "green": .48, "blue": .32}
RED = {"red": .74, "green": .18, "blue": .2}
JOURNAL_HEADERS = ["Quelle", "Symbol", "Side", "Status", "Entry (Berlin)", "Exit (Berlin)", "Entry", "Exit", "Menge",
                   "Netto", "Gebühren", "Funding", "Währung", "Börse", "Netz", "Entry-Modus", "TP-Modus", "Fillrolle",
                   "Belege", "Hinweise", "Trade-ID", "Quellen-ID"]
CHANNEL_HEADERS = ["Kanal", "Rang", "Herkunft", "Score", "Nachweise", "Drawdown R", "Letzte Änderung (Berlin)",
                   "Netto-R", "Setups im Filter", "Ausgeschlossen", "Quellen-ID"]


class PresentationManifest(Model):
    version: Literal[1] = 1
    binding: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = ""
    revision: int = Field(strict=True, ge=0)
    confirmed: bool = False
    attempted_at: float = Field(ge=0, allow_inf_nan=False)
    sheet_ids: dict[str, int]
    extents: dict[str, int] = Field(default_factory=dict)
    options: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def owned_sheets(self):
        if set(self.sheet_ids) != set(TITLES) or len(set(self.sheet_ids.values())) != len(TITLES) or any(type(identity) is not int or identity < 0 for identity in self.sheet_ids.values()):
            raise ValueError("Invalid presentation worksheet ownership")
        if any(title not in TITLES or type(height) is not int or not 0 <= height <= MAX_ROWS for title, height in self.extents.items()):
            raise ValueError("Invalid presentation extents")
        return self


def _stamp(value):
    return datetime.fromtimestamp(value / 1000, BERLIN).strftime("%d.%m.%Y %H:%M") if value is not None else ""


def _number(value):
    if isinstance(value, Decimal):
        value = float(value)
        if not math.isfinite(value):
            raise AqlaError("Presentation amount exceeds the numeric worksheet range")
    return fit_cell(value) if value is not None else ""


def _rect(rows, width):
    return [[*_number_cells(row), *[""] * (width - len(row))] for row in rows]


def _number_cells(row):
    return [_number(value) for value in row]


def tables(snapshot: PresentationSnapshot, spreadsheet_id: str, sheet_ids: dict[str, int]) -> dict[str, list[list]]:
    """RAW vectors, including numeric chart cells. No formula comes from source data."""
    scope, currency = snapshot.scope, snapshot.scope.currency
    dashboard = [[""] * 34 for _ in range(max(42, len(snapshot.cumulative_realized_net) + 1,
                                               len(snapshot.monthly_pnl if scope.period == "all" else snapshot.daily_pnl) + 1,
                                               len(snapshot.channels) + 1))]
    dashboard[0][0] = WORKBOOK_TITLE
    dashboard[1][0] = f"{scope.exchange} · {scope.network} · {currency} · {snapshot.resolved_period} · Europe/Berlin"
    dashboard[2][0] = "Auswahl ändert ausschließlich diese Auswertung. Parameter sind eine lesende Ansicht."
    for index, label, value in ((0, "Börse", scope.exchange), (3, "Netz", scope.network),
                                (6, "Währung", currency), (9, "Zeitraum", scope.period)):
        dashboard[3][index:index + 2] = [label, value]
    metrics = (("Realisierter Netto-PnL", snapshot.net_pnl), ("Gebühren", snapshot.fees), ("Funding", snapshot.funding))
    for index, (label, metric) in enumerate(metrics):
        dashboard[5][index * 2] = label
        dashboard[6][index * 2] = _number(metric.value)
        dashboard[7][index * 2] = f"{currency} · {metric.status} ({metric.known}/{metric.total})"
    dashboard[5][6], dashboard[6][6], dashboard[7][6] = "Trefferquote", _number(snapshot.hit_rate_percent), "% der bekannten Netto-Ergebnisse"
    dashboard[5][8], dashboard[6][8], dashboard[7][8] = "Offene Trades", snapshot.open_trades, "Zum Ende des gewählten Zeitraums"
    dashboard[5][10], dashboard[6][10], dashboard[7][10] = "Vollständige Belege", f"{snapshot.complete_trades}/{snapshot.evaluated_trades}", "Fehlende Kosten bleiben offen"
    dashboard[40][0] = "Netto-R: vollständige Setup-Familien wie im Ranking; je Setup Mittelwert aller Legs, unabhängig von deren Abrechnungswährung."
    dashboard[41][0] = "Kurve: Summe bekannter realisierter Ergebnisse, keine Kontoequity. Kostenchart: positiv = Aufwand, negatives Funding = Gutschrift."
    points = snapshot.monthly_pnl if scope.period == "all" else snapshot.daily_pnl
    chart_data = (
        (26, ["Datum", "Bekannter Netto-PnL " + currency], snapshot.cumulative_realized_net),
        (28, ["Monat" if scope.period == "all" else "Tag", "Bekannter Netto-PnL " + currency], points),
        (32, ["Kanal", "Setup Netto-R"], snapshot.channels),
    )
    for start, header, rows in chart_data:
        dashboard[0][start:start + 2] = header
        for offset, row in enumerate(rows, start=1):
            dashboard[offset][start:start + 2] = [row.source, _number(row.net_r)] if start == 32 else [row.label, _number(row.value)]
    dashboard[0][30:32] = ["Kostentyp", currency + " · Aufwand (+) / Gutschrift (-)"]
    dashboard[1][30:32] = ["Gebühren", _number(snapshot.fees.value)]
    dashboard[2][30:32] = ["Funding", _number(-snapshot.funding.value if snapshot.funding.value is not None else None)]
    journal = [JOURNAL_HEADERS]
    for row in reversed(snapshot.journal):
        roles = ", ".join(f"{role}: {count}" for role, count in row.execution_roles.items() if count)
        journal.append([row.source, row.symbol, row.side, row.status + (" · am Periodenende offen" if row.cohort == "open" else ""), _stamp(row.entry_at), _stamp(row.closed_at),
                        row.entry_price, row.exit_price, row.quantity, row.net_pnl, row.fees, row.funding, row.currency,
                        row.exchange, row.network, row.entry_execution, row.take_profit_execution, roles or "unknown",
                        "Vollständig" if row.complete else "Unvollständig", "; ".join(row.issues), row.trade_id, row.source_id])
    channels = [CHANNEL_HEADERS, *[[row.source, row.rank, row.origin, row.score, row.proof, row.drawdown_r,
                 _stamp(row.last_change), row.net_r, row.scope_setups, row.excluded_setups, row.source_id] for row in snapshot.channels]]
    parameters = [["Bereich", "Parameter", "Wert", "Kontext"],
                  *[[row.section, row.name, row.value, row.context] for row in snapshot.parameters]]
    months = [["Monat", "Stand", "Veröffentlicht (Berlin)", "Bericht"], *[[row.month, row.revision,
              _stamp(row.published_at), f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_ids['aqla Mainnet Monatsbericht ' + row.month]}"
              if 'aqla Mainnet Monatsbericht ' + row.month in sheet_ids else "Noch nicht in dieser Mappe veröffentlicht"] for row in snapshot.months]]
    return {"Dashboard": _rect(dashboard, 34), "Journal": _rect(journal, len(JOURNAL_HEADERS)), "Kanäle": _rect(channels, len(CHANNEL_HEADERS)),
            "Parameter": _rect(parameters, 4), "Monatsberichte": _rect(months, 4)}


def chart_specs(snapshot: PresentationSnapshot, sheet_id: int) -> list[dict]:
    currency = snapshot.scope.currency
    vectors = (("Kumulierte bekannte realisierte Netto-PnL · " + currency, "LINE", 26, len(snapshot.cumulative_realized_net), 0, 8, currency),
               (("Monats" if snapshot.scope.period == "all" else "Tages") + "ergebnisse · " + currency, "COLUMN", 28,
                len(snapshot.monthly_pnl if snapshot.scope.period == "all" else snapshot.daily_pnl), 6, 8, currency),
               ("Kosten nach Typ · " + currency, "BAR", 30, 2, 0, 24, currency),
               ("Kanäle · Netto-R je Setup", "BAR", 32, len(snapshot.channels), 6, 24, "R"))
    result = []
    for index, (title, kind, start, length, col, row, unit) in enumerate(vectors):
        def source(offset, length=length, start=start):
            return {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0,
                    "endRowIndex": max(2, length + 1), "startColumnIndex": start + offset, "endColumnIndex": start + offset + 1}]}}
        spec = {"title": title, "fontName": "Arial", "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                "hiddenDimensionStrategy": "SHOW_ALL",
                "basicChart": {"chartType": kind, "legendPosition": "NO_LEGEND", "headerCount": 1,
                               "domains": [{"domain": source(0)}], "series": [{"series": source(1), "targetAxis": "BOTTOM_AXIS" if kind == "BAR" else "LEFT_AXIS", "color": BLUE}],
                               "axis": [{"position": "BOTTOM_AXIS" if kind == "BAR" else "LEFT_AXIS", "title": unit}]}}
        result.append({"chartId": 104800 + index, "spec": spec, "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": col}, "widthPixels": 520,
            "heightPixels": 300, "offsetXPixels": 0, "offsetYPixels": 0}}})
    return result


def _metadata(raw):
    rows = [row for row in raw.get("developerMetadata", []) if row.get("metadataKey") == META_KEY]
    if len(rows) > 1:
        raise AqlaError("Duplicate presentation ownership metadata")
    if not rows:
        return None, None
    try:
        return PresentationManifest.model_validate_json(rows[0]["metadataValue"]), rows[0]["metadataId"]
    except (KeyError, ValueError):
        raise AqlaError("Invalid presentation ownership metadata") from None


def _store(client, manifest, metadata_id):
    value = manifest.model_dump_json()
    if metadata_id is None:
        operation = {"createDeveloperMetadata": {"developerMetadata": {"metadataKey": META_KEY, "metadataValue": value,
                     "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}}
    else:
        operation = {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {"metadataId": metadata_id}}],
                     "developerMetadata": {"metadataValue": value}, "fields": "metadataValue"}}
    client.request("POST", ":batchUpdate", json={"requests": [operation]})


def _raw(client):
    raw = client.request("GET", params={"fields": "properties.title,sheets(properties,charts,conditionalFormats,basicFilter),developerMetadata"})
    scopes = [row["metadataValue"] for row in raw.get("developerMetadata", []) if row.get("metadataKey") == client.scope_key]
    if client.scope and scopes and scopes != [client.scope]:
        raise AqlaError("This Google spreadsheet belongs to a different aqla account/network")
    return raw


def _read(client, title, height, width):
    result = []
    for start in range(0, height, 100):
        length = min(100, height - start)
        rows = client.values([f"'{title}'!A{start + 1}:{column(width)}{start + length}"])[0]
        result.extend([[*row, *[""] * (width - len(row))][:width] for row in rows])
        result.extend([[""] * width for _ in range(length - len(rows))])
    return result


def selection(client):
    """Read four validated display selectors; this never initializes or changes cells."""
    with client.lock:
        raw = _raw(client)
        manifest, _ = _metadata(raw)
        defaults = PresentationScope().model_dump(mode="json")
        if manifest is None:
            return {"selection": defaults, "valid": True}
        props = {row["properties"]["title"]: row["properties"] for row in raw.get("sheets", [])}
        if props.get("Dashboard", {}).get("sheetId") != manifest.sheet_ids.get("Dashboard"):
            raise AqlaError("Presentation worksheet ownership changed")
        cells = client.values([f"'Dashboard'!{col}4:{col}4" for col in ("B", "E", "H", "K")])
        try:
            selected = PresentationScope.model_validate(dict(zip(("exchange", "network", "currency", "period"),
                                                          [value[0][0] for value in cells], strict=True)))
            for field in ("currency", "period"):
                if getattr(selected, field) not in manifest.options.get(field, []):
                    raise ValueError("Unavailable presentation selection")
            return {"selection": selected.model_dump(mode="json"), "valid": True}
        except (ValueError, IndexError, TypeError):
            return {"selection": defaults, "valid": False, "error_code": "invalid_selection"}


def _owned_ids(raw, manifest):
    properties = {row["properties"]["title"]: row["properties"] for row in raw.get("sheets", [])}
    if manifest:
        for title in TITLES:
            if title in properties and properties[title]["sheetId"] != manifest.sheet_ids.get(title):
                raise AqlaError("Presentation worksheet ownership changed")
            observed = next((props for props in properties.values() if props["sheetId"] == manifest.sheet_ids[title]), None)
            if observed is not None and observed["title"] != title:
                raise AqlaError("Presentation worksheet ownership changed")
        return dict(manifest.sheet_ids)
    used = {props["sheetId"] for props in properties.values()}
    ids = {}
    blank = next((props for props in properties.values() if props["sheetId"] == 0 and props["title"] in ("Tabellenblatt1", "Sheet1")), None)
    for title in TITLES:
        if title in properties:
            ids[title] = properties[title]["sheetId"]
        elif title == "Dashboard" and blank:
            ids[title] = 0
        else:
            identity = int(digest([META_KEY, title])[:7], 16)
            while identity in used:
                identity += 1
            ids[title] = identity
            used.add(identity)
    return ids


def _guard(client, expected, *, allow_missing=False):
    raw = _raw(client)
    current, metadata_id = _metadata(raw)
    if current != expected:
        raise AqlaError("Presentation publication changed during delivery")
    ids = _owned_ids(raw, current)
    properties = {row["properties"]["title"]: row["properties"] for row in raw["sheets"]}
    if not allow_missing and any(title not in properties or properties[title]["sheetId"] != ids[title] for title in TITLES):
        raise AqlaError("Presentation worksheet ownership changed")
    return raw, metadata_id


def _technical(title):
    return title.startswith("aqla ") and any(title.endswith(" " + table) for table in HEADERS)


def _structure_requests(client, snapshot, raw, manifest, clock):
    properties = {row["properties"]["title"]: row["properties"] for row in raw.get("sheets", [])}
    ids = _owned_ids(raw, manifest)
    requests = []
    if manifest is None:
        # Claim only empty grids, atomically with the rename/add operations.
        for identity in ids.values():
            props = next((props for props in properties.values() if props["sheetId"] == identity), None)
            if props:
                if any(sheet.get("charts") for sheet in raw["sheets"] if sheet["properties"]["sheetId"] == identity):
                    raise AqlaError("Presentation cannot adopt a worksheet with existing charts")
                grid = props["gridProperties"]
                if grid["rowCount"] > MAX_ROWS or grid["columnCount"] > 100:
                    raise AqlaError("Presentation cannot adopt an unbounded existing worksheet")
                if any(any(row) for row in _read(client, props["title"], grid["rowCount"], grid["columnCount"])):
                    raise AqlaError("Presentation cannot overwrite an existing nonempty worksheet")
        claim = PresentationManifest(binding=snapshot.scope_binding, revision=0, sheet_ids=ids, attempted_at=clock)
        requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataKey": META_KEY,
            "metadataValue": claim.model_dump_json(), "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}})
    if client.scope and not any(row.get("metadataKey") == client.scope_key for row in raw.get("developerMetadata", [])):
        requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataKey": client.scope_key,
            "metadataValue": client.scope, "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}})
    for index, title in enumerate(TITLES):
        props = next((props for props in properties.values() if props["sheetId"] == ids[title]), None)
        if props is None:
            requests.append({"addSheet": {"properties": {"sheetId": ids[title], "title": title, "index": index,
                "gridProperties": {"rowCount": 1000, "columnCount": 34 if title == "Dashboard" else len(JOURNAL_HEADERS) if title == "Journal" else 11}}}})
        else:
            changes = {key: value for key, value in {"title": title, "index": index, "hidden": False}.items() if props.get(key) != value}
            if changes:
                requests.append({"updateSheetProperties": {"properties": {"sheetId": ids[title], **changes}, "fields": ",".join(changes)}})
    for title, props in properties.items():
        if _technical(title) and not props.get("hidden", False):
            requests.append({"updateSheetProperties": {"properties": {"sheetId": props["sheetId"], "hidden": True}, "fields": "hidden"}})
        elif title.startswith("aqla Mainnet Monatsbericht ") and props.get("hidden", False):
            requests.append({"updateSheetProperties": {"properties": {"sheetId": props["sheetId"], "hidden": False}, "fields": "hidden"}})
    if raw.get("properties", {}).get("title") != WORKBOOK_TITLE:
        requests.append({"updateSpreadsheetProperties": {"properties": {"title": WORKBOOK_TITLE}, "fields": "title"}})
    return requests


def _contains(actual, wanted):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(_contains(actual.get(key, 0 if value == 0 else None), value) for key, value in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and len(actual) == len(wanted) and all(_contains(a, b) for a, b in zip(actual, wanted, strict=True))
    return math.isclose(actual, wanted, rel_tol=1e-6, abs_tol=1e-6) if isinstance(wanted, float) and isinstance(actual, (int, float)) else actual == wanted


def _chart_requests(raw, snapshot, ids):
    sheet = next(row for row in raw["sheets"] if row["properties"]["sheetId"] == ids["Dashboard"])
    existing = {row["chartId"]: row for row in sheet.get("charts", [])}
    requests = []
    for chart in chart_specs(snapshot, ids["Dashboard"]):
        previous = existing.get(chart["chartId"])
        if previous is None:
            if any(chart["chartId"] == row["chartId"] for sheet in raw["sheets"] for row in sheet.get("charts", [])):
                raise AqlaError("Presentation chart ID belongs to another worksheet")
            requests.append({"addChart": {"chart": chart}})
        else:
            if not _contains(previous.get("spec"), chart["spec"]):
                requests.append({"updateChartSpec": {"chartId": chart["chartId"], "spec": chart["spec"]}})
            if not _contains(previous.get("position"), chart["position"]):
                requests.append({"updateEmbeddedObjectPosition": {"objectId": chart["chartId"], "newPosition": chart["position"], "fields": "overlayPosition"}})
    return requests


def _filter_range(identity, rows, width):
    return {"sheetId": identity, "startRowIndex": 0, "endRowIndex": max(2, rows),
            "startColumnIndex": 0, "endColumnIndex": width}


def _validation_requests(snapshot, dashboard):
    return [{"setDataValidation": {"range": {"sheetId": dashboard, "startRowIndex": 3, "endRowIndex": 4,
                "startColumnIndex": col, "endColumnIndex": col + 1}, "rule": {"condition": {"type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": value} for value in options]}, "strict": True,
                "showCustomUi": True, "inputMessage": field + " · nur Auswertung"}}}
            for col, field, options in ((1, "exchange", ["all", "hyperliquid", "aster", "lighter"]),
                (4, "network", ["mainnet", "testnet"]), (7, "currency", snapshot.available_currencies),
                (10, "period", snapshot.available_periods))]


def _format_matches(client, raw, snapshot, ids, values):
    """Inspect bounded view controls so an identical fingerprint can repair damage."""
    for sheet in raw["sheets"]:
        prop = sheet["properties"]
        title = prop["title"]
        if title not in TITLES:
            continue
        if not _contains(prop.get("gridProperties", {}), {"frozenRowCount": 0 if title == "Dashboard" else 1,
                                                        "hideGridlines": True}):
            return False
        if title != "Dashboard" and not _contains(sheet.get("basicFilter", {}).get("range"),
                _filter_range(ids[title], len(values[title]), len(values[title][0]))):
            return False
    controls = client.request("GET", params={"ranges": ["'Dashboard'!B4:K4"], "includeGridData": True,
        "fields": "sheets(properties.sheetId,data(startRow,startColumn,rowData(values(dataValidation))))"})
    actual = {}
    for sheet in controls.get("sheets", []):
        if sheet["properties"]["sheetId"] != ids["Dashboard"]:
            continue
        for grid in sheet.get("data", []):
            if grid.get("startRow", 0) != 3:
                continue
            for offset, cell in enumerate((grid.get("rowData") or [{}])[0].get("values", [])):
                actual[grid.get("startColumn", 0) + offset] = cell.get("dataValidation")
    return all(_contains(actual.get(rule["range"]["startColumnIndex"]), rule["rule"])
               for request in _validation_requests(snapshot, ids["Dashboard"])
               for rule in [request["setDataValidation"]])


def _format(client, raw, snapshot, ids, values):
    requests = []
    for title in TITLES:
        identity, width = ids[title], len(values[title][0])
        requests.extend([
            {"updateSheetProperties": {"properties": {"sheetId": identity, "gridProperties": {"frozenRowCount": 0 if title == "Dashboard" else 1, "hideGridlines": True}}, "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}},
            {"repeatCell": {"range": {"sheetId": identity}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "textFormat": {"fontFamily": "Arial", "fontSize": 10, "foregroundColor": DARK}, "verticalAlignment": "TOP", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": identity, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"backgroundColor": LIGHT, "textFormat": {"bold": True, "foregroundColor": BLUE}}}, "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.foregroundColor"}},
            {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 36}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": 0, "endIndex": width}, "properties": {"pixelSize": 110}, "fields": "pixelSize"}},
        ])
        if title != "Dashboard":
            previous = next(row for row in raw["sheets"] if row["properties"]["sheetId"] == identity).get("basicFilter", {})
            requests.append({"setBasicFilter": {"filter": {**previous, "range": _filter_range(identity, len(values[title]), width)}}})
        sheet = next(row for row in raw["sheets"] if row["properties"]["sheetId"] == identity)
        for _ in sheet.get("conditionalFormats", []):
            requests.append({"deleteConditionalFormatRule": {"sheetId": identity, "index": 0}})
        if title == "Journal":
            requests.extend([
                {"repeatCell": {"range": {"sheetId": identity, "startRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 12}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.########"}}}, "fields": "userEnteredFormat.numberFormat"}},
                {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": 19, "endIndex": 20}, "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": 21, "endIndex": 22}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            ])
            for col in (0, 4, 5, 17, 20):
                requests.append({"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": 180}, "fields": "pixelSize"}})
        if title in ("Kanäle", "Parameter", "Monatsberichte"):
            requests.append({"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": 0, "endIndex": width}, "properties": {"pixelSize": 210 if title != "Parameter" else 260}, "fields": "pixelSize"}})
        if title in ("Journal", "Kanäle"):
            money_col = 9 if title == "Journal" else 7
            for condition, color in (("NUMBER_GREATER", GREEN), ("NUMBER_LESS", RED)):
                requests.append({"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [{"sheetId": identity, "startRowIndex": 1, "startColumnIndex": money_col, "endColumnIndex": money_col + 1}], "booleanRule": {"condition": {"type": condition, "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"foregroundColor": color}}}}}})
    dashboard = ids["Dashboard"]
    requests.extend([
        {"unmergeCells": {"range": {"sheetId": dashboard, "startRowIndex": 0, "endRowIndex": 42, "startColumnIndex": 0, "endColumnIndex": 12}}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 12}, "properties": {"pixelSize": 90}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard, "dimension": "COLUMNS", "startIndex": 26, "endIndex": 34}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard, "dimension": "ROWS", "startIndex": 8, "endIndex": 42}, "properties": {"pixelSize": 22}, "fields": "pixelSize"}},
        {"repeatCell": {"range": {"sheetId": dashboard, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 12}, "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 20}}}, "fields": "userEnteredFormat.textFormat.fontSize"}},
        {"repeatCell": {"range": {"sheetId": dashboard, "startRowIndex": 5, "endRowIndex": 8, "startColumnIndex": 0, "endColumnIndex": 12}, "cell": {"userEnteredFormat": {"backgroundColor": LIGHT}}, "fields": "userEnteredFormat.backgroundColor"}},
        {"repeatCell": {"range": {"sheetId": dashboard, "startRowIndex": 6, "endRowIndex": 7, "startColumnIndex": 0, "endColumnIndex": 12}, "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 17, "bold": True}, "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}}, "fields": "userEnteredFormat.textFormat.fontSize,userEnteredFormat.textFormat.bold,userEnteredFormat.numberFormat"}},
    ])
    for row, height in ((3, 32), (5, 36), (6, 30), (7, 36), (40, 42), (41, 42)):
        requests.append({"updateDimensionProperties": {"range": {"sheetId": dashboard, "dimension": "ROWS", "startIndex": row, "endIndex": row + 1}, "properties": {"pixelSize": height}, "fields": "pixelSize"}})
    for row in (0, 1, 2, 40, 41):
        requests.append({"mergeCells": {"range": {"sheetId": dashboard, "startRowIndex": row, "endRowIndex": row + 1, "startColumnIndex": 0, "endColumnIndex": 12}, "mergeType": "MERGE_ALL"}})
    for row in (5, 6, 7):
        for col in range(0, 12, 2):
            requests.append({"mergeCells": {"range": {"sheetId": dashboard, "startRowIndex": row, "endRowIndex": row + 1, "startColumnIndex": col, "endColumnIndex": col + 2}, "mergeType": "MERGE_ALL"}})
    for condition, color in (("NUMBER_GREATER", GREEN), ("NUMBER_LESS", RED)):
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [{"sheetId": dashboard, "startRowIndex": 6, "endRowIndex": 7, "startColumnIndex": col, "endColumnIndex": col + 2} for col in (0, 4)], "booleanRule": {"condition": {"type": condition, "values": [{"userEnteredValue": "0"}]}, "format": {"textFormat": {"foregroundColor": color}}}}}})
    for col, pattern in ((6, '0.0" %"'), (8, "0")):
        requests.append({"repeatCell": {"range": {"sheetId": dashboard, "startRowIndex": 6, "endRowIndex": 7, "startColumnIndex": col, "endColumnIndex": col + 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}}, "fields": "userEnteredFormat.numberFormat"}})
    requests.extend(_validation_requests(snapshot, dashboard))
    # Links are text formatting, never formula strings supplied by a channel.
    props = {row["properties"]["title"]: row["properties"] for row in raw["sheets"]}
    for offset, trade in enumerate(reversed(snapshot.journal), start=1):
        names = [f"aqla {trade.exchange.capitalize()} {trade.network.capitalize()} Trades"]
        if trade.exchange == "hyperliquid":
            names += [f"aqla {trade.network.capitalize()} Trades", "aqla Trades"]
        target = next((props[name]["sheetId"] for name in names if name in props), None)
        if target is not None:
            requests.append({"repeatCell": {"range": {"sheetId": ids["Journal"], "startRowIndex": offset, "endRowIndex": offset + 1, "startColumnIndex": 20, "endColumnIndex": 21}, "cell": {"userEnteredFormat": {"textFormat": {"link": {"uri": f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={target}"}}}}, "fields": "userEnteredFormat.textFormat.link"}})
    client.request("POST", ":batchUpdate", json={"requests": requests})


def publish(client, snapshot, *, clock=None):
    snapshot = PresentationSnapshot.model_validate(snapshot)
    if getattr(client, "presentation_binding", snapshot.scope_binding) != snapshot.scope_binding:
        raise AqlaError("Presentation belongs to a different authoritative execution state")
    # Persist wall time: a reboot resets monotonic clocks and must not bypass the minute gate.
    clock = time.time() if clock is None else clock
    with client.lock:
        last = getattr(client, "presentation_attempt_at", None)
        if last is not None and 0 <= clock - last < 60:
            return {"status": "throttled", "fingerprint": snapshot.fingerprint, "revision": snapshot.revision}
        client.presentation_attempt_at = clock
        raw = _raw(client)
        manifest, metadata_id = _metadata(raw)
        if manifest and manifest.binding != snapshot.scope_binding:
            raise AqlaError("Presentation belongs to a different authoritative execution state")
        if manifest and snapshot.revision < manifest.revision and snapshot.fingerprint != manifest.fingerprint:
            raise AqlaError("Stale presentation revision")
        if manifest and 0 <= clock - manifest.attempted_at < 60:
            return {"status": "throttled", "fingerprint": snapshot.fingerprint, "revision": snapshot.revision}
        structure = _structure_requests(client, snapshot, raw, manifest, clock)
        if structure:
            if manifest is not None:
                manifest = manifest.model_copy(update={"confirmed": False, "attempted_at": clock})
                _store(client, manifest, metadata_id)
                _guard(client, manifest, allow_missing=True)
            client.request("POST", ":batchUpdate", json={"requests": structure})
        raw = _raw(client)
        manifest, metadata_id = _metadata(raw)
        ids = _owned_ids(raw, manifest)
        all_ids = {row["properties"]["title"]: row["properties"]["sheetId"] for row in raw["sheets"]}
        values = tables(snapshot, client.settings.spreadsheet_id, all_ids)
        extents = {title: max(len(values[title]), manifest.extents.get(title, 0)) for title in TITLES}
        if any(height > MAX_ROWS for height in extents.values()):
            raise AqlaError("Presentation exceeds its bounded worksheet budget")
        padded = {title: [*rows, *[[""] * len(rows[0]) for _ in range(extents[title] - len(rows))]] for title, rows in values.items()}
        growth = []
        for sheet in raw["sheets"]:
            prop = sheet["properties"]
            title = prop["title"]
            if title in TITLES:
                wanted = {"rowCount": max(extents[title], prop["gridProperties"]["rowCount"]),
                          "columnCount": max(len(values[title][0]), prop["gridProperties"]["columnCount"])}
                if any(prop["gridProperties"].get(key) != value for key, value in wanted.items()):
                    growth.append({"updateSheetProperties": {"properties": {"sheetId": prop["sheetId"], "gridProperties": wanted}, "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        properties = {row["properties"]["title"]: row["properties"] for row in raw["sheets"]}
        current = {title: _read(client, title, min(extents[title], properties[title]["gridProperties"]["rowCount"]),
                   min(len(values[title][0]), properties[title]["gridProperties"]["columnCount"])) for title in TITLES}
        current = {title: [[*row, *[""] * (len(values[title][0]) - len(row))] for row in rows]
                   + [[""] * len(values[title][0]) for _ in range(extents[title] - len(rows))] for title, rows in current.items()}
        changed = any(current[title] != padded[title] for title in TITLES)
        charts = _chart_requests(raw, snapshot, ids)
        intact = _format_matches(client, raw, snapshot, ids, values)
        if manifest.confirmed and manifest.fingerprint == snapshot.fingerprint and not changed and not structure and not charts and not growth and intact:
            return {"status": "unchanged", "fingerprint": snapshot.fingerprint, "revision": snapshot.revision,
                    "url": f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={ids['Dashboard']}"}
        pending = PresentationManifest(binding=snapshot.scope_binding, fingerprint=snapshot.fingerprint, revision=snapshot.revision,
            sheet_ids=ids, extents=extents, attempted_at=clock, options={"currency": snapshot.available_currencies, "period": snapshot.available_periods})
        _store(client, pending, metadata_id)
        raw, metadata_id = _guard(client, pending)
        if growth or charts:
            client.request("POST", ":batchUpdate", json={"requests": [*growth, *charts]})
        for title in TITLES:
            width = len(values[title][0])
            for start in range(0, extents[title], 100):
                chunk = padded[title][start:start + 100]
                if current[title][start:start + len(chunk)] == chunk:
                    continue
                client.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": [{
                    "range": f"'{title}'!A{start + 1}:{column(width)}{start + len(chunk)}", "values": chunk}]})
        if any(_read(client, title, extents[title], len(values[title][0])) != padded[title] for title in TITLES):
            raise AqlaError("Presentation readback failed; retry the derived publication")
        raw, metadata_id = _guard(client, pending)
        _format(client, raw, snapshot, ids, values)
        raw, metadata_id = _guard(client, pending)
        if _chart_requests(raw, snapshot, ids) or not _format_matches(client, raw, snapshot, ids, values):
            raise AqlaError("Presentation layout readback failed; retry the derived publication")
        confirmed = pending.model_copy(update={"confirmed": True})
        _store(client, confirmed, metadata_id)
        _guard(client, confirmed)
        return {"status": "published", "fingerprint": snapshot.fingerprint, "revision": snapshot.revision,
                "url": f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={ids['Dashboard']}"}
