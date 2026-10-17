import importlib.util
from pathlib import Path

from aqla_agent.decision_evaluation import complete_fields
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
