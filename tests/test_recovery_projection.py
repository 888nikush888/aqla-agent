import copy

import pytest

from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import SourceEvent, digest


@pytest.mark.asyncio
async def test_terminal_recovery_keeps_financial_checks_without_full_source_snapshots(setup, signal, monkeypatch):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = signal
    trade = await router.execute((await router.preview(source.event_id, value))["plan_id"])
    await router.close_trade(trade["trade_id"], "synthetic-close")
    await router.recover()
    original_snapshot = state.snapshot
    before, actions = original_snapshot(), copy.deepcopy(fake.actions)
    assert before["trades"][trade["trade_id"]]["status"] == "closed"
    assert before["trades"][trade["trade_id"]]["cleanup_complete"]
    monkeypatch.setattr(state, "snapshot", lambda: pytest.fail("Routine recovery copied unrelated source history"))
    diagnostics = {}
    reports = await router.recover(diagnostics_sink=diagnostics)
    assert not any(row.get("error") for row in reports)
    assert all(row["category"] == "complete" for row in diagnostics.values())
    assert original_snapshot()["trades"] == before["trades"] and fake.actions == actions
    await router.close()


@pytest.mark.parametrize('kind', ['new', 'duplicate', 'legacy_tombstone'])
def test_intake_selects_only_its_revision_and_keeps_cutoff_and_deduplication(setup, monkeypatch, kind):
    _, state, fake, _, trading = setup
    source = SourceEvent(chat_id='channel', message_id=20, sent_at=1000, reply_to_id=1, text='Original source')
    state.update(lambda data: data.update(sources_live_after=2000))
    if kind == 'duplicate':
        trading.ingest(source)
    elif kind == 'legacy_tombstone':
        legacy = digest([source.source_id, source.edited_at, source.text, source.media])[:32]
        state.update(lambda data: data['event_tombstones'].__setitem__(legacy, source.source_id))
    snapshot = state.snapshot
    before = snapshot()
    monkeypatch.setattr(state, 'snapshot', lambda: pytest.fail('Intake copied unrelated source history'))
    row = trading.ingest(source)
    if kind == 'legacy_tombstone':
        assert row['status'] == 'already_processed' and snapshot() == before
    else:
        assert row['source']['historical'] and row['source']['text'] == source.text
        row['source']['text'] = 'Changed detached result'
        assert snapshot()['events'][source.event_id]['source']['text'] == source.text
        if kind == 'duplicate':
            assert snapshot() == before
    assert not fake.actions
