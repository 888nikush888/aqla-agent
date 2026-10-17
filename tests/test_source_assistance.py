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
