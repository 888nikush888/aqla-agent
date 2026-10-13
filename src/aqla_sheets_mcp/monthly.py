"""Idempotent monthly analysis tabs. RAW data cannot become spreadsheet formulas."""
from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import Field, model_validator

from aqla_agent.monthly import (
    MAX_PUBLICATION_ROWS,
    SUM_LABEL,
    Fingerprint,
    MonthlyPublication,
    MonthlyPublishOutcome,
    Revision,
    month_bounds,
    stamp,
)
from aqla_hyperliquid_mcp.models import AqlaError, Model, digest

HEADERS = ["Trade-ID", "Entry (Berlin)", "Schließung (Berlin)", "Quelle", "Quellen-ID", "Markt", "Richtung",
           "Börse", "Netzwerk", "Profil", "Positionswert", "Margin-Hebel", "Anfangsmargin", "Brutto-PnL",
           "Gebühren", "Funding", "Netto-PnL", "Währung", "ROE %", "Vollständig", "Datenlücken"]
KEYS = ["trade_id", "entry_at", "closed_at", "source_name", "source_id", "symbol", "side", "exchange", "network",
        "profile", "entry_notional", "margin_leverage", "initial_margin", "gross_pnl", "fees", "funding", "net_pnl",
        "currency", "roe_percent", "complete", "issues"]
MANIFEST_PREFIX = "aqla.monthly."


class MonthlyManifest(Model):
    version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    revision: Revision
    fingerprint: Fingerprint
    length: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    extent: Annotated[int, Field(strict=True, ge=1, le=20_000)]
    content_digest: Fingerprint
    confirmed: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def valid_extent(self):
        if self.extent < self.length:
            raise ValueError("Invalid monthly content extent")
        return self


class PublicationRejected(AqlaError):
    def __init__(self, code):
        self.code = code
        super().__init__("Monthly publication rejected: " + code)


def _manifest(raw, sheet_id):
    entries = [item for item in raw.get("developerMetadata", []) if item.get("metadataKey") == MANIFEST_PREFIX + str(sheet_id)]
    if len(entries) > 1:
        raise PublicationRejected("schema")
    if not entries:
        return None, None
    entry = entries[0]
    if type(entry.get("metadataId")) is not int or entry.get("location") != {"sheetId": sheet_id}:
        raise PublicationRejected("schema")
    try:
        return MonthlyManifest.model_validate_json(entry["metadataValue"]), entry["metadataId"]
    except (ValueError, KeyError):
        raise PublicationRejected("schema") from None


def _check_scope(client, raw):
    scopes = [item["metadataValue"] for item in raw.get("developerMetadata", []) if item.get("metadataKey") == client.scope_key]
    if client.scope and scopes != [client.scope]:
        raise PublicationRejected("schema")


def _store_manifest(client, sheet_id, manifest, metadata_id):
    value = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    if metadata_id is None:
        operation = {"createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": MANIFEST_PREFIX + str(sheet_id), "metadataValue": value,
            "visibility": "DOCUMENT", "location": {"sheetId": sheet_id}}}}
    else:
        operation = {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {"metadataId": metadata_id}}],
                     "developerMetadata": {"metadataValue": value}, "fields": "metadataValue"}}
    client.request("POST", ":batchUpdate", json={"requests": [operation]})


def _read_values(client, title, length):
    from .client import column

    returned = client.values([f"'{title}'!A1:{column(len(HEADERS))}{length}"])[0]
    if len(returned) > length or any(len(row) > len(HEADERS) for row in returned):
        raise PublicationRejected("integrity")
    return [row + [""] * (len(HEADERS) - len(row)) for row in returned] + [[""] * len(HEADERS) for _ in range(length - len(returned))]


def _check_generation(manifest, cells, report):
    generations = [(manifest.revision, manifest.fingerprint)] if manifest else []
    revision, fingerprint = cells[2][1], cells[3][1]
    if revision != "" or fingerprint != "":
        if isinstance(revision, bool) or not re.fullmatch(r"[1-9]\d*", str(revision)) or not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)):
            raise PublicationRejected("schema")
        generations.append((int(revision), fingerprint))
    for previous_revision, previous_fingerprint in generations:
        if previous_revision > report["revision"] or previous_revision == report["revision"] and previous_fingerprint != report["fingerprint"]:
            raise PublicationRejected("conflict")


def _guard(client, title, sheet_id, report, expected_manifest=None):
    raw = client.request("GET", params={"fields": "sheets.properties,developerMetadata"})
    _check_scope(client, raw)
    properties = [item["properties"] for item in raw.get("sheets", []) if item["properties"]["title"] == title]
    if len(properties) != 1 or properties[0]["sheetId"] != sheet_id:
        raise PublicationRejected("conflict")
    manifest, metadata_id = _manifest(raw, sheet_id)
    _check_generation(manifest, _read_values(client, title, 4), report)
    if expected_manifest is not None and manifest != expected_manifest:
        raise PublicationRejected("conflict")
    return manifest, metadata_id


def table_values(report):
    s = report["summary"]
    rows = [["Aqla Mainnet Monatsbericht " + report["month"]],
            ["Zeitraum", stamp(report["start"]) + " bis " + stamp(report["end_exclusive"]) + " (Ende exklusiv)"],
            ["Revision", str(report["revision"])], ["Beleg-Fingerprint", report["fingerprint"]],
            ["Geschlossene Trades", str(s["trades"])], ["Vollständig / ausgeschlossen", f'{s["complete"]} / {s["excluded"]}'],
            ["Gewinne / Verluste / neutral", f'{s["wins"]} / {s["losses"]} / {s["break_even"]}'],
            ["Gewinnquote (vollständige Ergebnisse) %", s["win_rate_percent"] or ""],
            [SUM_LABEL + " (%)", s["roe_sum_percent"]], ["Margin-Vergleichsbasis", report["margin_basis"]]]
    rows.extend(["Netto-PnL " + currency, amount] for currency, amount in sorted(s["net_by_currency"].items()))
    rows += [[], ["Geschlossene Aqla-Trades · gesamtes Ergebnis im Schließmonat"], HEADERS]
    header_index = len(rows) - 1
    def render(row):
        values = []
        for key in KEYS:
            value = row.get(key)
            if key in ("entry_at", "closed_at"):
                value = stamp(value)
            elif key == "complete":
                value = "ja" if value else "nein"
            elif key == "issues":
                value = "; ".join(value)
            values.append(str(value) if value is not None else "")
        return values
    rows.extend(render(row) for row in report["rows"])
    rows += [[], ["Am Monatsende offene Aqla-Trades · ohne Ergebnissumme"], HEADERS]
    rows.extend(render(row) for row in report["open_trades"])
    rows += [[], ["Geschlossene Trades ohne belegte Schließzeit · keiner Monatssumme zugeordnet"], HEADERS]
    rows.extend(render(row) for row in report["undated_closed_trades"])
    return [row + [""] * (len(HEADERS) - len(row)) for row in rows], header_index


def _publish(client, report):
    month_bounds(report["month"])
    content = {key: value for key, value in report.items() if key not in ("fingerprint", "revision", "url", "published_current_revision")}
    if digest(content) != report["fingerprint"] or type(report["revision"]) is not int or report["revision"] < 1:
        raise PublicationRejected("schema")
    if client.prefix != "aqla Mainnet ":
        raise PublicationRejected("schema")
    values, header = table_values(report)
    if len(values) > MAX_PUBLICATION_ROWS:
        raise PublicationRejected("budget")
    title = client.prefix + "Monatsbericht " + report["month"]
    with client.lock:
        client.layout(create=True)  # Enforces the existing account/network scope.
        raw = client.request("GET", params={"fields": "sheets.properties,developerMetadata"})
        _check_scope(client, raw)
        props = [row["properties"] for row in raw["sheets"] if row["properties"]["title"] == title]
        if len(props) > 1:
            raise PublicationRejected("schema")
        created = not props
        if not props:
            used = {row["properties"]["sheetId"] for row in raw["sheets"]}
            sheet_id = int(digest([client.scope, title])[:7], 16)
            while sheet_id in used:
                sheet_id += 1
            client.request("POST", ":batchUpdate", json={"requests": [{"addSheet": {"properties": {
                "sheetId": sheet_id, "title": title, "gridProperties": {"rowCount": max(1000, len(values)), "columnCount": len(HEADERS)}}}}]})
            prop = {"sheetId": sheet_id, "gridProperties": {"rowCount": max(1000, len(values)), "columnCount": len(HEADERS)}}
        else:
            prop = props[0]
            sheet_id = prop["sheetId"]
        manifest, metadata_id = _manifest(raw, sheet_id)
        _check_generation(manifest, _read_values(client, title, 4), report)
        if manifest and manifest.confirmed:
            checked = _read_values(client, title, manifest.extent)
            previous = checked[:manifest.length]
            if digest(previous) != manifest.content_digest or any(any(row) for row in checked[manifest.length:]):
                raise PublicationRejected("integrity")
            if manifest.revision == report["revision"]:
                if manifest.length != len(values) or previous != values:
                    raise PublicationRejected("integrity")
                _guard(client, title, sheet_id, report, manifest)
                expected = values + [[""] * len(HEADERS) for _ in range(manifest.extent - len(values))]
                if _read_values(client, title, manifest.extent) != expected:
                    raise PublicationRejected("integrity")
                return MonthlyPublication(month=report["month"], fingerprint=report["fingerprint"], revision=report["revision"],
                    url=f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={sheet_id}")
        # Without a manifest, the old writer could have used every grid row.
        write_extent = max(len(values), (manifest.length if manifest.confirmed else manifest.extent)
                           if manifest else 0 if created else prop["gridProperties"]["rowCount"])
        extent = max(write_extent, manifest.extent if manifest else 0)
        if extent > 20_000:
            raise PublicationRejected("budget")
        if extent > prop["gridProperties"]["rowCount"]:
            client.request("POST", ":batchUpdate", json={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": extent}}, "fields": "gridProperties.rowCount"}}]})
        # Blank the old tail in the same bounded writes; a shrinking revision leaves no stale rows.
        padded = values + [[""] * len(HEADERS) for _ in range(extent - len(values))]
        legacy_confirmed = manifest is None and not created and _read_values(client, title, extent) == padded
        resumed = _read_values(client, title, extent) if (manifest is not None and not manifest.confirmed
            and (manifest.revision, manifest.fingerprint) == (report["revision"], report["fingerprint"])) else None
        if manifest is not None:
            _guard(client, title, sheet_id, report, manifest)
        pending = MonthlyManifest(revision=report["revision"], fingerprint=report["fingerprint"], length=len(values),
                                  extent=extent, content_digest=digest(values), confirmed=False)
        _store_manifest(client, sheet_id, pending, metadata_id)
        _, metadata_id = _guard(client, title, sheet_id, report, pending)
        from .client import column
        last = column(len(HEADERS))
        if not legacy_confirmed:
            for offset in range(0, write_extent, 100):
                chunk = padded[offset:min(offset + 100, write_extent)]
                if resumed is not None and resumed[offset:offset + len(chunk)] == chunk:
                    continue
                client.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": [{
                    "range": f"'{title}'!A{offset + 1}:{last}{offset + len(chunk)}", "values": chunk}]})
        # Verify every written cell, not merely a returned digest or a successful first chunk.
        _guard(client, title, sheet_id, report, pending)
        if _read_values(client, title, extent) != padded:
            raise PublicationRejected("integrity")
        client.request("POST", ":batchUpdate", json={"requests": [
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": header, "endRowIndex": header + 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": .85, "green": .93, "blue": .98}}}, "fields": "userEnteredFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 3},
                                           "properties": {"pixelSize": 210}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 20, "endIndex": 21},
                                           "properties": {"pixelSize": 440}, "fields": "pixelSize"}},
            {"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat.wrapStrategy"}}
        ]})
        _guard(client, title, sheet_id, report, pending)
        # Retain every formerly owned tail position across commit/readback loss.
        # Content length bounds writes; extent independently bounds verification.
        confirmed = pending.model_copy(update={"confirmed": True})
        _store_manifest(client, sheet_id, confirmed, metadata_id)
        _guard(client, title, sheet_id, report, confirmed)
        if _read_values(client, title, extent) != padded:
            raise PublicationRejected("integrity")
        return MonthlyPublication(month=report["month"], fingerprint=report["fingerprint"], revision=report["revision"],
            url=f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={sheet_id}")


def publish(client, report):
    """Return only typed outcomes and safe error codes to the native worker."""
    try:
        return MonthlyPublishOutcome(status="published", publication=_publish(client, report)).model_dump(mode="json")
    except PublicationRejected as error:
        return MonthlyPublishOutcome(status="blocked", error_code=error.code).model_dump(mode="json")
    except AqlaError as error:
        if str(error).startswith(("This Google spreadsheet", "Google Sheets journal", "Monat als")):
            return MonthlyPublishOutcome(status="blocked", error_code="schema").model_dump(mode="json")
        return MonthlyPublishOutcome(status="retry", error_code="temporary").model_dump(mode="json")
    except (ValueError, TypeError, KeyError, IndexError):
        return MonthlyPublishOutcome(status="blocked", error_code="schema").model_dump(mode="json")
