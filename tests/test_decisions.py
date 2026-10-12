import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from aqla_agent.decisions import (
    DEFAULT_DECISION_MODEL,
    CalibrationProfile,
    DecisionFailure,
    OpenRouterDecisionAdapter,
    SourceProcessingConfig,
    assemble,
    build_packet,
    candidates,
    checked_answers,
    questions_for,
    replay_decision,
)
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent


def packet_for(text="BTC long. Entry 100. SL 90. TP 110 / 120.", *, messages=None):
    sources = messages or [SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text=text)]
    rows = [{"event_id": s.event_id, "source_id": s.source_id, "source": s.model_dump(mode="json")} for s in sources]
    return build_packet({"event": rows[-1], "messages": rows, "trades": [],
                         "source_read_receipt": "private-receipt", "policy": {"private": "not-for-model"}})


def response_for(questions, *, kind="new_signal", overrides=None):
    answers = {}
    for key, question in questions.items():
        if question["type"] == "noul":
            probability = 1 if key != "live_action" or kind in ("new_signal", "trade_update") else 0
            answers[key] = {"type": "noul", "noul": probability}
            continue
        options = question["criteria"]
        prepared = {key: json.loads(value) if isinstance(value, str) and value.startswith("{") else value
                    for key, value in options.items()}
        selected = {"kind": kind, "side": "long", "entry": "limit", "entry_timing": "standard",
                    "update_action": "not_present", "source_exchange": "not_present", "reason_code": "result_report"}.get(key)
        if key == "symbol":
            selected = next((k for k, v in prepared.items() if isinstance(v, dict) and v.get("value") == "BTC"), "not_present")
        elif key.startswith("proof."):
            selected = next(k for k, v in prepared.items() if isinstance(v, dict))
        elif key.startswith("role."):
            instructions = json.loads(question["instructions"]) if isinstance(question["instructions"], str) else question["instructions"]
            raw = instructions["occurrence"]["quote"]
            selected = {"100": "entry0", "90": "stop_loss", "110": "target_1", "120": "target_2", "50%": "target_1", "100%": "target_2"}.get(raw, "ignore")
        selected = (overrides or {}).get(key, selected)
        assert selected in options, (key, selected)
        answers[key] = {"type": "choice", "choice": selected,
                        "probabilities": {k: float(k == selected) for k in options}}
    return {"model": DEFAULT_DECISION_MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0}}


def test_packet_omits_authority_and_accounts_but_keeps_original_reply_context():
    parent = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="BTC long. Entry 100.")
    reply = SourceEvent(chat_id="channel", message_id=2, sent_at=1001, reply_to_id=1, text="SL 90. TP 110.")
    packet = packet_for(messages=[parent, reply])
    assert [r["text"] for r in packet["messages"]] == [parent.text, reply.text]
    assert packet["messages"][1]["reply_to_id"] == 1
    assert "private" not in json.dumps(packet)
    assert packet["anchor_source_id"] == reply.source_id


def test_candidates_preserve_occurrences_percentages_and_ambiguous_normalizations():
    packet = packet_for("BTC Entry 1.234. SL 1.234. TP 50%. x5")
    spans = [s for s in candidates(packet) if s.kind == "number"]
    ambiguous = [s for s in spans if s.quote == "1.234"]
    assert {s.value for s in ambiguous} == {"1.234", "1234"}
    assert len({(s.start, s.end) for s in ambiguous}) == 2
    assert len({s.candidate_id for s in ambiguous}) == 4
    assert any(s.quote == "50%" and s.value == "50" for s in spans)
    for s in spans:
        assert packet["messages"][0]["text"][s.start:s.end] == s.quote


@pytest.mark.parametrize("word,value", [("четверть", 25), ("quarter", 25), ("Viertel", 25),
                                        ("половину", 50), ("half", 50), ("Halbe", 50)])
def test_exact_fraction_words_are_candidates_only_for_position_reduction(word, value):
    packet = packet_for("BTC: " + word + " position.")
    spans = candidates(packet)
    span = next(s for s in spans if s.notation == "position_fraction")
    assert span.quote == word and Decimal(span.value) == value
    assert packet["messages"][0]["text"][span.start:span.end] == word
    criteria = questions_for(packet, spans)["role." + span.candidate_id]["criteria"]
    assert set(criteria) == {"ignore", "reduce_percent", "unclear"}


@pytest.mark.parametrize("quote", ["х10", "Х10", "10х", "х 10", "x10", "10×"])
def test_multiplier_notation_preserves_cyrillic_and_latin_original_quotes(quote):
    from aqla_hyperliquid_mcp.rules import quoted_leverage_numbers
    span = next(s for s in candidates(packet_for("BTC плечо " + quote)) if s.kind == "number")
    assert span.quote == quote and Decimal(span.value) == 10
    assert list(quoted_leverage_numbers(span.quote)) == [Decimal(10)]


def test_replay_uses_edited_live_caption_and_validates_nonactional_evidence():
    original = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="", media="photo")
    edited = original.model_copy(update={"edited_at": 2000, "text": "Result report, no new instruction."})
    packet = packet_for(messages=[original, edited])
    questions = questions_for(packet, candidates(packet))
    result = replay_decision(packet, CalibrationProfile(), [response_for(questions, kind="report")],
                             model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.kind == "report"
    assert result.interpretation.evidence[0].quote == edited.text
    assert "missing_source_text" not in result.interpretation.reason_codes


@pytest.mark.parametrize("quote,amount,accepted", [("четверть", 25, True), ("Halbe", 50, True),
                                                   ("25%", 25, True), ("+286% четверть", 25, False),
                                                   ("50%", 25, False), ("quarter", 50, False), ("25", 25, False)])
def test_reduction_requires_one_original_quantity_and_matching_mathematics(quote, amount, accepted):
    from aqla_agent.decisions import validate_interpretation
    packet = packet_for("BTC: reduce " + quote)
    result = Interpretation(kind="trade_update", source_refs=[packet["anchor_source_id"]], update_action="reduce",
        reduce_percent=amount, evidence=[Evidence(field="reduce_percent", source_id=packet["anchor_source_id"], quote=quote)])
    if accepted:
        validate_interpretation(packet, result)
    else:
        with pytest.raises(DecisionFailure, match="reduction_quantity_proof_missing"):
            validate_interpretation(packet, result)


@pytest.mark.asyncio
async def test_normal_signal_uses_two_calls_and_only_original_numeric_quotes():
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await OpenRouterDecisionAdapter("synthetic-api-key", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(packet_for(), CalibrationProfile())
    assert len(requests) == 2 and result.verified
    signal = result.interpretation
    assert signal.kind == "new_signal" and signal.entry_prices == [Decimal("100")]
    assert signal.stop_loss == 90 and signal.take_profits == [110, 120]
    assert [p.quote for p in signal.evidence if p.field == "stop_loss"] == ["90"]
    assert len(result.usage) == 2
    assert result.usage[0]["cost_usd"] == 0 and result.usage[0]["cost_status"] == "actual"
    assert "private-receipt" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_result_report_uses_one_call_and_live_action_disagreement_is_rejected():
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"], kind="report"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(packet_for("BTC TP hit; result only."), CalibrationProfile())
    assert len(requests) == 1 and result.interpretation.kind == "report"
    packet = packet_for()
    spans = candidates(packet)
    questions = questions_for(packet, spans)
    response = response_for(questions, kind="report")
    response["answers"]["live_action"]["noul"] = 1
    values, probabilities = checked_answers(response, questions, CalibrationProfile())
    with pytest.raises(DecisionFailure, match="conflicting_live_intent"):
        assemble(packet, spans, values, probabilities, CalibrationProfile())


@pytest.mark.asyncio
async def test_verification_failure_never_produces_a_trading_result():
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        response = response_for(body["questions"])
        if len(calls) == 2:
            response["answers"]["verification.values"]["noul"] = 0.2
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(DecisionFailure, match="verification_failed"):
            await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(packet_for(), CalibrationProfile())
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", ["options", "nan", "sum", "model", "http"])
@pytest.mark.asyncio
async def test_bad_upstream_shapes_and_versions_fail_closed(mutation):
    def upstream(request):
        body = json.loads(request.content)
        response = response_for(body["questions"])
        if mutation == "options":
            response["answers"]["kind"]["choice"] = "execute_arbitrary_order"
        elif mutation == "nan":
            response["answers"]["live_action"]["noul"] = float("nan")
            return httpx.Response(200, content=json.dumps(response))
        elif mutation == "sum":
            response["answers"]["kind"]["probabilities"]["report"] = 0.7
        elif mutation == "model":
            response["model"] = "unapproved/model"
        elif mutation == "http":
            return httpx.Response(429, json={"error": "private upstream body"})
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(DecisionFailure) as error:
            await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(packet_for(), CalibrationProfile())
    assert "private upstream body" not in str(error.value)


def test_uncalibrated_profile_cannot_enable_sources_and_activation_needs_holdout():
    assert not CalibrationProfile().permits("channel")
    with pytest.raises(ValidationError):
        CalibrationProfile(validated=True, sources=["channel"], dataset_hash="same", holdout_hash="same",
                           holdout_cases=100, thresholds={"kind": 0.99})
    with pytest.raises(ValidationError):
        CalibrationProfile(thresholds={"kind": float("nan")})


def test_candidate_cap_never_silently_truncates_original_values():
    packet = packet_for(" ".join(str(i) for i in range(300)))
    with pytest.raises(DecisionFailure, match="candidate_limit"):
        candidates(packet)


@pytest.mark.asyncio
async def test_openrouter_catalog_binds_backend_revision_and_records_actual_cost():
    requests = []
    revision = "inception/mercury-decide-20260930"

    def upstream(request):
        requests.append(request)
        assert request.url.host == "openrouter.ai"
        if request.method == "GET":
            assert request.url.params["output_modalities"] == "decisions"
            return httpx.Response(200, json={"data": [{"id": DEFAULT_DECISION_MODEL, "canonical_slug": revision}]})
        assert request.url.path == "/api/v1/systemone"
        assert request.headers["Authorization"] == "Bearer synthetic"
        body = json.loads(request.content)
        value = response_for(body["questions"], kind="report")
        value["model"] = revision
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client)
        result = await adapter.interpret(packet_for("BTC result only."), CalibrationProfile())
        assert await adapter.identity() == revision
    assert [r.method for r in requests] == ["GET", "POST"]
    assert result.model_revision == revision and result.usage[0]["cost_usd"] == 0


@pytest.mark.asyncio
async def test_decision_models_are_configurable_and_missing_prices_are_unknown():
    model, revision = "other/decision-v2", "other/decision-v2-20261012"
    assert SourceProcessingConfig(model=model).model == model
    profile = CalibrationProfile(model=model, model_revision=revision, validated=True, sources=["channel"],
        master_hash="master", dataset_hash="train", holdout_hash="test", holdout_cases=30, thresholds={"kind": 0.99},
        source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5, "accepted_accuracy": 1, "critical_errors": 0}},
        source_permissions={"channel": ["report"]})
    assert profile.permits("channel", "master", model=model, model_revision=revision)
    assert not profile.permits("channel", "master", model=DEFAULT_DECISION_MODEL, model_revision=revision)
    assert not profile.permits("channel", "master", model=model, model_revision="changed")

    def upstream(request):
        body = json.loads(request.content)
        value = response_for(body["questions"], kind="report")
        value["model"] = revision
        value["usage"].pop("cost")
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await OpenRouterDecisionAdapter("synthetic", model=model, model_revision=revision, client=client).interpret(
            packet_for("BTC result only."), profile)
    assert result.usage[0]["cost_usd"] is None and result.usage[0]["cost_status"] == "unknown"
    assert "estimated_cost_usd" not in result.usage[0]


@pytest.mark.asyncio
async def test_raw_collection_keeps_low_confidence_but_cannot_claim_verification():
    captured = {}

    def upstream(request):
        body = json.loads(request.content)
        value = response_for(body["questions"])
        if "kind" in value["answers"]:
            value["answers"]["kind"]["probabilities"].update(new_signal=0.8, ambiguous=0.2)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL)
        result = await adapter.interpret(packet_for(), CalibrationProfile(), collect=True, observation=captured)
    assert result.collection_only and not result.verified and result.interpretation.stop_loss == 90
    with pytest.raises(DecisionFailure, match="uncertain_decision"):
        replay_decision(packet_for(), CalibrationProfile(), captured["responses"], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    accepted = replay_decision(packet_for(), CalibrationProfile(thresholds={"kind": 0.75}), captured["responses"],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert accepted.verified and not accepted.collection_only


@pytest.mark.asyncio
async def test_snapshot_change_between_calls_cannot_mix_model_revisions():
    requests = []
    revision = "inception/mercury-decide-20260930"

    def upstream(request):
        assert request.method == "POST", "An interpretation must not refresh its catalog halfway through"
        body = json.loads(request.content)
        requests.append(body)
        value = response_for(body["questions"])
        value["model"] = revision if len(requests) == 1 else "inception/mercury-decide-20261013"
        adapter.catalog_checked_at = -10000
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client, model_revision=revision)
        with pytest.raises(DecisionFailure, match="decision_model_changed"):
            await adapter.interpret(packet_for(), CalibrationProfile(model_revision=revision))
    assert len(requests) == 2


def test_unlinked_setup_numbers_and_ordinal_labels_are_never_financial_choices():
    older = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="ETH long. Entry 2500. SL 2400. TP 2700.")
    current = SourceEvent(chat_id="channel", message_id=2, sent_at=1001, text="BTC long. Entry 100.\nTarget 1: 110.\n2) 120. SL 90.")
    packet = packet_for(messages=[older, current])
    spans = candidates(packet)
    questions = questions_for(packet, spans)
    for span in spans:
        if span.kind == "number" and (span.source_id == older.source_id or span.notation == "ordinal"):
            assert "role." + span.candidate_id not in questions
    assert any(span.notation == "ordinal" and span.quote == "2" for span in spans)
