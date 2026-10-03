import copy
import json
import re
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.models import AqlaError, GoogleSheetsSettings, Settings, SourceEvent, now_ms
from aqla_hyperliquid_mcp.state import State
from aqla_sheets_mcp.client import PREFIX, SheetsClient, column
from aqla_sheets_mcp.journal import HEADERS, JournalRecord, acknowledge, batch, fingerprint, project, record
from aqla_sheets_mcp.server import create_server


class Google:
    """Sheets boundary: row indexes, RAW values, atomic batches and lost replies."""
    def __init__(self):
        self.properties, self.tables, self.metadata = {}, {}, []
        self.calls = []
        self.writes = 0
        self.lose_reply = False
        self.fail = False
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if self.fail:
            raise OSError("Temporary Google outage")
        if method == "GET" and url.endswith("/values:batchGet"):
            ranges = []
            for query in kwargs["params"]["ranges"]:
                title, first_col, first_row, last_col, last_row = self.coordinates(query)
                rows = copy.deepcopy(self.tables.get(title, [])[first_row - 1:last_row])
                rows = [row[first_col - 1:last_col] for row in rows]
                for row in rows:
                    while row and row[-1] == "":
                        row.pop()
                while rows and not rows[-1]:
                    rows.pop()
                ranges.append({"range": query, "values": rows})
            data = {"valueRanges": ranges}
        elif method == "GET":
            data = {"sheets": [{"properties": copy.deepcopy(value)} for value in self.properties.values()], "developerMetadata": copy.deepcopy(self.metadata)}
        elif url.endswith("/values:batchUpdate"):
            assert kwargs["json"]["valueInputOption"] == "RAW"
            for update in kwargs["json"]["data"]:
                title, first_col, first_row, last_col, last_row = self.coordinates(update["range"])
                rows = self.tables[title]
                while len(rows) < last_row:
                    rows.append([])
                for offset, cells in enumerate(update["values"]):
                    row = rows[first_row - 1 + offset]
                    row.extend([""] * max(0, last_col - len(row)))
                    row[first_col - 1:first_col - 1 + len(cells)] = copy.deepcopy(cells)
            self.writes += 1
            if self.lose_reply:
                self.lose_reply = False
                raise OSError("Response lost after the write was accepted")
            data = {}
        else:
            for operation in kwargs["json"]["requests"]:
                if "createDeveloperMetadata" in operation:
                    self.metadata.append(operation["createDeveloperMetadata"]["developerMetadata"])
                elif "addSheet" in operation:
                    props = copy.deepcopy(operation["addSheet"]["properties"])
                    self.properties[props["title"]] = props
                    self.tables[props["title"]] = []
                elif "updateCells" in operation:
                    update = operation["updateCells"]
                    title = next(title for title, props in self.properties.items() if props["sheetId"] == update["start"]["sheetId"])
                    self.tables[title] = [[cell["userEnteredValue"]["stringValue"] for cell in update["rows"][0]["values"]]]
                elif "updateSheetProperties" in operation:
                    update = operation["updateSheetProperties"]["properties"]
                    props = next(props for props in self.properties.values() if props["sheetId"] == update["sheetId"])
                    props["gridProperties"].update(update["gridProperties"])
            data = {}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    @staticmethod
    def coordinates(query):
        title, first_col, first_row, last_col, last_row = re.fullmatch(r"'([^']+)'!([A-Z]+)(\d+):([A-Z]+)(\d+)", query).groups()
        def index(letters):
            value = 0
            for letter in letters:
                value = value * 26 + ord(letter) - ord("A") + 1
            return value
        return title, index(first_col), int(first_row), index(last_col), int(last_row)

    def close(self):
        self.closed = True


@pytest.fixture
def sheets(tmp_path):
    selected = GoogleSheetsSettings(enabled=True, spreadsheet_id="ci-spreadsheet-" + "0" * 25, credentials_file=tmp_path / "google.secret")
    google = Google()
    client = SheetsClient(selected, session=google, scope="testnet:ci-account")
    return selected, google, client


def test_lost_sheet_reply_retries_by_id_without_appending_twice(sheets):
    _, google, client = sheets
    row = record("Regeln", "aktuell", "testnet", "ci-account", 1, False, "[]", "null")
    google.lose_reply = True
    with pytest.raises(AqlaError, match="remain pending"):
        client.sync([row])
    result = client.sync([row])
    assert result["accepted"] == {"Regeln:aktuell": row["fingerprint"]}
    assert google.writes == 1
    assert len(google.tables[PREFIX + "Regeln"]) == 2
    assert all((PREFIX + name) in google.tables for name in HEADERS)


def test_sorted_rows_updates_growth_and_old_revisions(sheets):
    _, google, client = sheets
    first = {**record("Regeln", "first", "testnet", "ci-account", 1, False, "[]", "null"), "revision": 1}
    second = {**record("Regeln", "second", "testnet", "ci-account", 2, False, "[]", "null"), "revision": 2}
    client.sync([first, second])
    google.tables[PREFIX + "Regeln"][1:] = list(reversed(google.tables[PREFIX + "Regeln"][1:]))
    newer = copy.deepcopy(first)
    newer["values"][3] = 3
    newer["revision"] = 3
    newer["fingerprint"] = fingerprint(newer["values"])
    google.properties[PREFIX + "Regeln"]["gridProperties"]["rowCount"] = 3
    third = record("Regeln", "third", "testnet", "ci-account", 1, True, "[]", "null")
    client.sync([newer, third])
    assert google.properties[PREFIX + "Regeln"]["gridProperties"]["rowCount"] >= 4
    assert client.sync([first])["accepted"] == {}
    rows = client.read("Regeln", filters={"ID": "first"})["rows"]
    assert rows[0]["Revision"] == 3
    with pytest.raises(AqlaError, match="Unknown journal filter"):
        client.read("Regeln", filters={"anything": "1"})
    google.tables[PREFIX + "Regeln"].append(copy.deepcopy(google.tables[PREFIX + "Regeln"][1]))
    with pytest.raises(AqlaError, match="Duplicate"):
        client.read("Regeln")


def test_literal_source_text_and_malformed_rows_are_not_executed(sheets):
    _, google, client = sheets
    row = record("Signale", "signal", "channel", 1, "now", "", "", False, "pending", "", "", "", "", "", '=HYPERLINK("invalid")', "source")
    client.sync([row])
    assert client.read("Signale")["rows"][0]["Text"] == row["values"][-2]
    assert client.read("Signale")["rows"][0]["_verified"] is True
    google.tables[PREFIX + "Signale"][1][7] = "edited-by-user"
    assert client.read("Signale")["unverified_rows"] == 1
    client.sync([row])
    assert client.read("Signale")["unverified_rows"] == 0
    wrong = {**row, "fingerprint": "0" * 64}
    with pytest.raises(ValueError):
        JournalRecord.model_validate(wrong)
    google.tables[PREFIX + "Signale"][0][0] = "changed header"
    with pytest.raises(AqlaError, match="headers changed"):
        client.read("Signale")


def test_read_tools_never_initialize_or_claim_a_foreign_sheet(sheets):
    _, google, client = sheets
    with pytest.raises(AqlaError, match="not been initialized"):
        client.read("Trades")
    assert all(method == "GET" for method, _, _ in google.calls)
    google.metadata = [{"metadataKey": "aqla.scope", "metadataValue": "mainnet:foreign-account"}]
    with pytest.raises(AqlaError, match="different aqla"):
        client.sync([record("Regeln", "aktuell", "testnet", "ci-account", 1, False, "[]", "null")])
    assert google.writes == 0


def test_durable_queue_survives_restart_and_ignores_stale_ack(tmp_path, sheets):
    selected, google, client = sheets
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=tmp_path / "state.json", google_sheets=selected)
    with State(settings) as state:
        old = batch(state.snapshot())
        accepted = client.sync(old["records"])["accepted"]
        state.update(lambda data: data.update(paused=True))
        state.update(lambda data: acknowledge(data, accepted))
        assert "Regeln:aktuell" in state.snapshot()["journal_pending"]
        assert state.snapshot()["journal_history"]
        google.fail = True
        with pytest.raises(AqlaError):
            client.sync(batch(state.snapshot())["records"])
        assert state.snapshot()["paused"]
    google.fail = False
    with State(settings) as restored:
        accepted = client.sync(batch(restored.snapshot(), 500)["records"])["accepted"]
        restored.update(lambda data: acknowledge(data, accepted))
        assert restored.snapshot()["journal_pending"] == {}
        assert restored.snapshot()["journal_history"] == {}


@pytest.mark.asyncio
async def test_trade_fills_fees_profit_and_statistics_are_actual_facts(setup, signal, sheets):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    for target in [row for row in trade["orders"] if row["kind"] == "tp"]:
        fake.fill(target["cloid"], target["quantity"], target["price"])
    await trading.recover()
    _, google, client = sheets
    records = project(state.snapshot())
    client.sync(list(records.values()))
    rows = client.read("Trades")["rows"]
    assert rows[0]["Status"] == "closed"
    assert rows[0]["Gefüllt"] > 0 and rows[0]["Offen"] == 0
    assert rows[0]["Risiko geplant USD"] > 0
    assert rows[0]["Risiko reserviert USD"] == 0
    orders = client.read("Orders")["rows"]
    assert any(row["Art"] == "entry" and row["Status Börse"] == "filled" for row in orders)
    assert all(row["Status Börse"] != "open" for row in orders)
    result = client.statistics("Quelle")
    assert result["groups"]["channel"]["wins"] == 1
    assert result["groups"]["channel"]["win_rate_known_percent"] == 100
    assert result["funding_included"] is False
    assert len(client.read("Fills")["rows"]) == 3
    google.tables[PREFIX + "Trades"][1][4] = "forged"
    assert client.statistics()["unverified_rows_excluded"] == 1


@pytest.mark.asyncio
async def test_actual_sheets_mcp_registration_reading_and_internal_auth(setup, sheets):
    settings, *_ = setup
    selected, _, client = sheets
    settings.google_sheets = selected
    server = create_server(settings, client=client)
    assert {tool.name for tool in await server.list_tools()} == {"journal_status", "journal_read", "journal_statistics", "sync_internal"}
    with pytest.raises(ToolError, match="denied"):
        await server.call_tool("sync_internal", {"token": "untrusted-source", "records": []})
    row = record("Regeln", "aktuell", "testnet", "ci-account", 1, False, "[]", "null")
    result = await server.call_tool("sync_internal", {"token": settings.integration_key_file.read_text(), "records": [row]})
    assert result.is_error is not True
    assert (await server.call_tool("journal_status", {})).is_error is not True
    assert (await server.call_tool("journal_read", {"table": "Regeln"})).is_error is not True
    assert (await server.call_tool("journal_statistics", {})).is_error is not True
    settings.google_sheets.enabled = False
    with pytest.raises(ToolError, match="disabled"):
        await server.call_tool("journal_read", {"table": "Trades"})
    client.close()


def test_google_credentials_error_is_safe_and_cell_limits_are_respected(sheets):
    selected, _, _ = sheets
    with pytest.raises(AqlaError, match="credentials"):
        SheetsClient(selected).layout()
    long = "😀" * 50000
    row = record("Regeln", "large", "testnet", "ci-account", 1, False, "[]", long)
    assert len(row["values"][-1].encode("utf-16-le")) < 100000
    assert row["values"][-1].endswith("[gekürzt]")
    assert column(27) == "AA"


def test_official_google_auth_accepts_ephemeral_service_account_json(sheets):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    selected, _, _ = sheets
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    selected.credentials_file.write_text(json.dumps({"type": "service_account", "project_id": "ci-only", "private_key_id": "ephemeral-test", "private_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), "client_email": "ci@ci-only.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}))
    client = SheetsClient(selected)
    session = client._session()
    assert session.credentials.service_account_email == "ci@ci-only.iam.gserviceaccount.com"
    assert session.credentials.scopes == ["https://www.googleapis.com/auth/spreadsheets"]
    client.close()


def test_unicode_batches_respect_transport_budgets_and_partial_delivery(setup, sheets):
    _, state, *_ = setup
    data = state.snapshot()
    data["journal_history"] = {}
    data["journal_pending"] = {}
    for index in range(15):
        row = record("Ereignisse", str(index), "now", index, "Signale", "source", "😀" * 25000, "😀" * 25000)
        data["journal_history"][str(index)] = row
        data["journal_pending"]["Ereignisse:" + str(index)] = row["fingerprint"]
    selected = batch(data, 500)["records"]
    assert 0 < len(selected) < 15
    assert len(json.dumps(selected, ensure_ascii=False).encode()) < 1_500_000
    _, google, client = sheets
    accepted = client.sync(selected)["accepted"]
    assert 0 < len(accepted) < len(selected)
    for _, url, kwargs in google.calls:
        if url.endswith("/values:batchUpdate"):
            assert len(json.dumps(kwargs["json"]).encode()) < 2_000_000


@pytest.mark.asyncio
async def test_backend_preview_rejection_is_tracked_and_cleared_after_success(setup, signal):
    from aqla_hyperliquid_mcp.server import close_server
    from aqla_hyperliquid_mcp.server import create_server as core_server

    settings, state, _, exchange, _ = setup
    source, interpretation = signal
    server = core_server(settings, state=state, exchange=exchange)
    args = {"event_id": source.event_id, "interpretation": interpretation.model_dump(mode="json"), "context_auth": server.aqla_authority.sign("source", "native")}
    state.update(lambda data: data.update(paused=True))
    with pytest.raises(ToolError, match="paused"):
        await server.call_tool("trade_preview", args)
    assert "paused" in state.snapshot()["events"][source.event_id]["last_error"]
    state.update(lambda data: data.update(paused=False))
    assert (await server.call_tool("trade_preview", args)).is_error is not True
    assert "last_error" not in state.snapshot()["events"][source.event_id]
    await close_server(server)


def test_pending_source_rows_are_not_pruned_before_sheet_delivery(setup, tmp_path):
    _, state, _, exchange, trading = setup
    exchange.settings.max_events = 100
    state.journal_enabled = True
    for identity in range(1, 111):
        source = SourceEvent(chat_id="channel", message_id=identity, sent_at=now_ms(), text="Report", historical=True)
        trading.ingest(source)
    assert len(state.snapshot()["events"]) == 110
    assert all("Signale:" + identity in state.snapshot()["journal_pending"] for identity in state.snapshot()["events"])
