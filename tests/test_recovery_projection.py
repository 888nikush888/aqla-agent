import copy

import pytest

from aqla_agent.router import Router


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
