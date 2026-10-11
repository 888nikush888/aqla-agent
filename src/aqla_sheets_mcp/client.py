"""Small Sheets API adapter. Stable row IDs make uncertain writes retry-safe."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import threading
from collections import defaultdict

from aqla_hyperliquid_mcp.models import AqlaError

from .journal import (
    HEADERS,
    LEGACY_HEADERS,
    PRE_PRESENTATION_HEADERS,
    PRE_RANK_HEADERS,
    TARGET_COLUMNS,
    JournalRecord,
    fingerprint,
    record_key,
)
from .parameters import OPERATING_PARAMETERS, PARAMETERS

BASE = "https://sheets.googleapis.com/v4/spreadsheets/"
PREFIX = "aqla "
META_HEADERS = ["_digest", "_revision"]
INDEX_CHUNK_ROWS = 10_000
MAX_INDEX_REQUESTS = 12
BODY_CHUNK_ROWS = 100
TEXT_CHUNK_ROWS = 10
MAX_BODY_REQUESTS = 12
MAX_SCAN_CURSOR_LENGTH = 2048
TEXT_TABLES = {"Signale", "Vorgänge", "Regeln", "Handelsparameter", "Ereignisse", "Pipelines"}


def _scan_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _cursor_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate cursor fields")
    return result


def _decode_scan_cursor(token):
    try:
        if not isinstance(token, str) or not 0 < len(token) <= MAX_SCAN_CURSOR_LENGTH:
            raise ValueError("Invalid cursor length")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in token):
            raise ValueError("Invalid cursor encoding")
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(raw, object_pairs_hook=_cursor_object)
        if not isinstance(cursor, dict) or set(cursor) != {"v", "binding", "index", "next"}:
            raise ValueError("Invalid cursor fields")
        if type(cursor["v"]) is not int or cursor["v"] != 1:
            raise ValueError("Unknown cursor version")
        if type(cursor["next"]) is not int or cursor["next"] < 0:
            raise ValueError("Invalid cursor position")
        for key in ("binding", "index"):
            value = cursor[key]
            if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("Invalid cursor digest")
        return cursor
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AqlaError("Invalid journal scan cursor; restart at offset 0") from None


def _encode_scan_cursor(binding, index, position):
    raw = json.dumps({"v": 1, "binding": binding, "index": index, "next": position}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _stale_scan_cursor():
    return AqlaError("stale_cursor: journal scope, schema or index changed; restart at offset 0")


def column(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


class SheetsClient:
    def publish_presentation(self, snapshot):
        from .presentation import publish
        return publish(self, snapshot)

    def presentation_selection(self):
        from .presentation import selection
        return selection(self)

    def publish_monthly(self, report):
        from .monthly import publish
        return publish(self, report)

    def __init__(self, settings, *, session=None, timeout=15, scope=None):
        self.settings, self.timeout = settings, timeout
        namespace = getattr(settings, "tab_namespace", "legacy")
        self.prefix = PREFIX if namespace == "legacy" else "aqla " + namespace.capitalize() + " "
        self.scope_key = "aqla.scope" if namespace == "legacy" else "aqla.scope." + namespace
        self.scope = scope
        self.session = session
        self.lock = threading.RLock()
        self.allowed_tables = set(HEADERS)

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
        scopes = [item["metadataValue"] for item in raw.get("developerMetadata", []) if item.get("metadataKey") == self.scope_key]
        if self.scope:
            if scopes and scopes != [self.scope]:
                raise AqlaError("This Google spreadsheet belongs to a different aqla account/network")
            if not scopes:
                if not create:
                    raise AqlaError("Google Sheets journal has not been initialized by the synchronizer yet")
                requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataKey": self.scope_key, "metadataValue": self.scope, "visibility": "DOCUMENT", "location": {"spreadsheet": True}}}})
        for table, headers in HEADERS.items():
            if table not in self.allowed_tables:
                continue
            title = self.prefix + table
            if title in properties:
                if create and table in PRE_PRESENTATION_HEADERS:
                    width = min(len(headers) + 2, properties[title]["gridProperties"]["columnCount"])
                    cells = self.values([f"'{title}'!A1:{column(width)}1"])
                    previous = [PRE_PRESENTATION_HEADERS[table]]
                    if table in PRE_RANK_HEADERS:
                        previous.append(PRE_RANK_HEADERS[table])
                    if table in LEGACY_HEADERS:
                        previous.append(LEGACY_HEADERS[table])
                    if table in ("Trades", "Orders", "Fills", "Vorgänge"):
                        previous.append([*LEGACY_HEADERS[table], *TARGET_COLUMNS])
                    old = next((headers for headers in previous if cells and cells[0] and cells[0][0] == [*headers, *META_HEADERS]), None)
                    if old is not None and len(old) < len(headers):
                        identity = properties[title]["sheetId"]
                        start = len(old)
                        extra = len(headers) - start
                        requests.extend([
                            {"insertDimension": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": start, "endIndex": start + extra}, "inheritFromBefore": True}},
                            {"updateCells": {"start": {"sheetId": identity, "rowIndex": 0, "columnIndex": 0}, "rows": [{"values": [{"userEnteredValue": {"stringValue": label}} for label in [*headers, *META_HEADERS]]}], "fields": "userEnteredValue"}},
                            {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": len(headers), "endIndex": len(headers) + 2}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
                        ])
                        properties[title]["gridProperties"]["columnCount"] += extra
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
            if table == "Handelsparameter":
                end = 1 + OPERATING_PARAMETERS + len(PARAMETERS)
                requests.extend([
                    {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
                    {"repeatCell": {"range": {"sheetId": identity, "startRowIndex": 0, "endRowIndex": end, "startColumnIndex": 1, "endColumnIndex": len(headers)}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}},
                    {"repeatCell": {"range": {"sheetId": identity, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": len(headers)}, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94}}}, "fields": "userEnteredFormat.backgroundColor"}},
                    {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
                    {"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "ROWS", "startIndex": 1, "endIndex": end}, "properties": {"pixelSize": 72}, "fields": "pixelSize"}},
                ])
                for index, width in enumerate((120, 170, 150, 95, 165, 145, 315), start=1):
                    requests.append({"updateDimensionProperties": {"range": {"sheetId": identity, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
        if requests:
            self.request("POST", ":batchUpdate", json={"requests": requests})
        return properties

    def values(self, ranges):
        response = self.request("GET", "/values:batchGet", params={"ranges": ranges, "valueRenderOption": "UNFORMATTED_VALUE"})
        values = response.get("valueRanges", [])
        if len(values) != len(ranges):
            raise AqlaError("Google Sheets returned an incomplete range response")
        return [item.get("values", []) for item in values]

    def existing(self, tables, properties):
        """Read bounded ID/version indexes, never historical record bodies."""
        headers = self.values([f"'{self.prefix + table}'!A1:{column(len(HEADERS[table]) + 2)}1" for table in tables])
        result = {}
        for table, values in zip(tables, headers, strict=True):
            expected = [*HEADERS[table], *META_HEADERS]
            if not values or values[0][:len(expected)] != expected:
                raise AqlaError("Google Sheets journal headers changed; restore the generated schema")
            result[table] = ({}, 1)
        capacities = {table: properties[self.prefix + table]["gridProperties"]["rowCount"] for table in tables}
        start = 2
        for _ in range(MAX_INDEX_REQUESTS):
            active = [table for table in tables if start <= capacities[table]]
            if not active:
                return result
            ranges = []
            for table in active:
                end = min(capacities[table], start + INDEX_CHUNK_ROWS - 1)
                ranges.extend([f"'{self.prefix + table}'!A{start}:A{end}",
                               f"'{self.prefix + table}'!{column(len(HEADERS[table]) + 1)}{start}:{column(len(HEADERS[table]) + 2)}{end}"])
            values = self.values(ranges)
            for position, table in enumerate(active):
                identities, metadata = values[position * 2:position * 2 + 2]
                found, last = result[table]
                if identities or metadata:
                    last = max(last, start + max(len(identities), len(metadata)) - 1)
                for offset, cells in enumerate(identities):
                    if not cells or not cells[0]:
                        continue
                    identity = str(cells[0])
                    if identity in found:
                        raise AqlaError("Duplicate journal IDs found in Google Sheets")
                    meta = metadata[offset] if offset < len(metadata) else []
                    found[identity] = (start + offset, [*meta, "", ""][:2])
                result[table] = (found, last)
            start += INDEX_CHUNK_ROWS
        if any(start <= capacity for capacity in capacities.values()):
            raise AqlaError("Google Sheets ID scan exceeded its request budget; journal remains pending")
        return result

    def bodies(self, table, indices, budget):
        """Fetch selected rows in small ranges with a shared request budget."""
        result = {}
        indices = list(dict.fromkeys(indices))
        chunk = TEXT_CHUNK_ROWS if table in TEXT_TABLES else BODY_CHUNK_ROWS
        width = len(HEADERS[table]) + 2
        while indices and budget[0] > 0:
            chosen = indices[:chunk]
            indices = indices[chunk:]
            # Coalesce adjacent IDs; cap the number of disjoint A1 ranges/URL size.
            groups = []
            for index in chosen:
                if groups and groups[-1][-1] + 1 == index:
                    groups[-1].append(index)
                else:
                    groups.append([index])
            if len(groups) > 20:
                deferred = [index for group in groups[20:] for index in group]
                indices = deferred + indices
                groups = groups[:20]
            ranges = [f"'{self.prefix + table}'!A{group[0]}:{column(width)}{group[-1]}" for group in groups]
            values = self.values(ranges)
            budget[0] -= 1
            for group, rows in zip(groups, values, strict=True):
                for offset, index in enumerate(group):
                    cells = rows[offset] if offset < len(rows) else []
                    result[index] = [*cells, *([""] * width)][:width]
        return result

    @staticmethod
    def revision(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 and int(value) == value:
            return int(value)
        return None

    def sync(self, records):
        validated = [JournalRecord.model_validate(row).model_dump(mode="json") for row in records]
        with self.lock:
            if not validated:
                return {"accepted": {}, "synced": 0}
            properties = self.layout()
            tables = list(dict.fromkeys(row["table"] for row in validated))
            existing = self.existing(tables, properties)
            if len({record_key(row) for row in validated}) != len(validated):
                raise AqlaError("Duplicate record IDs in journal delivery")
            budget, loaded = [MAX_BODY_REQUESTS], {}
            for table in tables:
                found, count = existing[table]
                capacity = properties[self.prefix + table]["gridProperties"]["rowCount"]
                targets, next_append = [], count + 1
                # Inspect append slots to avoid overwriting a trailing row whose ID was removed.
                for row in validated:
                    if row["table"] != table:
                        continue
                    if row["record_id"] in found:
                        targets.append(found[row["record_id"]][0])
                    else:
                        if next_append <= capacity:
                            targets.append(next_append)
                        next_append += 1
                loaded[table] = self.bodies(table, targets, budget)
            updates, accepted, needed = [], {}, {}
            for row in validated:
                table, identity = row["table"], row["record_id"]
                found, count = existing[table]
                previous = found.get(identity)
                if previous:
                    index, _ = previous
                    cells = loaded[table].get(index)
                    if cells is None:
                        break
                    if str(cells[0]) != identity:
                        raise AqlaError("Journal row IDs moved during synchronization; retry after sorting finishes")
                    size = len(HEADERS[table])
                    revision = self.revision(cells[size + 1])
                    if fingerprint(cells[:size]) == row["fingerprint"]:
                        maximum = max(row["revision"], revision or 0)
                        if cells[size] == row["fingerprint"] and revision == maximum:
                            accepted[record_key(row)] = row["fingerprint"]
                            continue
                        update = {"range": f"'{self.prefix + table}'!{column(size + 1)}{index}:{column(size + 2)}{index}", "values": [[row["fingerprint"], maximum]]}
                    elif revision is not None and revision > row["revision"]:
                        continue
                    else:
                        update = {"range": f"'{self.prefix + table}'!A{index}:{column(size + 2)}{index}", "values": [[*row["values"], row["fingerprint"], row["revision"]]]}
                else:
                    index = count + 1
                    capacity = properties[self.prefix + table]["gridProperties"]["rowCount"]
                    if index <= capacity:
                        cells = loaded[table].get(index)
                        if cells is None:
                            break
                        if any(value != "" for value in cells):
                            raise AqlaError("Journal append row contains data without an ID; restore the generated schema")
                    update = {"range": f"'{self.prefix + table}'!A{index}:{column(len(HEADERS[table]) + 2)}{index}", "values": [[*row["values"], row["fingerprint"], row["revision"]]]}
                if len(json.dumps([*updates, update], ensure_ascii=True).encode()) > 1_500_000:
                    break
                updates.append(update)
                needed[table] = max(index, needed.get(table, 0))
                found[identity] = (index, [row["fingerprint"], row["revision"]])
                existing[table] = (found, max(count, index))
                accepted[record_key(row)] = row["fingerprint"]
            growth = []
            for table, count in needed.items():
                props = properties[self.prefix + table]
                capacity = props.get("gridProperties", {}).get("rowCount", 1000)
                if count > capacity:
                    growth.append({"updateSheetProperties": {"properties": {"sheetId": props["sheetId"], "gridProperties": {"rowCount": max(count, capacity * 2)}}, "fields": "gridProperties.rowCount"}})
            if growth:
                self.request("POST", ":batchUpdate", json={"requests": growth})
            if updates:
                # RAW keeps source text starting with '=' as literal text.
                self.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": updates})
            if any(key.startswith("Handelsparameter:") for key in accepted):
                self.request("POST", ":batchUpdate", json={"requests": [{"autoResizeDimensions": {"dimensions": {
                    "sheetId": properties[self.prefix + "Handelsparameter"]["sheetId"], "dimension": "ROWS",
                    "startIndex": 0, "endIndex": existing["Handelsparameter"][1]}}}]})
            return {"accepted": accepted, "synced": len(accepted)}

    def read(self, table, offset=0, limit=100, filters=None, *, scan_cursor: str | None = None,
             start: str | None = None, end: str | None = None, time_column: str | None = None):
        with self.lock:
            headers = HEADERS[table]
            filters = dict(filters or {})
            if any(key not in headers for key in filters):
                raise AqlaError("Unknown journal filter column. Valid named columns: " + ", ".join(headers))
            bounds = None
            metadata = {}
            if start is not None or end is not None or time_column is not None:
                from aqla_agent.tool_queries import aware_time
                if start is None or end is None:
                    raise AqlaError("Journal time range requires aware start and end, with exclusive end")
                time_column = time_column or next((name for name in headers if name.endswith("(UTC)")), None)
                if time_column not in headers or not time_column.endswith("(UTC)"):
                    raise AqlaError("Choose an existing journal UTC time column")
                bounds = (aware_time(start), aware_time(end))
                if bounds[0] >= bounds[1]:
                    raise AqlaError("Journal time range requires start < end")
                metadata = {"time_range": {"start": start, "end": end, "column": time_column}, "undated_rows_excluded": 0}
            filtered = bool(filters) or bounds is not None
            cursor = None
            if scan_cursor is not None:
                if offset != 0 or not filtered:
                    raise AqlaError("A journal scan cursor requires offset 0 and nonempty filters")
                if not 1 <= limit <= 500:
                    raise AqlaError("Journal page limit must be between 1 and 500")
                cursor = _decode_scan_cursor(scan_cursor)
            try:
                properties = self.layout(create=False)
                found, _ = self.existing([table], properties)[table]
            except AqlaError as error:
                if cursor is not None and str(error).startswith(("This Google spreadsheet", "Google Sheets journal", "Duplicate journal IDs")):
                    raise _stale_scan_cursor() from None
                raise
            items = list(found.items())
            start = 0
            binding = index_digest = None
            if filtered:
                binding = _scan_digest({"spreadsheet": self.settings.spreadsheet_id, "scope": self.scope,
                                        "scope_key": self.scope_key, "prefix": self.prefix,
                                        "sheet_id": properties[self.prefix + table]["sheetId"], "table": table,
                                        "headers": [*headers, *META_HEADERS], "filters": filters,
                                        **({"time_range": metadata["time_range"]} if bounds else {})})
                index_digest = _scan_digest([[identity, index, *meta] for identity, (index, meta) in items])
                if cursor is not None:
                    if cursor["binding"] != binding or cursor["index"] != index_digest:
                        raise _stale_scan_cursor()
                    start = cursor["next"]
                    if start >= len(items):
                        raise AqlaError("Invalid journal scan cursor position; restart at offset 0")
            selected = items[start:] if filtered else items[offset:offset + limit]
            loaded = self.bodies(table, [item[1][0] for item in selected], [MAX_BODY_REQUESTS])
            if cursor is not None:
                for identity, (index, meta) in selected:
                    cells = loaded.get(index)
                    if cells is None:
                        break
                    if str(cells[0]) != identity or _scan_digest(cells[len(headers):len(headers) + 2]) != _scan_digest(meta):
                        raise _stale_scan_cursor()
            rows = []
            invalid = matched = scanned = 0
            for identity, (index, _) in selected:
                cells = loaded.get(index)
                if cells is None:
                    break
                if str(cells[0]) != identity:
                    raise AqlaError("Journal row IDs moved during reading; retry after sorting finishes")
                scanned += 1
                row = dict(zip(headers, cells[:len(headers)], strict=True))
                valid = fingerprint(cells[:len(headers)]) == cells[len(headers)]
                if not valid:
                    invalid += 1
                in_range = True
                if bounds is not None:
                    try:
                        timestamp = aware_time(str(row[time_column]))
                        in_range = bounds[0] <= timestamp < bounds[1]
                    except AqlaError:
                        metadata["undated_rows_excluded"] += 1
                        in_range = False
                if in_range and all(str(row[key]) == value for key, value in filters.items()):
                    if not filtered or offset <= matched < offset + limit:
                        rows.append({**row, "_verified": valid})
                    matched += 1
                    if cursor is not None and len(rows) == limit:
                        break
            complete = scanned == len(selected)
            if cursor is not None:
                return {"table": table, "rows": rows, "total": None, "total_complete": False,
                        "unverified_rows": invalid, "unverified_rows_scope": "scan", "scanned_rows": scanned,
                        "matched_rows_scanned": matched, "complete": complete, "scan_complete": complete,
                        "scan_truncated": not complete, "next_offset": None,
                        "next_scan_cursor": None if complete else _encode_scan_cursor(binding, index_digest, start + scanned), **metadata}
            total = matched if filtered and complete else None if filtered else len(found)
            next_offset = offset + len(rows) if (matched if filtered else len(found)) > offset + len(rows) else None
            if filtered and not complete and offset > matched:
                raise AqlaError("Filtered offset exceeds the scanned prefix; start at offset 0, follow next_offset, then next_scan_cursor")
            result = {"table": table, "rows": rows, "total": total, "total_complete": not filtered or complete,
                    "unverified_rows": invalid, "unverified_rows_scope": "scan" if filtered else "page",
                    "scanned_rows": scanned, "matched_rows_scanned": matched, "complete": complete,
                    "scan_truncated": not complete, "next_offset": next_offset, **metadata}
            if filtered:
                result.update(scan_complete=complete, next_scan_cursor=(
                    _encode_scan_cursor(binding, index_digest, scanned) if not complete and next_offset is None else None))
            return result

    def statistics(self, group_by="gesamt"):
        with self.lock:
            properties = self.layout(create=False)
            found, _ = self.existing(["Trades"], properties)["Trades"]
            loaded = self.bodies("Trades", [item[0] for item in found.values()], [MAX_BODY_REQUESTS])
            headers = HEADERS["Trades"]
            groups = defaultdict(lambda: {"trades": 0, "closed": 0, "wins": 0, "losses": 0, "break_even": 0, "net_before_funding_usd": 0.0, "incomplete_pnl": 0})
            unverified = 0
            scopes = {(str(dict(zip(headers, cells[:len(headers)], strict=True)).get("Währung") or "UNKNOWN"),
                       str(dict(zip(headers, cells[:len(headers)], strict=True)).get("Netzwerk") or "UNKNOWN")) for cells in loaded.values()}
            for identity, (index, _) in found.items():
                cells = loaded.get(index)
                if cells is None:
                    break
                if str(cells[0]) != identity:
                    raise AqlaError("Journal row IDs moved during analysis; retry after sorting finishes")
                if fingerprint(cells[:len(headers)]) != cells[len(headers)]:
                    unverified += 1
                    continue
                row = dict(zip(headers, cells[:len(headers)], strict=True))
                label = str(row[group_by]) if group_by != "gesamt" else "gesamt"
                # Keep per-currency groups; no USDT/USDC conversion or cross-network aggregation.
                scope = (row.get("Währung") or "UNKNOWN", row.get("Netzwerk") or "UNKNOWN")
                label = label if len(scopes) == 1 else label + " · " + " · ".join(scope)
                group = groups[label]
                group["currency"], group["network"] = scope
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
            return {"groups": dict(groups), "unverified_rows_excluded": unverified, "funding_included": False, "read_only_analysis": True,
                    "complete": len(loaded) == len(found), "scan_truncated": len(loaded) < len(found),
                    "scanned_trades": len(loaded), "total_trades": len(found)}

    def close(self):
        if self.session is not None:
            self.session.close()
