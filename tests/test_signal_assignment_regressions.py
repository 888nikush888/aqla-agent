"""Observed rejected-signal provenance and perpetual ticker regressions."""
import copy

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_agent.semantic_contract import supported_action_failure
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['BTCUSDT.P', 'BTC/USDT.P', 'btcusdc.p'])
@pytest.mark.parametrize('venue', ['hyperliquid'])
async def test_source_perpetual_ticker_resolves_its_existing_market(setup, venue, symbol):
    market = await setup[3].market(symbol)
    assert market.symbol == 'BTC' and market.asset_id == 0
    assert not setup[2].actions



def test_perpetual_suffix_does_not_rewrite_spot_or_hip3_identity():
    assert Hyperliquid.canonical_symbol("BTCUSDT.P", "spot") == "BTCUSDT.P"
    assert Hyperliquid.canonical_symbol("xyz:BTCUSDT.P") == "xyz:BTCUSDT.P"
    assert Hyperliquid.canonical_symbol("XAUUSDT.P") == "xyz:GOLD"


@pytest.mark.asyncio
@pytest.mark.parametrize('venue', ['hyperliquid'])
async def test_perpetual_ticker_update_still_binds_only_its_original_trade(setup, signal, request, venue):
    engine = setup[4]
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    trade = await engine.execute(plan['plan_id'])
    reply = SourceEvent(chat_id='channel', message_id=700, sent_at=now_ms(), reply_to_id=source.message_id, text='BTCUSDT.P close now')
    setup[4].ingest(reply)
    update = Interpretation(kind='trade_update', source_refs=[reply.source_id], symbol='BTCUSDT.P', update_action='close', evidence=[Evidence(field='symbol', source_id=reply.source_id, quote='BTCUSDT.P'), Evidence(field='update_action', source_id=reply.source_id, quote='close now')])
    assert [t['trade_id'] for t in engine.update_candidates(reply.event_id, update)] == [trade['trade_id']]
    assert not engine.update_candidates(reply.event_id, update.model_copy(update={'symbol': 'ETHUSDT.P'}))


@pytest.mark.asyncio
async def test_trade_update_rejects_unproved_channel_context_but_keeps_owned_setup(setup, signal):
    _, state, fake, _, engine = setup
    source, value = signal
    trade = await engine.execute((await engine.preview(source.event_id, value))["plan_id"])
    old = SourceEvent(chat_id="channel", message_id=701, sent_at=now_ms() - 86400000,
                      historical=True, text="Unrelated old channel discussion")
    reply = SourceEvent(chat_id="channel", message_id=702, reply_to_id=source.message_id,
                        sent_at=now_ms(), text="Close this BTC trade now")
    engine.ingest(old)
    engine.ingest(reply)
    update = Interpretation(kind="trade_update", source_refs=[reply.source_id, source.source_id], update_action="close")
    before = copy.deepcopy(state.snapshot())
    calls = copy.deepcopy(fake.actions)
    with pytest.raises(AqlaError, match="source_refs include context without setup evidence"):
        await engine.update_trade(reply.event_id, update.model_copy(update={"source_refs": [*update.source_refs, old.source_id]}))
    assert state.snapshot() == before and fake.actions == calls
    result = await engine.update_trade(reply.event_id, update)
    assert result["trade_id"] == trade["trade_id"] and fake.position == 0


@pytest.mark.parametrize("tail,expected", [("", "entry_execution_report_not_new_signal"),
    ("\nBuy BTC now. Entry: 83400. SL 83000. TP 84000.", None)])
def test_observed_entry_receipt_is_a_report_until_it_contains_a_new_instruction(tail, expected):
    text = "#FUTURES\n\n#BTC/USDT Entry 1 ✅\nAverage Entry Price: 83500 💵" + tail
    value = Interpretation(kind="new_signal", source_refs=["anchor"], symbol="BTC", side="long")
    assert supported_action_failure(text, value) == expected
    assert supported_action_failure(text, value.model_copy(update={"kind": "report"})) is None


@pytest.mark.asyncio
async def test_legacy_mcp_can_store_original_entry_receipt_without_reopening_its_parent(setup, signal):
    settings, state, fake, exchange, engine = setup
    parent, value = signal
    receipt = SourceEvent(chat_id="channel", message_id=703, sent_at=now_ms(), reply_to_id=parent.message_id,
        text="#BTC/USDT Entry 1 ✅\nAverage Entry Price: 100")
    engine.ingest(receipt)
    server = create_server(settings, state=state, exchange=exchange)
    try:
        auth = server.aqla_authority.sign("source", "observed-entry-receipt")
        mistaken = value.model_copy(update={"source_refs": [parent.source_id, receipt.source_id]})
        before = copy.deepcopy(state.snapshot())
        with pytest.raises(ToolError, match="entry_execution_report_not_new_signal"):
            await server.call_tool("trade_preview", {"event_id": receipt.event_id,
                "interpretation": mistaken.model_dump(mode="json"), "context_auth": auth})
        after = state.snapshot()
        assert after["events"][receipt.event_id].pop("last_error") == "entry_execution_report_not_new_signal"
        after["sequence"] = before["sequence"]
        assert after == before and not fake.actions
        report = Interpretation(kind="report", source_refs=[receipt.source_id], reason="Original entry fill confirmation",
            reason_codes=["result_report"], evidence=[Evidence(field="classification", source_id=receipt.source_id, quote=receipt.text)])
        proof = server.aqla_trading.context(receipt.event_id)["source_read_receipt"]
        await server.call_tool("interpret_source", {"event_id": receipt.event_id, "interpretation": report.model_dump(mode="json"),
            "source_read_receipt": proof, "context_auth": auth})
        assert state.snapshot()["events"][receipt.event_id]["status"] == "report"
        assert not state.snapshot()["events"][receipt.event_id].get("last_error")
        assert not state.snapshot()["plans"] and not state.snapshot()["operations"] and not fake.actions
    finally:
        await close_server(server)


@pytest.mark.parametrize("entry,prices,valid", [("range", [100], False), ("limit", [100, 101], False),
    ("range", [100, 101], True), ("limit", [100], True)])
def test_source_entry_shape_is_repaired_before_exchange_target_selection(setup, entry, prices, valid):
    _, state, fake, _, engine = setup
    source = SourceEvent(chat_id="channel", message_id=704, sent_at=now_ms(), text="BTC LONG Entry 100 - 101 SL 90 TP 110")
    engine.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", entry=entry,
        entry_prices=prices, stop_loss=90, take_profits=[110], evidence=[
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in
            [("symbol", "BTC"), ("side", "LONG"), ("stop_loss", "90"), ("take_profits.0", "110"),
             *[(f"entry_prices.{i}", str(price)) for i, price in enumerate(prices)]]])
    before = copy.deepcopy(state.snapshot())
    if valid:
        assert engine.record_interpretation(source.event_id, value)["status"] == "interpreted"
    else:
        with pytest.raises(AqlaError, match="Source entry type/prices disagree"):
            engine.record_interpretation(source.event_id, value)
        assert state.snapshot() == before
    assert not state.snapshot()["plans"] and not state.snapshot()["trades"] and not fake.actions
