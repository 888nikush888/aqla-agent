
import copy
import json

import pytest

from aqla_agent.efficiency import decision_context
from aqla_agent.execution_outcome import (
    diagnostic_view,
    event_outcomes,
    execution_counts,
    retry_view,
    trade_outcome,
)
from aqla_agent.messages import status_text, telegram_view
from aqla_agent.tool_queries import source_lookup
from aqla_hyperliquid_mcp.models import require_first
from aqla_hyperliquid_mcp.server import close_server, create_server


def trade(status="rejected", **fields):
    return {"trade_id": "own", "status": status, "filled_quantity": "0", "remaining_quantity": "0",
            "fill_events": {}, "orders": [], "entry_operation_id": "entry:own", "chat_id": "channel",
            "entry_vwap": None, "stop_price": "90", "targets": ["110"],
            "plan": {"plan_id": "plan", "trade_id": "own", "market": {"symbol": "AXS", "quote_token": "USDC"},
                     "source_refs": ["source"], "source_revisions": {"source": "event"}, "side": "long",
                     "quantity": "2", "entry_price": "100", "stop_loss": "90", "take_profits": ["110"],
                     "effective_leverage": 5, "network": "testnet", "exchange": "hyperliquid", "profile_id": "standard"}, **fields}


def data(row):
    return {"trades": {"own": row}, "operations": {"entry:own": {"operation_id": "entry:own", "phase": "rejected", "outcome": "not_sent"}},
            "events": {"event": {"event_id": "event", "source_id": "source", "status": "executed",
                                  "source": {"chat_id": "channel", "message_id": 1, "text": "AXS", "sent_at": 1000, "received_at": 1000}}},
            "notifications": {"notice": {"trade_id": "own", "change": {"status": row["status"]}}},
            "profiles": {}, "source_names": {}, "paused": False}


def rendered(value):
    return "\n\n".join(text for message in telegram_view(value, {})["messages"] for text in message["texts"])


def test_consumed_source_with_rejected_trade_never_proves_execution():
    snapshot = data(trade())
    before = copy.deepcopy(snapshot)
    result = event_outcomes(snapshot, snapshot["events"]["event"])
    assert not result["event_status_is_fill_proof"]
    assert result["targets"][0]["state"] == "not_sent"
    assert result["targets"][0]["entry_fill_confirmed"] is False
    assert execution_counts(snapshot)["known_own_open"] == 0
    assert "Entry nicht gesendet" in rendered(snapshot)
    assert snapshot == before


def test_saved_plan_and_arbitrary_trade_id_never_prove_entry_fill():
    row = trade()
    snapshot = data(row)
    snapshot["trades"] = {}
    snapshot["plans"] = {"plan": {"event_id": "event", "plan": row["plan"]}}
    event = snapshot["events"]["event"]
    assert event_outcomes(snapshot, event)["targets"][0]["state"] == "planned"
    event["targets"] = {"leg": {"status": "rejected", "trade_id": "missing", "plan_id": "plan"}}
    result = event_outcomes(snapshot, event)["targets"][0]
    assert result["state"] == "unknown" and result["entry_fill_confirmed"] is None


@pytest.mark.parametrize("status", ["open", "unknown", "conflicted", "closed"])
def test_legacy_scalar_quantity_without_receipts_stays_unknown(status):
    row = trade(status, filled_quantity="2", remaining_quantity="0" if status == "closed" else "2")
    facts = trade_outcome(row)
    assert facts["state"] == ("conflicted" if status == "conflicted" else "unknown")
    assert facts["entry_fill_confirmed"] is None
    assert facts["known_own_remaining_quantity"] is None
    assert facts["historical_fill_evidence_missing"]
    message = rendered(data(row))
    assert "historisch nicht vollständig belegt" in message
    assert "Entry realisiert" not in message and "Trade eröffnet" not in message


def test_own_entry_history_and_current_position_are_separate():
    row = trade("open", filled_quantity="2", remaining_quantity="1", fill_events={
        "e": {"kind": "entry", "sz": "2"}, "tp": {"kind": "tp", "sz": "1"}})
    facts = trade_outcome(row)
    assert facts["state"] == "filled" and facts["entry_fill_confirmed"]
    assert facts["confirmed_entry_quantity"] == "2" and facts["known_own_remaining_quantity"] == "1"
    row["fill_events"]["tp"]["sz"] = "2"
    row.update(status="closed", remaining_quantity="0")
    facts = trade_outcome(row)
    assert facts["state"] == "closed" and facts["entry_fill_confirmed"]
    assert facts["known_own_remaining_quantity"] == "0"
    row.update(status="conflicted", external_conflict="foreign exposure")
    facts = trade_outcome(row)
    assert facts["state"] == "conflicted" and facts["entry_fill_confirmed"]
    assert facts["known_own_remaining_quantity"] is None


def test_terminal_workflow_without_matching_exit_is_not_known_open_or_closed():
    row = trade("closed", filled_quantity="2", remaining_quantity="2", fill_events={"e": {"kind": "entry", "sz": "2"}})
    assert trade_outcome(row)["state"] == "unknown"
    counts = execution_counts(data(row))
    assert counts["known_own_open"] == 0 and counts["unsettled_ownership"] == 0
    assert counts["historical_unconfirmed"] == 1


@pytest.mark.parametrize("size", ["NaN", "Infinity", "-1", "bogus"])
def test_invalid_receipts_do_not_become_known_positions(size):
    row = trade("open", filled_quantity="2", remaining_quantity="2", fill_events={"e": {"kind": "entry", "sz": size}})
    facts = trade_outcome(row)
    assert facts["state"] == "unknown" and facts["known_own_remaining_quantity"] is None


def test_unknown_send_has_priority_over_retry_and_known_zero_quantity():
    marker = {"attempts": 1, "next_retry_at": 2000, "deadline_at": 5000, "exhausted": False, "authority": "execute"}
    row = trade("preparing", observation_retry=marker)
    operations = {"entry:own": {"operation_id": "entry:own", "phase": "unknown"}}
    result = trade_outcome(row, operations)
    assert result["state"] == "unknown" and result["entry_fill_confirmed"] is None
    assert not result["current_ownership_confirmed"]
    snapshot = data(row)
    snapshot["operations"] = operations
    message = rendered(snapshot)
    assert "Wiederaufnahme:** gesperrt" in message and "Nächste Prüfung" not in message


@pytest.mark.parametrize("phase", ["prepared", "sending", "unknown"])
def test_older_unsettled_entry_attempt_blocks_new_not_sent_retry(phase):
    marker = {"attempts": 2, "next_retry_at": 2000, "deadline_at": 5000, "exhausted": False, "authority": "execute"}
    row = trade("preparing", entry_operation_id="entry:own:attempt:2", observation_retry=marker)
    snapshot = data(row)
    snapshot["operations"] = {
        "entry:own": {"operation_id": "entry:own", "phase": phase},
        "entry:own:attempt:2": {"operation_id": "entry:own:attempt:2", "phase": "rejected", "outcome": "not_sent"}}
    facts = trade_outcome(row, snapshot["operations"])
    assert facts["state"] == "unknown" and facts["entry_submission"] == "unconfirmed"
    assert facts["known_own_remaining_quantity"] is None and not facts["current_ownership_confirmed"]
    assert facts["entry_fill_confirmed"] is None
    assert "Wiederaufnahme:** gesperrt" in rendered(snapshot) and "Nächste Prüfung" not in rendered(snapshot)


def test_historical_not_sent_does_not_obscure_new_confirmed_fill_or_adopt_foreign_operations():
    row = trade("open", entry_operation_id="entry:own:attempt:2", filled_quantity="2", remaining_quantity="2",
                fill_events={"e": {"kind": "entry", "sz": "2"}})
    operations = {
        "entry:own": {"operation_id": "entry:own", "phase": "rejected", "outcome": "not_sent"},
        "entry:own:attempt:2": {"operation_id": "entry:own:attempt:2", "phase": "acknowledged", "outcome": "accepted"},
        **{identity: {"operation_id": identity, "phase": "unknown"} for identity in (
            "close:own:entry", "sl:own:entry", "entry:own-other:attempt:2", "leverage:own", "entry:foreign:own")}}
    facts = trade_outcome(row, operations)
    assert facts["state"] == "filled" and facts["entry_submission"] == "accepted"
    assert facts["entry_fill_confirmed"] and facts["known_own_remaining_quantity"] == "2"
    assert facts["current_ownership_confirmed"]


@pytest.mark.parametrize("stored_on_trade", [True, False])
def test_safe_entry_diagnostic_exposes_stage_type_without_provider_body(stored_on_trade):
    diagnostic = {"code": "entry_guard", "stage": "admission", "category": "validation",
                  "exception_type": "KeyError", "retryable": False,
                  "error": "PROVIDER_SECRET_BODY", "trace": "PROVIDER_SECRET_TRACE", "account": "PROVIDER_SECRET_ACCOUNT"}
    expected = {key: diagnostic[key] for key in ("code", "stage", "category", "exception_type", "retryable")}
    row = trade("preparing")
    operations = {"entry:own": {"operation_id": "entry:own", "phase": "rejected", "outcome": "not_sent",
                                 "result": {"diagnostic": diagnostic}}}
    if stored_on_trade:
        row["entry_diagnostic"] = diagnostic
        operations.clear()  # Early failure before any operation is created.
    before = copy.deepcopy((row, operations))
    assert diagnostic_view(row, operations) == expected
    assert trade_outcome(row, operations)["entry_diagnostic"] == expected
    projected = {**row, "execution_outcome": trade_outcome(row, operations)}
    context = {"event": {"event_id": "event", "source_id": "source", "source": {"text": "original"}},
               "messages": [], "trades": [projected]}
    decision = decision_context(context)
    assert decision["trades"][0]["entry_diagnostic"] == expected
    assert "PROVIDER_SECRET" not in json.dumps(decision)
    assert (row, operations) == before
    assert diagnostic_view(trade()) is None


@pytest.mark.parametrize("stored_on_trade", [True, False])
@pytest.mark.asyncio
async def test_mcp_entry_diagnostic_is_safe_visible_and_read_only(setup, signal, stored_on_trade):
    settings, state, _, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    await trading.execute(plan["plan_id"])
    diagnostic = {"code": "entry_guard", "stage": "pre_send", "category": "validation",
                  "exception_type": "KeyError", "retryable": False, "error": "PROVIDER_SECRET_BODY", "trace": "PROVIDER_SECRET_TRACE"}
    def store(snapshot):
        row = snapshot["trades"][plan["trade_id"]]
        row.update(status="preparing", fill_events={}, orders=[], filled_quantity="0", remaining_quantity="0")
        if stored_on_trade:
            row["entry_diagnostic"] = diagnostic
            snapshot["operations"].pop(row["entry_operation_id"])
        else:
            snapshot["operations"][row["entry_operation_id"]].update(phase="rejected", outcome="not_sent", result={"diagnostic": diagnostic})
    state.update(store)
    server = create_server(settings, state=state, exchange=exchange)
    before = state.snapshot()
    try:
        for arguments in ({}, {"sections": ["prices"]}, {"sections": ["operations"]}):
            result = await call(server, "trade_diagnostics", trade_id=plan["trade_id"], **arguments)
            assert result["entry_diagnostic"]["exception_type"] == "KeyError"
            assert result["entry_diagnostic"]["stage"] == "pre_send"
            assert result["entry_diagnostic"]["retryable"] is False
            assert result["execution_outcome"]["entry_diagnostic"] == result["entry_diagnostic"]
            assert "PROVIDER_SECRET" not in json.dumps(result)
        for view in ("full", "decision"):
            context = await call(server, "source_context", event_id=source.event_id, view=view)
            assert context["trades"][0]["entry_diagnostic"]["exception_type"] == "KeyError"
            assert "PROVIDER_SECRET" not in json.dumps(context)
        assert state.snapshot() == before
    finally:
        await close_server(server)


def test_retry_markers_are_bounded_truthful_and_terminal_after_exhaustion():
    marker = {"attempts": 1, "next_retry_at": 2000, "deadline_at": 5000, "exhausted": False,
              "code": "stale_observation", "stage": "account", "category": "read", "authority": "execute", "exception_type": "TimeoutError",
              "error": "PRIVATE PROVIDER TRACE MUST NOT APPEAR"}
    row = trade("preparing", observation_retry=marker, unsent_reason="PRIVATE PROVIDER TRACE MUST NOT APPEAR")
    snapshot = data(row)
    assert trade_outcome(row)["state"] == "pending_observation"
    message = rendered(snapshot)
    assert "Technische Prüfung ausstehend" in message and "Nächste Prüfung" in message
    assert "Unveränderte Signalfrist" in message and "Signal abgelehnt" not in message
    assert "TimeoutError" in message
    assert "PRIVATE PROVIDER" not in message and "PRIVATE PROVIDER" not in json.dumps(retry_view(row))
    row.update(status="rejected", unsent_reason="Signal deadline expired; it is never renewed")
    marker["exhausted"] = True
    assert trade_outcome(row)["state"] == "not_sent"
    assert "Trade abgelehnt" in rendered(snapshot) and "Wiederaufnahme beendet" in rendered(snapshot)


def test_preview_retry_target_remains_preview_without_automatic_execution():
    snapshot = data(trade())
    snapshot["trades"] = {}
    snapshot["notifications"] = {"target": {"event_id": "event", "targets": [{"status": "pending", "exchange": "hyperliquid",
        "observation_retry": {"attempts": 1, "deadline_at": 5000, "exhausted": False, "authority": "preview"}}]}}
    message = rendered(snapshot)
    assert "Technische Prüfung ausstehend" in message and "keine automatische Ausführung" in message
    assert "Signal abgelehnt" not in message


def test_mixed_targets_preserve_scopes_revision_and_receipts():
    snapshot = data(trade())
    other = trade("open", trade_id="other", filled_quantity="2", remaining_quantity="2",
                  fill_events={"e": {"kind": "entry", "sz": "2"}})
    other["plan"] = {**other["plan"], "plan_id": "otherplan", "network": "mainnet", "exchange": "aster",
                     "market": {"symbol": "AXS", "quote_token": "USDT"}}
    snapshot["trades"]["other"] = other
    snapshot["events"]["event"]["targets"] = {
        "test": {"status": "rejected", "trade_id": "own", "plan_id": "plan"},
        "main": {"status": "open", "trade_id": "other", "plan_id": "otherplan"}}
    results = event_outcomes(snapshot, snapshot["events"]["event"])["targets"]
    assert [(row["network"], row["quote_token"], row["state"]) for row in results] == [
        ("testnet", "USDC", "not_sent"), ("mainnet", "USDT", "filled")]
    assert all(row["source_revision_matches"] for row in results)
    assert execution_counts(snapshot)["known_own_open"] == 1
    other.update(status="conflicted", external_conflict="manual")
    counts = execution_counts(snapshot)
    assert counts["known_own_open"] == 0 and counts["unsettled_ownership"] == 1
    assert "Belegte eigene offene Positionen:** 0" in status_text(snapshot, {}, {})


@pytest.mark.parametrize("kind,expected", [("new_signal", "source_revision_unconfirmed"), ("trade_update", "update_only")])
def test_later_revision_never_inherits_old_entry_fill_as_its_execution(kind, expected):
    row = trade("open", filled_quantity="2", remaining_quantity="2", fill_events={"e": {"kind": "entry", "sz": "2"}})
    snapshot = data(row)
    event = snapshot["events"]["event"]
    event.update(event_id="later-revision", status="updated", interpretation={"kind": kind},
                 targets={"target": {"status": "rejected", "trade_id": "own", "plan_id": "plan"}})
    before = copy.deepcopy(snapshot)
    result = event_outcomes(snapshot, event)["targets"][0]
    assert result["state"] == expected and result["entry_fill_confirmed"] is None
    assert result["source_revision_matches"] is False
    assert result["bound_trade_history"]["entry_fill_confirmed"] is True
    assert snapshot == before


def test_lookup_labels_old_fills_as_bound_history_for_a_later_revision():
    row = trade("open", filled_quantity="2", remaining_quantity="2", fill_events={"e": {"kind": "entry", "sz": "2"}})
    snapshot = data(row)
    event = snapshot["events"].pop("event")
    event["event_id"] = "later"
    snapshot["events"]["later"] = event
    result = source_lookup(snapshot, "1970-01-01T00:00:00+00:00", "1970-01-01T00:01:00+00:00")
    retained = require_first(row for row in result["rows"] if row.get("event_id") == "later")
    assert "bound_trade_history" in result["entry_fill_fields_scope"]
    assert retained["execution_outcome"]["state"] == "source_revision_unconfirmed"
    assert retained["execution_outcome"]["entry_fill_confirmed"] is None
    assert retained["execution_outcome"]["bound_trade_history"]["entry_fill_confirmed"]


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    return result.structured_content or json.loads(require_first(block.text for block in result.content if block.type == "text"))


@pytest.mark.asyncio
async def test_mcp_views_preserve_consumed_event_and_actual_no_fill_outcome(setup, signal, monkeypatch):
    settings, state, _, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    await trading.execute(plan["plan_id"])
    identity = plan["trade_id"]
    def rejected(snapshot):
        row = snapshot["trades"][identity]
        row.update(status="rejected", fill_events={}, filled_quantity="0", remaining_quantity="0", orders=[])
        snapshot["operations"][row["entry_operation_id"]].update(phase="rejected", outcome="not_sent")
    state.update(rejected)
    server = create_server(settings, state=state, exchange=exchange)
    before = state.snapshot()
    async def no_inventory_refresh():
        pytest.fail("Summary must not refresh exchange inventory")
    monkeypatch.setattr(server.aqla_trading, "inventory_status", no_inventory_refresh)
    try:
        summary = await call(server, "status", view="summary")
        assert not summary["inventory_refreshed"] and summary["execution_counts"]["known_own_open"] == 0
        assert "trades" not in summary
        for view in ("full", "decision"):
            context = await call(server, "source_context", event_id=source.event_id, view=view)
            assert context["execution_outcome"]["targets"][0]["state"] == "not_sent"
        batch = await call(server, "source_context_batch", event_ids=[source.event_id])
        assert batch["items"][0]["execution_outcome"]["targets"][0]["state"] == "not_sent"
        diagnostics = await call(server, "trade_diagnostics", trade_id=identity, sections=["operations"])
        assert diagnostics["execution_outcome"]["state"] == "not_sent" and "fill_events" not in diagnostics
        compact = await call(server, "trade_diagnostics", trade_id=identity, view="compact", limit=100)
        assert any(row["section"] == "execution_outcome" for row in compact["rows"])
        assert state.snapshot() == before
        ack = await call(server, "interpret_source", event_id=source.event_id, interpretation=interpretation.model_dump(mode="json"),
                         result_view="ack", context_auth=server.aqla_authority.sign("source", "duplicate-classification"))
        assert ack["status"] == "executed" and ack["event_status_is_fill_proof"] is False
        assert ack["execution_outcome"]["targets"][0]["state"] == "not_sent"
    finally:
        await close_server(server)
