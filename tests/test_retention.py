import pytest

from aqla_hyperliquid_mcp.models import Interpretation, SourceEvent, now_ms
from aqla_sheets_mcp.journal import acknowledge, batch


def test_retention_trims_finished_source_bodies_but_keeps_pending_and_dedupe(setup):
    settings, state, _, _, trading = setup
    settings.max_events = 100
    originals = []
    for index in range(1, 150):
        source = SourceEvent(chat_id="channel", message_id=index, sent_at=now_ms() - 100000 + index, text="Bericht " + str(index))
        originals.append(source)
        trading.ingest(source)
        trading.record_interpretation(source.event_id, Interpretation(kind="report", source_refs=[source.source_id]))
    snapshot = state.snapshot()
    assert len(snapshot["events"]) == 100
    assert snapshot["event_tombstones"]
    tombstone = next(source for source in originals if source.event_id in snapshot["event_tombstones"])
    assert trading.ingest(tombstone)["status"] == "already_processed"
    assert tombstone.event_id not in state.snapshot()["events"]
    assert state.snapshot()["sequence"] == snapshot["sequence"]
    pending = SourceEvent(chat_id="channel", message_id=151, sent_at=now_ms(), text="Neues Setup")
    trading.ingest(pending)
    assert state.snapshot()["events"][pending.event_id]["status"] == "pending"


def test_unknown_operations_nonces_and_source_trade_bindings_are_never_pruned(setup):
    settings, state, _, _, trading = setup
    settings.max_events = 100
    state.update(lambda row: row.update(operations={"unknown": {"phase": "unknown", "request_hash": "bound"}}, nonces={"signer": 123}, bindings={"source": {"trade_id": "owner"}}))
    for index in range(1, 125):
        source = SourceEvent(chat_id="channel", message_id=index, sent_at=now_ms(), text="Kontext")
        trading.ingest(source)
        trading.record_interpretation(source.event_id, Interpretation(kind="unrelated", source_refs=[source.source_id]))
    snapshot = state.snapshot()
    assert snapshot["operations"]["unknown"]["phase"] == "unknown"
    assert snapshot["nonces"]["signer"] == 123
    assert snapshot["bindings"]["source"]["trade_id"] == "owner"


@pytest.mark.parametrize("old_edit", [False, True], ids=["historical-message", "historical-edit"])
def test_backdated_context_is_enqueued_before_retention_and_released_after_delivery(setup, old_edit):
    settings, state, _, _, trading = setup
    settings.max_events = 100
    state.journal_enabled = True
    base = now_ms() - 100000
    for identity in range(1, 101):
        trading.ingest(SourceEvent(chat_id="channel", message_id=identity, sent_at=base + identity,
                                   text="Historischer Kontext", historical=True))
    assert len(state.snapshot()["journal_pending"]) == 200
    source = SourceEvent(chat_id="channel", message_id=1 if old_edit else 101,
                         sent_at=base + 1 if old_edit else base - 10000, received_at=base - 30000,
                         edited_at=base + 10 if old_edit else None, text="Nachgelieferter Kontext", historical=True)
    assert trading.ingest(source)["status"] == "context"
    snapshot = state.snapshot()
    assert len(snapshot["events"]) == 101
    assert source.event_id in snapshot["events"]
    assert source.event_id not in snapshot["event_tombstones"]
    records = batch(snapshot, 500)["records"]
    row = next(row for row in records if row["table"] == "Signale" and row["record_id"] == source.event_id)
    assert row["values"][13] == source.text
    assert any(row["values"][4] == source.event_id for row in records if row["table"] == "Ereignisse")
    accepted = {row["table"] + ":" + row["record_id"]: row["fingerprint"] for row in records}
    state.update(lambda data: acknowledge(data, accepted))
    state.update(trading.prune_events)
    snapshot = state.snapshot()
    assert len(snapshot["events"]) == 100
    assert source.event_id in snapshot["event_tombstones"]
    assert "Signale:" + source.event_id not in snapshot["journal_pending"]


def test_repeated_source_ingest_does_not_replace_state_or_enqueue_journal_rows(setup, monkeypatch):
    _, state, _, _, trading = setup
    state.journal_enabled = True
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=now_ms(), text="Kontext", historical=True)
    first = trading.ingest(source)
    snapshot = state.snapshot()
    def reject_write(_):
        pytest.fail("A duplicate source ingest must not write the state")
    monkeypatch.setattr(state, "_write", reject_write)
    assert trading.ingest(source) == first
    assert state.snapshot() == snapshot
