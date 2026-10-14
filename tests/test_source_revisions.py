from collections import OrderedDict
from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.rules import latest_sources, verify_evidence
from aqla_hyperliquid_mcp.trades import Trading


@pytest.mark.asyncio
@pytest.mark.parametrize("original_age,expired", [(60, False), (14401, True)])
async def test_proven_multi_message_setup_keeps_original_deadline_after_reply_and_edit(setup, monkeypatch, original_age, expired):
    _, state, fake, _, trading = setup
    state.update(lambda data: data["policy"].update(max_signal_age_seconds=14400))
    parent = SourceEvent(chat_id="channel", message_id=10, sent_at=now_ms() - original_age * 1000,
                         text="BTC kaufen")
    reply = SourceEvent(chat_id="channel", message_id=11, reply_to_id=10, sent_at=now_ms(),
                        edited_at=now_ms(), text="SL 90. Ziele 110 und 120.")
    trading.ingest(parent)
    trading.ingest(reply)
    value = Interpretation(kind="new_signal", source_refs=[parent.source_id, reply.source_id], symbol="BTC", side="long",
        stop_loss=90, take_profits=[110, 120], evidence=[
            Evidence(field="symbol", source_id=parent.source_id, quote="BTC"),
            Evidence(field="side", source_id=parent.source_id, quote="kaufen"),
            *[Evidence(field=field, source_id=reply.source_id, quote=quote) for field, quote in
              (("stop_loss", "90"), ("take_profits.0", "110"), ("take_profits.1", "120"))]])
    verify_evidence(value, state.snapshot()["events"])
    if expired:
        with pytest.raises(AqlaError, match="Signal deadline expired"):
            await trading.preview(reply.event_id, value)
        assert not state.snapshot()["plans"]
    else:
        plan = await trading.preview(reply.event_id, value)
        assert plan["source_refs"] == value.source_refs
        assert plan["deadline_at"] == parent.sent_at + 14400 * 1000
    assert not fake.actions


@pytest.mark.asyncio
async def test_out_of_order_source_delivery_selects_same_latest_revision_for_evidence_and_plan(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    edited = source.model_copy(update={"edited_at": source.sent_at + 2, "text": source.text.replace("90", "95")})
    trading.ingest(edited)
    older = source.model_copy(update={"edited_at": source.sent_at + 1})
    trading.ingest(older)  # The latest arrival is an older edit.
    interpretation = interpretation.model_copy(update={
        "stop_loss": Decimal(95),
        "evidence": [Evidence(field=proof.field, source_id=proof.source_id, quote="SL 95") if proof.field == "stop_loss" else proof for proof in interpretation.evidence]})
    verify_evidence(interpretation, state.snapshot()["events"])
    plan = await trading.preview(edited.event_id, interpretation)
    assert plan["source_revisions"] == {source.source_id: edited.event_id}
    assert Trading.source_revisions(state.snapshot(), [source.source_id]) == plan["source_revisions"]
    interpretation.stop_loss = Decimal(90)
    interpretation.evidence = [proof.model_copy(update={"quote": "SL 90"}) if proof.field == "stop_loss" else proof for proof in interpretation.evidence]
    with pytest.raises(AqlaError, match="Evidence does not occur"):
        verify_evidence(interpretation, state.snapshot()["events"])
    assert fake.actions == []


@pytest.mark.parametrize("reverse", [True, False])
def test_conflicting_same_timestamp_revisions_are_rejected_independently_of_arrival_order(setup, signal, reverse):
    _, state, _, _, trading = setup
    source, interpretation = signal
    conflict = source.model_copy(update={"text": source.text + " Changed"})
    newer = source.model_copy(update={"edited_at": source.sent_at + 1})
    trading.ingest(conflict)
    trading.ingest(newer)
    events = list(state.snapshot()["events"].items())
    events = OrderedDict(reversed(events) if reverse else events)
    assert latest_sources(events, interpretation.source_refs)[source.source_id]["event_id"] == newer.event_id
    verify_evidence(interpretation, events)
    current_conflict = newer.model_copy(update={"text": newer.text + " conflicting current edit"})
    trading.ingest(current_conflict)
    with pytest.raises(AqlaError, match="conflicting content"):
        latest_sources(state.snapshot()["events"], interpretation.source_refs)
