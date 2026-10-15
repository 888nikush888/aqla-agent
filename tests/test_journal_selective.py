import copy

import pytest
from oracles import journal_85aa9d7 as oracle

from aqla_hyperliquid_mcp.models import SourceEvent
from aqla_sheets_mcp import journal


@pytest.fixture
def journal_data(setup, monkeypatch):
    _, state, _, _, trading = setup
    monkeypatch.setattr(journal, "now_ms", lambda: 1_800_000_000_000)
    monkeypatch.setattr(oracle, "now_ms", lambda: 1_800_000_000_000)
    for index in range(3):
        trading.ingest(SourceEvent(chat_id="channel", message_id=index + 1, sent_at=1_800_000_000_000 + index,
                                   received_at=1_800_000_000_000 + index, text="独立😀" * 70, historical=True))
    data = state.snapshot()
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
