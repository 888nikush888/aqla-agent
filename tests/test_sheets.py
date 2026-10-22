import copy
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.models import AqlaError, GoogleSheetsSettings, Policy, Settings, SourceEvent, now_ms
from aqla_hyperliquid_mcp.state import State
from aqla_sheets_mcp.client import PREFIX, SheetsClient, column
from aqla_sheets_mcp.journal import HEADERS, JournalRecord, acknowledge, batch, fingerprint, project, record
from aqla_sheets_mcp.parameters import PARAMETERS
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
                    if not self.tables[title]:
                        self.tables[title].append([])
                    self.tables[title][0] = [cell["userEnteredValue"]["stringValue"] for cell in update["rows"][0]["values"]]
                elif "insertDimension" in operation:
                    dimensions = operation["insertDimension"]["range"]
                    title = next(title for title, props in self.properties.items() if props["sheetId"] == dimensions["sheetId"])
                    extra = dimensions["endIndex"] - dimensions["startIndex"]
                    for row in self.tables[title]:
                        row[dimensions["startIndex"]:dimensions["startIndex"]] = [""] * extra
                    self.properties[title]["gridProperties"]["columnCount"] += extra
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
    assert client.read("Signale")["rows"][0]["Text"] == row["values"][HEADERS["Signale"].index("Text")]
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


def test_restart_preserves_only_unacknowledged_records_without_full_refresh(tmp_path, sheets):
    selected, _, client = sheets
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=tmp_path / "state.sqlite3", google_sheets=selected)
    with State(settings) as state:
        state.acknowledge_journal(client.sync(state.journal_batch(500)["records"])["accepted"])
        assert not state.snapshot()["journal_pending"]
        state.update(lambda data: data.update(paused=True))
        pending = state.snapshot()["journal_pending"]
        assert {key for key in pending if not key.startswith("Ereignisse:")} == {"Regeln:aktuell", "Handelsparameter:paused"}
        # Sheets accepted the write, but its local ACK was interrupted.
        client.sync(state.journal_batch(500)["records"])
    with State(settings) as state:
        assert state.snapshot()["journal_pending"] == pending
        state.acknowledge_journal(client.sync(state.journal_batch(500)["records"])["accepted"])
    with State(settings) as state:
        assert state.journal_batch(500) == {"records": [], "pending": 0}


@pytest.mark.parametrize("change", ["disabled_changes", "projection_change", "destination"])
def test_restart_catches_offline_changes_new_projection_and_destination(tmp_path, sheets, monkeypatch, change):
    from aqla_sheets_mcp import journal

    selected, _, client = sheets
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=tmp_path / "state.sqlite3", google_sheets=selected)
    with State(settings) as state:
        state.acknowledge_journal(client.sync(state.journal_batch(500)["records"])["accepted"])
        if change == "disabled_changes":
            state.journal_enabled = False
            state.update(lambda data: data.update(paused=True))
        assert not state.snapshot()["journal_pending"]
    if change == "destination":
        settings.google_sheets = selected.model_copy(update={"spreadsheet_id": "new-spreadsheet-" + "1" * 25})
    elif change == "projection_change":
        original_parameters = journal.parameter_values
        def changed_parameters(data):
            rows = list(original_parameters(data))
            rows[0] = [*rows[0]]
            rows[0][3] = "changed projection"
            return rows
        monkeypatch.setattr(journal, "parameter_values", changed_parameters)
    with State(settings) as state:
        pending = state.snapshot()["journal_pending"]
        if change == "destination":
            assert pending == {key: row["fingerprint"] for key, row in project(state.snapshot()).items()}
        elif change == "disabled_changes":
            assert set(pending) == {"Regeln:aktuell", "Handelsparameter:paused"}
        else:
            assert len(pending) == 1 and next(iter(pending)).startswith("Handelsparameter:")


def test_replaced_order_lost_sheet_reply_survives_restart_and_never_duplicates(setup, completed_journal_trade, sheets):
    settings, state, *_ = setup
    selected, google, client = sheets
    snapshot, trade = completed_journal_trade
    state.update(lambda data: data.update(trades=snapshot["trades"]))
    state.close()
    settings.google_sheets = selected
    with State(settings) as state:
        state.acknowledge_journal(client.sync(state.journal_batch(500)["records"])["accepted"])
        old = project(state.snapshot())["Orders:" + trade["entry_cloid"]]
        state.update(lambda data: data["trades"][trade["trade_id"]].update(entry_cloid="replacement-entry"))
        google.lose_reply = True
        with pytest.raises(AqlaError):
            client.sync(state.journal_batch(500)["records"])
        expected_pending = state.snapshot()["journal_pending"]
    with State(settings) as state:
        assert state.snapshot()["journal_pending"] == expected_pending
        state.acknowledge_journal(client.sync(state.journal_batch(500)["records"])["accepted"])
        assert not state.snapshot()["journal_pending"]
        assert state.snapshot()["journal_order_archive"][old["record_id"]] == old
        rows = client.read("Orders")["rows"]
        assert len([row for row in rows if row["ID"] == old["record_id"]]) == 1
        assert all(row["_verified"] for row in rows)


@pytest.mark.asyncio
async def test_trade_fills_fees_profit_and_statistics_are_actual_facts(setup, signal, sheets, monkeypatch):
    _, state, fake, exchange, trading = setup
    original = exchange.info
    async def quoted_fills(kind, **arguments):
        result = await original(kind, **arguments)
        return [{**row, "feeToken": "USDC"} for row in result] if kind == "userFillsByTime" else result
    monkeypatch.setattr(exchange, "info", quoted_fills)
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


@pytest.fixture
def completed_journal_trade(setup):
    from test_monthly import trade

    snapshot = setup[1].snapshot()
    row = trade()
    row["plan"].update(entry_price="101", quantity="2", entry_limit="101", stop_loss="90",
        take_profits=["105", "110"], risk_usdc="22", source_refs=[], account=snapshot["account"],
        network=snapshot["network"], policy={"risk_percent": "1"})
    row.update(entry_vwap="101", filled_quantity="2", remaining_quantity="0", stop_price="90", reserved_risk="0", orders=[])
    snapshot["trades"] = {row["trade_id"]: row}
    return snapshot, row


@pytest.mark.parametrize("damage", ["partial_exit", "manual", "mixed", "fee_currency", "entry_quantity", "entry_open", "open_partial"])
def test_incomplete_journal_outcomes_never_count_as_known_wins(completed_journal_trade, sheets, damage):
    snapshot, row = completed_journal_trade
    if damage in ("partial_exit", "open_partial"):
        row["fill_events"].pop(list(row["fill_events"])[-1])
        row.pop("closed_at")
        if damage == "open_partial":
            row["status"] = "open"
    elif damage == "manual":
        row["manual_closure"] = {"pnl_complete": False}
    elif damage == "mixed":
        row["external_conflict"] = "Mixed position"
    elif damage == "fee_currency":
        next(iter(row["fill_events"].values()))["feeToken"] = None
    elif damage == "entry_quantity":
        row.pop("entry_fill_quantity_confirmed")
    else:
        row["order_observations"]["entry"]["status"] = "open"
    exported = project(snapshot)["Trades:" + row["trade_id"]]
    cells = dict(zip(HEADERS["Trades"], exported["values"], strict=True))
    assert cells["Netto vor Funding USD"] == ""
    assert isinstance(cells["PnL realisiert vor Gebühren/Funding"], float)
    client = sheets[2]
    client.sync([exported])
    group = client.statistics()["groups"]["gesamt"]
    assert group["wins"] == group["losses"] == group["break_even"] == 0
    assert group["incomplete_pnl"] == (0 if damage == "open_partial" else 1)
    assert group["win_rate_known_percent"] is None


def test_complete_journal_outcome_does_not_require_funding(completed_journal_trade, sheets):
    snapshot, row = completed_journal_trade
    row.pop("funding_coverage")
    row.pop("funding_events")
    exported = project(snapshot)["Trades:" + row["trade_id"]]
    cells = dict(zip(HEADERS["Trades"], exported["values"], strict=True))
    assert cells["Netto vor Funding USD"] == 15.1
    sheets[2].sync([exported])
    group = sheets[2].statistics()["groups"]["gesamt"]
    assert group["wins"] == 1 and group["incomplete_pnl"] == 0


@pytest.mark.asyncio
async def test_verified_late_fees_refresh_journal_and_statistics_without_mutating_fills(setup, completed_journal_trade, sheets, monkeypatch):
    from aqla_agent.reporting import Reporting
    from aqla_hyperliquid_mcp.models import AccountSnapshot, Interpretation, Market, TradePlan
    from aqla_hyperliquid_mcp.rules import make_plan

    settings, state, fake, exchange, trading = setup
    _, row = completed_journal_trade
    created = row["plan"]["created_at"]
    plan = make_plan(Interpretation(kind="new_signal", source_refs=["historical-owned-source"], symbol="BTC",
        side="long", entry="limit", entry_prices=["101"], stop_loss="90", take_profits=["105", "110"]),
        Market.model_validate(row["plan"]["market"]),
        AccountSnapshot(equity="10000", available_margin="10000", observed_at=created),
        Policy(sizing_mode="fixed_notional", fixed_notional_usdc="202", leverage=20),
        1, created, (settings.network, settings.account.lower()), stamp=created)
    row.update(plan=plan.model_dump(mode="json"), trade_id=plan.trade_id, funding_coverage={})
    assert TradePlan.model_validate(row["plan"]) == plan
    observed = copy.deepcopy(list(row["fill_events"].values()))
    for fill in row["fill_events"].values():
        fill["fee"] = fill["feeToken"] = None
    before = copy.deepcopy(row["fill_events"])
    state.journal_enabled = True
    state.update(lambda data: data["trades"].__setitem__(plan.trade_id, row))
    initial = project(state.snapshot())
    assert all(record["values"][HEADERS["Fills"].index("Gebühren-Token")] == ""
               for record in initial.values() if record["table"] == "Fills")
    fake.fills = observed
    reads = []
    def transport(request):
        assert request.url.path == "/info"
        kind = json.loads(request.content)["type"]
        reads.append(kind)
        return httpx.Response(200, json=[]) if kind == "userFunding" else fake.handle(request)
    async with httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(exchange, "client", client)
        result = await Reporting(state, SimpleNamespace(engines={"hyperliquid": trading})).refresh(plan.trade_id)
    assert result["complete"] and reads == ["userFillsByTime", "userFunding"] and not fake.actions
    saved = state.snapshot()
    assert saved["trades"][plan.trade_id]["fill_events"] == before
    exported = project(saved)
    key = "Trades:" + plan.trade_id
    assert exported[key]["fingerprint"] != initial[key]["fingerprint"]
    assert saved["journal_pending"][key] == exported[key]["fingerprint"]
    cells = dict(zip(HEADERS["Trades"], exported[key]["values"], strict=True))
    assert cells["Gebühren USD"] == .4 and cells["Netto vor Funding USD"] == 15.1
    assert all(record["values"][HEADERS["Fills"].index("Gebühr")] == .1
               and record["values"][HEADERS["Fills"].index("Gebühren-Token")] == "USDC"
               for record in exported.values() if record["table"] == "Fills")
    saved["trades"][plan.trade_id].pop("funding_coverage")
    saved["trades"][plan.trade_id].pop("funding_events")
    assert project(saved)[key] == exported[key]
    sheets[2].sync([exported[key]])
    group = sheets[2].statistics()["groups"]["gesamt"]
    assert group["wins"] == 1 and group["incomplete_pnl"] == 0 and group["net_before_funding_usd"] == 15.1


@pytest.mark.parametrize("damage", ["fee", "currency", "foreign_currency", "manual", "partial_exit"])
def test_late_fee_evidence_keeps_incomplete_or_foreign_outcomes_unknown(completed_journal_trade, damage):
    from aqla_agent.monthly import fill_identity

    snapshot, row = completed_journal_trade
    row["report_fee_evidence"] = {fill_identity(fill): {"fee": fill["fee"], "feeToken": fill["feeToken"]}
                                  for fill in row["fill_events"].values()}
    for fill in row["fill_events"].values():
        fill["fee"] = fill["feeToken"] = None
    fee = next(iter(row["report_fee_evidence"].values()))
    if damage in ("fee", "currency"):
        fee["fee" if damage == "fee" else "feeToken"] = None
    elif damage == "foreign_currency":
        fee["feeToken"] = "HYPE"
    elif damage == "manual":
        row["manual_closure"] = True
    else:
        row["fill_events"].pop(list(row["fill_events"])[-1])
    before = copy.deepcopy(row["fill_events"])
    exported = project(snapshot)
    cells = dict(zip(HEADERS["Trades"], exported["Trades:" + row["trade_id"]]["values"], strict=True))
    assert cells["Netto vor Funding USD"] == "" and row["fill_events"] == before
    if damage in ("fee", "currency", "foreign_currency"):
        assert cells["Gebühren USD"] == ""
    if damage in ("currency", "foreign_currency"):
        first_fill = next(record for record in exported.values() if record["table"] == "Fills")
        assert first_fill["values"][HEADERS["Fills"].index("Gebühren-Token")] == ("" if damage == "currency" else "HYPE")


def test_restart_requeues_changed_financial_projection_with_stable_ids(setup, completed_journal_trade, sheets):
    settings, state, *_ = setup
    snapshot, row = completed_journal_trade
    key = "Trades:" + row["trade_id"]
    old = project(snapshot)[key]
    next(iter(row["fill_events"].values()))["feeToken"] = None
    state.update(lambda data: data.update(trades=snapshot["trades"], journal_pending={key: old["fingerprint"]}))
    state.close()
    settings.google_sheets = sheets[0]
    with State(settings) as restarted:
        current = next(record for record in restarted.journal_batch(500)["records"] if record["table"] == "Trades")
        assert current["record_id"] == old["record_id"]
        assert current["fingerprint"] != old["fingerprint"]
        assert restarted.snapshot()["journal_pending"][key] == current["fingerprint"]
        sheets[2].sync([old])
        accepted = sheets[2].sync([current])["accepted"]
        assert restarted.acknowledge_journal(accepted)["acknowledged"] == 1
        assert key not in restarted.snapshot()["journal_pending"]
        result = sheets[2].read("Trades")
        assert result["total"] == 1 and result["rows"][0]["Netto vor Funding USD"] == ""


@pytest.mark.asyncio
async def test_actual_sheets_mcp_registration_reading_and_internal_auth(setup, sheets):
    settings, *_ = setup
    selected, _, client = sheets
    settings.google_sheets = selected
    server = create_server(settings, client=client)
    assert {tool.name for tool in await server.list_tools()} == {"journal_status", "journal_read", "journal_statistics", "sync_internal", "publish_monthly_internal", "sync_presentation_internal", "presentation_selection_internal"}
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


@pytest.mark.asyncio
async def test_journal_read_mcp_schema_exposes_bounded_scan_cursor(setup, sheets):
    settings, *_ = setup
    selected, google, client = sheets
    settings.google_sheets = selected
    server = create_server(settings, client=client)
    tool = next(tool for tool in await server.list_tools() if tool.name == "journal_read")
    schema = tool.input_schema
    cursor = schema["properties"]["scan_cursor"]
    assert cursor["default"] is None
    assert {"type": "null"} in cursor["anyOf"]
    assert {"type": "string", "maxLength": 2048} in cursor["anyOf"]
    assert "scan_cursor" not in schema.get("required", [])
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 500
    with pytest.raises(ToolError):
        await server.call_tool("journal_read", {"table": "Trades", "filters": {"Quelle": "channel"}, "scan_cursor": "a" * 2049})
    assert google.calls == []


@pytest.mark.asyncio
async def test_journal_read_mcp_forwards_scoped_cursor_and_rejects_nonzero_offset(setup, sheets, monkeypatch):
    from aqla_hyperliquid_mcp.models import ExecutionSettings

    settings, *_ = setup
    selected, google, client = sheets
    settings.google_sheets = selected
    account = "0x" + "1" * 40
    settings.execution_accounts["hyperliquid:mainnet"] = ExecutionSettings(exchange="hyperliquid", network="mainnet", account=account)
    scoped = SheetsClient(selected, session=google, scope="mainnet:" + account)
    scoped.prefix, scoped.scope_key = "aqla Hyperliquid Mainnet ", "aqla.scope.hyperliquid.mainnet"
    scoped.allowed_tables = {"Trades", "Orders", "Fills", "Vorgänge"}
    scoped.layout()
    expected = [f"scoped-trade-{index:04d}" for index in range(1205)]
    rows = [record("Trades", identity, "channel", "BTC", "long", "closed", "now", 100, 100, 1, 1, 0, 90, "[]", 10, 0, 1, 0, 1, "[]")
            for identity in expected]
    google.tables[scoped.prefix + "Trades"][1:] = [[*row["values"], row["fingerprint"], 1] for row in rows]
    google.properties[scoped.prefix + "Trades"]["gridProperties"]["rowCount"] = len(rows) + 1
    original, calls = SheetsClient.read, []

    def observe_read(self, *args, **kwargs):
        calls.append((self.prefix, args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SheetsClient, "read", observe_read)
    server = create_server(settings, client=client)
    args = {"table": "Trades", "offset": 1200, "filters": {"Quelle": "channel"}, "account_key": "hyperliquid:mainnet"}
    first = json.loads((await server.call_tool("journal_read", args)).content[0].text)
    assert first["rows"] == [] and first["next_scan_cursor"] is not None
    args.update(offset=0, limit=2, scan_cursor=first["next_scan_cursor"])
    second = json.loads((await server.call_tool("journal_read", args)).content[0].text)
    assert [row["ID"] for row in second["rows"]] == expected[1200:1202]
    assert second["total"] is None and second["total_complete"] is False
    assert calls[-1] == (scoped.prefix, ("Trades", 0, 2, {"Quelle": "channel"}), {"scan_cursor": first["next_scan_cursor"]})
    with pytest.raises(ToolError, match="requires offset 0"):
        await server.call_tool("journal_read", {**args, "offset": 1})
    with pytest.raises(ToolError, match="stale_cursor:"):
        await server.call_tool("journal_read", {**args, "account_key": "hyperliquid:testnet"})


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


def test_parameter_page_covers_every_rule_without_activating_defaults(setup):
    _, state, *_ = setup
    data = state.snapshot()
    data["policy"] = None
    data["policy_revision"] = 0
    original = copy.deepcopy(data)
    rows = {row["record_id"]: row for row in project(data).values() if row["table"] == "Handelsparameter"}
    assert set(PARAMETERS) == set(Policy.model_fields) | {"entry_execution", "take_profit_execution"}
    assert len(rows) == len(PARAMETERS) + 6 + 2 * len(data["venue_limits"]) + len(data["profiles"]["backup"]["parameters"]) + 11 + 2 * len(data["account_limits"])
    assert all(rows[name]["values"][3] == "Nicht festgelegt" for name in PARAMETERS)
    assert rows["risk_percent"]["values"][6] == 0.5
    assert rows["policy_selected"]["values"][3] == "Nein"
    assert data == original
    for row in rows.values():
        JournalRecord.model_validate(row)


@pytest.mark.asyncio
async def test_parameter_page_tracks_authenticated_rule_changes_and_stale_ack(setup, sheets, tmp_path):
    from aqla_hyperliquid_mcp.server import close_server
    from aqla_hyperliquid_mcp.server import create_server as core_server
    from aqla_sheets_mcp.journal import seed

    settings, state, fake, exchange, _ = setup
    selected, _, client = sheets
    settings.google_sheets = selected
    state.update(lambda data: data.update(policy=None, policy_revision=0))
    state.journal_enabled = True
    state.update(seed)
    old = client.sync(batch(state.snapshot(), 500)["records"])["accepted"]
    if "setup_password_hash_file" in Settings.model_fields:
        from aqla_agent.setup import write_password_hash

        settings.setup_password_hash_file = tmp_path / "synthetic-setup.json"
        write_password_hash(settings.setup_password_hash_file, "synthetic-only-fixture")
        settings.telegram_owner_id = "12345"
    server = core_server(settings, state=state, exchange=exchange)
    try:
        context_auth = server.aqla_authority.sign("operator", "rules")
        if "setup_password_hash_file" in Settings.model_fields:
            from dataclasses import asdict

            from aqla_agent.setup import SetupBinding

            binding = SetupBinding("12345", "12345", "synthetic-sheets-gateway", "synthetic-sheets-session")
            gate = server._aqla_setup_gate
            generation = gate.bind(binding)
            identity = "b" * 64
            gate.register(binding, identity, None, generation)
            context_auth = server.aqla_authority.sign("operator", "rules", setup={"identity": identity, "binding": asdict(binding)})
        policy = Policy(risk_percent="1.25", sizing_mode="fixed_notional", fixed_notional_usdc="250",
                        leverage=3, margin_mode="cross", take_profit_allocation="custom",
                        take_profit_weights=[60, 40], break_even_after_tp=2,
                        allowed_updates=[], allow_stop_widening=True, cost_reserve_bps="12")
        result = await server.call_tool("control", {"action": "set_policy", "expected_revision": 0,
            "policy": policy.model_dump(mode="json"), "context_auth": context_auth})
        assert result.is_error is not True
        state.update(lambda data: acknowledge(data, old))
        assert "Handelsparameter:leverage" in state.snapshot()["journal_pending"]
        delivered = client.sync(batch(state.snapshot(), 500)["records"])["accepted"]
        state.update(lambda data: acknowledge(data, delivered))
        assert state.snapshot()["journal_pending"] == {}
        rows = {row["ID"]: row for row in client.read("Handelsparameter")["rows"]}
        assert rows["risk_percent"]["Aktueller Wert"] == 1.25
        assert rows["leverage"]["Aktueller Wert"] == 3
        assert rows["leverage"]["Standard bei Regelerstellung"] == 2
        assert rows["fixed_notional_usdc"]["Aktueller Wert"] == 250
        assert rows["margin_mode"]["Aktueller Wert"] == "Cross"
        assert rows["break_even_after_tp"]["Aktueller Wert"] == 2
        assert rows["allow_stop_widening"]["Aktueller Wert"] == "Ja"
        assert rows["allowed_updates"]["Aktueller Wert"] == "Keine"
        assert rows["take_profit_weights"]["Aktueller Wert"] == "60.0, 40.0"
        assert rows["policy_selected"]["Aktueller Wert"] == "Ja"
        assert rows["policy_revision"]["Aktueller Wert"] == 1
        assert all(row["_verified"] for row in rows.values())
        sheets_server = create_server(settings, client=client)
        assert (await sheets_server.call_tool("journal_read", {"table": "Handelsparameter"})).is_error is not True
        assert fake.actions == []
    finally:
        await close_server(server)


def test_parameter_page_migrates_existing_journal_without_overwriting_other_tabs(setup, sheets):
    _, state, *_ = setup
    _, google, client = sheets
    records = list(project(state.snapshot()).values())
    legacy = [row for row in records if row["table"] != "Handelsparameter"]
    client.sync(legacy)
    google.properties.pop(PREFIX + "Handelsparameter")
    google.tables.pop(PREFIX + "Handelsparameter")
    google.properties["Eigene Notizen"] = {"title": "Eigene Notizen", "sheetId": 99,
        "gridProperties": {"rowCount": 100, "columnCount": 2}}
    google.tables["Eigene Notizen"] = [["Meine Notizen"], ["bleiben unverändert"]]
    original = copy.deepcopy(google.tables)
    parameters = [row for row in records if row["table"] == "Handelsparameter"]
    assert len(client.sync(parameters)["accepted"]) == len(parameters)
    assert all(google.tables[title] == values for title, values in original.items())
    assert client.read("Handelsparameter")["total"] == len(parameters)
