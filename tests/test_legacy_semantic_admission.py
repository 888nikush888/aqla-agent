"""Legacy Hermes tool admission retains original-text safety without model calls."""
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server


@pytest.fixture
async def legacy_server(setup):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    yield server, state, fake
    await close_server(server)


def original(server, text, **fields):
    source = SourceEvent(chat_id="channel", message_id=900, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    quotes = fields.pop("quotes", [("classification", text)])
    value = Interpretation(source_refs=[source.source_id], **fields, evidence=[
        Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in quotes])
    receipt = server.aqla_trading.context(source.event_id)["source_read_receipt"]
    return source, value, receipt


CASES = [
    ("BTC LONG. Entry 100.\nTargets: 50% / 100%\nMargin: 2% of deposit",
     {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry_prices": [100],
      "quotes": [("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100")]}, "source_percentage_targets_missing_or_changed"),
    ("BTC LONG. Entry half MARKET 100 and half LIMIT 95. SL 90. TP 110.",
     {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry": "market", "entry_prices": [100], "stop_loss": 90,
      "take_profits": [110], "quotes": [("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100"),
          ("stop_loss", "90"), ("take_profits.0", "110")]}, "entry_legs_not_representable"),
    ("BTC reduce 50% and move stop to95.", {"kind": "trade_update", "symbol": "BTC", "update_action": "reduce", "reduce_percent": 50,
      "quotes": [("symbol", "BTC"), ("update_action", "reduce 50%"), ("reduce_percent", "50%")]}, "management_actions_not_representable"),
    ("BTC close tomorrow.", {"kind": "trade_update", "symbol": "BTC", "update_action": "close",
      "quotes": [("symbol", "BTC"), ("update_action", "close tomorrow")]}, "scheduled_management_not_representable"),
    ("BTC move stop after 1h candle close to95.", {"kind": "trade_update", "symbol": "BTC", "update_action": "move_stop", "stop_loss": 95,
      "quotes": [("symbol", "BTC"), ("update_action", "move stop after 1h candle close to95"), ("stop_loss", "95")]}, "conditional_stop_not_representable"),
    ("BTC LONG. Entry market 100. SL 90. TP 110. Another entry point in",
     {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry": "market", "entry_prices": [100], "stop_loss": 90,
      "take_profits": [110], "quotes": [("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100"),
          ("stop_loss", "90"), ("take_profits.0", "110")]}, "source_financial_instruction_incomplete"),
    ("BTC reduce 50% and move stop to95.", {"kind": "report", "reason": "Result report", "reason_codes": ["result_report"]},
     "management_actions_not_representable"),
    ("BTC target reached. Stop to entry.", {"kind": "report", "reason": "Result report", "reason_codes": ["result_report"]},
     "classification_omits_management_instruction"),
    ("BTC LONG. Entry MARKET 100. SL 90. TP 110.",
     {"kind": "trade_update", "symbol": "BTC", "side": "long", "update_action": "move_stop", "stop_loss": 90,
      "quotes": [("stop_loss", "90")]}, "unbound_setup_edit_is_not_management"),
    ("BTC LONG. Entry now; second entry if 95. SL 90. TP 110.",
     {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry": "market", "entry_prices": [95], "stop_loss": 90,
      "take_profits": [110], "quotes": [("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "95"),
          ("stop_loss", "90"), ("take_profits.0", "110")]}, "additional_entry_legs_not_representable"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["interpret_source", "trade_preview", "trade_update"])
@pytest.mark.parametrize("text,fields,code", CASES)
async def test_public_legacy_tools_reject_partial_or_unsupported_instructions_before_financial_work(legacy_server, tool, text, fields, code):
    server, state, fake = legacy_server
    source, value, receipt = original(server, text, **fields)
    args = {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
            "context_auth": server.aqla_authority.sign("source", "legacy-safety")}
    if tool != "trade_update":
        args["source_read_receipt"] = receipt
    expected = "receipt" if tool == "trade_update" and value.kind == "report" else code
    with pytest.raises(ToolError, match=expected):
        await server.call_tool(tool, args)
    row = state.snapshot()["events"][source.event_id]
    assert row["interpretation"] is None and row["status"] == "pending"
    assert not state.snapshot()["plans"] and not state.snapshot()["trades"] and not state.snapshot()["operations"]
    assert not state.snapshot().get("source_jobs", {}) and not fake.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("text,fields", [
    ("Earlier we agreed to close half and move stop to95.", {"kind": "report", "reason": "Past result", "reason_codes": ["result_report"]}),
    ("Trading example only: BTC close half and move stop to95.", {"kind": "report", "reason": "Example only", "reason_codes": ["result_report"]}),
    ("BTC LONG. Entry 100. SL 90. TP 110.", {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry": "market",
      "entry_prices": [100], "stop_loss": 90, "take_profits": [110], "quotes": [("symbol", "BTC"), ("side", "LONG"),
          ("entry_prices.0", "100"), ("stop_loss", "90"), ("take_profits.0", "110")]}),
    ("BTC: stop to entry now.", {"kind": "trade_update", "symbol": "BTC", "update_action": "break_even",
      "quotes": [("symbol", "BTC"), ("update_action", "stop to entry now")]}),
    ("BTC move stop to95.", {"kind": "trade_update", "symbol": "BTC", "update_action": "move_stop", "stop_loss": 95,
      "quotes": [("symbol", "BTC"), ("update_action", "move stop to95"), ("stop_loss", "95")]}),
    ("BTC close now.", {"kind": "trade_update", "symbol": "BTC", "update_action": "close",
      "quotes": [("symbol", "BTC"), ("update_action", "close now")]}),
])
async def test_valid_legacy_report_signal_and_break_even_keep_existing_auth_and_evidence_contract(legacy_server, text, fields):
    server, state, fake = legacy_server
    source, value, receipt = original(server, text, **fields)
    args = {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"), "source_read_receipt": receipt}
    with pytest.raises(ToolError, match="auth|context|origin|signature|authority"):
        await server.call_tool("interpret_source", args)
    args["context_auth"] = server.aqla_authority.sign("source", "legacy-valid")
    await server.call_tool("interpret_source", args)
    row = state.snapshot()["events"][source.event_id]
    assert row["interpretation"] == value.model_dump(mode="json")
    assert not fake.actions and not state.snapshot()["operations"] and not state.snapshot().get("source_jobs", {})


@pytest.mark.asyncio
async def test_legacy_batch_preserves_partial_success_and_each_current_receipt(legacy_server):
    server, state, fake = legacy_server
    unsafe, value, receipt = original(server, "BTC reduce 50% and move stop to95.", kind="report", reason="Result report", reason_codes=["result_report"])
    safe = SourceEvent(chat_id="channel", message_id=901, sent_at=now_ms(), text="Yesterday BTC result report.")
    server.aqla_trading.ingest(safe)
    safe_value = Interpretation(kind="report", source_refs=[safe.source_id], reason="Past result", reason_codes=["result_report"],
        evidence=[Evidence(field="classification", source_id=safe.source_id, quote=safe.text)])
    items = [{"event_id": source.event_id, "source_read_receipt": proof,
              **interpretation.model_dump(mode="json", include={"kind", "source_refs", "reason", "reason_codes", "evidence"})}
             for source, proof, interpretation in ((unsafe, receipt, value), (safe, server.aqla_trading.context(safe.event_id)["source_read_receipt"], safe_value))]
    result = await server.call_tool("interpret_source_batch", {"items": items, "context_auth": server.aqla_authority.sign("source", "legacy-batch")})
    data = result.structured_content or json.loads(result.content[0].text)
    assert data["succeeded"] == 1 and data["partial_success"]
    assert not data["items"][0]["ok"] and "management_actions_not_representable" in data["items"][0]["error"]
    assert state.snapshot()["events"][unsafe.event_id]["interpretation"] is None
    assert state.snapshot()["events"][safe.event_id]["status"] == "report"
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.asyncio
async def test_single_and_batch_nonaction_writes_recheck_anchor_only_scope(legacy_server):
    server, state, fake = legacy_server
    parent = SourceEvent(chat_id="channel", message_id=910, sent_at=now_ms(), text="BTC LONG. Entry 100.")
    source = SourceEvent(chat_id="channel", message_id=911, reply_to_id=910, sent_at=now_ms(), text="Yesterday the target was reached.")
    for item in (parent, source):
        server.aqla_trading.ingest(item)
    value = Interpretation(kind="report", source_refs=[source.source_id, parent.source_id], reason="Completed report",
        evidence=[Evidence(field="classification", source_id=source.source_id, quote=source.text)])
    receipt = server.aqla_trading.context(source.event_id)["source_read_receipt"]
    auth = server.aqla_authority.sign("source", "classification-scope")
    with pytest.raises(ToolError, match="Non-actional source_refs"):
        await server.call_tool("interpret_source", {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
            "source_read_receipt": receipt, "context_auth": auth})
    item = value.model_dump(mode="json", include={"kind", "source_refs", "evidence", "reason", "reason_codes"})
    response = await server.call_tool("interpret_source_batch", {"items": [{**item, "event_id": source.event_id,
        "source_read_receipt": receipt}], "context_auth": auth})
    result = response.structured_content or json.loads(response.content[0].text)
    assert not result["items"][0]["ok"] and "Non-actional source_refs" in result["items"][0]["error"]
    assert state.snapshot()["events"][source.event_id]["interpretation"] is None
    assert not state.snapshot()["plans"] and not state.snapshot()["operations"] and not fake.actions


@pytest.mark.asyncio
async def test_actual_legacy_status_keeps_previous_decision_jobs_as_history(legacy_server):
    server, state, fake = legacy_server
    state.update_metadata(("source_jobs",), lambda data: data["source_jobs"].update({
        "previous-applying": {"status": "applying", "created_at": 1}, "previous-done": {"status": "done", "created_at": 2}}))
    response = await server.call_tool("status", {"view": "summary", "context_auth": server.aqla_authority.sign("operator", "legacy-status")})
    data = response.structured_content or json.loads(response.content[0].text)
    processing = data["source_processing"]
    assert processing["mode"] == "legacy" and processing["processor"] == "Hermes + DeepSeek + Tools"
    assert processing["jobs"] == {} and processing["historical_jobs"]["applying"] == 1 and processing["historical_jobs"]["done"] == 1
    assert processing["decision"] == {} and processing["diagnostics"] == []
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.parametrize("tool", ["interpret_source", "trade_preview"])
@pytest.mark.parametrize("tail", [
    "", "Do not use limit orders.", "No limit orders.", "Risk limit 1%.", "Limit risk to 1%.",
    "Keine Limit Orders.", "Risiko Limit 1%.", "Limit für Risiko 1%.",
    "Не используйте лимитные ордера.", "Лимит риска 1%.", "Риск лимит 1%.",
    "Do not use limit orders at 95 and 96.",
])
async def test_single_legacy_entry_ignores_negated_order_forms_and_risk_commentary(legacy_server, tool, tail):
    server, state, fake = legacy_server
    source, value, _ = original(server, "BTC LONG. Entry MARKET 100. SL 90. TP 110. " + tail,
        kind="new_signal", symbol="BTC", side="long", entry="market", entry_prices=[100], stop_loss=90,
        take_profits=[110], quotes=[("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100"),
                                  ("stop_loss", "90"), ("take_profits.0", "110")])
    await server.call_tool(tool, {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                                 "context_auth": server.aqla_authority.sign("source", "legacy-one-entry")})
    row = state.snapshot()["events"][source.event_id]
    assert row["interpretation"] == value.model_dump(mode="json")
    assert bool(state.snapshot()["plans"]) is (tool == "trade_preview")
    assert not fake.actions and not state.snapshot()["operations"]


@pytest.mark.parametrize("tool", ["interpret_source", "trade_preview"])
@pytest.mark.parametrize("entry", [
    "Entry half MARKET 100 and half LIMIT 95",
    "Einstieg Hälfte Markt 100 und Hälfte LIMIT 95",
    "Вход половина по рынку 100 и половина лимит 95",
    "Entry MARKET 100. No limit orders except LIMIT 95",
])
async def test_affirmative_mixed_legacy_legs_still_reject_before_orders(legacy_server, tool, entry):
    server, state, fake = legacy_server
    source, value, _ = original(server, "BTC LONG. " + entry + ". SL 90. TP 110.",
        kind="new_signal", symbol="BTC", side="long", entry="market", entry_prices=[100], stop_loss=90,
        take_profits=[110], quotes=[("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100"),
                                  ("stop_loss", "90"), ("take_profits.0", "110")])
    with pytest.raises(ToolError, match="entry_legs_not_representable"):
        await server.call_tool(tool, {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                                     "context_auth": server.aqla_authority.sign("source", "legacy-mixed-entry")})
    assert state.snapshot()["events"][source.event_id]["interpretation"] is None
    assert not state.snapshot()["plans"] and not state.snapshot()["operations"] and not fake.actions


@pytest.mark.parametrize("tool", ["interpret_source", "trade_preview"])
async def test_integer_sentence_boundaries_preserve_decimal_legacy_prices(legacy_server, tool):
    server, state, fake = legacy_server
    source, value, _ = original(server, "BTC LONG. Entry MARKET 100.25. SL 90.5. TP 110.75. Do not use limit orders.",
        kind="new_signal", symbol="BTC", side="long", entry="market", entry_prices=["100.25"], stop_loss="90.5",
        take_profits=["110.75"], quotes=[("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "100.25"),
                                       ("stop_loss", "90.5"), ("take_profits.0", "110.75")])
    await server.call_tool(tool, {"event_id": source.event_id, "interpretation": value.model_dump(mode="json"),
                                 "context_auth": server.aqla_authority.sign("source", "legacy-decimal-entry")})
    assert state.snapshot()["events"][source.event_id]["interpretation"] == value.model_dump(mode="json")
    assert not fake.actions and not state.snapshot()["operations"]
