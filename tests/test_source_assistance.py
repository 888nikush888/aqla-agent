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
