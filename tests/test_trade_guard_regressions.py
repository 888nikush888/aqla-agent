from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Interpretation


async def admitted_without_entry(trading, source, interpretation, monkeypatch):
    plan = await trading.preview(source.event_id, interpretation)
    original = trading._entry_steps

    async def crash(*args):
        raise RuntimeError("Crash after admission, before entry preparation")

    monkeypatch.setattr(trading, "_entry_steps", crash)
    with pytest.raises(RuntimeError, match="before entry preparation"):
        await trading.execute(plan["plan_id"])
    monkeypatch.setattr(trading, "_entry_steps", original)
    return plan


def entry_actions(fake):
    return [action for action in fake.actions if action["type"] == "order" and any(not order["r"] for order in action["orders"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel_entry", "close", "operator_close", "operator_cancel"])
async def test_proved_unsent_cancellation_is_terminal_before_recovery(setup, signal, monkeypatch, action):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await admitted_without_entry(trading, source, interpretation, monkeypatch)
    if action.startswith("operator_"):
        method = trading.close_position if action == "operator_close" else trading.cancel_entry
        await method(plan["trade_id"])
    else:
        cancellation = source.model_copy(update={"message_id": 2, "reply_to_id": 1, "text": "Entry absagen"})
        trading.ingest(cancellation)
        update = Interpretation(kind="trade_update", source_refs=[cancellation.source_id], update_action=action)
        await trading.update_trade(cancellation.event_id, update)
        assert state.snapshot()["events"][cancellation.event_id]["status"] == "updated"
        assert state.snapshot()["bindings"]["update:" + cancellation.event_id]["trade_id"] == plan["trade_id"]
        await trading.update_trade(cancellation.event_id, update)
    await trading.recover()
    await trading.recover()
    repeated = await trading.execute(plan["plan_id"])
    assert repeated["status"] == "cancelled" and repeated["reserved_risk"] == "0"
    assert state.snapshot()["bindings"][source.source_id]["trade_id"] == plan["trade_id"]
    assert state.snapshot()["events"][source.event_id]["status"] == "executed"
    assert fake.position == 0 and fake.actions == []


@pytest.mark.asyncio
async def test_operator_cancellation_during_unsent_dispatch_blocks_inflight_entry(setup, signal, monkeypatch):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.verify_signer
    calls = 0

    async def cancelled():
        nonlocal calls
        await original()
        calls += 1
        if calls == 3:
            await trading.cancel_entry(plan["trade_id"])

    monkeypatch.setattr(exchange, "verify_signer", cancelled)
    with pytest.raises(AqlaError, match="terminated"):
        await trading.execute(plan["plan_id"])
    await trading.recover()
    assert not entry_actions(fake)
    assert state.snapshot()["trades"][plan["trade_id"]]["status"] == "cancelled"
    assert state.snapshot()["trades"][plan["trade_id"]]["reserved_risk"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "sending", "unknown"])
async def test_prepared_or_uncertain_partial_entry_is_not_classified_unsent(setup, signal, monkeypatch, phase):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    interpretation.entry = "limit"
    interpretation.entry_prices = [Decimal(100)]
    fake.partial_limit_fraction = Decimal("0.5")
    plan = await trading.preview(source.event_id, interpretation)
    original = trading.refresh_trade

    async def crash(*args):
        raise RuntimeError("Crash after entry dispatch")

    monkeypatch.setattr(trading, "refresh_trade", crash)
    with pytest.raises(RuntimeError):
        await trading.execute(plan["plan_id"])
    state.update(lambda data: data["operations"]["entry:" + plan["trade_id"]].update(phase=phase))
    await trading.cancel_entry(plan["trade_id"])
    assert state.snapshot()["trades"][plan["trade_id"]]["status"] != "cancelled"
    monkeypatch.setattr(trading, "refresh_trade", original)
    await trading.recover()
    trade = state.snapshot()["trades"][plan["trade_id"]]
    assert trade["status"] == "open"
    assert Decimal(trade["remaining_quantity"]) == Decimal(plan["quantity"]) / 2
    assert any(fake.orders[order["cloid"]]["status"] == "open" for order in trade["orders"] if order["kind"] == "sl")
    assert len(entry_actions(fake)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["new_signal", "trade_update"])
async def test_historical_only_references_cannot_borrow_unrelated_live_anchor(setup, signal, kind):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    if kind == "trade_update":
        plan = await trading.preview(source.event_id, interpretation)
        await trading.execute(plan["plan_id"])
    historical = source.model_copy(update={"message_id": 10, "historical": True, "text": "Rest schließen" if kind == "trade_update" else source.text})
    live = source.model_copy(update={"message_id": 11, "text": "Guten Morgen"})
    trading.ingest(historical)
    trading.ingest(live)
    if kind == "new_signal":
        instruction = interpretation.model_copy(update={
            "source_refs": [historical.source_id],
            "evidence": [proof.model_copy(update={"source_id": historical.source_id}) for proof in interpretation.evidence]})
        method = trading.preview
    else:
        instruction = Interpretation(kind="trade_update", source_refs=[historical.source_id], update_action="close")
        method = trading.update_trade
    before = len(fake.actions)
    with pytest.raises(AqlaError, match="live anchor"):
        await method(live.event_id, instruction)
    assert len(fake.actions) == before
    assert state.snapshot()["events"][live.event_id]["status"] == "pending"
    assert state.snapshot()["events"][historical.event_id]["status"] == "context"


@pytest.mark.asyncio
async def test_live_reply_and_latest_edit_can_use_historical_supplement(setup, signal):
    _, _, fake, _, trading = setup
    source, interpretation = signal
    historical = source.model_copy(update={"message_id": 10, "historical": True})
    live = source.model_copy(update={"message_id": 11, "reply_to_id": 10, "text": "Diesen BTC Trade jetzt kaufen"})
    trading.ingest(historical)
    trading.ingest(live)
    instruction = interpretation.model_copy(update={
        "source_refs": [live.source_id, historical.source_id],
        "evidence": [proof.model_copy(update={"source_id": historical.source_id}) for proof in interpretation.evidence]})
    plan = await trading.preview(live.event_id, instruction)
    assert (await trading.execute(plan["plan_id"]))["status"] == "open"
    edited = live.model_copy(update={"edited_at": live.sent_at + 1, "text": "Korrektur: Rest schließen"})
    trading.ingest(edited)
    update = Interpretation(kind="trade_update", source_refs=[edited.source_id, historical.source_id], update_action="close")
    with pytest.raises(AqlaError, match="latest anchor revision"):
        trading.record_interpretation(live.event_id, update)
    await trading.update_trade(edited.event_id, update)
    await trading.recover()
    assert fake.position == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("read_number", [1, 2])
@pytest.mark.parametrize("recovery", [False, True])
async def test_fresh_account_foreign_position_blocks_admission_and_dispatch(setup, signal, monkeypatch, read_number, recovery):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    if recovery:
        plan = await admitted_without_entry(trading, source, interpretation, monkeypatch)
    else:
        plan = await trading.preview(source.event_id, interpretation)
    original = exchange.account_snapshot
    calls = 0

    async def foreign(market):
        nonlocal calls
        calls += 1
        if calls == read_number:
            fake.position = 1.0
        return await original(market)

    monkeypatch.setattr(exchange, "account_snapshot", foreign)
    if recovery:
        await trading.recover()
    else:
        with pytest.raises(AqlaError, match="exchange exposure"):
            await trading.execute(plan["plan_id"])
    assert not entry_actions(fake)
    assert fake.position == 1.0
    assert "entry:" + plan["trade_id"] not in state.snapshot()["operations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_missing_account_position_is_unknown_and_blocks_entry(setup, signal, monkeypatch, recovery):
    _, _, fake, exchange, trading = setup
    source, interpretation = signal
    plan = (await admitted_without_entry(trading, source, interpretation, monkeypatch) if recovery
            else await trading.preview(source.event_id, interpretation))
    original = exchange.info

    async def incomplete(kind, **params):
        result = await original(kind, **params)
        if kind == "clearinghouseState":
            # The separate position check is unchanged. Only the fresh account read
            # is incomplete, so it must not erase the earlier observation.
            result = {key: value for key, value in result.items() if key != "assetPositions"}
        return result

    original_account = exchange.account_snapshot

    async def unknown(market):
        with monkeypatch.context() as patch:
            patch.setattr(exchange, "info", incomplete)
            return await original_account(market)

    monkeypatch.setattr(exchange, "account_snapshot", unknown)
    if recovery:
        await trading.recover()
    else:
        with pytest.raises(AqlaError, match="position is unknown"):
            await trading.execute(plan["plan_id"])
    assert not entry_actions(fake)
    assert fake.position == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"asset_id": 1}, {"size_decimals": 3}, {"quote_token": "USDH"}])
async def test_unsent_recovery_requires_current_market_identity_and_collateral(setup, signal, monkeypatch, change):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await admitted_without_entry(trading, source, interpretation, monkeypatch)
    original = exchange.market

    async def changed(*args, **kwargs):
        return (await original(*args, **kwargs)).model_copy(update=change)

    monkeypatch.setattr(exchange, "market", changed)
    await trading.recover()
    trade = state.snapshot()["trades"][plan["trade_id"]]
    assert trade["status"] == "cancelled" and trade["reserved_risk"] == "0"
    assert "metadata changed" in trade["unsent_reason"]
    assert fake.actions == []
