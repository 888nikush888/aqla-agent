"""Small Sheets API adapter. Stable row IDs make uncertain writes retry-safe."""
from __future__ import annotations

import json
import threading
from collections import defaultdict

from aqla_hyperliquid_mcp.models import AqlaError

from .journal import HEADERS, JournalRecord, fingerprint, record_key

BASE = "https://sheets.googleapis.com/v4/spreadsheets/"
PREFIX = "aqla "
META_HEADERS = ["_digest", "_revision"]


def column(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


class SheetsClient:
    def __init__(self, settings, *, session=None, timeout=15, scope=None):
        self.settings, self.timeout = settings, timeout
        self.scope = scope
        self.session = session
        self.lock = threading.RLock()

    def _session(self):
        if self.session is None:
            try:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2.service_account import Credentials
                credentials = Credentials.from_service_account_file(str(self.settings.credentials_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
                self.session = AuthorizedSession(credentials, refresh_timeout=self.timeout)
            except Exception:
                raise AqlaError("Google Sheets service-account credentials are unavailable or invalid") from None
        return self.session

    def request(self, method, suffix="", **kwargs):
        try:
            response = self._session().request(method, BASE + self.settings.spreadsheet_id + suffix, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except AqlaError:
            raise
        except Exception:
            raise AqlaError("Google Sheets request failed; journal records remain pending") from None

    def layout(self, *, create=True):
        raw = self.request("GET", params={"fields": "sheets.properties,developerMetadata"})
        properties = {sheet["properties"]["title"]: sheet["properties"] for sheet in raw.get("sheets", [])}
        used = {item["sheetId"] for item in properties.values()}
        requests = []
        scopes = [item["metadataValue"] for item in raw.get("developerMetadata", []) if item.get("metadataKey") == "aqla.scope"]
        if self.scope:
            if scopes and scopes != [self.scope]:
                raise AqlaError("This Google spreadsheet belongs to a different aqla account/network")
            if not scopes:
                if not create:
                    raise AqlaError("Google Sheets journal has not been initialized by the synchronizer yet")
                requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataKey": "aqla.scope", "metadataValue": self.scope, "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}})
        for table, headers in HEADERS.items():
            title = PREFIX + table
            if title in properties:
                continue
            if not create:
                raise AqlaError("Google Sheets journal is incomplete; wait for its automatic synchronization")
            identity = 1
            while identity in used:
                identity += 1
            used.add(identity)
            props = {"sheetId": identity, "title": title, "gridProperties": {"rowCount": 1000, "columnCount": len(headers) + 2, "frozenRowCount": 1}}
            properties[title] = props
            requests.extend([
                {"addSheet": {"properties": props}},
                {"updateCells": {"start": {"sheetId": identity, "rowIndex": 0, "columnIndex": 0}, "rows": [{"values": [{"userEnteredValue": {"stringValue": name}, "userEnteredFormat": {"textFormat": {"bold": True}}} for name in [*headers, *META_HEADERS]]}], "fields": "userEnteredValue,userEnteredFormat.textFormat.bold"}},
                {"setBasicFilter": {"filter": {"range": {"sheetId": identity, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(headers) + 2}}}},
                {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": len(headers), "endIndex": len(headers) + 2}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
            ])
        if requests:
            self.request("POST", ":batchUpdate", json={"requests": requests})
        return properties

    def existing(self, tables):
        ranges = [f"'{PREFIX + table}'!A:{column(len(HEADERS[table]) + 2)}" for table in tables]
        response = self.request("GET", "/values:batchGet", params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"})
        result = {}
        for table, value_range in zip(tables, response.get("valueRanges", []), strict=True):
            values = value_range.get("values", [])
            expected = [*HEADERS[table], *META_HEADERS]
            if not values or values[0][:len(expected)] != expected:
                raise AqlaError("Google Sheets journal headers changed; restore the generated schema")
            found = {}
            for index, row in enumerate(values[1:], 2):
                if not row or not row[0]:
                    continue
                identity = str(row[0])
                if identity in found:
                    raise AqlaError("Duplicate journal IDs found in Google Sheets")
                found[identity] = (index, [*row, *([""] * (len(expected) - len(row)))][:len(expected)])
            result[table] = (found, len(values))
        return result

    def sync(self, records):
        validated = [JournalRecord.model_validate(row).model_dump(mode="json") for row in records]
        with self.lock:
            if not validated:
                return {"accepted": {}, "synced": 0}
            properties = self.layout()
            tables = list(dict.fromkeys(row["table"] for row in validated))
            existing = self.existing(tables)
            updates, accepted, needed = [], {}, {}
            for row in validated:
                table, identity = row["table"], row["record_id"]
                found, count = existing[table]
                previous = found.get(identity)
                if previous:
                    index, cells = previous
                    size = len(HEADERS[table])
                    valid = cells[size] == fingerprint(cells[:size])
                    revision = cells[size + 1]
                    if valid and cells[size] == row["fingerprint"]:
                        accepted[record_key(row)] = row["fingerprint"]
                        continue
                    if valid and isinstance(revision, (int, float)) and revision > row["revision"]:
                        continue
                else:
                    index = count + 1
                update = {"range": f"'{PREFIX + table}'!A{index}:{column(len(HEADERS[table]) + 2)}{index}", "values": [[*row["values"], row["fingerprint"], row["revision"]]]}
                if len(json.dumps([*updates, update], ensure_ascii=True).encode()) > 1_500_000:
                    break
                updates.append(update)
                needed[table] = max(index, needed.get(table, 0))
                found[identity] = (index, update["values"][0])
                existing[table] = (found, max(count, index))
                accepted[record_key(row)] = row["fingerprint"]
            growth = []
            for table, count in needed.items():
                props = properties[PREFIX + table]
                capacity = props.get("gridProperties", {}).get("rowCount", 1000)
                if count > capacity:
                    growth.append({"updateSheetProperties": {"properties": {"sheetId": props["sheetId"], "gridProperties": {"rowCount": max(count, capacity * 2)}}, "fields": "gridProperties.rowCount"}})
            if growth:
                self.request("POST", ":batchUpdate", json={"requests": growth})
            if updates:
                # RAW keeps source text starting with '=' as literal text.
                self.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": updates})
            return {"accepted": accepted, "synced": len(accepted)}

    def read(self, table, offset=0, limit=100, filters=None):
        with self.lock:
            self.layout(create=False)
            found, _ = self.existing([table])[table]
            headers = HEADERS[table]
            filters = filters or {}
            if any(key not in headers for key in filters):
                raise AqlaError("Unknown journal filter column")
            rows = []
            invalid = 0
            for _, cells in found.values():
                row = dict(zip(headers, cells[:len(headers)], strict=True))
                valid = fingerprint(cells[:len(headers)]) == cells[len(headers)]
                if not valid:
                    invalid += 1
                if all(str(row[key]) == value for key, value in filters.items()):
                    rows.append({**row, "_verified": valid})
            return {"table": table, "rows": rows[offset:offset + limit], "total": len(rows), "unverified_rows": invalid, "next_offset": offset + limit if offset + limit < len(rows) else None}

    def statistics(self, group_by="gesamt"):
        with self.lock:
            self.layout(create=False)
            found, _ = self.existing(["Trades"])["Trades"]
            headers = HEADERS["Trades"]
            groups = defaultdict(lambda: {"trades": 0, "closed": 0, "wins": 0, "losses": 0, "break_even": 0, "net_before_funding_usd": 0.0, "incomplete_pnl": 0})
            unverified = 0
            for _, cells in found.values():
                if fingerprint(cells[:len(headers)]) != cells[len(headers)]:
                    unverified += 1
                    continue
                row = dict(zip(headers, cells[:len(headers)], strict=True))
                group = groups[str(row[group_by]) if group_by != "gesamt" else "gesamt"]
                group["trades"] += 1
                if row["Status"] == "closed":
                    group["closed"] += 1
                    net = row["Netto vor Funding USD"]
                    if not isinstance(net, (float, int)):
                        group["incomplete_pnl"] += 1
                        continue
                    group["net_before_funding_usd"] += net
                    group["wins"] += int(net > 0)
                    group["losses"] += int(net < 0)
                    group["break_even"] += int(net == 0)
            for group in groups.values():
                known = group["wins"] + group["losses"] + group["break_even"]
                group["win_rate_known_percent"] = group["wins"] * 100 / known if known else None
            return {"groups": dict(groups), "unverified_rows_excluded": unverified, "funding_included": False, "read_only_analysis": True}

    def close(self):
        if self.session is not None:
            self.session.close()
