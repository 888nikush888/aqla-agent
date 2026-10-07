"""Idempotent monthly analysis tabs. RAW data cannot become spreadsheet formulas."""
from __future__ import annotations

from aqla_agent.monthly import SUM_LABEL, month_bounds, stamp
from aqla_hyperliquid_mcp.models import AqlaError, digest

HEADERS = ["Trade-ID", "Entry (Berlin)", "Schließung (Berlin)", "Quelle", "Quellen-ID", "Markt", "Richtung",
           "Börse", "Netzwerk", "Profil", "Positionswert", "Margin-Hebel", "Anfangsmargin", "Brutto-PnL",
           "Gebühren", "Funding", "Netto-PnL", "Währung", "ROE %", "Vollständig", "Datenlücken"]
KEYS = ["trade_id", "entry_at", "closed_at", "source_name", "source_id", "symbol", "side", "exchange", "network",
        "profile", "entry_notional", "margin_leverage", "initial_margin", "gross_pnl", "fees", "funding", "net_pnl",
        "currency", "roe_percent", "complete", "issues"]


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


def publish(client, report):
    month_bounds(report["month"])
    content = {key: value for key, value in report.items() if key not in ("fingerprint", "revision", "url", "published_current_revision")}
    if digest(content) != report["fingerprint"] or not isinstance(report["revision"], int) or report["revision"] < 1:
        raise AqlaError("Monthly report content/revision does not match its immutable fingerprint")
    if client.prefix != "aqla Mainnet ":
        raise AqlaError("Mainnet monthly reports require the separate Mainnet Sheets namespace")
    values, header = table_values(report)
    if len(values) > 10_000:
        raise AqlaError("Monthly report exceeds the bounded sheet publication budget")
    title = client.prefix + "Monatsbericht " + report["month"]
    with client.lock:
        client.layout(create=True)  # Enforces the existing account/network scope.
        raw = client.request("GET", params={"fields": "sheets.properties"})
        props = [row["properties"] for row in raw["sheets"] if row["properties"]["title"] == title]
        if len(props) > 1:
            raise AqlaError("Duplicate monthly worksheet title")
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
        total_rows = max(prop["gridProperties"]["rowCount"], len(values))
        if total_rows > 20_000:
            raise AqlaError("Monthly sheet grid exceeds the bounded verification budget")
        if total_rows > prop["gridProperties"]["rowCount"]:
            client.request("POST", ":batchUpdate", json={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": total_rows}}, "fields": "gridProperties.rowCount"}}]})
        # Blank the old tail in the same bounded writes; a shrinking revision leaves no stale rows.
        padded = values + [[""] * len(HEADERS) for _ in range(total_rows - len(values))]
        from .client import column
        last = column(len(HEADERS))
        for offset in range(0, total_rows, 100):
            chunk = padded[offset:offset + 100]
            client.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": [{
                "range": f"'{title}'!A{offset + 1}:{last}{offset + len(chunk)}", "values": chunk}]})
        # Verify every written cell, not merely a returned digest or a successful first chunk.
        checked = client.request("GET", "/values:batchGet", params={"ranges": [f"'{title}'!A1:{last}{total_rows}"]})
        returned = checked["valueRanges"][0].get("values", [])
        normalized = [(row + [""] * len(HEADERS))[:len(HEADERS)] for row in returned]
        normalized += [[""] * len(HEADERS) for _ in range(total_rows - len(normalized))]
        if normalized != padded:
            raise AqlaError("Monthly Sheets publication is not completely confirmed; no notification yet")
        client.request("POST", ":batchUpdate", json={"requests": [
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": header, "endRowIndex": header + 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": .85, "green": .93, "blue": .98}}}, "fields": "userEnteredFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 3},
                                           "properties": {"pixelSize": 210}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 20, "endIndex": 21},
                                           "properties": {"pixelSize": 440}, "fields": "pixelSize"}},
            {"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat.wrapStrategy"}}
        ]})
        return {"month": report["month"], "fingerprint": report["fingerprint"], "revision": report["revision"],
                "url": f"https://docs.google.com/spreadsheets/d/{client.settings.spreadsheet_id}/edit#gid={sheet_id}"}
