"""Current source contracts repair errors without changing archived interpretations."""
import copy
import json
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from aqla_agent.source_disposition import issue_receipt, verify_disposition
from aqla_hyperliquid_mcp.models import (
    AqlaError,
    Evidence,
    Interpretation,
    SourceClassification,
    SourceEvent,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import verify_evidence
from aqla_hyperliquid_mcp.server import close_server, create_server


def axs(trading, *, text="#AXS LONG X35 TP 1.3; 1.33"):
    source = SourceEvent(chat_id="channel", message_id=4996, sent_at=now_ms(), text=text)
    trading.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="AXS", side="long",
        leverage=35, entry="market", take_profits=["1.3", "1.33"], evidence=[
            Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                ("symbol", "AXS"), ("side", "LONG"), ("leverage", "X35"),
                ("take_profits.0", "1.3;"), ("take_profits.1", "1.33"))])
    return source, value


def test_missing_context_and_all_indexed_prices_return_one_repair(setup):
    _, state, _, _, trading = setup
    source, value = axs(trading)
    value.evidence = [Evidence(field="take_profits", source_id=source.source_id, quote="1.3; 1.33")]
    with pytest.raises(AqlaError) as failure:
        verify_evidence(value, state.snapshot()["events"])
    error = str(failure.value)
    for field in ("symbol", "side", "leverage", "take_profits.0", "take_profits.1"):
        assert field in error
    assert "Unsupported source evidence fields: take_profits" in error
    assert "one exact original quote per field" in error
    assert "#AXS" not in error and len(error) < 1000
    assert not state.snapshot()["plans"]


def test_indexed_original_prices_pass_and_shared_multi_number_quote_fails(setup):
    _, state, _, _, trading = setup
    source, value = axs(trading)
    verify_evidence(value, state.snapshot()["events"])
    value.evidence = [proof if not proof.field.startswith("take_profits.") else
        proof.model_copy(update={"quote": "1.3; 1.33"}) for proof in value.evidence]
    with pytest.raises(AqlaError) as failure:
        verify_evidence(value, state.snapshot()["events"])
    assert "isolate one number: take_profits.0" in str(failure.value)
    assert "isolate one number: take_profits.1" in str(failure.value)
    assert not state.snapshot()["plans"]


def test_comma_original_quote_kept_but_tool_values_use_dot_decimals(setup):
    _, state, _, _, trading = setup
    _, value = axs(trading, text="#AXS LONG X35 TP 1.3; 1,33")
    value.evidence[-1].quote = "1,33"
    verify_evidence(value, state.snapshot()["events"])
    invalid = value.model_dump(mode="json")
    invalid["take_profits"][1] = "1,33"
    with pytest.raises(ValidationError, match="dot decimals.*1.33"):
        Interpretation.model_validate(invalid)
    assert value.take_profits == [Decimal("1.3"), Decimal("1.33")]
    assert value.evidence[-1].quote == "1,33"


def test_legacy_evidence_names_and_serialization_stay_readable():
    value = Interpretation(kind="ambiguous", source_refs=["historical"], reason="historical decision",
        evidence=[Evidence(field="old.custom.evidence", source_id="historical", quote="50%")])
    stored = value.model_dump(mode="json")
    assert "reason_codes" not in stored
    assert Interpretation.model_validate(stored).model_dump(mode="json") == stored
    batch = SourceClassification(event_id="historical-event", kind="ambiguous", source_refs=["historical"],
        evidence=value.evidence, reason=value.reason, source_read_receipt="historical-receipt")
    assert "reason_codes" not in batch.model_dump()


@pytest.mark.asyncio
async def test_actual_mcp_schema_has_indexed_fields_and_decimal_guidance(setup):
    settings, state, _, exchange, trading = setup
    server = create_server(settings, state=state, exchange=exchange)
    try:
        tool = next(tool for tool in await server.list_tools() if tool.name == "interpret_source")
        schema = tool.input_schema
        _, value = axs(trading)
        arguments = {"event_id": "fixture", "interpretation": value.model_dump(mode="json")}
        validator = Draft202012Validator(schema)
        assert not list(validator.iter_errors(arguments))
        invalid = copy.deepcopy(arguments)
        invalid["interpretation"]["evidence"][-1]["field"] = "take_profits"
        assert any(error.validator == "enum" for error in validator.iter_errors(invalid))
        invalid = copy.deepcopy(arguments)
        invalid["interpretation"]["take_profits"][-1] = "1,33"
        assert list(validator.iter_errors(invalid))
        assert "dot-decimal" in json.dumps(schema) and "take_profits.1" in json.dumps(schema)
    finally:
        await close_server(server)


def percentage_source(trading, *, message_id=1):
    source = SourceEvent(chat_id="channel", message_id=message_id, sent_at=now_ms(),
        text="STRK SHORT X35 TP 50% / 100%")
    trading.ingest(source)
    return source


def classification(source, reason, *, codes=()):
    return Interpretation(kind="ambiguous", source_refs=[source.source_id], reason=reason, reason_codes=list(codes),
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])


@pytest.mark.parametrize("kind", ["ambiguous", "incomplete"])
@pytest.mark.parametrize("reason,codes", [
    ("Percentage basis is unclear: price movement vs nominal ROE", ["percentage_basis"]),
    ("Targets 50% / 100% ambiguous: price movement or nominal ROE", []),
    ("Ziele 50 % / 100 % sind nicht eindeutig belegbar (Kursbewegung vs. nominale ROE)", []),
    ("Цели 50% / 100% неоднозначны: движение цены или ROE", []),
    ("TP-Prozente unklar: Kursbewegung oder ROE", ["other"]),
    ("TP-Prozentbasis unbekannt (Kurs vs ROE)", ["percentage_basis", "other"]),
    ("Target percent basis unknown: course or ROE", ["other"]),
    ("Цели 50%: неизвестно, движение цены или ROE", ["other"]),
])
def test_fixed_percentage_policy_cannot_be_only_non_actionable_reason(setup, kind, reason, codes):
    _, state, fake, _, trading = setup
    source = percentage_source(trading)
    key = b"synthetic-source-receipt-key-32-bytes"
    value = classification(source, reason, codes=codes).model_copy(update={"kind": kind})
    before = state.snapshot()
    token = issue_receipt(before["events"], source.event_id, key)
    with pytest.raises(AqlaError, match="source-roe-v1.*roe_percent"):
        verify_disposition(before, source.event_id, value, token, key)
    assert state.snapshot() == before and not fake.actions


@pytest.mark.parametrize("kind,reason,codes", [
    ("report", "Result report mentions 50%, historical ROI unclear", []),
    ("unrelated", "Article about 50% returns: nominal ROE unclear", ["other"]),
    ("ambiguous", "Risk margin percentage 40% is unclear", []),
    ("incomplete", "Margin allocation 40% is unknown", ["other"]),
])
def test_roi_commentary_and_risk_percentages_are_not_tp_policy_failures(setup, kind, reason, codes):
    _, state, fake, _, trading = setup
    source = SourceEvent(chat_id="channel", message_id=7, sent_at=now_ms(), text="Report: allocation 40% of margin")
    trading.ingest(source)
    key = b"synthetic-source-receipt-key-32-bytes"
    value = classification(source, reason, codes=codes).model_copy(update={"kind": kind})
    before = state.snapshot()
    token = issue_receipt(before["events"], source.event_id, key)
    verify_disposition(before, source.event_id, value, token, key)
    assert state.snapshot() == before and not fake.actions


@pytest.mark.parametrize("kind", ["report", "unrelated"])
def test_explicit_percentage_basis_cannot_be_relabelled_as_non_trade(setup, kind):
    _, state, fake, _, trading = setup
    source = percentage_source(trading)
    key = b"synthetic-source-receipt-key-32-bytes"
    value = classification(source, "Percentage basis is unclear", codes=["percentage_basis"]).model_copy(update={"kind": kind})
    before = state.snapshot()
    token = issue_receipt(before["events"], source.event_id, key)
    with pytest.raises(AqlaError, match="source-roe-v1"):
        verify_disposition(before, source.event_id, value, token, key)
    assert state.snapshot() == before and not fake.actions


@pytest.mark.parametrize("reason,codes", [
    ("Percentage basis unclear; market missing", ["percentage_basis", "missing_market"]),
    ("Percentage basis unclear; direction unknown", []),
    ("Ziele 50% uneindeutig als Kurs/ROE; Referenz fehlt", []),
    ("Цели 50% неоднозначны (цены/ROE); направление неизвестно", []),
    ("Conflicting source directions", ["contradictory_source"]),
    ("Missing entry price for deferred source entry", ["missing_entry_price"]),
    ("Result report, no new entry", ["result_report"]),
])
def test_independent_ambiguity_is_not_auto_promoted_to_trade(setup, reason, codes):
    _, state, fake, _, trading = setup
    source = percentage_source(trading)
    key = b"synthetic-source-receipt-key-32-bytes"
    value = classification(source, reason, codes=codes)
    before = state.snapshot()
    token = issue_receipt(before["events"], source.event_id, key)
    verify_disposition(before, source.event_id, value, token, key)
    assert state.snapshot() == before and not fake.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["interpret_source", "trade_preview"])
async def test_public_percent_rejection_keeps_anchor_ready_and_all_proofs_enforced(setup, tool):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    try:
        source = percentage_source(server.aqla_trading)
        receipt = server.aqla_trading.context(source.event_id)["source_read_receipt"]
        value = classification(source, "TP percent unclear: price movement vs ROE")
        arguments = {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
            "context_auth": server.aqla_authority.sign("source", "fixture"), "source_read_receipt": receipt}
        before = state.snapshot()
        with pytest.raises(ToolError, match="source-roe-v1"):
            await server.call_tool(tool, arguments)
        assert state.snapshot() == before
        arguments["source_read_receipt"] = "forged"
        with pytest.raises(ToolError, match="read receipt"):
            await server.call_tool(tool, arguments)
        assert state.snapshot() == before and not fake.actions
    finally:
        await close_server(server)


@pytest.mark.asyncio
async def test_batch_percent_failure_cannot_commit_or_authorize_other_item(setup):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    try:
        first = percentage_source(server.aqla_trading)
        second = percentage_source(server.aqla_trading, message_id=2)
        values = [classification(first, "TP percentage unclear: course vs ROE", codes=["percentage_basis"]),
            classification(second, "Direction unknown", codes=["missing_direction"])]
        items = [{**value.model_dump(mode="json", include={"kind", "source_refs", "reason", "reason_codes", "evidence"}), "event_id": source.event_id,
            "source_read_receipt": server.aqla_trading.context(source.event_id)["source_read_receipt"]}
            for source, value in zip([first, second], values, strict=True)]
        before = copy.deepcopy(state.snapshot()["events"][first.event_id])
        result = await server.call_tool("interpret_source_batch", {
            "items": items, "context_auth": server.aqla_authority.sign("source", "fixture")})
        payload = result.structured_content or json.loads(next(block.text for block in result.content if block.type == "text"))
        assert payload["succeeded"] == 1 and payload["partial_success"]
        assert not payload["items"][0]["ok"] and "source-roe-v1" in payload["items"][0]["error"]
        assert payload["items"][1]["ok"] and payload["items"][1]["status"] == "ambiguous"
        assert state.snapshot()["events"][first.event_id] == before
        assert not fake.actions and not state.snapshot()["plans"] and not state.snapshot()["trades"]
    finally:
        await close_server(server)
