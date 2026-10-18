import importlib.util
from pathlib import Path

import pytest

from aqla_agent.decision_evaluation import case_context, case_packet, complete_fields
from aqla_agent.source_replay import canonical_symbol, mismatches
from aqla_hyperliquid_mcp.models import Interpretation


def test_replay_grader_does_not_hide_wrong_prices_or_false_entries():
    value = Interpretation(kind="new_signal", symbol="BTC", side="long", source_refs=["anchor"],
                           entry="limit", entry_prices=[100], stop_loss=90, take_profits=[110])
    truth = complete_fields(value)
    assert mismatches(value, truth) == []
    assert mismatches(value.model_copy(update={"stop_loss": 91}), truth) == ["stop_loss"]
    assert "kind" in mismatches(Interpretation(kind="ambiguous", source_refs=["anchor"]), truth)
    report = Interpretation(kind="report", source_refs=["anchor"])
    assert "kind" in mismatches(value, complete_fields(report))
    assert canonical_symbol("1000PEPEUSDT.P") != canonical_symbol("PEPE")
    assert canonical_symbol("BTCUSDC.P") == "BTC"


def test_native_context_retains_unlinked_originals_while_semantic_candidates_stay_scoped():
    case = {"id": "synthetic-context", "messages": ["ETH SHORT. Entry 200. Stop 210.", "BTC LONG. Entry 100. Stop 90."]}
    context = case_context(case)
    packet = case_packet(case)
    assert len(context["messages"]) == 2 and len(packet["messages"]) == 1
    assert context["messages"][-1]["source"]["text"] == packet["messages"][0]["text"]


def test_only_final_valid_checked_native_result_can_pass():
    spec = importlib.util.spec_from_file_location("evaluate_high_tools", Path(__file__).resolve().parents[1] / "scripts/evaluate_high_tools.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    value = Interpretation(kind="report", source_refs=["anchor"], reason="Original report")
    case = {"expected": complete_fields(value)}
    checked = {"valid": True, "interpretation": value.model_dump(mode="json")}
    good = {"completed": True, "checks": [checked], "forbidden_tools": []}
    assert script.grade(case, good)["passed"]
    assert not script.grade(case, {**good, "checks": []})["passed"]
    assert not script.grade(case, {**good, "completed": False})["passed"]
    assert not script.grade(case, {**good, "forbidden_tools": ["terminal"]})["passed"]
    assert not script.grade(case, {**good, "checks": [checked, {**checked, "valid": False}]})["passed"]
    assert not script.grade(case, {**good, "transport_turn_attempts": 4})["passed"]


@pytest.mark.parametrize("result,expected", [
    ({"completed": False, "error": "[Errno 32] Broken pipe"}, True),
    ({"completed": False, "error": "Request timed out."}, True),
    ({"completed": False, "error": "localized text", "failure_reason": "timeout", "failure_retryable": True}, True),
    ({"completed": False, "failure_reason": "rate_limit", "failure_retryable": True}, True),
    ({"completed": False, "failure_reason": "server_error", "failure_retryable": True}, True),
    ({"completed": False, "error": "connection issue", "failure_reason": "billing", "failure_retryable": False}, False),
    ({"completed": False, "error": "timeout", "failure_reason": "auth_permanent", "failure_retryable": False}, False),
    ({"completed": False, "error": "Invalid interpretation"}, False),
    ({"completed": True, "error": "timeout mentioned in an answer"}, False),
])
def test_native_transport_retry_uses_classification_without_rescuing_semantic_failures(result, expected):
    spec = importlib.util.spec_from_file_location("evaluate_high_tools", Path(__file__).resolve().parents[1] / "scripts/evaluate_high_tools.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.transport_retryable(result) is expected
