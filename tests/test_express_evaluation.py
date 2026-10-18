import importlib.util
from pathlib import Path

import pytest
from test_express import TEXT, source

from aqla_agent.decision_evaluation import complete_fields
from aqla_agent.express import compile_template


def runner():
    spec = importlib.util.spec_from_file_location("express_evaluation", Path(__file__).resolve().parents[1] / "scripts/evaluate_express.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_replay_checks_reference_not_just_parser_validity():
    evaluate = runner().evaluate
    event = source()
    truth = complete_fields(compile_template(event))
    case = {"id": "synthetic", "messages": [event.model_dump(mode="json")], "expected": truth, "express_expected": True}
    assert evaluate(case)["passed"]
    assert not evaluate({**case, "express_expected": False})["passed"]
    assert not evaluate({**case, "expected": {**truth, "side": "short"}})["passed"]
    incomplete = source(TEXT + "\nStop to entry")
    assert not evaluate({**case, "messages": [incomplete.model_dump(mode="json")]})["passed"]
    assert evaluate({**case, "express_expected": False, "messages": [incomplete.model_dump(mode="json")]})["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "#FUTURES\n\n"])
async def test_paired_fixture_has_equal_orders_and_protection(tmp_path, prefix):
    from express_benchmark_fixture import apply_fake
    event = source(prefix + TEXT)
    value = compile_template(event)
    high = await apply_fake(event, value, tmp_path / "high", express=False)
    express = await apply_fake(event, value, tmp_path / "express", express=True)
    assert high["outcome"] == express["outcome"]
    assert high["outcome"]["status"] == "open"
    assert high["real_order_requests"] == express["real_order_requests"] == 0
    assert high["entry_dispatch_seconds"] < high["seconds"]
