from decimal import Decimal

import pytest
from test_shared_account import shared  # noqa: F401 -- complete fake DEX/account inventory

from aqla_agent.conflict_review import observe_conflict
from aqla_hyperliquid_mcp.models import AqlaError, TradePlan


async def unsubmitted(setup, signal):
    settings, state, fake, _, engine = setup
    settings.shared_account = True
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    source, value = signal
    raw = await engine.preview(source.event_id, value)
    plan = TradePlan.model_validate(raw)
    trade = engine.new_trade(state.snapshot(), state.snapshot()["plans"][plan.plan_id], plan)
    trade["status"] = "conflicted"
    state.update(lambda data: data["trades"].__setitem__(plan.trade_id, trade))
    fake.position = -2
    return state, fake, engine, plan, trade


@pytest.mark.asyncio
async def test_foreign_position_does_not_keep_a_never_submitted_trade_as_owner(setup, signal, shared):  # noqa: F811
    state, fake, engine, plan, _ = await unsubmitted(setup, signal)
    before = list(fake.actions)
    await engine.recover()
    current = state.snapshot()["trades"][plan.trade_id]
    assert current["status"] == "rejected" and current["cleanup_complete"]
    assert current["ownership_release"]["basis"] == "never_submitted_entry"
    assert current["reserved_risk"] == "0" and all(row["not_sent"] for row in current["entry_orders"])
    assert engine.recovery_checked and fake.actions == before and Decimal(str(fake.position)) == Decimal("-2")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "sending", "unknown", "acknowledged"])
async def test_a_signed_secondary_split_leg_never_becomes_a_zero_order_proof(setup, signal, phase, shared):  # noqa: F811
    state, fake, engine, plan, trade = await unsubmitted(setup, signal)
    row = trade["entry_orders"][-1]
    state.update(lambda data: data["operations"].__setitem__(row["operation_id"],
        {"phase": phase, "outcome": "unknown", "wire_action": {"type": "order", "orders": []}}))
    review, _ = await observe_conflict(engine, plan.trade_id)
    assert not review["resolvable"] and not fake.actions
    assert state.snapshot()["trades"][plan.trade_id]["status"] == "conflicted"


@pytest.mark.asyncio
async def test_manual_exposure_after_preview_is_a_clean_admission_rejection(setup, signal, shared, monkeypatch):  # noqa: F811
    settings, state, fake, exchange, engine = setup
    settings.shared_account = True
    state.update(lambda data: data["policy"].update(entry_range_choice="split"))
    source, value = signal
    raw = await engine.preview(source.event_id, value)
    original, reads = exchange.account_snapshot, 0
    async def raced(market):
        nonlocal reads
        reads += 1
        if reads >= 2:
            fake.position = 2
        return await original(market)
    monkeypatch.setattr(exchange, "account_snapshot", raced)
    with pytest.raises(AqlaError, match="no free logical owner"):
        await engine.execute(raw["plan_id"])
    trade = state.snapshot()["trades"][raw["trade_id"]]
    assert trade["status"] == "rejected" and not fake.actions and fake.position == 2
    await engine.recover()
    assert engine.recovery_checked
