from aqla_hyperliquid_mcp.models import Interpretation, SourceEvent, now_ms


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
