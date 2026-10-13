import pytest
from test_updates import stop_update, waiting_gtc

from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms
from aqla_sheets_mcp.journal import acknowledge, batch


async def rejected_sources(setup, monkeypatch, kind="rejected"):
    settings, state, fake, exchange, _ = setup
    settings.max_events = 100
    router = Router(settings, state, exchange)
    clock = {"now": now_ms()}
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: clock["now"])
    sources = []
    for index in range(1, 121):
        source = SourceEvent(chat_id="channel", message_id=index, sent_at=clock["now"] - 100000 + index,
                             received_at=clock["now"] - 100000 + index, text="UNKNOWN long SL 90 TP 110 120" if kind == "rejected" else "ordinary context")
        sources.append(source)
        router.ingest(source)
        if kind == "rejected":
            fields = [("symbol", "UNKNOWN"), ("side", "long"), ("stop_loss", "90"), ("take_profits.0", "110"), ("take_profits.1", "120")]
            value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="UNKNOWN", side="long", entry="market",
                                   stop_loss=90, take_profits=[110, 120], evidence=[Evidence(field=field, quote=quote, source_id=source.source_id) for field, quote in fields])
            with pytest.raises(AqlaError, match="unavailable/delisted"):
                await router.preview(source.event_id, value)
        else:
            await router.preview(source.event_id, Interpretation(kind="unrelated", source_refs=[source.source_id]))
    clock["now"] += 3_600_000
    state.update(router.source.prune_events)
    assert not fake.actions and not state.snapshot()["trades"] and not state.snapshot()["plans"]
    return router, sources, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rejected", "unrelated"])
async def test_terminal_rejections_are_pruned_after_deadline_without_active_refs(setup, monkeypatch, kind):
    router, sources, _ = await rejected_sources(setup, monkeypatch, kind)
    data = setup[1].snapshot()
    assert len(data["events"]) == 100 and len(data["event_tombstones"]) == 20
    assert set(data["events"]) | set(data["event_tombstones"]) == {source.event_id for source in sources}
    assert all(row["status"] == kind for row in data["events"].values())
    assert all(data["event_tombstones"][source.event_id] == source.source_id for source in sources[:20])
    await router.close()


@pytest.mark.asyncio
async def test_rejected_tombstone_prevents_old_entry_replay(setup, monkeypatch):
    router, sources, _ = await rejected_sources(setup, monkeypatch)
    state = setup[1]
    old = sources[0]
    before = state.snapshot()
    assert old.event_id in before["event_tombstones"]
    assert router.ingest(old)["status"] == "already_processed"
    with pytest.raises(AqlaError, match="Unknown source event"):
        await router.preview(old.event_id, Interpretation(kind="new_signal", source_refs=[old.source_id], symbol="BTC", side="long", entry="market", stop_loss=90, take_profits=[110, 120]))
    assert state.snapshot() == before
    assert old.event_id not in state.snapshot()["events"]
    await router.close()


def test_retention_preserves_new_revision_and_pending_export(setup, monkeypatch):
    settings, state, _, _, trading = setup
    settings.max_events = 1000
    stamp = now_ms()
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: stamp + 3_600_000)
    originals = []
    for index in range(1, 121):
        source = SourceEvent(chat_id="channel", message_id=index, sent_at=stamp - 100000 + index,
                             received_at=stamp - 100000 + index, text="stored terminal event")
        trading.ingest(source)
        state.update(lambda data, event_id=source.event_id: data["events"][event_id].update(status="rejected"))
        originals.append(source)
    newer = originals[0].model_copy(update={"edited_at": stamp + 1, "received_at": stamp + 1, "text": "new revision"})
    trading.ingest(newer)
    state.update(lambda data: data.setdefault("journal_pending", {}).update({"Signale:" + originals[1].event_id: {"pending": True}}))
    settings.max_events = 100
    state.update(trading.prune_events)
    after = state.snapshot()
    assert newer.event_id != originals[0].event_id and newer.source_id == originals[0].source_id
    assert newer.event_id in after["events"] and after["events"][newer.event_id]["status"] == "pending"
    assert originals[1].event_id in after["events"]
    assert originals[1].event_id not in after["event_tombstones"]
    assert len(after["events"]) == 100
    assert len(after["event_tombstones"]) == 21
    assert after["event_tombstones"][originals[0].event_id] == newer.source_id


def add_terminal_events(state, stamp, count=120):
    sources = [SourceEvent(chat_id="channel", message_id=1000 + index, sent_at=stamp + index,
                           received_at=stamp + index, text="Terminal context") for index in range(count)]
    state.update(lambda data: data["events"].update({source.event_id: {"event_id": source.event_id, "source_id": source.source_id,
        "source": source.model_dump(mode="json"), "status": "rejected", "interpretation": None} for source in sources}))
    return sources


def test_retention_and_fresh_ingestion_preserve_opaque_legacy_history(setup):
    settings, state, fake, _, trading = setup
    settings.max_events = 100
    opaque = {"old-report": {"event_id": "old-report", "classification": "report"},
              "old-pending": {"event_id": "old-pending", "status": "pending"},
              "partial-source": {"event_id": "partial-source", "source_id": "old", "source": {"text": "old"}, "status": "pending"}}
    state.update(lambda data: data["events"].update(opaque))
    sources = add_terminal_events(state, now_ms() - 100000)
    fresh = SourceEvent(chat_id="channel", message_id=2000, sent_at=now_ms(), text="Fresh report")
    assert trading.ingest(fresh)["status"] == "pending"
    data = state.snapshot()
    assert len(data["events"]) == settings.max_events
    assert all(data["events"][identity] == row for identity, row in opaque.items())
    assert not set(opaque).intersection(data["event_tombstones"])
    assert len(data["event_tombstones"]) == len(sources) + len(opaque) + 1 - settings.max_events
    assert trading.context(fresh.event_id)["event"]["source"] == fresh.model_dump(mode="json")
    value = Interpretation(kind="report", source_refs=[fresh.source_id], reason="Report", evidence=[
        Evidence(field="classification", source_id=fresh.source_id, quote=fresh.text)])
    assert trading.record_interpretation(fresh.event_id, value)["status"] == "report"
    assert all(state.snapshot()["events"][identity] == row for identity, row in opaque.items())
    assert not fake.actions


@pytest.mark.asyncio
async def test_retention_preserves_unexpired_plan_revision_then_releases_it(setup, signal, monkeypatch):
    settings, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    saved = state.snapshot()["plans"][plan["plan_id"]]
    state.update(lambda data: data["events"][source.event_id].update(status="rejected"))
    add_terminal_events(state, plan["created_at"] + 100000)
    settings.max_events = 100
    clock = {"now": plan["deadline_at"] - 1}
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: clock["now"])
    state.update(trading.prune_events)
    assert len(state.snapshot()["events"]) == 100
    assert source.event_id in state.snapshot()["events"]
    clock["now"] = plan["deadline_at"] + 1
    add_terminal_events(state, plan["created_at"] + 200000, count=1)
    state.update(trading.prune_events)
    after = state.snapshot()
    assert len(after["events"]) == 100
    assert after["event_tombstones"][source.event_id] == source.source_id
    assert after["plans"][plan["plan_id"]] == saved
    assert not fake.actions


def test_pending_exports_are_not_pruned_to_force_quantity_limit(setup, monkeypatch):
    settings, state, fake, _, trading = setup
    stamp = now_ms()
    sources = add_terminal_events(state, stamp)
    state.update(lambda data: data.setdefault("journal_pending", {}).update({"Signale:" + source.event_id: {"pending": True} for source in sources}))
    settings.max_events = 100
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: stamp + 3_600_000)
    state.update(trading.prune_events)
    assert set(state.snapshot()["events"]) == {source.event_id for source in sources}
    assert not state.snapshot()["event_tombstones"] and not fake.actions


@pytest.mark.asyncio
async def test_retention_preserves_pending_stop_event_and_bound_source_revision(setup, signal, monkeypatch):
    settings, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    dependency = SourceEvent(chat_id="channel", message_id=3, sent_at=now_ms(), text="Stop context")
    trading.ingest(dependency)
    trading.record_interpretation(dependency.event_id, Interpretation(kind="report", source_refs=[dependency.source_id]))
    source, interpretation = stop_update(trading)
    interpretation.source_refs.append(dependency.source_id)
    await trading.update_trade(source.event_id, interpretation)
    pending = state.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"]
    assert source.source_id not in trade["plan"]["source_refs"]
    assert dependency.source_id not in trade["plan"]["source_refs"]
    assert pending["source_revisions"][dependency.source_id] == dependency.event_id
    before_actions = list(fake.actions)
    stamp = now_ms() + 100000
    add_terminal_events(state, stamp)
    settings.max_events = 100
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: stamp + 3_600_000)
    state.update(trading.prune_events)
    after = state.snapshot()
    assert len(after["events"]) == 100
    assert after["events"][source.event_id]["status"] == "needs_reconciliation"
    assert after["events"][dependency.event_id]["status"] == "report"
    assert after["trades"][trade["trade_id"]]["pending_stop_update"] == pending
    assert source.event_id not in after["event_tombstones"] and dependency.event_id not in after["event_tombstones"]
    assert fake.actions == before_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,status", [("rejected", "rejected"), ("superseded", "superseded"), ("closed_without_application", "rejected")])
async def test_terminal_stop_update_sources_are_retained_only_while_referenced(setup, signal, monkeypatch, phase, status):
    settings, state, fake, _, trading = setup
    trade = await waiting_gtc(setup, signal)
    source, interpretation = stop_update(trading)
    await trading.update_trade(source.event_id, interpretation)
    state.update(lambda data: trading.write_stop_phase(data, trade["trade_id"],
        data["trades"][trade["trade_id"]]["pending_stop_update"], phase, "Terminal retention test"))
    assert state.snapshot()["events"][source.event_id]["status"] == status
    before_actions = list(fake.actions)
    stamp = now_ms() + 100000
    add_terminal_events(state, stamp)
    settings.max_events = 100
    monkeypatch.setattr("aqla_agent.trading.now_ms", lambda: stamp + 3_600_000)
    state.update(trading.prune_events)
    after = state.snapshot()
    assert len(after["events"]) == 100
    assert after["event_tombstones"][source.event_id] == source.source_id
    assert signal[0].event_id in after["events"]  # The still-active Entry keeps its original source.
    assert after["trades"][trade["trade_id"]]["stop_update_history"]["update:" + source.event_id]["phase"] == phase
    assert trading.ingest(source)["status"] == "already_processed"
    duplicate = await trading.update_trade(source.event_id, interpretation)
    assert duplicate["duplicate"] is True and duplicate["result"]["phase"] == phase
    assert state.snapshot() == after
    assert fake.actions == before_actions


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
