import pytest

from aqla_agent.decision_evaluation import (
    CRITICAL_FIELDS,
    REQUIRED_SCENARIOS,
    EvaluationCheckpoint,
    calibration_profile,
    calibration_question_results,
    case_packet,
    equal,
    evaluate,
    evaluate_split,
    fit_thresholds,
    revision_time_context,
    source_gate_diagnostics,
)
from aqla_agent.decisions import DecisionFailure, DecisionResult
from aqla_hyperliquid_mcp.models import Interpretation


def test_calibration_separates_kind_labels_and_intent_polarities():
    packet = case_packet({"id":"labels","messages":["BTC result report."]})
    decisions = calibration_question_results(packet,{"kind":"report"},
        {"values":{"kind":"report"},"probabilities":{"kind":0.94,"live_action":0.01},
         "required_questions":["kind","live_action"]},None)
    assert [row["family"] for row in decisions] == ["kind.report","live_action.false"]
    thresholds, disabled = fit_thresholds([
        {"raw_correct":True,"question_results":decisions},
        {"raw_correct":False,"baseline_gate_passed":True,"question_results":[
            {"family":"kind.incomplete","correct":False,"confidence":1,"prediction":"incomplete"}]},
    ])
    assert disabled == ["kind.incomplete"]
    assert thresholds["kind.report"] == 0.94 and thresholds["live_action.false"] == 0.99


def test_legacy_revision_identity_preserves_real_reply_anchor_without_rehashing():
    from aqla_hyperliquid_mcp.models import SourceEvent, digest
    source = SourceEvent(chat_id="channel", message_id=2, sent_at=1000, received_at=1000, reply_to_id=1, text="Original reply")
    legacy = digest([source.source_id, source.edited_at, source.text, source.media])[:32]
    assert legacy != source.event_id
    case = {"id":"legacy", "messages":[source.model_dump(mode="json")], "message_event_ids":[legacy], "anchor_event_id":legacy}
    packet = case_packet(case)
    assert packet["anchor_event_id"] == legacy and packet["messages"][0]["reply_to_id"] == 1
    with pytest.raises(ValueError, match="identities must match"):
        case_packet({**case,"message_event_ids":["fabricated"]})


def test_revision_time_replay_excludes_future_edits_but_keeps_observed_later_messages():
    def event(message_id, sent_at, edited_at=None, reply=None):
        return {"source": {"chat_id": "channel", "message_id": message_id, "sent_at": sent_at,
                           "edited_at": edited_at, "reply_to_id": reply, "text": "Original"}}
    parent = event(1, 10)
    original = event(2, 20, reply=1)
    future_edit = event(2, 20, 100, reply=1)
    newer = event(3, 40)
    anchor_edit = event(2, 20, 60, reply=1)
    context = revision_time_context([parent, original, future_edit, newer, anchor_edit], anchor_edit, limit=2)
    assert future_edit not in context and newer in context and parent in context and anchor_edit in context


@pytest.mark.asyncio
async def test_complete_default_fields_do_not_disappear_from_annotation_comparison():
    from aqla_agent.decision_evaluation import complete_fields
    from aqla_hyperliquid_mcp.models import Evidence
    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            return DecisionResult(model="synthetic", interpretation=Interpretation(kind="report",
                source_refs=[packet["anchor_source_id"]], evidence=[Evidence(field="classification",
                    source_id=packet["anchor_source_id"], quote="Original report")], reason="Result"))
    from aqla_hyperliquid_mcp.models import SourceEvent
    source = SourceEvent(chat_id="default", message_id=1, sent_at=1000, text="Original report")
    baseline = Interpretation(kind="report", source_refs=[source.source_id], evidence=[Evidence(field="classification",
        source_id=source.source_id, quote="Original report")], reason="Result")
    expected = {key: value for key, value in complete_fields(baseline).items() if key in CRITICAL_FIELDS}
    rows = await evaluate([{"id": "default", "messages": ["Original report"], "expected": expected}], Adapter())
    assert rows[0]["passed"] and not rows[0]["mismatch_fields"]


@pytest.mark.asyncio
async def test_checkpoint_resumes_same_observation_without_another_request(tmp_path):
    calls = []
    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            calls.append(packet)
            return DecisionResult(model="synthetic", collection_only=True,
                interpretation=Interpretation(kind="report", source_refs=[packet["anchor_source_id"]]))
    checkpoint = EvaluationCheckpoint(tmp_path / "checkpoint.json", {"model": "synthetic", "version": "v1"})
    cases = [{"id": "one", "messages": ["Original report"], "expected": {"kind": "report"}}]
    first = await evaluate(cases, Adapter(), collect=True, master="Approved master", checkpoint=checkpoint)
    resumed = EvaluationCheckpoint(checkpoint.path, checkpoint.identity)
    assert await evaluate(cases, Adapter(), collect=True, master="Approved master", checkpoint=resumed) == first
    assert len(calls) == 1
    with pytest.raises(ValueError, match="source context changed"):
        await evaluate(cases, Adapter(), collect=True, master="Changed master", checkpoint=resumed)
    with pytest.raises(ValueError, match="another dataset"):
        EvaluationCheckpoint(checkpoint.path, {"model": "changed"})


@pytest.mark.asyncio
async def test_checkpoint_provider_failure_requires_explicit_retry(tmp_path):
    calls = []
    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            calls.append(packet)
            if len(calls) == 1:
                raise DecisionFailure("decision_api_unavailable")
            return DecisionResult(model="synthetic", collection_only=True,
                interpretation=Interpretation(kind="report", source_refs=[packet["anchor_source_id"]]))
    cases = [{"id": "one", "messages": ["Original report"], "expected": {"kind": "report"}}]
    checkpoint = EvaluationCheckpoint(tmp_path / "checkpoint.json", {"model": "synthetic"})
    for _ in range(2):
        with pytest.raises(DecisionFailure, match="decision_api_unavailable"):
            await evaluate(cases, Adapter(), collect=True, checkpoint=checkpoint)
    assert len(calls) == 1
    explicit_retry = EvaluationCheckpoint(checkpoint.path, checkpoint.identity, retry_provider_failures=True)
    assert (await evaluate(cases, Adapter(), collect=True, checkpoint=explicit_retry))[0]["passed"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_invalid_interpretation_schema_defers_case_without_stopping_collection(tmp_path):
    import json

    import httpx
    from test_decisions import response_for

    from aqla_agent.decisions import DEFAULT_DECISION_MODEL, OpenRouterDecisionAdapter
    def upstream(request):
        body = json.loads(request.content)
        invalid = body["state"]["messages"][-1]["text"].startswith("BTC")
        return httpx.Response(200, json=response_for(body["questions"], kind="new_signal" if invalid else "report",
            overrides={"entry_timing":"deferred"} if invalid else None))
    dataset = [{"id":"bad-schema", "messages":["BTC long, deferred LIMIT, price missing."], "expected":{"kind":"incomplete"}},
               {"id":"good-report", "messages":["Result report, no new order."], "expected":{"kind":"report"}}]
    checkpoint = EvaluationCheckpoint(tmp_path / "checkpoint.json", {"version":"synthetic"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        rows = await evaluate(dataset, OpenRouterDecisionAdapter("synthetic",client=client,model_revision=DEFAULT_DECISION_MODEL),
                              collect=True,checkpoint=checkpoint)
    assert rows[0]["failure_code"] == "decision_schema_invalid" and not rows[0]["accepted"]
    assert rows[1]["passed"] and len(checkpoint.rows) == 2


def test_incomplete_observation_set_cannot_issue_activation_profile():
    kinds = ("new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous")
    cases, rows = [], []
    for i in range(72):
        expected = dict.fromkeys(CRITICAL_FIELDS)
        expected["kind"] = kinds[i % 6]
        cases.append({"id": str(i), "split": "calibration" if i < 36 else "holdout", "expected": expected,
            "group_id": "episode" + str(i), "origin": "real", "annotation_complete": True,
            "language": ("de", "en", "ru")[i % 3], "tags": sorted(REQUIRED_SCENARIOS)})
        rows.append({"id": str(i), "kind": kinds[i % 6], "source_id": "channel", "passed": True,
            "accepted": True, "raw_correct": True, "critical_error": False,
            "question_results": [{"family": "kind", "confidence": 1, "correct": True}]})
    assert calibration_profile(rows, cases, ["channel"], "approved-master").validated
    assert not calibration_profile(rows[:-1], cases, ["channel"], "approved-master").validated
    rows[-1]["failure_code"] = "decision_api_unavailable"
    assert not calibration_profile(rows, cases, ["channel"], "approved-master").validated


@pytest.mark.asyncio
async def test_semantic_holdout_does_not_grant_untrained_source_kind_permission():
    import json

    import httpx
    from test_decisions import response_for

    from aqla_agent.decisions import DEFAULT_DECISION_MODEL, OpenRouterDecisionAdapter
    def upstream(request):
        body = json.loads(request.content)
        kind = "report" if body["state"].get("anchor_source_id") == training_ref else "new_signal"
        return httpx.Response(200, json=response_for(body["questions"], kind=kind))
    from aqla_hyperliquid_mcp.models import SourceEvent
    training_source = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, received_at=1000, text="Result report")
    training_ref = training_source.source_id
    test_source = SourceEvent(chat_id="channel", message_id=2, sent_at=2000, received_at=2000,
        text="BTC long. Entry 100. SL 90. TP 110 / 120.")
    dataset = [{"id": "train", "split": "calibration", "group_id": "one", "origin": "real",
                "messages": [training_source.model_dump(mode="json")], "expected": {"kind": "report"}},
               {"id": "hold", "split": "holdout", "group_id": "two", "origin": "real",
                "messages": [test_source.model_dump(mode="json")], "expected": {"kind": "new_signal"}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        rows = await evaluate_split(dataset, OpenRouterDecisionAdapter("synthetic", client=client,
            model_revision=DEFAULT_DECISION_MODEL), ["channel"])
    assert rows[-1]["accepted"] and rows[-1]["passed"]
    profile = calibration_profile(rows, dataset, ["channel"], "master")
    assert not profile.validated and not profile.permits_kind("channel", "new_signal")


def test_calibration_rejects_thread_leakage_before_model_calls():
    dataset = [{"id": "one", "expected": {}, "split": "calibration", "group_id": "same"},
               {"id": "two", "expected": {}, "split": "holdout", "group_id": "same"}]
    with pytest.raises(ValueError, match="different source threads"):
        calibration_profile([], dataset, ["channel"])


def test_partial_annotations_and_critical_errors_cannot_activate_profile():
    dataset = [{"id": str(i), "expected": {"kind": "report"}, "annotation_complete": False,
                "split": "calibration" if i < 30 else "holdout"} for i in range(60)]
    rows = [{"id": c["id"], "kind": "report", "passed": True, "critical_error": False,
             "probabilities": {"kind": 1.0, "live_action": 0.0}, "raw_correct": True,
             "accepted": True, "source_id": "channel",
             "question_results": [{"family": "kind", "confidence": 1, "correct": True}]} for c in dataset]
    assert not calibration_profile(rows, dataset, ["channel"]).validated
    for case in dataset:
        case["group_id"] = "thread-" + case["id"]
        case["origin"] = "real"
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
    for row in rows:
        row.update(raw_correct=True, question_results=[{"family": "kind", "correct": True, "confidence": row["probabilities"]["kind"]}])
    assert calibration_profile(rows, dataset, ["channel"]).thresholds["kind"] == 0.999
    assert equal(["1.00", "2"], [1, 2])
    assert not equal(None, 0)


def test_confidence_one_wrong_answer_disables_family_instead_of_accepting_equality():
    thresholds, disabled = fit_thresholds([{"raw_correct": False, "baseline_gate_passed": True,
        "question_results": [{"family": "kind", "correct": False, "confidence": 1}]}])
    assert "kind" in disabled and "kind" not in thresholds


def test_probability_buckets_tolerate_float_noise_but_never_round_errors_down():
    thresholds, disabled = fit_thresholds([
        {"raw_correct":True,"question_results":[{"family":"kind.report","correct":True,"confidence":0.99142251605}]},
        {"raw_correct":False,"baseline_gate_passed":True,"question_results":[{"family":"kind.new_signal","correct":False,"confidence":0.99991111}]},
    ])
    assert thresholds["kind.report"]==0.9914 and 0.99142251575 >= thresholds["kind.report"]
    assert thresholds["kind.new_signal"]==1 and 0.99991111 < thresholds["kind.new_signal"]
    assert disabled==[]


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


def test_source_kind_diagnostics_do_not_credit_two_cases_from_one_holdout_group():
    cases = [{"id": "cal", "split": "calibration", "origin": "real", "group_id": "cal-group"},
             {"id": "h1", "split": "holdout", "origin": "real", "group_id": "same-thread"},
             {"id": "h2", "split": "holdout", "origin": "real", "group_id": "same-thread"}]
    rows = [{"id": case["id"], "kind": "report", "source_id": "channel", "passed": True,
             "accepted": True, "critical_error": False} for case in cases]
    diagnostic = source_gate_diagnostics(rows, cases, ["channel"])["channel"]
    proof = diagnostic["evidence"]["kinds"]["report"]
    assert proof["calibration_correct_groups"] == 1
    assert proof["holdout_correct_accepted_groups"] == 1
    assert proof["blocking_reasons"] == ["kind_holdout_correct_accepted_groups_below_2"]
    assert "source_no_tested_kind" in diagnostic["blocking_reasons"]
    assert "report" not in diagnostic["eligible_kinds"]


def test_failed_calibration_explicitly_reports_global_and_source_kind_blockers():
    cases = [{"id": "one", "split": "calibration", "expected": {"kind": "report"},
              "origin": "real", "group_id": "one-group"}]
    rows = [{"id": "one", "source_id": "channel", "kind": "report", "passed": True,
             "accepted": True, "raw_correct": True, "question_results": []}]
    profile = calibration_profile(rows, cases, ["channel"])
    reasons = profile.activation_diagnostics["blocking_reasons"]
    assert not profile.validated
    assert "calibration_cases_below_30" in reasons and "holdout_cases_below_30" in reasons
    assert "approved_master_hash_missing" in reasons
    proof = profile.activation_diagnostics["sources"]["channel"]
    assert "source_holdout_groups_below_5" in proof["blocking_reasons"]
    assert "kind_holdout_correct_accepted_groups_below_2" in proof["evidence"]["kinds"]["report"]["blocking_reasons"]


def test_equal_bare_quotes_require_distinct_original_occurrence_annotations():
    from aqla_agent.decision_evaluation import question_labels
    from aqla_agent.decisions import candidates
    packet = case_packet({"id": "equal", "messages": ["BTC LONG. SL 7. TP1 7."]})
    source = packet["anchor_source_id"]
    spans = [span for span in candidates(packet) if span.kind == "number" and span.quote == "7"]
    expected = {"stop_loss": 7, "take_profits": [7], "evidence": [
        {"field": "stop_loss", "source_id": source, "quote": "7"},
        {"field": "take_profits.0", "source_id": source, "quote": "7"}]}
    labels = question_labels(packet, expected)
    assert all(labels["role." + span.candidate_id] == {"unclear"} for span in spans)
    annotations = [{"field": field, "source_id": source, "start": span.start, "end": span.end, "quote": span.quote}
                   for field, span in zip(("stop_loss", "take_profits.0"), spans, strict=True)]
    labels = question_labels(packet, expected, candidate_occurrences=annotations)
    assert labels["role." + spans[0].candidate_id] == {"stop_loss"}
    assert labels["role." + spans[1].candidate_id] == {"target_1"}
    with pytest.raises(ValueError, match="original position and Evidence"):
        question_labels(packet, expected, candidate_occurrences=[{**annotations[0], "start": 0}])
    with pytest.raises(ValueError, match="annotated twice"):
        question_labels(packet, expected, candidate_occurrences=[annotations[0], annotations[0]])
    question_results = calibration_question_results(packet, expected, {
        "values": {"role." + spans[0].candidate_id: "stop_loss", "role." + spans[1].candidate_id: "target_1"},
        "probabilities": {"role." + span.candidate_id: 1 for span in spans},
        "required_questions": ["role." + span.candidate_id for span in spans]}, None)
    assert question_results == []
    assert fit_thresholds([{"raw_correct": True, "question_results": question_results}]) == ({}, [])


def test_exact_repeat_annotations_can_name_equivalent_mentions_of_one_field():
    from aqla_agent.decision_evaluation import question_labels
    from aqla_agent.decisions import candidates
    packet = case_packet({"id": "repeat", "messages": ["BTC LONG. TP1 7. First target 7."]})
    source = packet["anchor_source_id"]
    spans = [span for span in candidates(packet) if span.kind == "number" and span.quote == "7"]
    expected = {"take_profits": [7], "evidence": [{"field": "take_profits.0", "source_id": source, "quote": "7"}]}
    annotations = [{"field": "take_profits.0", "source_id": source, "start": span.start, "end": span.end, "quote": "7"}
                   for span in spans]
    labels = question_labels(packet, expected, candidate_occurrences=annotations)
    assert all(labels["role." + span.candidate_id] == {"target_1"} for span in spans)


@pytest.mark.asyncio
async def test_changed_gold_cannot_resume_cached_observation_under_same_input(tmp_path):
    calls = []
    class Adapter:
        async def interpret(self, packet, profile, **kwargs):
            calls.append(packet)
            return DecisionResult(model="synthetic", interpretation=Interpretation(kind="report",
                source_refs=[packet["anchor_source_id"]]))
    checkpoint = EvaluationCheckpoint(tmp_path / "immutable.json", {"model": "synthetic"})
    cases = [{"id": "one", "messages": ["Original report"], "expected": {"kind": "report"}}]
    await evaluate(cases, Adapter(), checkpoint=checkpoint)
    cases[0]["expected"]["kind"] = "unrelated"
    with pytest.raises(ValueError, match="annotation provenance changed"):
        await evaluate(cases, Adapter(), checkpoint=checkpoint)
    assert len(calls) == 1


def test_calibration_freeze_cannot_change_after_holdout_started(tmp_path):
    from aqla_agent.decisions import CalibrationProfile
    checkpoint = EvaluationCheckpoint(tmp_path / "frozen.json", {"model": "synthetic"})
    original = CalibrationProfile(thresholds={"kind.report": 0.99})
    checkpoint.freeze(original)
    checkpoint.freeze(original)
    with pytest.raises(ValueError, match="cannot be retuned"):
        checkpoint.freeze(original.model_copy(update={"thresholds": {"kind.report": 0.5}}))


def test_proven_strict_deferred_gate_labels_raw_unspecified_as_correct():
    from aqla_agent.decision_evaluation import question_labels
    packet = case_packet({"id": "gate", "messages": ["BTC LONG. Entry 100. Deferred; strictly at the specified price."]})
    expected = {"entry": "limit", "entry_timing": "deferred", "entry_prices": [100]}
    assert question_labels(packet, expected)["entry"] == {"limit", "unspecified"}
    packet = case_packet({"id": "ordinary", "messages": ["BTC LONG. Entry 100. Enter later."]})
    assert question_labels(packet, expected)["entry"] == {"limit"}


@pytest.mark.asyncio
async def test_swapped_same_value_occurrence_roles_are_a_critical_error_even_when_public_values_match():
    import json

    import httpx
    from test_decisions import response_for

    from aqla_agent.decision_evaluation import complete_fields
    from aqla_agent.decisions import DEFAULT_DECISION_MODEL, OpenRouterDecisionAdapter, candidates
    from aqla_hyperliquid_mcp.models import Evidence
    case = {"id": "swapped", "messages": ["BTC LONG. Entry 100. SL 7. TP1 7."], "annotation_complete": True}
    packet = case_packet(case)
    source = packet["anchor_source_id"]
    occurrences = [span for span in candidates(packet) if span.kind == "number" and span.quote == "7"]
    gold = Interpretation(kind="new_signal", symbol="BTC", side="long", entry="limit", entry_prices=[100],
        stop_loss=7, take_profits=[7], source_refs=[source], evidence=[
            Evidence(field="symbol", source_id=source, quote="BTC"),
            Evidence(field="side", source_id=source, quote=packet["messages"][0]["text"]),
            Evidence(field="entry_prices.0", source_id=source, quote="100"),
            Evidence(field="take_profits.0", source_id=source, quote="7"),
            Evidence(field="stop_loss", source_id=source, quote="7")])
    case["expected"] = complete_fields(gold)
    case["candidate_occurrences"] = [{"field": field, "source_id": source, "start": span.start, "end": span.end, "quote": "7"}
                                     for field, span in zip(("stop_loss", "take_profits.0"), occurrences, strict=True)]

    def upstream(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=response_for(body["questions"], overrides={
            "role." + occurrences[0].candidate_id: "target_1", "role." + occurrences[1].candidate_id: "stop_loss"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        rows = await evaluate([case], OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL))
    assert rows[0]["accepted"] and not rows[0]["passed"] and rows[0]["critical_error"]
    assert rows[0]["mismatch_fields"] == ["candidate_occurrences"]


@pytest.mark.asyncio
async def test_invalid_occurrence_annotation_is_rejected_before_any_catalog_or_model_request():
    class Forbidden:
        async def identity(self):
            raise AssertionError("Invalid gold must not fetch a model catalog")
        async def interpret(self, *args, **kwargs):
            raise AssertionError("Invalid gold must not make a model request")
    source = {"chat_id": "channel", "message_id": 1, "sent_at": 1000, "text": "BTC LONG. SL 7."}
    case = {"id": "invalid", "messages": [source], "expected": {"stop_loss": 7}, "split": "calibration",
            "candidate_occurrences": [{"field": "stop_loss", "source_id": "unknown", "start": 0, "end": 1, "quote": "7"}]}
    with pytest.raises(ValueError, match="original position and Evidence"):
        await evaluate_split([case], Forbidden(), [])
