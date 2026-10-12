import pytest

from aqla_agent.decision_evaluation import (
    CRITICAL_FIELDS,
    REQUIRED_SCENARIOS,
    calibration_profile,
    equal,
    evaluate,
    evaluate_split,
)
from aqla_agent.decisions import DecisionResult
from aqla_hyperliquid_mcp.models import Interpretation


def test_calibration_rejects_thread_leakage_before_model_calls():
    dataset = [{"id": "one", "expected": {}, "split": "calibration", "group_id": "same"},
               {"id": "two", "expected": {}, "split": "holdout", "group_id": "same"}]
    with pytest.raises(ValueError, match="different source threads"):
        calibration_profile([], dataset, ["channel"])


def test_partial_annotations_and_critical_errors_cannot_activate_profile():
    dataset = [{"id": str(i), "expected": {"kind": "report"}, "annotation_complete": False,
                "split": "calibration" if i < 30 else "holdout"} for i in range(60)]
    rows = [{"id": c["id"], "kind": "report", "passed": True, "critical_error": False,
             "probabilities": {"kind": 1.0, "live_action": 0.0}} for c in dataset]
    assert not calibration_profile(rows, dataset, ["channel"]).validated
    for case in dataset:
        case["group_id"] = "thread-" + case["id"]
        case["annotation_complete"] = True
        case["expected"] = dict.fromkeys(CRITICAL_FIELDS)
        case["expected"]["kind"] = "report"
    assert not calibration_profile(rows, dataset, ["channel"], "master-v1").validated
    kinds = ("new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous")
    for i, (case, row) in enumerate(zip(dataset, rows, strict=True)):
        case["expected"]["kind"] = row["kind"] = kinds[i % 6]
        case["language"] = ("de", "en", "ru")[i % 3]
        case["tags"] = sorted(REQUIRED_SCENARIOS)
    assert calibration_profile(rows, dataset, ["channel"], "master-v1").validated
    proof = dataset[-1]["expected"].pop("evidence")
    assert not calibration_profile(rows, dataset, ["channel"], "master-v1").validated
    dataset[-1]["expected"]["evidence"] = proof
    rows[-1]["critical_error"] = True
    assert not calibration_profile(rows, dataset, ["channel"]).validated


def test_probability_thresholds_are_fitted_only_to_training_group():
    dataset = [{"id": "train", "expected": {}, "split": "calibration"},
               {"id": "test", "expected": {}, "split": "holdout"}]
    rows = [{"id": "train", "kind": "new_signal", "passed": True, "critical_error": False, "probabilities": {"kind": 0.999}},
            {"id": "test", "kind": "new_signal", "passed": True, "critical_error": False, "probabilities": {"kind": 0.5}}]
    assert calibration_profile(rows, dataset, ["channel"]).thresholds["kind"] == 0.999
    assert equal(["1.00", "2"], [1, 2])
    assert not equal(None, 0)


@pytest.mark.asyncio
async def test_high_confidence_missed_action_is_a_critical_error():
    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            return DecisionResult(model="inception/mercury-decide:free", interpretation=Interpretation(
                kind="ambiguous", source_refs=[packet["anchor_source_id"]], reason="Synthetic wrong classification"))

    rows = await evaluate([{"id": "missed", "messages": ["BTC long. SL 90. TP 110."],
                            "expected": {"kind": "new_signal"}}], Adapter())
    assert rows[0]["missed_actionable"] and rows[0]["critical_error"]


def test_real_source_revision_overlap_is_rejected_even_with_distinct_groups():
    original = {"chat_id": "channel", "message_id": 17, "sent_at": 1000, "text": "Original"}
    edited = {**original, "edited_at": 2000, "text": "Edited"}
    dataset = [{"id": "train", "split": "calibration", "group_id": "one", "messages": [original], "expected": {}},
               {"id": "test", "split": "holdout", "group_id": "two", "messages": [edited], "expected": {}}]
    with pytest.raises(ValueError, match="original source messages or edits"):
        calibration_profile([], dataset, ["channel"])


@pytest.mark.asyncio
async def test_split_source_leakage_is_rejected_before_any_paid_request():
    class Forbidden:
        async def interpret(self, *args, **kwargs):
            raise AssertionError("Leaking dataset must not make a model call")

    source = {"chat_id": "channel", "message_id": 17, "sent_at": 1000, "text": "Original"}
    dataset = [{"id": "train", "split": "calibration", "group_id": "one", "messages": [source], "expected": {}},
               {"id": "test", "split": "holdout", "group_id": "two", "messages": [source], "expected": {}}]
    with pytest.raises(ValueError, match="original source messages or edits"):
        await evaluate_split(dataset, Forbidden(), ["channel"])


@pytest.mark.asyncio
async def test_evaluation_preserves_real_edit_and_reply_metadata():
    observed = []

    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            observed.append(packet)
            return DecisionResult(model="inception/mercury-decide:free", interpretation=Interpretation(
                kind="report", source_refs=[packet["anchor_source_id"]], reason="Synthetic report"))

    messages = [{"chat_id": "channel", "message_id": 17, "sent_at": 1000, "text": "Original report"},
                {"chat_id": "channel", "message_id": 17, "sent_at": 1000, "edited_at": 2000,
                 "received_at": 2100, "reply_to_id": 16, "thread_id": 8, "text": "Edited report"}]
    rows = await evaluate([{"id": "edit", "messages": messages, "expected": {"kind": "report"}}], Adapter())
    assert rows[0]["passed"]
    old, edited = observed[0]["messages"]
    assert old["source_id"] == edited["source_id"] and old["event_id"] != edited["event_id"]
    assert edited["edited_at"] == 2000 and edited["received_at"] == 2100
    assert edited["reply_to_id"] == 16 and edited["thread_id"] == 8
    assert observed[0]["anchor_event_id"] == edited["event_id"]
