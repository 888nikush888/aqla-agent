from decimal import Decimal

import pytest

from aqla_agent.decisions import validate_interpretation
from aqla_agent.source_assistance import check_interpretation, context_packet
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.state import State


@pytest.mark.asyncio
async def test_reserved_child_identity_survives_a_crash_before_operation_creation(setup, signal, monkeypatch):
    _, state, fake, _, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source)
    fake.mid = "105"
    original = engine.close_position
    async def interrupted(*_, **__):
        raise RuntimeError("interrupted before close intent")
    monkeypatch.setattr(engine, "close_position", interrupted)
    with pytest.raises(RuntimeError):
        await engine.update_trade(update.event_id, sequence)
    saved = state.snapshot()["trades"][trade["trade_id"]]["management_sequences"][update.event_id]
    identity = saved["operations"][0]
    assert identity not in state.snapshot()["operations"]
    monkeypatch.setattr(engine, "close_position", original)
    await engine.recover()
    await engine.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]["management_sequences"][update.event_id]
    assert current["operations"] == [identity] and current["phase"] == "confirmed"


@pytest.mark.asyncio
async def test_roi_is_not_a_reduction_fraction_and_cannot_send_either_step(setup, signal):
    from aqla_hyperliquid_mcp.models import AqlaError
    _, _state, fake, _, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source, "BTC: reduce 50% ROE and move stop to entry now.")
    sequence.evidence[1].quote = "reduce 50% ROE"
    before = list(fake.actions)
    with pytest.raises(AqlaError, match="pnl_percentage"):
        await engine.update_trade(update.event_id, sequence)
    assert fake.actions == before


@pytest.mark.asyncio
async def test_forbidden_second_stop_cannot_cancel_waves_or_reduce_the_position(setup, signal):
    from aqla_hyperliquid_mcp.models import AqlaError
    _, state, fake, _, engine = setup
    state.update(lambda data: data["policy"].update(management_mode="waverider"))
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source, "BTC: reduce 50% and move stop to 80 now.")
    sequence.management_steps[1] = type(sequence.management_steps[1])(action="move_stop", stop_loss=80)
    sequence.evidence[-1].quote = "move stop to 80 now"
    sequence.evidence.append(Evidence(field="management_steps.1.stop_loss", source_id=update.source_id, quote="80"))
    before = list(fake.actions)
    with pytest.raises(AqlaError, match="widening"):
        await engine.update_trade(update.event_id, sequence)
    assert fake.actions == before and not state.snapshot()["trades"][trade["trade_id"]]["waverider"].get("finished")


@pytest.mark.asyncio
async def test_a_multi_market_instruction_cannot_execute_only_its_selected_market(setup, signal):
    from aqla_hyperliquid_mcp.models import AqlaError
    _, _, fake, exchange, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    await engine.execute(plan["plan_id"])
    exchange.market_cache["ETH"] = exchange.market_cache["BTC"].model_copy(update={"symbol": "ETH", "asset_id": 1})
    update, sequence = instruction(engine, source, "BTC and ETH: reduce 50% and move stop to entry now.")
    before = list(fake.actions)
    with pytest.raises(AqlaError, match="one current market"):
        await engine.update_trade(update.event_id, sequence)
    assert fake.actions == before


def instruction(engine, source, text="BTC: reduce 50% and move stop to entry now."):
    update = SourceEvent(chat_id="channel", message_id=2, reply_to_id=source.message_id, sent_at=now_ms(), text=text)
    engine.ingest(update)
    signal = Interpretation(kind="trade_update", source_refs=[update.source_id], symbol="BTC", update_action="sequence",
        management_steps=[{"action": "reduce", "reduce_percent": 50}, {"action": "break_even"}],
        evidence=[Evidence(field=field, source_id=update.source_id, quote=quote) for field, quote in (
            ("symbol", "BTC"), ("management_steps.0.action", "reduce 50%"),
            ("management_steps.0.reduce_percent", "50%"), ("management_steps.1.action", "move stop to entry now"))])
    return update, signal


@pytest.mark.asyncio
async def test_typed_sequence_is_available_through_the_existing_public_mcp_tool(setup, signal):
    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, fake, exchange, _ = setup
    source, value = signal
    server = create_server(settings, state=state, exchange=exchange)
    router = server.aqla_trading
    try:
        preview = await router.preview(source.event_id, value)
        trade = await router.execute(preview["targets"][0]["plan_id"])
        update, sequence = instruction(router, source)
        fake.mid = "105"
        result = await server.call_tool("trade_update", {"event_id": update.event_id,
            "interpretation": sequence.model_dump(mode="json"), "context_auth": server.aqla_authority.sign("operator", "synthetic-sequence")})
        assert not result.is_error
        await router.recover()
        current = state.snapshot()["trades"][trade["trade_id"]]
        assert current["management_sequences"][update.event_id]["phase"] == "confirmed"
        assert Decimal(current["remaining_quantity"]) == Decimal(trade["remaining_quantity"]) / 2
    finally:
        await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "split", "waverider"])
async def test_reduction_then_stop_survives_restart_with_one_fixed_quantity_basis(setup, signal, mode):
    settings, state, fake, exchange, engine = setup
    if mode != "legacy":
        state.update(lambda data: data["policy"].update(entry_range_choice="split", management_mode="waverider" if mode == "waverider" else "standard"))
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source)
    context = engine.context(update.event_id)
    validate_interpretation(context_packet(context), sequence)
    assert check_interpretation(context, sequence)["valid"]
    fake.mid = "105"
    result = await engine.update_trade(update.event_id, sequence)
    assert result["result"]["pending_sequence"]
    saved = state.snapshot()["trades"][trade["trade_id"]]["management_sequences"][update.event_id]
    assert saved["base_quantity"] == trade["remaining_quantity"]
    first = saved["operations"][0]
    action = state.snapshot()["operations"][first]["wire_action"]
    state.close()
    resumed = State(settings)
    exchange.state = resumed
    engine = Trading(exchange, resumed)
    try:
        await engine.recover()
        await engine.recover()
        current = resumed.snapshot()["trades"][trade["trade_id"]]
        assert current["management_sequences"][update.event_id]["completed"]
        assert current["management_sequences"][update.event_id]["phase"] == "confirmed"
        assert Decimal(current["remaining_quantity"]) == Decimal(trade["remaining_quantity"]) / 2
        assert Decimal(current["stop_price"]) == Decimal(current["entry_vwap"])
        assert fake.actions.count(action) == 1
        before = len(fake.actions)
        await engine.update_trade(update.event_id, sequence)
        assert len(fake.actions) == before
    finally:
        resumed.close()


@pytest.mark.asyncio
async def test_unknown_reduction_blocks_the_stop_until_exact_reconciliation(setup, signal, monkeypatch):
    _, state, fake, _exchange, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source)
    fake.mid = "105"
    fake.lost_response = True
    await engine.update_trade(update.event_id, sequence)
    current = state.snapshot()["trades"][trade["trade_id"]]
    identity = current["management_sequences"][update.event_id]["operations"][0]
    action = state.snapshot()["operations"][identity]["wire_action"]
    assert state.snapshot()["operations"][identity]["phase"] == "unknown"
    assert not current.get("pending_stop_update")
    before = len(fake.actions)
    from aqla_agent.management_sequences import resume
    await resume(engine, trade["trade_id"])
    assert len(fake.actions) == before and not state.snapshot()["trades"][trade["trade_id"]].get("pending_stop_update")
    await engine.recover()
    await engine.recover()
    assert fake.actions.count(action) == 1
    assert state.snapshot()["trades"][trade["trade_id"]]["management_sequences"][update.event_id]["phase"] == "confirmed"


@pytest.mark.asyncio
async def test_a_source_edit_supersedes_only_unsubmitted_later_steps(setup, signal):
    _, state, fake, _, engine = setup
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan["plan_id"])
    update, sequence = instruction(engine, source)
    fake.mid = "105"
    await engine.update_trade(update.event_id, sequence)
    edited = update.model_copy(update={"edited_at": now_ms() + 1, "text": "BTC: keep the original stop."})
    engine.ingest(edited)
    await engine.recover()
    current = state.snapshot()["trades"][trade["trade_id"]]
    assert current["management_sequences"][update.event_id]["phase"] == "superseded"
    assert Decimal(current["remaining_quantity"]) == Decimal(trade["remaining_quantity"]) / 2
    assert current["stop_price"] == trade["stop_price"]
