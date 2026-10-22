import copy

import pytest
from oracles import journal_02765ac as financial_oracle
from oracles import journal_85aa9d7 as oracle

from aqla_hyperliquid_mcp.models import SourceEvent
from aqla_sheets_mcp import journal


@pytest.mark.parametrize("removed", ["entry", "tp", "close"])
def test_replaced_orders_keep_exact_export_until_and_after_digest_ack(financial_data, removed):
    before = copy.deepcopy(financial_data)
    trade = financial_data["trades"]["trade"]
    identity = trade["entry_cloid"] if removed == "entry" else (trade["orders"] if removed == "tp" else trade["exit_orders"])[0]["cloid"]
    key = "Orders:" + identity
    original = journal.project(before)[key]
    if removed == "entry":
        trade["entry_cloid"] = "new-entry"
    else:
        trade["orders" if removed == "tp" else "exit_orders"] = []
    financial_data["sequence"] += 1
    journal.enqueue_changes(before, financial_data)
    assert financial_data["journal_order_archive"][identity] == original
    assert journal.project(financial_data)[key] == original
    financial_data["journal_pending"] = {key: original["fingerprint"]}
    assert journal.batch(financial_data)["records"] == [{**original, "revision": financial_data["sequence"]}]
    preserved = copy.deepcopy(financial_data)
    assert journal.acknowledge(financial_data, {key: "stale"})["acknowledged"] == 0
    assert financial_data == preserved
    assert journal.acknowledge(financial_data, {key: original["fingerprint"]}) == {"acknowledged": 1, "pending": 0}
    financial_data["profiles"]["standard"]["revision"] += 1
    assert journal.project(financial_data)[key] == original
    assert financial_data["journal_order_archive"][identity] == original
    assert financial_data["trades"] == preserved["trades"] and financial_data["operations"] == before["operations"]


def test_current_order_wins_over_archive_and_refreshes_archive_on_next_replacement(financial_data):
    trade = financial_data["trades"]["trade"]
    identity = trade["entry_cloid"]
    original = journal.project(financial_data)["Orders:" + identity]
    financial_data["journal_order_archive"] = {identity: copy.deepcopy(original)}
    trade["plan"]["quantity"] = "3"
    current = journal.project(financial_data)["Orders:" + identity]
    assert current != original
    financial_data["journal_pending"] = {"Orders:" + identity: current["fingerprint"]}
    assert journal.batch(financial_data)["records"] == [{**current, "revision": financial_data["sequence"]}]
    previous = copy.deepcopy(financial_data)
    trade["entry_cloid"] = "replacement"
    financial_data["sequence"] += 1
    journal.enqueue_changes(previous, financial_data)
    assert financial_data["journal_order_archive"][identity] == current


@pytest.fixture
def financial_data(setup, monkeypatch):
    from test_monthly import trade

    snapshot = setup[1].snapshot()
    for pipeline in snapshot["pipelines"].values():
        for target in pipeline["targets"]:
            target["fallback_exchanges"] = []
    monkeypatch.setattr(journal, "now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(financial_oracle, "now_ms", lambda: 1_800_000_000_000)
    row = trade()
    row["plan"].update(entry_price="101", quantity="2", entry_limit="101", stop_loss="90", take_profits=["105", "110"],
        risk_usdc="22", source_refs=[], account=snapshot["account"], network=snapshot["network"], policy={"risk_percent": "1"})
    row.update(entry_vwap="101", filled_quantity="2", remaining_quantity="0", stop_price="90", reserved_risk="0",
        orders=[{"kind": "tp", "cloid": "target", "index": 1}],
        exit_orders=[{"kind": "close", "cloid": "exit", "quantity": "1", "price": "110", "retired": True}])
    row["fill_execution_roles"] = {identity: "maker" for identity in row["fill_events"]}
    snapshot["trades"] = {row["trade_id"]: row}
    snapshot["operations"] = {"operation": {"created_at": 1_800_000_000_000, "phase": "confirmed", "nonce": 7,
        "wire_action": {"type": "order", "orders": [{"c": "target", "p": "105", "s": ".5"}]},
        "request": {"type": "synthetic"}, "result": {"status": "ok"}}}
    return snapshot


def test_retired_finance_keeps_its_history_without_republishing_an_inactive_scope(financial_data):
    from aqla_agent.pipelines import migrate
    financial_data["trades"]["trade"]["plan"]["exchange"] = "aster"
    financial_data["operations"]["operation"]["exchange"] = "aster"
    financial_data["execution_accounts"]["aster:mainnet"] = {"exchange": "aster", "network": "mainnet", "account": "historical"}
    records = journal.project(financial_data)
    retired = {key: row["fingerprint"] for key, row in records.items() if row["table"] in ("Trades", "Orders", "Fills", "Vorgänge")}
    financial_data["journal_pending"] = dict(retired)
    before = copy.deepcopy(financial_data)
    assert migrate(financial_data)
    journal.seed(financial_data)
    assert financial_data["trades"] == before["trades"] and financial_data["operations"] == before["operations"]
    assert financial_data["retired_execution_configuration"]["journal_pending"] == retired
    assert not set(journal.project(financial_data)).intersection(retired)
    assert not {journal.record_key(row) for row in journal.batch(financial_data)["records"]}.intersection(retired)


@pytest.mark.parametrize("table", ["Trades", "Orders", "Fills", "Vorgänge"])
@pytest.mark.parametrize("damage", [None, "missing_fee", "fee_currency", "partial_exit"])
def test_financial_selected_batch_and_ack_match_frozen_projection_without_source_scan(financial_data, monkeypatch, table, damage):
    class NoSourceScan(dict):
        def items(self):
            pytest.fail("Targeted finance projected unrelated source history")

        def values(self):
            pytest.fail("Targeted finance scanned unrelated source history")

    row = financial_data["trades"]["trade"]
    if damage == "missing_fee":
        next(iter(row["fill_events"].values()))["fee"] = None
    elif damage == "fee_currency":
        next(iter(row["fill_events"].values()))["feeToken"] = "other"
    elif damage == "partial_exit":
        row["fill_events"].pop(list(row["fill_events"])[-1])
    records = financial_oracle.project(financial_data)
    assert journal.project(financial_data) == records
    financial_data["journal_pending"] = {key: row["fingerprint"] for key, row in records.items() if row["table"] == table}
    expected_batch = financial_oracle.batch(financial_data, 100)
    expected = copy.deepcopy(financial_data)
    accepted = {journal.record_key(row): row["fingerprint"] for row in expected_batch["records"]}
    expected_ack = financial_oracle.acknowledge(expected, accepted)
    financial_data["events"] = NoSourceScan(financial_data["events"])
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Targeted finance used full projection"))
    assert journal.batch(financial_data, 100) == expected_batch
    assert journal.acknowledge(financial_data, accepted) == expected_ack
    assert financial_data == expected


@pytest.mark.parametrize("table", ["Trades", "Orders", "Fills", "Vorgänge"])
def test_financial_history_overrides_and_two_fingerprint_guards_match_frozen_oracle(financial_data, monkeypatch, table):
    records = financial_oracle.project(financial_data)
    key, original = next((key, row) for key, row in records.items() if row["table"] == table)
    override = copy.deepcopy(original)
    override["values"][1] = "historical override"
    override["fingerprint"] = journal.fingerprint(override["values"])
    financial_data["journal_history"] = {"noncanonical-key": override}
    financial_data["journal_pending"] = {key: override["fingerprint"]}
    expected_batch = financial_oracle.batch(financial_data, 100)
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Financial override used full projection"))
    assert journal.batch(financial_data, 100) == expected_batch
    for accepted_version, pending_version in ((original["fingerprint"], override["fingerprint"]),
            (override["fingerprint"], "new pending generation"), (override["fingerprint"], override["fingerprint"])):
        actual, expected = copy.deepcopy(financial_data), copy.deepcopy(financial_data)
        actual["journal_pending"][key] = expected["journal_pending"][key] = pending_version
        accepted = {key: accepted_version}
        assert journal.acknowledge(actual, accepted) == financial_oracle.acknowledge(expected, accepted)
        assert actual == expected


@pytest.mark.parametrize("table", ["Orders", "Fills"])
def test_financial_owner_collisions_and_missing_records_keep_full_projection_semantics(financial_data, monkeypatch, table):
    duplicate = copy.deepcopy(financial_data["trades"]["trade"])
    duplicate["trade_id"] = "second"
    duplicate["plan"]["profile_id"] = "other"
    financial_data["trades"]["second"] = duplicate
    records = financial_oracle.project(financial_data)
    financial_data["journal_pending"] = {key: row["fingerprint"] for key, row in records.items() if row["table"] == table}
    financial_data["journal_pending"][table + ":missing"] = "missing"
    expected = financial_oracle.batch(financial_data, 100)
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Financial owner lookup used full projection"))
    assert journal.batch(financial_data, 100) == expected
    assert all(row["values"][1] == "second" for row in expected["records"])


@pytest.mark.parametrize("table", ["Trades", "Orders", "Fills"])
def test_financial_batch_indexes_owners_once_and_projects_only_requested_trade_table(financial_data, monkeypatch, table):
    class CountingTrades(dict):
        scans = 0

        def items(self):
            self.scans += 1
            return super().items()

    other = copy.deepcopy(financial_data["trades"]["trade"])
    other.update(trade_id="other", entry_cloid="other-entry", orders=[], exit_orders=[])
    other["fill_events"] = {"other-" + identity: row for identity, row in other["fill_events"].items()}
    financial_data["trades"]["other"] = other
    records = financial_oracle.project(financial_data)
    financial_data["journal_pending"] = {key: row["fingerprint"] for key, row in records.items()
                                         if row["table"] == table and (row["record_id"] == "trade" if table == "Trades" else row["values"][1] == "trade")}
    expected = financial_oracle.batch(financial_data, 100)
    financial_data["trades"] = CountingTrades(financial_data["trades"])
    generated, original = [], journal._trade_records

    def counted(data, identity, trade, aliases, actions, tables):
        generated.append((identity, tables))
        return original(data, identity, trade, aliases, actions, tables)

    monkeypatch.setattr(journal, "_trade_records", counted)
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Targeted financial batch used full projection"))
    assert journal.batch(financial_data, 100) == expected
    assert generated == [("trade", (table,))]
    assert financial_data["trades"].scans == (0 if table == "Trades" else 1)


@pytest.fixture
def journal_data(setup, monkeypatch):
    _, state, _, _, trading = setup
    monkeypatch.setattr(journal, "now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(oracle, "now_ms", lambda: 1_800_000_000_000)
    for index in range(3):
        trading.ingest(SourceEvent(chat_id="channel", message_id=index + 1, sent_at=1_800_000_000_000 + index,
                                   received_at=1_800_000_000_000 + index, text="独立😀" * 70, historical=True))
    data = state.snapshot()
    for pipeline in data["pipelines"].values():
        for target in pipeline["targets"]:
            target["fallback_exchanges"] = []
    history = oracle.record("Ereignisse", "history", "now", 1, "Signale", "one", "before", "after")
    data["journal_history"] = {"history": history}
    data["journal_pending"] = {key: row["fingerprint"] for key, row in oracle.project(data).items()}
    return data


@pytest.mark.parametrize("kind", ["signals", "history", "parameters", "rules", "ranks", "mixed", "stale", "missing", "empty"])
def test_selective_ack_matches_frozen_complete_state_oracle(journal_data, kind):
    records = oracle.project(journal_data)
    selected = {"signals": {"Signale"}, "history": {"Ereignisse"}, "parameters": {"Handelsparameter"},
                "rules": {"Regeln"}, "ranks": {"Ränge"}, "mixed": set(journal.TABLES)}
    accepted = {key: row["fingerprint"] for key, row in records.items() if row["table"] in selected.get(kind, set())}
    if kind == "stale":
        accepted = {key: "stale" for key in records}
    elif kind == "missing":
        accepted = {"Signale:missing": "missing", "Ereignisse:missing": "missing"}
    expected, actual = copy.deepcopy(journal_data), copy.deepcopy(journal_data)
    result = oracle.acknowledge(expected, accepted)
    assert journal.acknowledge(actual, accepted) == result
    assert actual == expected


@pytest.mark.parametrize("limit", [0, 1, 3, 100, 500])
def test_pending_batch_matches_frozen_order_and_utf8_budget(journal_data, limit):
    assert journal.batch(journal_data, limit) == oracle.batch(journal_data, limit)


def test_signal_and_history_ack_never_calls_complete_projection(journal_data, monkeypatch):
    records = oracle.project(journal_data)
    accepted = {key: row["fingerprint"] for key, row in records.items() if row["table"] in ("Signale", "Ereignisse")}
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Selective ACK used complete projection"))
    assert journal.acknowledge(journal_data, accepted)["acknowledged"] == len(accepted)


def test_empty_ack_never_calls_complete_projection(journal_data, monkeypatch):
    before = copy.deepcopy(journal_data)
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Empty ACK used complete projection"))
    assert journal.acknowledge(journal_data, {}) == {"acknowledged": 0, "pending": len(journal_data["journal_pending"])}
    assert journal_data == before


def test_signal_pending_batch_projects_only_selected_keys(journal_data, monkeypatch):
    records = oracle.project(journal_data)
    journal_data["journal_pending"] = {key: row["fingerprint"] for key, row in records.items() if row["table"] == "Signale"}
    expected = oracle.batch(journal_data, 1)
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Signal batch used complete projection"))
    assert journal.batch(journal_data, 1) == expected


@pytest.mark.parametrize("opaque", ["legacy text", {"event_id": "opaque", "classification": "report"}, None])
def test_opaque_events_are_preserved_and_skipped_by_full_and_selective_journal(journal_data, monkeypatch, opaque):
    journal_data["events"]["opaque"] = opaque
    journal_data["bindings"]["opaque"] = "historical-trade"
    events, bindings = copy.deepcopy(journal_data["events"]), copy.deepcopy(journal_data["bindings"])
    rows = journal.project(journal_data)
    assert "Signale:opaque" not in rows
    journal.seed(journal_data)
    previous = copy.deepcopy(journal_data)
    journal_data["sequence"] += 1
    journal.enqueue_changes(previous, journal_data)
    assert journal_data["events"] == events and journal_data["bindings"] == bindings
    assert "Signale:opaque" not in journal_data["journal_pending"]
    valid = next(row for row in rows.values() if row["table"] == "Signale")
    key = journal.record_key(valid)
    journal_data["journal_pending"] = {"Signale:opaque": "unknown", key: valid["fingerprint"]}
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Selective source batch used full projection"))
    result = journal.batch(journal_data)
    assert [row["record_id"] for row in result["records"]] == [valid["record_id"]]
    assert journal.acknowledge(journal_data, {"Signale:opaque": "unknown", key: valid["fingerprint"]}) == {
        "acknowledged": 1, "pending": 1}
    assert journal_data["journal_pending"] == {"Signale:opaque": "unknown"}
    assert journal_data["events"] == events and journal_data["bindings"] == bindings


def test_history_override_with_noncanonical_map_key_matches_complete_oracle(journal_data):
    key = next(key for key in oracle.project(journal_data) if key.startswith("Signale:"))
    row = copy.deepcopy(oracle.project(journal_data)[key])
    row["values"][13] = "Historical override body"
    row["fingerprint"] = oracle.fingerprint(row["values"])
    journal_data["journal_history"]["different-storage-key"] = row
    journal_data["journal_pending"][key] = row["fingerprint"]
    expected, actual = copy.deepcopy(journal_data), copy.deepcopy(journal_data)
    assert journal.batch(actual, 100) == oracle.batch(expected, 100)
    assert journal.acknowledge(actual, {key: row["fingerprint"]}) == oracle.acknowledge(expected, {key: row["fingerprint"]})
    assert actual == expected


@pytest.mark.asyncio
async def test_trade_order_fill_operation_rank_mixed_ack_complete_state_oracle(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    original = exchange.info
    async def quoted_fills(kind, **arguments):
        result = await original(kind, **arguments)
        return [{**row, "feeToken": "USDC"} for row in result] if kind == "userFillsByTime" else result
    monkeypatch.setattr(exchange, "info", quoted_fills)
    state.journal_enabled = True
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    active = await trading.execute(plan["plan_id"])
    for target in [row for row in active["orders"] if row["kind"] == "tp"]:
        fake.fill(target["cloid"], target["quantity"], target["price"])
    await trading.recover()
    monkeypatch.setattr(journal, "now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(oracle, "now_ms", lambda: 1_800_000_000_000)
    legacy_project = oracle.project

    def additive_oracle(data):
        # Keep the frozen oracle source and its complete-state ACK algorithm;
        # extend only its wire body for the independently specified new columns.
        records = legacy_project(data)
        for row in records.values():
            if row["table"] == "Trades":
                plan = data["trades"][row["record_id"]]["plan"]
                row["values"] += [plan.get("entry_execution", "standard"), plan.get("take_profit_execution", "standard")]
            elif row["table"] == "Fills":
                trade = data["trades"][row["values"][1]]
                role = trade.get("fill_execution_roles", {}).get(row["record_id"], "unknown")
                row["values"] += [role if role in ("maker", "taker", "unknown") else "unknown"]
            row["fingerprint"] = oracle.fingerprint(row["values"])
        return records

    monkeypatch.setattr(oracle, "project", additive_oracle)
    data = state.snapshot()
    for pipeline in data["pipelines"].values():
        for target in pipeline["targets"]:
            target["fallback_exchanges"] = []
    data["ranking_history"] = {"rank-history": {"source_id": "channel", "time": 1_800_000_000_000, "old": "F", "new": "E",
        "metrics": {"score": "40", "drawdown_r": "0"}, "network": "testnet", "revision": 2}}
    records = oracle.project(data)
    data["journal_pending"] = {key: row["fingerprint"] for key, row in records.items()}
    for table in ("Trades", "Orders", "Fills", "Vorgänge", "Ränge", "Ranghistorie", None):
        accepted = {key: row["fingerprint"] for key, row in records.items() if table is None or row["table"] == table}
        expected, actual = copy.deepcopy(data), copy.deepcopy(data)
        assert journal.acknowledge(actual, accepted) == oracle.acknowledge(expected, accepted)
        assert actual == expected
    accepted = {key: row["fingerprint"] if index % 2 else "stale" for index, (key, row) in enumerate(records.items())}
    valid_key = next(key for key in accepted if accepted[key] == records[key]["fingerprint"])
    data["journal_pending"][valid_key] = "different pending generation"
    expected, actual = copy.deepcopy(data), copy.deepcopy(data)
    assert journal.acknowledge(actual, accepted) == oracle.acknowledge(expected, accepted)
    assert actual == expected and actual["journal_history"]
    expected_batch = oracle.batch(data, 500)
    # Financial corrections precede the backlog; record bodies and ACK behavior stay unchanged.
    records = expected_batch["records"]
    expected_batch["records"] = [row for row in records if row["table"] in ("Trades", "Fills")] + [
        row for row in records if row["table"] not in ("Trades", "Fills")]
    assert journal.batch(data, 500) == expected_batch


@pytest.mark.parametrize("kind", ["matched", "stale", "empty", "missing"])
def test_state_journal_ack_matches_generic_whole_state_including_sequence(setup, journal_data, monkeypatch, kind):
    _, state, *_ = setup
    state.update(lambda data: data.update(journal_data))
    state.journal_enabled = True
    before = state.snapshot()
    records = oracle.project(before)
    accepted = {key: row["fingerprint"] for key, row in records.items() if row["table"] in ("Signale", "Ereignisse")}
    if kind == "stale":
        accepted = {key: "stale" for key in accepted}
    elif kind == "empty":
        accepted = {}
    elif kind == "missing":
        accepted = {"Signale:missing": "missing"}
    expected = copy.deepcopy(before)
    result = oracle.acknowledge(expected, accepted)
    expected["sequence"] += 1
    oracle.enqueue_changes(before, expected)
    assert state.acknowledge_journal(accepted) == result
    assert state.snapshot() == expected
    from aqla_hyperliquid_mcp.storage import read_snapshot
    assert read_snapshot(state.path) == expected
