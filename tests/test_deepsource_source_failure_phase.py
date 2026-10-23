"""Source validation stages keep the original safe failure facts."""
from types import SimpleNamespace

import pytest

from aqla_agent.decisions import CalibrationProfile, DecisionFailure
from aqla_agent.source_failure import classify_failure
from aqla_agent.source_worker import SourceWorker
from aqla_agent.usage_metrics import UsageRun


@pytest.mark.asyncio
async def test_input_limit_preserves_decision_phase_and_measured_size():
    worker = SourceWorker(SimpleNamespace(), {"mode": "hybrid", "max_context_characters": 2000},
                          adapter=SimpleNamespace())
    packet = {"anchor_event_id": "anchor", "anchor_source_id": "original",
              "messages": [{"event_id": "anchor", "source_id": "original", "text": "x" * 2100}]}
    progress = {"phase": "prepare"}
    with pytest.raises(DecisionFailure, match="^source_context_limit$") as raised:
        await worker.new_decision(packet, UsageRun("source_python"), CalibrationProfile(),
                                  "master", "revision", progress)

    assert raised.value.checked_input_characters > 2100
    assert classify_failure(raised.value, progress["phase"]) == {
        "phase": "decision", "code": "source_context_limit", "exception_type": "DecisionFailure",
        "retryable": False, "field_paths": [],
    }
