
from decimal import Decimal

import pytest

from aqla_agent.source_assistance import check_interpretation, source_assistance
from aqla_hyperliquid_mcp.models import (
    Evidence,
    Interpretation,
    SourceEntryLeg,
    SourceEvent,
    now_ms,
    require_first,
)


def pair(trading, *, first=50, second=50, third=False):
    text = f"BTC LONG\nEntry 1 MARKET 100 ({first}%)\nEntry 2 LIMIT 95 ({second}%)\nSL 90\nTP 110"
    if third:
        text += "\nEntry 3 LIMIT 93"
    source = SourceEvent(chat_id="channel", message_id=90, sent_at=now_ms(), text=text)
    trading.ingest(source)
    proofs = [("symbol", "BTC"), ("side", "LONG"), ("stop_loss", "90"), ("take_profits.0", "110"),
        ("entry_legs.0.kind", f"Entry 1 MARKET 100 ({first}%)"), ("entry_legs.0.price", "100"),
        ("entry_legs.1.kind", f"Entry 2 LIMIT 95 ({second}%)"), ("entry_legs.1.price", "95")]
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", entry="split",
        entry_legs=[SourceEntryLeg(kind="market", price=100), SourceEntryLeg(kind="limit", price=95)],
        stop_loss=90, take_profits=[110], evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in proofs])
    return source, value


def test_two_complete_legs_pass_the_same_production_checker(setup):
    trading = setup[-1]
    source, value = pair(trading)
    result = check_interpretation(trading.context(source.event_id), value)
    assert result["valid"], result["errors"]
    assert len(result["interpretation"]["entry_legs"]) == 2
    brief = source_assistance(trading.context(source.event_id))
    assert "new_signal" not in brief["original_structure_vetoes"]
    assert "split" in brief["contract"]["entry"]["criteria"]


def test_weight_repair_identifies_the_missing_context_without_changing_values(setup):
    trading = setup[-1]
    source, value = pair(trading, first=25, second=75)
    for index, weight in enumerate((25, 75)):
        value.entry_legs[index].weight_percent = Decimal(weight)
        value.evidence.append(Evidence(field=f"entry_legs.{index}.weight_percent", source_id=source.source_id, quote=f"{weight}%"))
    assert check_interpretation(trading.context(source.event_id), value)["valid"]
    value.evidence = [proof.model_copy(update={"quote": proof.quote.split(" (")[0]})
                      if proof.field.endswith(".kind") else proof for proof in value.evidence]
    rejected = check_interpretation(trading.context(source.event_id), value)
    failure = require_first(row for row in rejected["errors"] if row["code"] == "split_entry_weights_missing_or_changed")
    assert "entry_legs.i.kind" in failure["repair"] and "INCLUDING its price AND allocation" in failure["repair"]
    assert [row["weight_percent"] for row in rejected["interpretation"]["entry_legs"]] == ["25", "75"]


def test_tp_label_digit_cannot_merge_with_its_three_digit_price(setup):
    trading = setup[-1]
    source, value = pair(trading)
    source = source.model_copy(update={"message_id": 92, "text": source.text.replace("TP 110", "TP1 110")})
    trading.ingest(source)
    value.source_refs = [source.source_id]
    value.evidence = [proof.model_copy(update={"source_id": source.source_id,
        "quote": "TP1 110" if proof.field == "take_profits.0" else proof.quote}) for proof in value.evidence]
    result = check_interpretation(trading.context(source.event_id), value)
    assert result["valid"], result["errors"]
    assert require_first(row["quote"] for row in result["interpretation"]["evidence"] if row["field"] == "take_profits.0") == "110"
    assert "entry_prices=[]" in source_assistance(trading.context(source.event_id))["contract"]["entry"]["criteria"]["split"]


@pytest.mark.parametrize("change,code", [("drop", "entry_legs_not_representable"), ("third", "entry_legs_not_representable"), ("weights", "split_entry_weights_missing_or_changed")])
def test_source_legs_or_weights_cannot_be_silently_dropped(setup, change, code):
    trading = setup[-1]
    source, value = pair(trading, first=25 if change == "weights" else 50, second=75 if change == "weights" else 50, third=change == "third")
    if change == "drop":
        value = value.model_copy(update={"entry": "range", "entry_legs": [], "entry_prices": [Decimal(95), Decimal(100)]})
    result = check_interpretation(trading.context(source.event_id), value)
    assert not result["valid"]
    assert code in {row["code"] for row in result["errors"]}


@pytest.mark.parametrize("heading", ["ETH LONG", "LONG ETH", "ETH лонг"])
def test_two_independent_bare_markets_are_not_a_two_entry_setup(setup, heading):
    trading = setup[-1]
    source, value = pair(trading)
    changed = source.model_copy(update={"message_id": 91, "text": source.text.replace("Entry 2", heading + "\nEntry 2")})
    trading.ingest(changed)
    value.source_refs = [changed.source_id]
    value.evidence = [proof.model_copy(update={"source_id": changed.source_id}) for proof in value.evidence]
    result = check_interpretation(trading.context(changed.event_id), value)
    assert not result["valid"]
    assert "entry_legs_not_representable" in {row["code"] for row in result["errors"]}
