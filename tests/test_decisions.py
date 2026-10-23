
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
    numeric_occurrences,
    questions_for,
    replay_decision,
    verification_questions,
)
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, require_first


def packet_for(text="BTC long. Entry 100. SL 90. TP 110 / 120.", *, messages=None):
    sources = messages or [SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text=text)]
    rows = [{"event_id": s.event_id, "source_id": s.source_id, "source": s.model_dump(mode="json")} for s in sources]
    return build_packet({"event": rows[-1], "messages": rows, "trades": [],
                         "source_read_receipt": "private-receipt", "policy": {"private": "not-for-model"}})


def selected_answer(key, question, prepared, kind):
    selected = {"kind": kind, "side": "long", "entry": "limit", "entry_timing": "standard",
                "update_action": "not_present", "source_exchange": "not_present", "reason_code": "result_report"}.get(key)
    if key == "symbol":
        selected = next((k for k, v in prepared.items() if isinstance(v, dict) and v.get("value") == "BTC"), "not_present")
    elif key.startswith("proof."):
        selected = require_first(k for k, v in prepared.items() if isinstance(v, dict))
    elif key.startswith("role."):
        instructions = json.loads(question["instructions"]) if isinstance(question["instructions"], str) else question["instructions"]
        raw = instructions["occurrence"]["quote"]
        selected = {"100": "entry0", "90": "stop_loss", "110": "target_1", "120": "target_2", "50%": "target_1", "100%": "target_2"}.get(raw, "ignore")
    elif key.startswith("normalization."):
        selected = "not_present"
    return selected


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
        selected = selected_answer(key, question, prepared, kind)
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


def test_ambiguous_numeric_occurrence_has_one_normalization_choice_and_one_role():
    packet = packet_for("BTC long. Entry limit 12.473. SL 12.473. TP 100%.")
    spans = candidates(packet)
    questions = questions_for(packet, spans)
    ambiguous = [(identity, group) for identity, group in numeric_occurrences(spans) if len(group) > 1]
    assert len(ambiguous) == 2 and ambiguous[0][0] != ambiguous[1][0]
    for identity, group in ambiguous:
        assert {span.value for span in group} == {"12.473", "12473"}
        assert "role." + identity in questions and "normalization." + identity in questions
        assert all("role." + span.candidate_id not in questions for span in group)
        prepared = {key: json.loads(value) for key, value in questions["normalization." + identity]["criteria"].items()
                    if key not in ("not_present", "unclear")}
        assert prepared == {span.candidate_id: span.model_dump(mode="json") for span in group}
        role_occurrence = json.loads(questions["role." + identity]["instructions"])["occurrence"]
        assert role_occurrence["quote"] == "12.473" and "value" not in role_occurrence
        assert role_occurrence["candidate_ids"] == [span.candidate_id for span in group]
    unique = require_first(span for span in spans if span.quote == "100%")
    assert "role." + unique.candidate_id in questions
    assert "normalization." + unique.candidate_id not in questions


@pytest.mark.asyncio
@pytest.mark.parametrize("quote,value", [("12.473", "12.473"), ("12.473", "12473"),
    ("3,963", "3.963"), ("1,206", "1.206"), ("1.984", "1.984"), ("1.984", "1984")])
async def test_only_model_selected_normalization_receives_shared_occurrence_role(quote, value):
    packet = packet_for("BTC long. Entry limit " + quote + ".")
    identity, group = numeric_occurrences(candidates(packet))[0]
    selected = require_first(span for span in group if Decimal(span.value) == Decimal(value))
    requests, observation = [], {}

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"], overrides={
            "role." + identity: "entry0", "normalization." + identity: selected.candidate_id}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(
            packet, CalibrationProfile(), observation=observation)
    assert len(requests) == 2 and result.verified
    assert result.interpretation.entry_prices == [Decimal(value)]
    assert [(proof.source_id, proof.quote) for proof in result.interpretation.evidence if proof.field == "entry_prices.0"] == [
        (selected.source_id, quote)]
    assert observation["values"]["normalization." + identity] == selected.candidate_id
    assert observation["responses"][0]["answers"]["normalization." + identity]["choice"] == selected.candidate_id
    assert "normalization." + identity in result.required_questions
    assert result.probabilities["normalization." + identity] == 1
    assert not any(key.startswith("normalization.") for key in requests[1]["questions"])


@pytest.mark.parametrize("normalization,failure", [("not_present", "numeric_normalization_missing"),
                                                   ("unclear", "uncertain_decision")])
def test_requested_ambiguous_value_cannot_be_silently_dropped_or_normalized(normalization, failure):
    packet = packet_for("BTC long. Entry limit 12.473.")
    identity, _ = numeric_occurrences(candidates(packet))[0]
    questions = questions_for(packet, candidates(packet))
    response = response_for(questions, overrides={"role." + identity: "entry0", "normalization." + identity: normalization})
    with pytest.raises(DecisionFailure, match=failure):
        replay_decision(packet, CalibrationProfile(), [response], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)


@pytest.mark.asyncio
async def test_uncertain_numeric_normalization_stops_before_second_model_call():
    packet = packet_for("BTC long. Entry limit 12.473.")
    identity, group = numeric_occurrences(candidates(packet))[0]
    requests, observation = [], {}

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        response = response_for(body["questions"], overrides={"role." + identity: "entry0",
            "normalization." + identity: group[0].candidate_id})
        answer = response["answers"]["normalization." + identity]
        answer["probabilities"] = {key: 0.51 if key == group[0].candidate_id else 0.49 if key == group[1].candidate_id else 0
                                   for key in answer["probabilities"]}
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(DecisionFailure, match="uncertain_decision"):
            await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(
                packet, CalibrationProfile(thresholds={"number_normalization": 0.99}), observation=observation)
    assert len(requests) == 1 and observation["probabilities"]["normalization." + identity] == 0.51
    assert observation["values"]["normalization." + identity] == group[0].candidate_id


def test_separate_equal_quotes_choose_values_independently_and_dates_stay_unrequested():
    packet = packet_for("BTC long. Date 13.10.2026. Entry limit 1.234. SL 1.234.")
    groups = numeric_occurrences(candidates(packet))
    assert len(groups) == 2
    first, second = groups[0], groups[1]
    assert first[0] != second[0] and first[1][0].quote == second[1][0].quote == "1.234"
    overrides = {}
    for (identity, group), role, value in zip(groups, ("entry0", "stop_loss"), ("1234", "1.234"), strict=True):
        overrides["role." + identity] = role
        overrides["normalization." + identity] = require_first(span.candidate_id for span in group if span.value == value)
    result = replay_decision(packet, CalibrationProfile(), [response_for(questions_for(packet, candidates(packet)), overrides=overrides),
        response_for(verification_questions())], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.entry_prices == [1234] and result.interpretation.stop_loss == Decimal("1.234")


def test_percent_normalization_preserves_original_quote_and_unit():
    packet = packet_for("BTC long. Entry 100. TP1 1.234%.")
    identity, group = require_first((identity, group) for identity, group in numeric_occurrences(candidates(packet)) if len(group) > 1)
    selected = require_first(span for span in group if span.value == "1.234")
    overrides = {"role." + identity: "target_1", "normalization." + identity: selected.candidate_id}
    result = replay_decision(packet, CalibrationProfile(), [response_for(questions_for(packet, candidates(packet)), overrides=overrides),
        response_for(verification_questions())], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.take_profits == [Decimal("1.234")]
    assert result.interpretation.take_profit_unit == "roe_percent"
    assert require_first(proof.quote for proof in result.interpretation.evidence if proof.field == "take_profits.0") == "1.234%"


def test_ignored_ambiguous_number_does_not_require_a_guessed_normalization():
    packet = packet_for("BTC long. Entry 100. Historical reference 12.473.")
    identity, _ = require_first((identity, group) for identity, group in numeric_occurrences(candidates(packet)) if len(group) > 1)
    response = response_for(questions_for(packet, candidates(packet)), overrides={"normalization." + identity: "unclear"})
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.entry_prices == [100]
    assert "normalization." + identity not in result.required_questions


@pytest.mark.asyncio
async def test_native_worker_reviews_uncertain_normalization_without_any_financial_mutation(setup, tmp_path):
    from types import SimpleNamespace

    from aqla_agent.source_worker import SourceWorker
    from aqla_hyperliquid_mcp.models import now_ms
    from aqla_hyperliquid_mcp.server import close_server, create_server

    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source = SourceEvent(chat_id="channel", message_id=31, sent_at=now_ms(), text="BTC long. Entry limit 12.473.")
    server.aqla_trading.ingest(source)
    master = tmp_path / "home/skills/aqla-master/SKILL.md"
    master.parent.mkdir(parents=True)
    master.write_text("Synthetic source guidance", encoding="utf-8")
    calls, fallbacks = [], []

    async def mcp_call(_, tool, args):
        result = await server.call_tool(tool, args)
        return result.structured_content or json.loads(result.content[0].text)

    extension = SimpleNamespace(authority=server.aqla_authority, server="aqla", mcp_call=mcp_call,
        gateway_identity="worker-v11", owner="42", master_path=master, skill_hash=lambda: "master-v11", binding=None)

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        normalization = require_first(key for key in body["questions"] if key.startswith("normalization."))
        identity = normalization.removeprefix("normalization.")
        return httpx.Response(200, json=response_for(body["questions"], overrides={
            "role." + identity: "entry0", normalization: "unclear"}))

    async def unresolved(packet, code, usage):
        fallbacks.append(code)
        assert code == "uncertain_decision" and packet["decision_review"]["values"]
        assert any(key.startswith("normalization.") and value == "unclear"
                   for key, value in packet["decision_review"]["values"].items())
        raise DecisionFailure("uncertain_decision")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            worker = SourceWorker(extension, {"mode": "hybrid"}, fallback=unresolved,
                adapter=OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL))
            await worker.tick()
            await worker.tick()
        assert len(calls) == len(fallbacks) == 1
        assert len(state.snapshot()["source_jobs"]) == 1
        assert not fake.actions and not state.snapshot()["operations"] and not state.snapshot()["plans"]
        assert state.snapshot()["events"][source.event_id]["status"] == "pending"
    finally:
        await close_server(server)


@pytest.mark.parametrize("word,value", [("четверть", 25), ("quarter", 25), ("Viertel", 25),
                                        ("половину", 50), ("half", 50), ("Halbe", 50)])
def test_exact_fraction_words_are_candidates_only_for_position_reduction(word, value):
    packet = packet_for("BTC: " + word + " position.")
    spans = candidates(packet)
    span = require_first(s for s in spans if s.notation == "position_fraction")
    assert span.quote == word and Decimal(span.value) == value
    assert packet["messages"][0]["text"][span.start:span.end] == word
    criteria = questions_for(packet, spans)["role." + span.candidate_id]["criteria"]
    assert set(criteria) == {"ignore", "reduce_percent", "unclear"}


@pytest.mark.parametrize("quote", ["х10", "Х10", "10х", "х 10", "x10", "10×"])
def test_multiplier_notation_preserves_cyrillic_and_latin_original_quotes(quote):
    from aqla_hyperliquid_mcp.rules import quoted_leverage_numbers
    span = require_first(s for s in candidates(packet_for("BTC плечо " + quote)) if s.kind == "number")
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


@pytest.mark.parametrize("kind", ["incomplete", "ambiguous"])
def test_uncertain_new_trading_intent_requires_bounded_review(kind):
    packet = packet_for("BTC limit order; direction unclear.")
    questions = questions_for(packet, candidates(packet))
    response = response_for(questions, kind=kind, overrides={"side":"unclear"})
    response["answers"]["live_action"]["noul"] = 1
    with pytest.raises(DecisionFailure,match="uncertain_trading_intent_requires_review"):
        replay_decision(packet, CalibrationProfile(), [response], model=DEFAULT_DECISION_MODEL,
                        revision=DEFAULT_DECISION_MODEL)
    assert "reason_code" not in questions


@pytest.mark.parametrize("quote,amount,accepted", [("четверть", 25, True), ("Halbe", 50, True),
                                                   ("25%", 25, True), ("+286% четверть", 25, False),
                                                   ("50%", 25, False), ("quarter", 50, False), ("25", 25, False)])
def test_reduction_requires_one_original_quantity_and_matching_mathematics(quote, amount, accepted):
    from aqla_agent.decisions import validate_interpretation
    packet = packet_for("BTC: reduce " + quote)
    result = Interpretation(kind="trade_update", symbol="BTC", source_refs=[packet["anchor_source_id"]], update_action="reduce",
        reduce_percent=amount, evidence=[Evidence(field="symbol", source_id=packet["anchor_source_id"], quote="BTC"),
            Evidence(field="reduce_percent", source_id=packet["anchor_source_id"], quote=quote)])
    if accepted:
        validate_interpretation(packet, result)
    else:
        with pytest.raises(AqlaError, match="Reduction percentage.*source evidence"):
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
        source_evidence={"channel": {"calibration_groups": 3, "holdout_groups": 5, "accepted_accuracy": 1, "critical_errors": 0,
                                     "kinds": {"report": {"calibration_correct_groups": 1, "holdout_correct_accepted_groups": 2}}}},
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


def test_packet_keeps_actual_replies_and_own_setup_context_but_prunes_unbound_history():
    unrelated=SourceEvent(chat_id="channel",message_id=1,sent_at=1000,text="ETH long. Entry 2500. SL 2400.")
    parent=SourceEvent(chat_id="channel",message_id=2,sent_at=1001,text="BTC long. Entry 100. SL 90.")
    anchor=SourceEvent(chat_id="channel",message_id=3,sent_at=1002,reply_to_id=2,text="BTC: move stop to 95.")
    packet=packet_for(messages=[unrelated,parent,anchor])
    assert [row["source_id"] for row in packet["messages"]]==[parent.source_id,anchor.source_id]
    rows=[{"event_id":s.event_id,"source_id":s.source_id,"source":s.model_dump(mode="json")} for s in (unrelated,parent,anchor)]
    owned=build_packet({"event":rows[-1],"messages":rows,"bound_source_refs":[parent.source_id],
        "trades":[{"trade_id":"owned","status":"open","plan":{"source_refs":[unrelated.source_id],"market":{"symbol":"ETH"},"side":"long"}}]})
    assert owned["anchor_setup_bound"] and len(owned["messages"])==3
    unbound_edit=anchor.model_copy(update={"edited_at":1003,"reply_to_id":None})
    assert not packet_for(messages=[anchor,unbound_edit])["anchor_setup_bound"]


@pytest.mark.parametrize("symbol,side,text", [("LINK","short","LINK SHORT. Entry range 14 - 15. Stop 16. Targets 12 / 11. Leverage 6x."),
                                            ("W","long","W LONG. New setup, no exits or leverage stated.")])
def test_complete_identity_with_new_intent_cannot_be_silently_stored_incomplete(symbol,side,text):
    packet=packet_for(text)
    questions=questions_for(packet,candidates(packet))
    selected=require_first(key for key,value in questions["symbol"]["criteria"].items()
                  if isinstance(value,str) and value.startswith("{") and json.loads(value).get("value")==symbol)
    response=response_for(questions,kind="incomplete",overrides={"symbol":selected,"side":side})
    response["answers"]["live_action"]["noul"]=0.999
    with pytest.raises(DecisionFailure,match="uncertain_trading_intent_requires_review"):
        replay_decision(packet,CalibrationProfile(),[response],model=DEFAULT_DECISION_MODEL,revision=DEFAULT_DECISION_MODEL)


def test_explicit_market_form_cannot_be_lost_as_unspecified():
    from aqla_agent.decisions import validate_interpretation
    packet=packet_for("BTC LONG. MARKET entry 100. SL 90. TP 110.")
    source=packet["anchor_source_id"]
    interpretation=Interpretation(kind="new_signal",symbol="BTC",side="long",source_refs=[source],
        entry="unspecified",entry_prices=[100],stop_loss=90,take_profits=[110],evidence=[
            Evidence(field="symbol",source_id=source,quote="BTC"),Evidence(field="side",source_id=source,quote=packet["messages"][0]["text"]),
            Evidence(field="entry_prices.0",source_id=source,quote="100"),Evidence(field="stop_loss",source_id=source,quote="90"),
            Evidence(field="take_profits.0",source_id=source,quote="110")])
    with pytest.raises(DecisionFailure,match="explicit_entry_form_requires_review"):
        validate_interpretation(packet,interpretation)


def test_classification_proofs_cover_original_text_without_another_semantic_choice():
    text="Original greeting. "*80
    packet=packet_for(text)
    questions=questions_for(packet,candidates(packet))
    assert "proof.classification" not in questions
    result=replay_decision(packet,CalibrationProfile(),[response_for(questions,kind="unrelated")],
                           model=DEFAULT_DECISION_MODEL,revision=DEFAULT_DECISION_MODEL)
    assert "".join(proof.quote for proof in result.interpretation.evidence)==text


def test_calibrated_numeric_roles_have_separate_thresholds_and_disabled_families():
    profile = CalibrationProfile(thresholds={"number_role.ignore": 0.85, "number_role.stop_loss": 0.995},
                                 disabled_questions=["number_role.leverage"])
    assert profile.threshold("role.original", "ignore") == 0.85
    assert profile.threshold("role.original", "stop_loss") == 0.995
    assert profile.question_disabled("role.original", "leverage")
    assert not profile.question_disabled("role.original", "stop_loss")
    assert CalibrationProfile(thresholds={"number_role":0.8}).threshold("role.original") == 0.8


@pytest.mark.parametrize("symbol", ["4STOCK", "1000BONK", "1000RATS"])
def test_digit_prefixed_asset_candidates_preserve_original_positions(symbol):
    packet = packet_for("#" + symbol + " LONG. Entry 100. SL 90. TP 110.")
    spans = candidates(packet)
    selected = require_first(span for span in spans if span.kind == "symbol" and span.value == symbol)
    assert packet["messages"][0]["text"][selected.start:selected.end] == selected.quote == symbol
    options = questions_for(packet, spans)["symbol"]["criteria"]
    assert selected.candidate_id in options


@pytest.mark.parametrize("symbol", ["wld", "near", "ena", "fartcoin"])
def test_lowercase_explicit_setup_headings_are_generic_symbol_candidates(symbol):
    packet = packet_for(symbol + " short\nEntry 100. SL 110. TP 90.")
    span = require_first(span for span in candidates(packet) if span.kind == "symbol" and span.value == symbol)
    assert span.quote == symbol and span.start == 0
    assert span.candidate_id in questions_for(packet, candidates(packet))["symbol"]["criteria"]


def test_prose_word_next_to_direction_does_not_become_lowercase_asset():
    packet = packet_for("the long position is open.\nWe have a near short-term target.")
    values = {span.value for span in candidates(packet) if span.kind == "symbol"}
    assert not values & {"the", "near", "position"}


def test_timeframes_numeric_sizes_and_multipliers_are_not_new_digit_prefixed_symbols():
    packet = packet_for("BTC LONG. 4H 15x 15X 30m $250K 1000 1/2/2026.")
    values = {span.value for span in candidates(packet) if span.kind == "symbol"}
    assert not values & {"4H", "15x", "15X", "30m", "250K", "1000"}


@pytest.mark.asyncio
async def test_complete_outgoing_decision_size_is_checked_before_catalog_or_provider():
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(requests.append)) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client)
        with pytest.raises(DecisionFailure, match="source_context_limit"):
            await adapter.interpret(packet_for(), CalibrationProfile(), max_context_characters=100)
        with pytest.raises(DecisionFailure, match="source_context_limit"):
            await adapter.evaluate({"original": "x" * 500}, verification_questions(), max_context_characters=100)
    assert requests == []


@pytest.mark.asyncio
async def test_invalid_successful_response_is_not_logged_as_transport_failure():
    usage = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"model": "changed"}))) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL)
        with pytest.raises(DecisionFailure, match="decision_model_changed"):
            await adapter.evaluate({}, {}, usage=usage)
    assert usage[0]["response_success"] is True
    assert usage[0]["failed"] and usage[0]["usage_available"] is False


@pytest.mark.asyncio
async def test_prepared_candidates_are_reused_without_changing_request(monkeypatch):
    import aqla_agent.decisions as decision_module
    packet = packet_for()
    spans = candidates(packet)
    requests = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body["questions"]))

    monkeypatch.setattr(decision_module, "candidates", lambda _: pytest.fail("Prepared candidates must be reused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        result = await OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL).interpret(
            packet, CalibrationProfile(), spans=spans)
    assert result.verified and len(requests) == 2


@pytest.mark.parametrize("field,value", [("retry_max_attempts", 0), ("retry_max_attempts", 6),
    ("retry_max_attempts", True), ("retry_backoff_seconds", 301), ("retry_max_age_seconds", 29),
    ("retry_max_age_seconds", "900")])
def test_new_retry_config_requires_bounded_integer_values(field, value):
    with pytest.raises(ValidationError):
        SourceProcessingConfig(**{field: value})


def test_python_bound_setup_cannot_become_a_second_entry():
    from aqla_agent.decisions import validate_interpretation
    packet = packet_for("BTC LONG.")
    packet["anchor_setup_bound"] = True
    result = Interpretation(kind="new_signal", symbol="BTC", side="long", source_refs=[packet["anchor_source_id"]],
        evidence=[Evidence(field="symbol", source_id=packet["anchor_source_id"], quote="BTC"),
                  Evidence(field="side", source_id=packet["anchor_source_id"], quote="LONG")])
    with pytest.raises(DecisionFailure, match="bound_setup_cannot_reenter"):
        validate_interpretation(packet, result)


@pytest.mark.parametrize("wording", ["Deferred, not now; strictly at the specified price.",
    "Вход отложенный, не сейчас, а строго по указанной цене.", "Später einsteigen, nur zum angegebenen Preis."])
def test_exact_live_price_gate_derives_supported_limit_without_changing_raw_model_choice(wording):
    packet = packet_for("BTC LONG. Entry 100. " + wording)
    questions = questions_for(packet, candidates(packet))
    response = response_for(questions, overrides={"entry": "unspecified", "entry_timing": "deferred"})
    observed = {}
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL, observation=observed)
    assert result.interpretation.entry == "limit" and result.interpretation.entry_timing == "deferred"
    assert result.interpretation.entry_prices == [100]
    assert observed["values"]["entry"] == "unspecified" and observed["probabilities"]["entry"] == 1


def test_time_delay_or_ordinary_reference_does_not_invent_price_bound_entry():
    packet = packet_for("BTC LONG. Entry 100. Enter later.")
    response = response_for(questions_for(packet, candidates(packet)), overrides={"entry": "unspecified", "entry_timing": "deferred"})
    with pytest.raises(DecisionFailure, match="decision_schema_invalid"):
        replay_decision(packet, CalibrationProfile(), [response], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)


@pytest.mark.parametrize("delimiter", ["-", "–", "—", "to", "bis", "до"])
def test_exact_two_selected_live_prices_derive_range_without_changing_raw_choice(delimiter):
    packet = packet_for("BTC LONG. 100 " + delimiter + " 105. SL 90. TP 110.")
    spans = candidates(packet)
    overrides = {"entry": "unspecified", **{"role." + span.candidate_id: "entry1" for span in spans if span.quote == "105"}}
    response = response_for(questions_for(packet, spans), overrides=overrides)
    observation = {}
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL, observation=observation)
    assert result.interpretation.entry == "range" and result.interpretation.entry_prices == [100, 105]
    assert observation["values"]["entry"] == "unspecified" and observation["probabilities"]["entry"] == 1


def test_two_prices_in_separate_mentions_do_not_prove_a_range():
    packet = packet_for("BTC LONG. Entry 100. Reference 105. SL 90. TP 110.")
    spans = candidates(packet)
    response = response_for(questions_for(packet, spans), overrides={"entry": "unspecified",
        **{"role." + span.candidate_id: "entry1" for span in spans if span.quote == "105"}})
    with pytest.raises(DecisionFailure, match="decision_evidence_invalid"):
        replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
            model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    response = response_for(questions_for(packet, spans), overrides={"entry": "unspecified"})
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.entry == "unspecified"
    assert result.interpretation.entry_prices == [100]


def test_exact_same_live_tp_role_repetition_coalesces_without_merging_target_roles():
    packet = packet_for("BTC LONG. Entry 100. SL 90. TP1: 110. First target 110.")
    spans = candidates(packet)
    response = response_for(questions_for(packet, spans))
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.take_profits == [110]
    assert len([span for span in spans if span.kind == "number" and span.value == "110"]) == 2
    packet = packet_for("BTC LONG. Entry 100. SL 90. TP1: 110. TP2: 110.")
    spans = candidates(packet)
    targets = [span for span in spans if span.kind == "number" and span.value == "110"]
    response = response_for(questions_for(packet, spans), overrides={"role." + targets[1].candidate_id: "target_2"})
    result = replay_decision(packet, CalibrationProfile(), [response, response_for(verification_questions())],
        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.take_profits == [110, 110]


@pytest.mark.parametrize("extra", ["TP1: 120.", "ETH LONG. TP1: 110.", "TP1: 110%."])
def test_same_role_unequal_or_independent_or_different_unit_mentions_still_conflict(extra):
    packet = packet_for("BTC LONG. Entry 100. SL 90. TP1: 110. " + extra)
    spans = candidates(packet)
    overrides = {"role." + span.candidate_id: "target_1" for span in spans if span.kind == "number" and span.value in ("110", "120")}
    response = response_for(questions_for(packet, spans), overrides=overrides)
    with pytest.raises(DecisionFailure, match="conflicting_numeric_roles"):
        replay_decision(packet, CalibrationProfile(), [response], model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)


def test_calibrated_kind_and_intent_polarities_do_not_share_error_thresholds():
    profile = CalibrationProfile(thresholds={"kind.new_signal":0.8,"kind.incomplete":0.995,
        "live_action.true":0.85,"live_action.false":0.98},disabled_questions=["kind.ambiguous"])
    assert profile.threshold("kind","new_signal") == 0.8
    assert profile.threshold("kind","incomplete") == 0.995
    assert profile.threshold("live_action",True) == 0.85
    assert profile.threshold("live_action",False) == 0.98
    assert profile.question_disabled("kind","ambiguous")
    assert not profile.question_disabled("kind","new_signal")
    assert CalibrationProfile(disabled_questions=["kind"]).question_disabled("kind","new_signal")


def test_equivalent_symbol_occurrences_do_not_split_choice_confidence():
    packet = packet_for("BTC LONG. BTC Entry 100. SL 90. BTC TP 110.")
    spans = candidates(packet)
    assert len([span for span in spans if span.kind == "symbol" and span.value == "BTC"]) == 3
    options = questions_for(packet, spans)["symbol"]["criteria"]
    matching = [key for key,value in options.items() if value.startswith("{") and json.loads(value).get("value") == "BTC"]
    assert len(matching) == 1


def test_symbol_options_preserve_asset_x_but_exclude_exact_leverage_prefixes():
    packet = packet_for("OP SHORT X 15. Leverage X20.")
    options = questions_for(packet,candidates(packet))["symbol"]["criteria"]
    values = [json.loads(value)["value"] for value in options.values() if value.startswith("{")]
    assert "OP" in values and "X" not in values and "X20" not in values
    packet = packet_for("X LONG. Entry 100. SL 90. TP 110.")
    options = questions_for(packet,candidates(packet))["symbol"]["criteria"]
    assert any(value.startswith("{") and json.loads(value)["value"]=="X" for value in options.values())


def test_equivalent_reply_symbols_use_current_or_nearest_original_quote():
    root = SourceEvent(chat_id="channel",message_id=1,sent_at=1000,text="BTC LONG. Entry 100.")
    parent = SourceEvent(chat_id="channel",message_id=2,sent_at=1001,reply_to_id=1,text="BTC still open.")
    anchor = SourceEvent(chat_id="channel",message_id=3,sent_at=1002,reply_to_id=2,text="Move stop to entry now.")
    packet = packet_for(messages=[root,parent,anchor])
    packet["messages"] = [packet["messages"][2],packet["messages"][0],packet["messages"][1]]
    options = questions_for(packet,candidates(packet))["symbol"]["criteria"]
    matching = [json.loads(value) for value in options.values() if value.startswith("{") and json.loads(value)["value"]=="BTC"]
    assert len(matching)==1 and matching[0]["source_id"]==parent.source_id


def test_update_venue_absence_does_not_require_historical_footer_confidence():
    parent = SourceEvent(chat_id="channel",message_id=1,sent_at=1000,text="BTC LONG. Exchange: Bybit. Entry 100.")
    anchor = SourceEvent(chat_id="channel",message_id=2,sent_at=1001,reply_to_id=1,text="BTC: close now.")
    packet = packet_for(messages=[parent,anchor])
    spans=candidates(packet)
    response=response_for(questions_for(packet,spans),kind="trade_update",overrides={"update_action":"close","side":"not_present"})
    profile=CalibrationProfile(disabled_questions=["source_exchange"])
    result=replay_decision(packet,profile,[response,response_for(verification_questions())],model=DEFAULT_DECISION_MODEL,revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.source_exchange is None and "source_exchange" not in result.required_questions


@pytest.mark.parametrize("quote,value", [("1/2",50),("1/4",25),("3/4",75),("2/4",50)])
def test_exact_fractional_position_quantities_keep_original_quotes(quote,value):
    packet=packet_for("BTC: close "+quote+" of the position now.")
    span=require_first(s for s in candidates(packet) if s.notation=="position_fraction")
    assert span.quote==quote and span.value==str(value)
    interpretation=Interpretation(kind="trade_update",symbol="BTC",update_action="reduce",reduce_percent=value,
        source_refs=[packet["anchor_source_id"]],evidence=[Evidence(field="symbol",source_id=packet["anchor_source_id"],quote="BTC"),
            Evidence(field="reduce_percent",source_id=packet["anchor_source_id"],quote=quote)])
    from aqla_agent.decisions import validate_interpretation
    validate_interpretation(packet,interpretation)


def test_dates_and_nonterminating_fractions_do_not_become_position_quantities():
    packet=packet_for("BTC: report 4/1/2 and 1/2/2026. Also 1/3 is not silently rounded.")
    assert not any(s.notation=="position_fraction" for s in candidates(packet))


def test_two_current_management_actions_cannot_execute_as_one_partial_update():
    packet=packet_for("BTC: close 25% and move the stop to break-even now.")
    questions=questions_for(packet,candidates(packet))
    response=response_for(questions,kind="trade_update",overrides={"update_action":"reduce","side":"not_present"})
    response["answers"]["single_management_action"]["noul"]=0.01
    with pytest.raises(DecisionFailure,match="management_actions_not_representable"):
        replay_decision(packet,CalibrationProfile(),[response,response_for(verification_questions())],
            model=DEFAULT_DECISION_MODEL,revision=DEFAULT_DECISION_MODEL)


@pytest.mark.parametrize("text,quote,amount", [("BTC: exit at +70%.","70%",70),
    ("Закройте BTC - 30%. Слабый актив.","30%",30),("BTC: выйдем на −20%.","20%",20)])
def test_signed_pnl_cannot_be_used_as_a_positive_position_reduction(text,quote,amount):
    from aqla_agent.decisions import validate_interpretation
    packet=packet_for(text)
    result=Interpretation(kind="trade_update",symbol="BTC",update_action="reduce",reduce_percent=amount,
        source_refs=[packet["anchor_source_id"]],evidence=[Evidence(field="reduce_percent",source_id=packet["anchor_source_id"],quote=quote)])
    with pytest.raises(DecisionFailure,match="pnl_percentage_is_not_reduction_quantity"):
        validate_interpretation(packet,result)


def test_update_without_market_needs_python_authoritative_setup_binding():
    from aqla_agent.decisions import validate_interpretation
    packet=packet_for("Close the position now.")
    result=Interpretation(kind="trade_update",update_action="close",source_refs=[packet["anchor_source_id"]])
    with pytest.raises(DecisionFailure,match="update_identity_missing"):
        validate_interpretation(packet,result)
    packet["anchor_setup_bound"]=True
    validate_interpretation(packet,result)


def test_explicit_order_form_mentions_keep_anchor_quotes_and_positions():
    parent = SourceEvent(chat_id="channel",message_id=1,sent_at=1000,text="BTC LONG LIMIT 100.")
    anchor = SourceEvent(chat_id="channel",message_id=2,sent_at=1001,reply_to_id=1,text="BTC SHORT. Вход: Рынок 110.")
    packet = packet_for(messages=[parent,anchor])
    question = questions_for(packet,candidates(packet))["entry"]
    instruction = json.loads(question["instructions"])
    mentions = instruction["original_order_form_mentions"]
    assert [row["form"] for row in mentions] == ["market"]
    assert all(anchor.text[row["start"]:row["end"]] == row["quote"] for row in mentions)
    assert "proof.side" not in questions_for(packet,candidates(packet))


def test_update_numbers_from_reply_parent_do_not_gate_current_changed_value():
    parent = SourceEvent(chat_id="channel", message_id=1, sent_at=1000, text="BTC LONG. Entry 100. SL 90. TP 110.")
    anchor = SourceEvent(chat_id="channel", message_id=2, sent_at=1001, reply_to_id=1, text="BTC: stop now to 95.")
    packet = packet_for(messages=[parent,anchor])
    spans = candidates(packet)
    overrides = {"update_action":"move_stop", "side":"not_present"}
    overrides.update({"role."+span.candidate_id:"stop_loss" for span in spans if span.kind=="number" and span.quote=="95"})
    questions = questions_for(packet,spans)
    response = response_for(questions,kind="trade_update",overrides=overrides)
    profile = CalibrationProfile(thresholds={"number_role.ignore":1})
    result = replay_decision(packet,profile,[response,response_for(verification_questions())],
                             model=DEFAULT_DECISION_MODEL,revision=DEFAULT_DECISION_MODEL)
    assert result.interpretation.stop_loss == 95
    assert result.interpretation.entry_prices == []
    assert len(result.interpretation.evidence) == 3


@pytest.mark.parametrize("kind", ["incomplete", "ambiguous"])
@pytest.mark.parametrize("probability,disabled", [(0.49, []), (0.0, ["live_action"])])
def test_uncertain_or_disabled_negative_intent_cannot_be_stored_without_review(kind, probability, disabled):
    packet = packet_for("BTC: unclear instruction.")
    questions = questions_for(packet, candidates(packet))
    response = response_for(questions, kind=kind)
    response["answers"]["live_action"]["noul"] = probability
    with pytest.raises(DecisionFailure, match="conflicting_live_intent"):
        replay_decision(packet, CalibrationProfile(disabled_questions=disabled), [response],
                        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)


@pytest.mark.parametrize("comment", ["Daily market update follows.", "Market cap 100 billion.", "Daily range 98 - 102."])
def test_market_commentary_does_not_force_an_unstated_order_form(comment):
    from aqla_agent.decisions import validate_interpretation
    packet = packet_for("BTC LONG. Entry 100. SL 90. TP 110. " + comment)
    source = packet["anchor_source_id"]
    interpretation = Interpretation(kind="new_signal", symbol="BTC", side="long", source_refs=[source],
        entry="unspecified", entry_prices=[100], stop_loss=90, take_profits=[110], evidence=[
            Evidence(field="symbol", source_id=source, quote="BTC"),
            Evidence(field="side", source_id=source, quote=packet["messages"][0]["text"]),
            Evidence(field="entry_prices.0", source_id=source, quote="100"),
            Evidence(field="stop_loss", source_id=source, quote="90"),
            Evidence(field="take_profits.0", source_id=source, quote="110")])
    validate_interpretation(packet, interpretation)


@pytest.mark.parametrize("amount", ["50%", "half"])
def test_close_action_cannot_discard_a_selected_partial_reduction(amount):
    packet = packet_for("BTC: close " + amount + " of the position now.")
    spans = candidates(packet)
    fraction = require_first(span for span in spans if span.kind == "number")
    questions = questions_for(packet, spans)
    response = response_for(questions, kind="trade_update", overrides={
        "update_action": "close", "side": "not_present", "role." + fraction.candidate_id: "reduce_percent"})
    with pytest.raises(DecisionFailure, match="update_role_conflict"):
        replay_decision(packet, CalibrationProfile(), [response],
                        model=DEFAULT_DECISION_MODEL, revision=DEFAULT_DECISION_MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,code,invalid", [("wrong_model", "decision_model_changed", 1),
    ("answer_shape", "answer_shape", 1), ("low_confidence", "uncertain_decision", 0)])
async def test_received_contract_invalid_response_and_valid_confidence_deferral_have_distinct_metrics(outcome, code, invalid):
    from aqla_agent.usage_metrics import source_efficiency
    packet = packet_for("BTC result report.")
    usage, requests = [], []

    def upstream(request):
        requests.append(request)
        value = response_for(json.loads(request.content)["questions"], kind="report")
        if outcome == "wrong_model":
            value["model"] = "unexpected/model"
        elif outcome == "answer_shape":
            value["answers"] = {}
        else:
            value["answers"]["kind"]["probabilities"].update(report=0.98, unrelated=0.02)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        adapter = OpenRouterDecisionAdapter("synthetic", client=client, model_revision=DEFAULT_DECISION_MODEL)
        with pytest.raises(DecisionFailure, match=code):
            await adapter.interpret(packet, CalibrationProfile(thresholds={"kind": 0.99}), usage=usage)
    metrics = source_efficiency([{"origin": "source_python", "workflow": {"source_revision_hash": outcome}, "api_responses": usage}])
    assert metrics["api_response_failures"] == 0 and metrics["invalid_model_responses"] == invalid
    assert len(requests) == 1 and usage[0]["response_success"] is True
    if outcome == "low_confidence":
        assert usage[0]["contract_valid"] and usage[0]["review_deferred"]
        assert usage[0].get("interpretation_valid") is not False
    else:
        assert usage[0]["contract_valid"] is False
