import copy
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_agent.decisions import build_packet
from aqla_agent.efficiency import SOURCE_TOOLS
from aqla_agent.source_assistance import check_interpretation, precise_numeric_evidence, source_assistance
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server


def material(text="BTC LONG. Entry 100. Stop 90. Target 1: 110. Target 2: 120."):
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=now_ms(), text=text)
    row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
    context = {"event": row, "messages": [row], "trades": []}
    interpretation = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long",
        entry_prices=[100], stop_loss=90, take_profits=[110, 120], evidence=[
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "Entry 100"),
                ("stop_loss", "Stop 90"), ("take_profits.0", "Target 1: 110"), ("take_profits.1", "Target 2: 120"))])
    return source, context, interpretation


def test_check_shortens_original_proofs_but_does_not_change_values_or_input():
    _, context, original = material()
    before = copy.deepcopy(context), original.model_dump(mode="json")
    result = check_interpretation(context, original)
    assert result["valid"] and result["execution_authorized"] is False
    checked = Interpretation.model_validate(result["interpretation"])
    assert [p.quote for p in checked.evidence][-2:] == ["110", "120"]
    assert checked.model_dump(exclude={"evidence"}) == original.model_dump(exclude={"evidence"})
    assert (context, original.model_dump(mode="json")) == before


def test_repeated_integer_price_needs_context_and_a_unique_numbered_line_survives_formatting():
    text = "BTC SHORT. Entry 2. Stop 3.\nTarget 1: 1.8\nTarget 2: 1.4\nTarget 3: 1\nRisk 1%"
    source, context, _ = material(text)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="short",
        entry_prices=[2], stop_loss=3, take_profits=["1.8", "1.4", 1], evidence=[
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "BTC"), ("side", "SHORT"), ("entry_prices.0", "Entry 2"), ("stop_loss", "Stop 3"),
                ("take_profits.0", "1.8"), ("take_profits.1", "1.4"), ("take_profits.2", "1"))])
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "source_numeric_proof_needs_context"
    assert set(result["errors"][0]["field_paths"]) - {"evidence"} == {"take_profits.2"}
    value.evidence[-1].quote = "Target 3: 1"
    result = check_interpretation(context, value)
    assert result["valid"] and result["interpretation"]["evidence"][-1]["quote"] == "Target 3: 1"


@pytest.mark.parametrize("quote", ["1", "1.2", "Target 3: 1", "Target 3: 1.2"])
def test_cropping_decimal_digits_cannot_manufacture_a_price_proof(quote):
    from aqla_hyperliquid_mcp.rules import numeric_evidence_values
    assert numeric_evidence_values("Target 3: 1.25", quote) is None


@pytest.mark.parametrize("quote", ["Target 1: 1", "Target 3: 1", "Target 1: 1%"])
def test_real_integer_equal_to_a_numbering_label_remains_a_value_when_context_is_kept(quote):
    from decimal import Decimal

    from aqla_hyperliquid_mcp.rules import numeric_evidence_values
    assert numeric_evidence_values(quote, quote) == {Decimal(1)}


@pytest.mark.parametrize("quote,text", [
    ("Target 1: 111", "BTC LONG. Target 1: 110"),
    ("Target 1: 110", "Target 1: 110; Target 1: 110"),
    ("1) 1", "1) 1"),
    ("TP 1: 1.234", "TP 1: 1.234"),
])
def test_fabricated_repeated_and_ambiguous_quotes_are_not_rewritten(quote, text):
    source = SourceEvent(chat_id="channel", message_id=2, sent_at=now_ms(), text=text)
    packet = build_packet({"event": {"event_id": source.event_id}, "messages": [
        {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}], "trades": []})
    value = 1 if quote == "1) 1" else "1.234" if "1.234" in quote else 110
    original = Interpretation(kind="new_signal", source_refs=[source.source_id], take_profits=[value],
        evidence=[Evidence(field="take_profits.0", source_id=source.source_id, quote=quote)])
    assert precise_numeric_evidence(packet, original).evidence[0].quote == quote


def test_percent_and_comma_spelling_survive_and_never_convert_to_prices():
    _, context, value = material("BTC LONG. Entry 100. Stop 90. Target 1: 11,5%. Target 2: 20%.")
    value.take_profits = ["11.5", 20]
    value.take_profit_unit = "roe_percent"
    value.evidence[-2].quote, value.evidence[-1].quote = "Target 1: 11,5%", "Target 2: 20%"
    result = check_interpretation(context, value)
    assert result["valid"]
    assert [p["quote"] for p in result["interpretation"]["evidence"]][-2:] == ["11,5%", "20%"]
    value.take_profit_unit = "price"
    assert not check_interpretation(context, value)["valid"]


@pytest.mark.parametrize("targets,unit", [([], "price"), ([50], "roe_percent"), ([50, 100], "price")])
def test_explicit_percent_targets_cannot_disappear_into_backup_or_allocations(targets, unit):
    source, context, value = material("BTC LONG. Entry 100. Stop 90.\nЦели: 50% / 100%\nMargin: 2% of deposit")
    assert "new_signal" not in source_assistance(context)["original_structure_vetoes"]
    value.take_profits, value.take_profit_unit = targets, unit
    value.evidence = value.evidence[:4] + [Evidence(field=f"take_profits.{i}", source_id=source.source_id, quote=f"{number}%")
        for i, number in enumerate(targets)]
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "source_percentage_targets_missing_or_changed"
    assert result["errors"][0]["field_paths"] == ["take_profits", "take_profit_unit"]
    value.take_profits, value.take_profit_unit = [50, 100], "roe_percent"
    value.evidence = value.evidence[:4] + [Evidence(field=f"take_profits.{i}", source_id=source.source_id, quote=f"{number}%")
        for i, number in enumerate((50, 100))]
    assert check_interpretation(context, value)["valid"]


def test_percent_completeness_applies_only_to_new_target_updates_and_current_source_sections():
    from aqla_agent.semantic_contract import source_percentage_target_failure
    value = Interpretation(kind="trade_update", symbol="BTC", source_refs=["current"], update_action="replace_targets")
    text = "Targets: 50% / 100%"
    assert source_percentage_target_failure(text, value) == "source_percentage_targets_missing_or_changed"
    value.take_profits, value.take_profit_unit = [50, 100], "roe_percent"
    assert source_percentage_target_failure(text, value) is None
    value.take_profits = []
    assert source_percentage_target_failure("Earlier result.\n" + text, value) is None
    value.update_action = "break_even"
    assert source_percentage_target_failure(text, value) is None


@pytest.mark.parametrize("text,expected", [
    ("🎯 Targets: 50% / 100%\nMargin: 2%", [50, 100]),
    ("Ziele: 12,5% / 25%.", ["12.5", 25]),
    ("Targets: 110 (50% position) / 120 (50% position)", []),
    ("Targets: 50% of position at 110", []),
    ("Old targets: 50% / 100%", []),
    ("Margin: 50% / 100%", []),
    ("Targets: 1.234% / 2000%", []),
    ("Targets -50% / 100%", []),
])
def test_percent_target_recognizer_keeps_mixed_allocations_and_ambiguous_numbers_for_review(text, expected):
    from decimal import Decimal

    from aqla_agent.percent_targets import explicit_percentage_targets
    assert explicit_percentage_targets(text) == [Decimal(str(value)) for value in expected]


@pytest.mark.parametrize("label", ["Target 1:", "TP 1:", "1)", "Ziel 1:", "Цель 1:"])
@pytest.mark.parametrize("quote_only_number", [False, True])
def test_numbered_label_never_becomes_a_price_even_when_submitted_already_shortened(label, quote_only_number):
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.rules import verify_evidence
    source, context, value = material(f"BTC LONG. Entry 100. Stop 90.\n{label} 110.")
    value.take_profits = [1]
    value.evidence = value.evidence[:4] + [Evidence(field="take_profits.0", source_id=source.source_id,
        quote="1" if quote_only_number else label + " 110")]
    result = check_interpretation(context, value)
    assert not result["valid"]
    assert result["interpretation"]["evidence"][-1]["quote"] == value.evidence[-1].quote
    with pytest.raises(AqlaError, match="numbering label"):
        verify_evidence(value, {source.event_id: context["event"]})
    assert not any(c["kind"] == "number" and c["value"] == "1" for c in source_assistance(context)["candidates"])


def test_assistance_reuses_original_contract_and_excludes_ordinals():
    source, context, _ = material()
    aid = source_assistance(context)
    assert "NON-FINANCIAL" in aid["contract"]["kind"]["criteria"]["unrelated"]
    assert "NEW management" in aid["contract"]["kind"]["criteria"]["trade_update"]
    assert all(c["quote"] == source.text[c["start"]:c["end"]] for c in aid["candidates"])
    assert not any(c["notation"] == "ordinal" for c in aid["candidates"])
    assert "mcp__aqla__source_check_interpretation" in SOURCE_TOOLS


def test_empty_incomplete_needs_no_quote_and_foreign_quotes_get_a_precise_repair():
    source, context, _ = material("")
    value = Interpretation(kind="incomplete", source_refs=[source.source_id], reason="No original text",
        evidence=[Evidence(field="classification", source_id="neighbour", quote="BTC profit")])
    checked = check_interpretation(context, value)
    assert not checked["valid"]
    assert checked["errors"][0]["field_paths"] == ["evidence.0"]
    assert "evidence=[]" in checked["errors"][0]["repair"]
    value.evidence = []
    assert check_interpretation(context, value)["valid"]
    for kind in ("report", "unrelated", "new_signal", "trade_update", "ambiguous"):
        value.kind = kind
        assert not check_interpretation(context, value)["valid"]


@pytest.mark.parametrize("value,unit,quote", [(1,"price","1"),(1,"roe_percent","1"),(2,"roe_percent","2%")])
def test_expected_move_interval_cannot_become_one_tp_endpoint(value, unit, quote, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, _ = material("BTC LONG\nExpected local move: 1-2%.")
    draft = Interpretation(kind="new_signal", symbol="BTC", side="long", take_profits=[value], take_profit_unit=unit,
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
            ("symbol","BTC"),("side","LONG"),("take_profits.0",quote))])
    checked = check_interpretation(context, draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "source_percentage_interval_needs_role_review"
    settings, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="percentage-interval endpoint"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    draft.take_profits, draft.take_profit_unit, draft.evidence = [], "price", draft.evidence[:2]
    assert check_interpretation(context, draft)["valid"]


@pytest.mark.parametrize("label", ["Targets:", "TP1:", "Цели:"])
def test_actual_tp_interval_cannot_be_erased_for_backup_and_keeps_real_ambiguity(label):
    source, context, draft = material("BTC LONG\n" + label + " 1%-2%")
    draft.entry_prices, draft.stop_loss, draft.take_profits = [], None, []
    draft.evidence = draft.evidence[:2]
    checked = check_interpretation(context, draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "source_percentage_target_interval_not_representable"
    ambiguous = Interpretation(kind="ambiguous", source_refs=[source.source_id],
        reason="Ambiguous ROE target percentage interval cannot be an ordered exit list", reason_codes=["other"],
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    assert check_interpretation(context, ambiguous)["valid"]


@pytest.mark.parametrize("goal", [
    "Locally only for 3-4% and no more.",
    "Expected local move: 3-4% and no more.",
    "Lokal nur für 3-4% und nicht mehr.",
    "Локально, это на 3-4% и не больше.",
    "Только на 3-4%.",
    "Risk: 1-2%. Locally only for 3-4% and no more.",
])
def test_binding_profit_span_requires_ambiguity_and_cannot_use_backup(goal, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, draft = material("BTC LONG\n" + goal)
    draft.entry_prices, draft.stop_loss, draft.take_profits = [], None, []
    draft.evidence = draft.evidence[:2]
    checked = check_interpretation(context, draft)
    assert not checked["valid"]
    assert checked["errors"][0]["code"] == "source_percentage_target_interval_not_representable"
    _, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="source_percentage_target_interval_not_representable"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    ambiguous = Interpretation(kind="ambiguous", source_refs=[source.source_id],
        reason="Binding ROE profit interval cannot be executed as one exact exit list",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    assert check_interpretation(context, ambiguous)["valid"]


@pytest.mark.parametrize("metadata", [
    "Risk: at most 3-4%.", "Margin: only 3-4%.", "Nur 3-4% Risiko.",
    "Маржа: только 3-4% депозита.", "Риск не более 3-4%.",
])
def test_binding_risk_or_allocation_span_is_not_an_exit_instruction(metadata):
    source, context, draft = material("BTC LONG\n" + metadata)
    draft.entry_prices, draft.stop_loss, draft.take_profits = [], None, []
    draft.evidence = draft.evidence[:2]
    assert check_interpretation(context, draft)["valid"]


@pytest.mark.parametrize("targets", ["Targets: 1% / 2% / 3%", "Targets: 1% - 2% - 3%", "TP1: 1%\nTP2: 2%\nTP3: 3%"])
def test_discrete_percent_target_lists_are_not_cropped_into_an_interval(targets):
    source, context, _ = material("BTC LONG\n" + targets)
    draft = Interpretation(kind="new_signal", symbol="BTC", side="long", take_profits=[1,2,3], take_profit_unit="roe_percent",
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
            ("symbol","BTC"),("side","LONG"),("take_profits.0","1%"),("take_profits.1","2%"),("take_profits.2","3%"))])
    assert check_interpretation(context, draft)["valid"]


def test_separate_real_price_target_with_same_number_as_outlook_has_a_valid_unique_proof():
    source, context, draft = material("BTC SHORT. Entry 2. Stop 3. Target 1: 1.\nExpected local move: 1-2%.")
    draft.side, draft.entry_prices, draft.stop_loss, draft.take_profits = "short", [2], 3, [1]
    draft.evidence = [Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
        ("symbol","BTC"),("side","SHORT"),("entry_prices.0","2"),("stop_loss","3"),("take_profits.0","Target 1: 1"))]
    assert check_interpretation(context, draft)["valid"]


@pytest.mark.parametrize("status", ["Entry 2 ✅", "All entries achieved", "All entries filled", "All entries executed"])
def test_completed_entry_status_cannot_reopen_the_setup(status):
    source, context, draft = material("BTC LONG\n" + status + "\nAverage Entry Price: 100")
    draft.entry_prices, draft.stop_loss, draft.take_profits = [100], None, []
    draft.evidence = [Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
        ("symbol","BTC"),("side","LONG"),("entry_prices.0","100"))]
    checked = check_interpretation(context, draft)
    assert not checked["valid"]
    assert checked["errors"][0]["code"] == "entry_execution_report_not_new_signal"
    report = Interpretation(kind="report", source_refs=[source.source_id], reason="Completed entry status",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    assert check_interpretation(context, report)["valid"]


@pytest.mark.parametrize("caption", ["#setup_crypto", "#scenario_btc", "#план_крипто", "#сценарии_рынка"])
def test_bare_setup_topic_is_not_explicit_market_report_framing(caption):
    source, context, _ = material(caption)
    draft = Interpretation(kind="report", source_refs=[source.source_id], reason="Missing media",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=caption)])
    for kind in ("report", "unrelated"):
        draft.kind = kind
        checked = check_interpretation(context, draft)
        assert not checked["valid"]
        assert checked["errors"][0]["code"] == "source_setup_topic_needs_original_context"
    draft.kind = "incomplete"
    assert check_interpretation(context, draft)["valid"]
    assistance = source_assistance(context)
    assert assistance["original_structure_vetoes"]["report"] == "source_setup_topic_needs_original_context"


def test_discrete_entry_error_explains_why_renaming_to_range_is_not_a_correction():
    source, context, _ = material("BTC LONG\nEntry: limit orders at 100 and 95\nStop 90\nTarget 110")
    value = Interpretation(kind="new_signal", symbol="BTC", side="long", entry="range", entry_prices=[95,100],
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
            ("symbol","BTC"),("side","LONG"),("entry_prices.0","95"),("entry_prices.1","100"))])
    checked = check_interpretation(context, value)
    error = next(e for e in checked["errors"] if e["code"] == "entry_legs_not_representable")
    assert not checked["valid"] and {"kind","entry","entry_prices"} <= set(error["field_paths"])
    assert "single_entry_form" in error["repair"] and "ambiguous" in error["repair"]


@pytest.mark.parametrize("entry,values", [("unspecified", [95,100]), ("range", [95,100]),
    ("market", [100]), ("limit", [95]), ("unspecified", [100])])
@pytest.mark.parametrize("orders", [
    "Buy at current prices 100\nPending buy order 95",
    "Покупаем по текущим ценам = 100\nОтложенный ордер на покупку = 95",
    "Пoкупаем пo текущим ценам = 100\noтлoженный oрдер на пoкупку = 95",
])
def test_separate_current_and_pending_orders_cannot_be_renamed_or_partially_dropped(entry, values, orders, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, _ = material("BTC LONG\n" + orders + "\nStop 90\nTarget 110")
    draft = Interpretation(kind="new_signal", symbol="BTC", side="long", entry=entry, entry_prices=values,
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in
            [("symbol","BTC"),("side","LONG")]+[(f"entry_prices.{i}",str(v)) for i,v in enumerate(values)]])
    checked = check_interpretation(context, draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "entry_legs_not_representable"
    _, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="entry_legs_not_representable|at most one reference"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    review = Interpretation(kind="ambiguous", source_refs=[source.source_id], reason="Separate entry orders cannot fit one entry",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    assert check_interpretation(context, review)["valid"]
    assert source.text == context["event"]["source"]["text"]


@pytest.mark.parametrize("entry", ["market", "unspecified"])
def test_one_reference_limit_applies_even_without_recognized_entry_words(entry):
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.rules import verify_entry_shape
    with pytest.raises(AqlaError, match="at most one reference"):
        verify_entry_shape(Interpretation(kind="new_signal", source_refs=["current"], entry=entry, entry_prices=[95,100]))
    for values in ([],[100]):
        verify_entry_shape(Interpretation(kind="new_signal", source_refs=["current"], entry=entry, entry_prices=values))


@pytest.mark.parametrize("commentary", [
    "Price swept the historical low at 85 and reacted strongly.",
    "Цена сняла ликвидность с исторического минимума на 85 и отскочила.",
    "Der Kurs reagierte am historischen Tief 85.",
    "The asset failed to hold above the key resistance zone 85-86.",
    "Актив не смог закрепиться выше ключевой зоны 85-86.",
])
def test_historical_or_analysis_level_does_not_become_an_unstated_entry(commentary, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, _ = material("BTC LONG\n" + commentary + "\nStop 80\nTarget 110")
    draft = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[85], stop_loss=80, take_profits=[110],
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
            ("symbol","BTC"),("side","LONG"),("entry_prices.0","85"),("stop_loss","80"),("take_profits.0","110"))])
    checked = check_interpretation(context, draft)
    assert not checked["valid"]
    assert checked["errors"][0]["code"] == "source_entry_reference_needs_role_review"
    assert "entry_prices.0" in checked["errors"][0]["field_paths"]
    _, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="Entry evidence selects only an analysis level"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    draft.entry_prices = []
    draft.evidence = [proof for proof in draft.evidence if proof.field != "entry_prices.0"]
    corrected = check_interpretation(context, draft)
    assert corrected["valid"] and corrected["interpretation"]["kind"] == "new_signal"
    assert corrected["interpretation"]["stop_loss"] == "80" and corrected["interpretation"]["take_profits"] == ["110"]


@pytest.mark.parametrize("assignment", [
    "Entry: 85 at the historical low.", "Buy at support 85.",
    "Вход: 85 на историческом минимуме.", "Current price: 85 at resistance.",
    "Einstieg 85 am historischen Tief.",
])
def test_explicit_entry_assignment_or_current_price_can_use_a_technical_level(assignment):
    source, context, _ = material("BTC LONG\n" + assignment)
    draft = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[85], source_refs=[source.source_id],
        evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in (
            ("symbol","BTC"),("side","LONG"),("entry_prices.0","85"))])
    assert check_interpretation(context, draft)["valid"]


@pytest.mark.parametrize("management,expected", [
    ("Purchase from 100 we close at current price 110.", "long"),
    ("Close the purchase at current price 110.", "long"),
    ("Покупку от уровня 100 закрываем по текущим ценам 110.", "long"),
    ("Пoкупку oт урoвня 100 закрываем пo текущим ценам 110.", "long"),
    ("Продажу от уровня 110 закрываем по текущим ценам 100.", "short"),
])
def test_current_managed_purchase_or_sale_direction_cannot_be_omitted_or_reversed(management, expected, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, _ = material("BTC\n" + management)
    draft = Interpretation(kind="trade_update", symbol="BTC", update_action="close", source_refs=[source.source_id],
        evidence=[Evidence(field="symbol", source_id=source.source_id, quote="BTC"),
            Evidence(field="update_action", source_id=source.source_id, quote=management)])
    for side in (None,"short" if expected=="long" else "long"):
        draft.side = side
        checked = check_interpretation(context, draft)
        assert not checked["valid"] and checked["errors"][0]["code"] == "managed_position_direction_missing_or_changed"
    _, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="managed_position_direction_missing_or_changed"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    draft.side = expected
    draft.evidence.append(Evidence(field="side", source_id=source.source_id, quote=management))
    assert check_interpretation(context, draft)["valid"]


def test_update_direction_is_not_inferred_from_prices_or_market_opinion():
    source, context, _ = material("BTC\nClose the position from 100 at current price 110.\nI expect a short market move later.")
    draft = Interpretation(kind="trade_update", symbol="BTC", update_action="close", source_refs=[source.source_id],
        evidence=[Evidence(field="symbol", source_id=source.source_id, quote="BTC"),
            Evidence(field="update_action", source_id=source.source_id, quote="Close the position")])
    assert check_interpretation(context, draft)["valid"] and draft.side is None


@pytest.mark.parametrize("permission", [
    "Кто еще не фиксил - можно сделать это сейчас.",
    "Ктo еще не фиксил - мoжнo сделать этo сейчас.",
    "Кто еще не фиксировал, может это сделать сейчас.",
    "Who has not taken profit yet can do it now.",
    "Wer noch nicht Gewinne mitgenommen hat, kann das jetzt tun.",
])
def test_conditional_exit_permission_cannot_be_dropped_to_execute_only_the_stop(permission, setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    source, context, _ = material("BTC\n" + permission + "\nStop to entry now.")
    draft = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even", source_refs=[source.source_id],
        evidence=[Evidence(field="symbol", source_id=source.source_id, quote="BTC"),
            Evidence(field="update_action", source_id=source.source_id, quote="Stop to entry now")])
    checked = check_interpretation(context, draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "scheduled_management_not_representable"
    assert checked["errors"][0]["repair"] and checked["errors"][0]["field_paths"] == ["kind", "update_action"]
    _, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    trading.ingest(source)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="scheduled_management_not_representable"):
        trading.record_interpretation(source.event_id, draft)
    assert state.snapshot() == before and not fake.actions
    corrected = Interpretation(kind="ambiguous", source_refs=[source.source_id], reason="Conditional exit and separate stop cannot be partially executed",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    assert check_interpretation(context, corrected)["valid"]


@pytest.mark.parametrize("text", [
    "BTC LONG. Buy at current prices 100. No pending buy order 95.",
    "BTC LONG. Покупаем по текущим ценам 100. Не ставим отложенный ордер 95.",
    "BTC LONG. Pending buy order 100.", "BTC LONG. Entry range 95-100.",
])
def test_single_or_negated_pending_order_is_not_two_live_entry_legs(text):
    from aqla_agent.semantic_contract import entry_structure_failure
    assert entry_structure_failure(text) is None


@pytest.mark.parametrize("text", ["BTC first target hit. Stop to entry.", "BTC Ziel erreicht. Stopp auf Einstand.",
                                  "BTC тейк. Стоп на точку входа."])
def test_a_report_cannot_hide_one_current_management_instruction(text):
    source, context, _ = material(text)
    report = Interpretation(kind="report", source_refs=[source.source_id], reason="Past result",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=text)])
    result = check_interpretation(context, report)
    assert not result["valid"] and result["errors"][0]["code"] == "classification_omits_management_instruction"


@pytest.mark.parametrize("text", ["A daily close above 100 would confirm market structure.", "This might be an exit pump."])
def test_market_analysis_with_close_or_exit_nouns_remains_a_report(text):
    source, context, _ = material(text)
    report = Interpretation(kind="report", source_refs=[source.source_id], reason="Market analysis",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=text)])
    assert check_interpretation(context, report)["valid"]


@pytest.mark.parametrize("phrase", ["second entry if 95", "zweiter Einstieg bei 95", "второй набор в случае 95"])
def test_second_entry_leg_cannot_become_the_only_reference_price(phrase):
    source, context, _ = material("BTC LONG. Entry now, " + phrase + ". Stop 90. Target 110.")
    value = Interpretation(kind="new_signal", symbol="BTC", side="long", source_refs=[source.source_id],
        entry="market", entry_prices=[95], stop_loss=90, take_profits=[110], evidence=[
            Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
                ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "95"), ("stop_loss", "90"), ("take_profits.0", "110"))])
    assert not check_interpretation(context, value)["valid"]


@pytest.mark.parametrize("side,bound", [("short", "100+"), ("long", "100-"), ("short", ">=100"), ("long", "≤100")])
def test_one_directional_entry_boundary_requires_one_limit_without_changing_price(side, bound):
    text = f"BTC {side.upper()}\n{side.upper()}-zone: {bound}\nStop 90. Target 110."
    source, context, value = material(text)
    value.side = side
    value.take_profits = [110]
    value.evidence = [Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
        ("symbol", "BTC"), ("side", side.upper()), ("entry_prices.0", "100"), ("stop_loss", "90"), ("take_profits.0", "110"))]
    failure = check_interpretation(context, value)
    assert not failure["valid"] and failure["errors"][0]["code"] == "source_price_boundary_requires_limit"
    value.entry = "limit"
    result = check_interpretation(context, value)
    assert result["valid"] and result["interpretation"]["entry_prices"] == ["100"]


@pytest.mark.parametrize("selected", [None, 2, 5])
@pytest.mark.parametrize("interval", ["2-5x", "2x-5x", "x2-x5", "Leverage: 2-5"])
def test_leverage_interval_cannot_be_cherry_picked_or_declared_missing(selected, interval):
    source, context, value = material(f"BTC LONG ({interval}). Entry 100. Stop 90. Target 1: 110. Target 2: 120.")
    value.leverage = selected
    if selected:
        value.evidence.append(Evidence(field="leverage", source_id=source.source_id, quote=str(selected)))
    assert not check_interpretation(context, value)["valid"]
    value.kind = "ambiguous"
    value.entry_prices, value.take_profits, value.stop_loss, value.leverage = [], [], None, None
    value.evidence = [Evidence(field="classification", source_id=source.source_id, quote=f"BTC LONG ({interval})")]
    value.reason = "Source leverage is an interval; one source value cannot be selected."
    assert check_interpretation(context, value)["valid"]


@pytest.mark.parametrize("entry", ["market", "limit"])
def test_one_untyped_entry_reference_cannot_become_an_explicit_order_form(entry):
    _, context, value = material()
    value.entry = entry
    assert not check_interpretation(context, value)["valid"]
    value.entry = "unspecified"
    assert check_interpretation(context, value)["valid"]


def test_limit_inflection_is_shared_with_source_contract():
    source, context, value = material("BTC LONG. Покупка лимитным ордером: 100. Stop 90. Target 1: 110. Target 2: 120.")
    value.entry = "limit"
    value.evidence[2] = Evidence(field="entry_prices.0", source_id=source.source_id, quote="100")
    assert check_interpretation(context, value)["valid"]
    assert source_assistance(context)["order_form_mentions"][0]["form"] == "limit"


def test_symbol_alias_cannot_hide_the_source_leverage_interval():
    source, context, value = material("$BTC/USDT LONG (2-5x). Entry 100. Stop 90. Target 1: 110. Target 2: 120.")
    value.symbol = "BTCUSDT.P"
    value.evidence[0] = Evidence(field="symbol", source_id=source.source_id, quote="$BTC/USDT")
    assert not check_interpretation(context, value)["valid"]


def test_unaccepted_full_setup_edit_is_not_a_management_instruction():
    source, context, _ = material()
    value = Interpretation(kind="trade_update", symbol="BTC", side="long", update_action="move_stop", stop_loss=90,
        source_refs=[source.source_id], evidence=[Evidence(field="stop_loss", source_id=source.source_id, quote="90")])
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "unbound_setup_edit_is_not_management"
    context["bound_source_refs"] = [source.source_id]
    assert check_interpretation(context, value)["valid"]


@pytest.mark.parametrize("stop", ["Stop to 90", "Stopp auf 90", "Стоп на 90"])
@pytest.mark.parametrize("has_targets", [False, True])
def test_initial_numeric_stop_field_does_not_turn_full_setup_into_management(stop, has_targets):
    targets = "Target 1: 110\nTarget 2: 120\n" if has_targets else ""
    source, context, value = material(f"BTC LONG X30\n{targets}{stop}")
    value.entry_prices = []
    if not has_targets:
        value.take_profits = []
    value.evidence = [p for p in value.evidence if not p.field.startswith("entry_prices")
        and (has_targets or not p.field.startswith("take_profits"))]
    value.evidence[2].quote = "90"
    assert check_interpretation(context, value)["valid"]
    update = Interpretation(kind="trade_update", symbol="BTC", update_action="move_stop", stop_loss=90,
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q)
            for f, q in (("symbol", "BTC"), ("stop_loss", "90"))])
    assert not check_interpretation(context, update)["valid"]


@pytest.mark.parametrize("announcement,stop,direction", [
    ("По BTC целюсь в шорт.", "Стоп на 90", "шорт"),
    ("BTC: aiming for a short.", "Stop to 90", "short"),
    ("BTC: peile einen Short an.", "Stopp auf 90", "Short"),
])
def test_aimed_new_setup_initial_stop_needs_no_targets_or_entry_price(announcement, stop, direction):
    source, context, _ = material(announcement + "\n" + stop)
    value = Interpretation(kind="new_signal", symbol="BTC", side="short", stop_loss=90, source_refs=[source.source_id],
        evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
            ("symbol", "BTC"), ("side", direction), ("stop_loss", "90"))])
    assert "new_signal" not in source_assistance(context)["original_structure_vetoes"]
    assert check_interpretation(context, value)["valid"]
    value.kind, value.update_action = "trade_update", "move_stop"
    assert not check_interpretation(context, value)["valid"]


def test_unbound_update_cannot_keep_a_neighbour_market_after_removing_its_proof():
    source, context, _ = material("Taking 40% off here; moved stop loss to entry ($1,130).")
    value = Interpretation(kind="trade_update", symbol="ZEC", update_action="reduce", reduce_percent=40,
        source_refs=[source.source_id], evidence=[Evidence(field="reduce_percent", source_id=source.source_id, quote="40%")])
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "update_identity_missing"
    value.bound_trade_id = "model-guessed-trade"
    assert not check_interpretation(context, value)["valid"]
    value.bound_trade_id = None
    context["bound_source_refs"] = [source.source_id]
    assert check_interpretation(context, value)["valid"]


def test_real_reply_supplies_original_market_for_unbound_update():
    parent, context, _ = material("BTC LONG. Entry 100.")
    source = SourceEvent(chat_id=parent.chat_id, message_id=2, reply_to_id=parent.message_id,
        sent_at=now_ms(), text="Reduce 40% now.")
    row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
    context = {**context, "event": row, "messages": [*context["messages"], row]}
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="reduce", reduce_percent=40,
        source_refs=[source.source_id, parent.source_id], evidence=[
            Evidence(field="symbol", source_id=parent.source_id, quote="BTC"),
            Evidence(field="reduce_percent", source_id=source.source_id, quote="40%")])
    assert check_interpretation(context, value)["valid"]


def test_writer_rechecks_unbound_update_identity_before_saving(setup):
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.models import AqlaError
    settings, state, fake, exchange, _ = setup
    trading = Trading(exchange, state)
    source, _, _ = material("Reduce 40% now.")
    trading.ingest(source)
    before = state.snapshot()
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="reduce", reduce_percent=40,
        source_refs=[source.source_id], evidence=[Evidence(field="reduce_percent", source_id=source.source_id, quote="40%")])
    with pytest.raises(AqlaError, match="update_identity_missing"):
        trading.record_interpretation(source.event_id, value)
    assert state.snapshot() == before and not fake.actions


def test_closed_owned_setup_still_proves_binding_but_does_not_become_an_open_position():
    from aqla_agent.trading import Trading
    source, context, _ = material()
    index = Trading.context_index({"events": {source.event_id: context["event"]},
        "trades": {"owned": {"chat_id": source.chat_id, "status": "closed", "plan": {"source_refs": [source.source_id]}}}})
    assert not index[2] and index[3][source.chat_id] == {source.source_id}
    context["bound_source_refs"] = list(index[3][source.chat_id])
    assert source_assistance(context)["anchor_setup_bound"]


@pytest.mark.parametrize("action", [None, "move_stop", "replace_targets", "reduce"])
def test_check_does_not_accept_an_update_without_its_required_changed_value(action):
    source, context, _ = material("BTC: change the stop.")
    value = Interpretation(kind="trade_update", symbol="BTC", source_refs=[source.source_id], update_action=action)
    assert not check_interpretation(context, value)["valid"]


def test_one_check_reports_independent_reference_and_intent_errors_together():
    source, context, _ = material()
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="move_stop", stop_loss=90,
        source_refs=[source.event_id], evidence=[Evidence(field="stop_loss", source_id=source.source_id, quote="90")])
    result = check_interpretation(context, value)
    assert {e["code"] for e in result["errors"]} == {"unbound_setup_edit_is_not_management", "live_anchor_missing"}
    assert result["anchor_source_id"] == source.source_id
    assert result["interpretation"]["source_refs"] == [source.event_id]
    assert source_assistance(context)["original_structure_vetoes"]["trade_update"] == "unbound_setup_edit_is_not_management"


def test_nonactional_classification_targets_only_the_current_message():
    parent, _, _ = material("BTC LONG. Entry 100.")
    source = SourceEvent(chat_id=parent.chat_id, message_id=2, reply_to_id=parent.message_id, sent_at=now_ms(),
        text="Yesterday BTC reached its target.")
    rows = [{"event_id": s.event_id, "source_id": s.source_id, "source": s.model_dump(mode="json")} for s in (parent, source)]
    context = {"event": rows[-1], "messages": rows, "trades": []}
    value = Interpretation(kind="report", source_refs=[source.source_id, parent.source_id], reason="Completed result",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "classification_scope_requires_anchor_only"
    value.source_refs = [source.source_id]
    assert check_interpretation(context, value)["valid"]


def test_update_rejects_only_redundant_unbound_parent_identity():
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.rules import verify_setup_references
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even", source_refs=["current", "parent"],
        evidence=[Evidence(field="symbol", source_id=ref, quote="BTC") for ref in ("current", "parent")])
    with pytest.raises(AqlaError, match="redundant parent symbol"):
        verify_setup_references(value, "current")
    verify_setup_references(value, "current", iter(["parent"]))
    # A short reply genuinely needs its parent's market; it is not redundant.
    value.evidence = value.evidence[1:]
    verify_setup_references(value, "current")
    value.evidence.insert(0, Evidence(field="symbol", source_id="current", quote="BTC"))
    value.evidence.append(Evidence(field="update_action", source_id="parent", quote="Stop to entry"))
    verify_setup_references(value, "current")


def test_parent_classification_does_not_count_as_a_contributed_actionable_field():
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.rules import verify_setup_references
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even", source_refs=["current", "parent"],
        evidence=[Evidence(field="symbol", source_id="current", quote="BTC"),
            Evidence(field="classification", source_id="parent", quote="BTC LONG")])
    with pytest.raises(AqlaError, match="classification quotes alone"):
        verify_setup_references(value, "current")
    verify_setup_references(value, "current", ["parent"])
    value.evidence[-1] = Evidence(field="symbol", source_id="parent", quote="BTC")
    value.evidence[0].field = "update_action"
    verify_setup_references(value, "current")


def test_check_preserves_trusted_closed_parent_binding_without_exposing_a_position():
    parent, _, _ = material()
    source = SourceEvent(chat_id=parent.chat_id, message_id=2, reply_to_id=parent.message_id,
        sent_at=now_ms(), text="BTC stop to entry.")
    rows = [{"event_id": s.event_id, "source_id": s.source_id, "source": s.model_dump(mode="json")} for s in (parent, source)]
    context = {"event": rows[-1], "messages": rows, "trades": []}
    value = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even",
        source_refs=[source.source_id, parent.source_id], evidence=[
            Evidence(field="symbol", source_id=s.source_id, quote="BTC") for s in (parent, source)])
    assert not check_interpretation(context, value)["valid"]
    context["bound_source_refs"] = [parent.source_id]
    assert check_interpretation(context, value)["valid"] and context["trades"] == []


def test_unbound_update_uses_its_explicit_current_market_instead_of_duplicate_parent_identity():
    parent, _, _ = material()
    source = SourceEvent(chat_id=parent.chat_id, message_id=2, reply_to_id=parent.message_id,
        sent_at=now_ms(), text="BTC target reached. Stop to entry. ETH had a small loss.")
    rows = [{"event_id": s.event_id, "source_id": s.source_id, "source": s.model_dump(mode="json")} for s in (parent, source)]
    context = {"event": rows[-1], "messages": rows, "trades": []}
    draft = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even",
        source_refs=[source.source_id, parent.source_id], evidence=[
            Evidence(field="symbol", source_id=parent.source_id, quote="BTC"),
            Evidence(field="update_action", source_id=source.source_id, quote="Stop to entry")])
    checked = check_interpretation(context, draft)
    assert not checked["valid"] and checked["errors"][0]["code"] == "update_identity_needs_current_quote"
    context["bound_source_refs"] = [parent.source_id]
    assert check_interpretation(context, draft)["valid"]
    context.pop("bound_source_refs")
    draft.evidence[0].source_id = source.source_id
    draft.source_refs = [source.source_id]
    assert check_interpretation(context, draft)["valid"]


@pytest.mark.parametrize("action,field,values,quote", [("move_stop", "stop_loss", {"stop_loss": 95}, "95"),
    ("replace_targets", "take_profits.0", {"take_profits": [110]}, "110"), ("reduce", "reduce_percent", {"reduce_percent": 50}, "50%")])
def test_update_value_proof_must_be_current_even_when_parent_is_our_owned_setup(action, field, values, quote):
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.rules import verify_setup_references
    value = Interpretation(kind="trade_update", symbol="BTC", source_refs=["current", "parent"], update_action=action,
        **values, evidence=[Evidence(field=field, source_id="parent", quote=quote)])
    with pytest.raises(AqlaError, match="current anchor, not copied parent"):
        verify_setup_references(value, "current", ["parent"])
    value.evidence[0].source_id = "current"
    verify_setup_references(value, "current", ["parent"])


def test_large_original_keeps_binding_and_contract_when_candidate_limit_is_exceeded():
    source, context, _ = material("BTC LONG. " + "Target 110. " * 300)
    context["bound_source_refs"] = [source.source_id]
    aid = source_assistance(context)
    assert aid["anchor_setup_bound"] and aid["contract"]["kind"]
    assert not aid["candidates_complete"] and aid["candidate_error"] == "candidate_limit"


def test_explicit_example_framing_cannot_authorize_its_quoted_setup():
    text = "BTC LONG. Entry 100. Stop 90. Target 1: 110. Target 2: 120.\nEverything above is an example."
    source, context, value = material(text)
    result = check_interpretation(context, value)
    assert not result["valid"] and result["errors"][0]["code"] == "source_example_requires_report_review"
    assert "example" in result["errors"][0]["repair"]
    report = Interpretation(kind="report", source_refs=[source.source_id], reason="Explicitly scoped example",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote="Everything above is an example.")])
    assert check_interpretation(context, report)["valid"]


@pytest.mark.asyncio
async def test_real_mcp_check_is_read_only_and_cannot_replace_auth_or_freshness(setup):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source, _, value = material()
    server.aqla_trading.ingest(source)
    before = state.snapshot()
    try:
        reply = await server.call_tool("source_check_interpretation", {"event_id": source.event_id,
            "interpretation": value.model_dump(mode="json")})
        checked = reply.structured_content or json.loads(reply.content[0].text)
        assert checked["valid"] and state.snapshot() == before and not fake.actions
        with pytest.raises(ToolError, match="auth|context|origin|signature|authority"):
            await server.call_tool("interpret_source", {"event_id": source.event_id, "interpretation": checked["interpretation"]})
        edit = source.model_copy(update={"text": source.text.replace("110", "111"), "edited_at": now_ms()+1})
        server.aqla_trading.ingest(edit)
        reply = await server.call_tool("source_check_interpretation", {"event_id": source.event_id,
            "interpretation": checked["interpretation"]})
        stale = reply.structured_content or json.loads(reply.content[0].text)
        assert not stale["valid"] and stale["reread_required"] and not fake.actions
        assert stale["source_version"] != checked["source_version"]
    finally:
        await close_server(server)
