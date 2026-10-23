"""Bounded semantic decisions; all values and quotations remain Python-owned."""
from __future__ import annotations

import json
import math
import re
import time
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, Model, digest, require_first

from .interpretation_contract import ENTRY_FORM_MARKERS as ENTRY_FORM_MARKERS
from .interpretation_contract import DecisionFailure, validate_interpretation
from .source_candidates import (
    EXCHANGES as EXCHANGES,
)
from .source_candidates import (
    NONE as NONE,
)
from .source_candidates import (
    NUMBERS as NUMBERS,
)
from .source_candidates import (
    PACKET_VERSION as PACKET_VERSION,
)
from .source_candidates import (
    SYMBOLS as SYMBOLS,
)
from .source_candidates import (
    UNKNOWN as UNKNOWN,
)
from .source_candidates import (
    CandidateSpan as CandidateSpan,
)
from .source_candidates import (
    build_packet as build_packet,
)
from .source_candidates import (
    candidates as candidates,
)
from .source_candidates import (
    choice as choice,
)
from .source_candidates import (
    linked_candidate_refs as linked_candidate_refs,
)
from .source_candidates import (
    numeric_occurrences as numeric_occurrences,
)
from .source_candidates import (
    questions_for as questions_for,
)
from .source_candidates import (
    verification_questions as verification_questions,
)

VERSION = "aqla-decisions-v45"
QUESTION_CONTRACT_VERSION = "aqla-decision-questions-v29"
# The original-context wire shape is unchanged. Keeping its independent schema
# version permits exact-state, exact-question observations to be reused for
# development while decision/validation policy has its own version and cache key.
DEFAULT_DECISION_MODEL = "inception/mercury-decide:free"
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/v1/systemone"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
KINDS = ("new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous")
ACTIONS = ("move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry")
ROLES = ("ignore", "entry0", "entry1", "stop_loss", "leverage", "reduce_percent",
         *(f"target_{i + 1}" for i in range(20)), UNKNOWN)


def calibration_family(question, prediction=None):
    base = ("number_role" if question.startswith("role.") else
            "number_normalization" if question.startswith("normalization.") else question)
    if prediction is not None and base in ("number_role", "kind", "live_action"):
        return base + "." + str(prediction).lower()
    return base





def redundant_numeric_mention(packet, prior, span, role):
    """Only explicit, repeated labels for one live field may share its value."""
    if (span.source_id != packet["anchor_source_id"] or prior.source_id != span.source_id
            or prior.end > span.start or prior.value != span.value
            or ("%" in prior.quote) != ("%" in span.quote) or prior.notation != span.notation):
        return False
    original = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    if re.search(r"(?:#|\b)[A-Z][A-Z0-9]{1,15}\s+(?:LONG|SHORT)\b", original[prior.end:span.start]):
        return False
    if role.startswith("target_"):
        ordinal = int(role.split("_")[1])
        words = {1: "first|erste(?:r|s)?|перв(?:ая|ый)", 2: "second|zweite(?:r|s)?|втор(?:ая|ой)",
                 3: "third|dritte(?:r|s)?|трет(?:ья|ий)"}.get(ordinal, r"(?!)")
        label = re.compile(r"(?:\b(?:tp|target|take[-\s]?profit|тейк|цел[ьи]|ziel)\s*" + str(ordinal)
            + r"\b|\b(?:" + words + r")\s+(?:target|take[-\s]?profit|тейк|цел[ьи]|ziel))\s*[:=@-]?\s*$", re.I)
    elif role == "stop_loss":
        label = re.compile(r"\b(?:sl|stop(?:[-\s]+loss)?|стоп)\s*[:=@-]?\s*$", re.I)
    elif role == "leverage":
        label = re.compile(r"\b(?:leverage|hebel|плечо)\s*[:=@-]?\s*$", re.I)
    else:
        return False
    return all(label.search(original[max(0, occurrence.start - 50):occurrence.start]) for occurrence in (prior, span))




ANSWER_FAILURES = {"answer_shape", "answer_type", "answer_options", "answer_probabilities", "answer_choice", "answer_probability"}
DECISION_DEFERRALS = {"uncertain_decision", "uncertain_trading_intent_requires_review", "conflicting_live_intent",
    "conflicting_management_intent", "verification_failed", "source_not_calibrated", "source_decision_kind_not_calibrated",
    "decision_calibration_model_changed", "entry_form_not_representable", "source_context_limit", "candidate_limit",
    "future_entry_opportunity_requires_review", "source_financial_instruction_incomplete"}


def mark_decision_failure(usage, code):
    """Keep HTTP success, response contract, interpretation and admission distinct."""
    row = next((row for row in reversed(usage) if row.get("phase") in ("decision", "decision_verification")), None)
    if row is None or row.get("response_success") is not True:
        return
    if code in ANSWER_FAILURES:
        row["contract_valid"] = False
    elif code in DECISION_DEFERRALS:
        row["review_deferred"] = True
    elif row.get("contract_valid") is not False:
        row["interpretation_valid"] = False






class DecisionResult(Model):
    interpretation: Interpretation
    model: str
    model_revision: str | None = None
    question_version: str = VERSION
    verified: bool = False
    probabilities: dict[str, float] = Field(default_factory=dict)
    usage: list[dict] = Field(default_factory=list)
    fallback: bool = False
    collection_only: bool = False
    required_questions: list[str] = Field(default_factory=list)


class CalibrationProfile(Model):
    version: str = VERSION
    model: str = DEFAULT_DECISION_MODEL
    model_revision: str = ""
    master_hash: str = ""
    thresholds: dict[str, float] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    dataset_hash: str = ""
    holdout_hash: str = ""
    holdout_cases: int = Field(default=0, ge=0)
    critical_errors: int = Field(default=0, ge=0)
    validated: bool = False
    disabled_questions: list[str] = Field(default_factory=list)
    source_evidence: dict[str, dict] = Field(default_factory=dict)
    source_permissions: dict[str, list[str]] = Field(default_factory=dict)
    activation_diagnostics: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def checked(self):
        if any(not math.isfinite(value) or not 0.5 <= value <= 1 for value in self.thresholds.values()):
            raise ValueError("Decision thresholds must be finite probabilities between 0.5 and 1")
        if self.validated and (self.version != VERSION or not self.model or not self.model_revision or not self.sources
                or not self.master_hash or not self.dataset_hash or not self.holdout_hash or self.dataset_hash == self.holdout_hash
                or self.holdout_cases < 30 or self.critical_errors != 0 or not self.thresholds):
            raise ValueError("Activation requires separate calibration/holdout evidence and zero critical errors")
        if self.validated:
            for source in self.sources:
                proof = self.source_evidence.get(source, {})
                permitted = self.source_permissions.get(source, [])
                if (proof.get("calibration_groups", 0) < 3 or proof.get("holdout_groups", 0) < 5
                        or proof.get("accepted_accuracy", 0) < 0.95 or proof.get("critical_errors", 1) != 0
                        or not permitted or not set(permitted) <= set(KINDS)):
                    raise ValueError("Every activated source needs independent held-out evidence and tested kinds")
                for kind in permitted:
                    kind_proof = proof.get("kinds", {}).get(kind, {})
                    if (kind_proof.get("calibration_correct_groups", 0) < 1
                            or kind_proof.get("holdout_correct_accepted_groups", 0) < 2):
                        raise ValueError("Every activated source kind needs distinct correctly admitted held-out groups")
        return self

    def permits(self, source, master_hash=None, *, model=DEFAULT_DECISION_MODEL, model_revision=None):
        return (self.validated and source in self.sources and self.master_hash == master_hash
                and self.model == model and self.model_revision == model_revision)

    def threshold(self, question, prediction=None):
        family = calibration_family(question, prediction)
        if family not in self.thresholds:
            family = calibration_family(question)
        return self.thresholds.get(question, self.thresholds.get(family, 0.99))

    def question_disabled(self, question, prediction=None):
        return (question in self.disabled_questions or calibration_family(question) in self.disabled_questions
                or calibration_family(question, prediction) in self.disabled_questions)

    def permits_kind(self, source, kind):
        return self.validated and kind in self.source_permissions.get(source, [])


class SourceProcessingConfig(Model):
    mode: Literal["legacy", "shadow", "hybrid"] = "legacy"
    model: str = Field(default=DEFAULT_DECISION_MODEL, min_length=3, max_length=200, pattern=r"^[\w.-]+/[\w.:-]+$")
    model_revision: str | None = None
    key_env: str = "OPENROUTER_API_KEY"
    calibration_file: str | None = None
    timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_context_characters: int = Field(default=60000, ge=2000, le=100000)
    retry_seconds: int = Field(default=300, ge=30, le=3600)
    retry_max_attempts: int = Field(default=3, ge=1, le=5, strict=True)
    retry_backoff_seconds: int = Field(default=15, ge=1, le=300, strict=True)
    retry_max_age_seconds: int = Field(default=900, ge=30, le=3600, strict=True)
    fallback_max_tokens: int = Field(default=8192, ge=2048, le=16384)
    fallback_reasoning_effort: Literal["low", "medium", "high"] = "medium"












def checked_answers(response, questions, profile, *, collect=False):
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise DecisionFailure("answer_shape")
    values, probabilities = {}, {}
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise DecisionFailure("answer_type")
        if question["type"] == "noul":
            probability = answer.get("noul")
            value = probability
        else:
            distribution = answer.get("probabilities")
            value = answer.get("choice")
            if not isinstance(distribution, dict) or set(distribution) != set(question["criteria"]) or value not in distribution:
                raise DecisionFailure("answer_options")
            if any(isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1
                   for p in distribution.values()) or abs(sum(distribution.values()) - 1) > 0.001:
                raise DecisionFailure("answer_probabilities")
            probability = distribution[value]
            if probability != max(distribution.values()):
                raise DecisionFailure("answer_choice")
        if isinstance(probability, bool) or not isinstance(probability, (float, int)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise DecisionFailure("answer_probability")
        probabilities[key] = probability
        # Irrelevant fields are gated later; a missing optional field is still a real choice.
        values[key] = value
    return values, probabilities


def assemble(packet, spans, values, probabilities, profile, *, collect=False, required_questions=None):
    by_id = {span.candidate_id: span for span in spans}
    selected = {}
    evidence = []
    refs = {packet["anchor_source_id"]}

    def read(question):
        if required_questions is not None and question not in required_questions:
            required_questions.append(question)
        if question not in values or values[question] == UNKNOWN or (not collect and
                (profile.question_disabled(question, values[question]) or probabilities[question] < profile.threshold(question, values[question]))):
            raise DecisionFailure("uncertain_decision")
        return values[question]

    def proof(field):
        original = [span for span in spans if span.kind == "text" and span.source_id == packet["anchor_source_id"]]
        if not original:
            raise DecisionFailure("evidence_missing")
        evidence.extend(Evidence(field=field, source_id=span.source_id, quote=span.quote) for span in original)

    kind = read("kind")
    action_probability = values["live_action"]
    if required_questions is not None:
        required_questions.append("live_action")
    if kind in ("incomplete", "ambiguous") and action_probability >= 0.5:
        raise DecisionFailure("uncertain_trading_intent_requires_review")
    oriented = action_probability if kind in ("new_signal", "trade_update") else 1 - action_probability
    positive_intent = kind in ("new_signal", "trade_update")
    if not collect and (profile.question_disabled("live_action", positive_intent)
                        or oriented < profile.threshold("live_action", positive_intent)):
        raise DecisionFailure("conflicting_live_intent")
    if kind in ("report", "unrelated", "incomplete", "ambiguous"):
        if kind in ("report", "unrelated") and values.get("update_action") not in (NONE, UNKNOWN):
            raise DecisionFailure("conflicting_management_intent")
        if not require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"]).strip():
            return Interpretation(kind="incomplete", source_refs=sorted(refs), reason="Originaltext fehlt.", reason_codes=["missing_source_text"])
        evidence.extend(Evidence(field="classification", source_id=span.source_id, quote=span.quote)
                        for span in spans if span.kind == "text" and span.source_id == packet["anchor_source_id"])
        code = {"report": "result_report", "unrelated": "non_trade", "incomplete": "other", "ambiguous": "other"}[kind]
        return Interpretation(kind=kind, source_refs=sorted(refs), evidence=evidence,
                              reason={"report": "Bericht ohne neue Handlungsanweisung", "unrelated": "Kein Handelsauftrag", "incomplete": "Unvollständige Quelle", "ambiguous": "Mehrdeutige Quelle"}[kind],
                              reason_codes=[code])
    fields = {}
    # Updates can only newly specify a venue with an original anchor occurrence.
    # Absence of such a candidate is an authoritative lexical fact, not a vote
    # between historical footer venues and not_present.
    source_exchange = (NONE if kind == "trade_update" and not any(
        s.kind == "exchange" and s.source_id == packet["anchor_source_id"] for s in spans) else read("source_exchange"))
    if source_exchange != NONE:
        span = by_id[source_exchange]
        fields["source_exchange"] = span.value
        evidence.append(Evidence(field="source_exchange", source_id=span.source_id, quote=span.quote))
        refs.add(span.source_id)
    symbol = read("symbol")
    if symbol != NONE:
        span = by_id[symbol]
        fields["symbol"] = span.value
        evidence.append(Evidence(field="symbol", source_id=span.source_id, quote=span.quote))
        refs.add(span.source_id)
    side = read("side")
    if side != NONE:
        fields["side"] = side
        proof("side")
    if kind == "new_signal":
        if required_questions is not None:
            required_questions.append("single_entry_form")
        if not collect and (profile.question_disabled("single_entry_form") or values["single_entry_form"] < profile.threshold("single_entry_form")):
            raise DecisionFailure("entry_form_not_representable")
        if not fields.get("symbol") or not fields.get("side"):
            raise DecisionFailure("signal_identity_missing")
        fields["entry"] = read("entry")
        fields["entry_timing"] = read("entry_timing")
        if fields["entry_timing"] == "deferred":
            proof("entry_timing")
    else:
        if required_questions is not None:
            required_questions.append("single_management_action")
        if not collect and (profile.question_disabled("single_management_action")
                or values["single_management_action"] < profile.threshold("single_management_action")):
            raise DecisionFailure("management_actions_not_representable")
        action = read("update_action")
        if action == NONE:
            raise DecisionFailure("update_action_missing")
        fields["update_action"] = action
        proof("update_action")
    used_occurrences = set()
    for identity, alternatives in numeric_occurrences(spans):
        span = alternatives[0]
        if kind == "trade_update" and span.source_id != packet["anchor_source_id"]:
            # Older setup values cannot become newly changed update values.
            # This temporal/source constraint is Python-owned, not a confidence vote.
            continue
        question = "role." + identity
        if question not in values:
            continue
        role = read(question)
        if role == "ignore":
            continue
        if len(alternatives) > 1:
            normalized = read("normalization." + identity)
            if normalized == NONE:
                raise DecisionFailure("numeric_normalization_missing")
            selected_span = next((candidate for candidate in alternatives if candidate.candidate_id == normalized), None)
            if selected_span is None:
                raise DecisionFailure("numeric_normalization_invalid")
            span = selected_span
        if role in selected and redundant_numeric_mention(packet, selected[role], span, role):
            continue
        occurrence = (span.source_id, span.start, span.end)
        if role in selected or occurrence in used_occurrences or any(
                prior.source_id == span.source_id and prior.start < span.end and span.start < prior.end
                for prior in selected.values()):
            raise DecisionFailure("conflicting_numeric_roles")
        selected[role] = span
        used_occurrences.add(occurrence)
    allowed = set(ROLES) if kind == "new_signal" else {
        "move_stop": {"stop_loss"}, "replace_targets": {f"target_{i + 1}" for i in range(20)},
        "reduce": {"reduce_percent"}, "break_even": set(), "close": set(), "cancel_entry": set(),
    }[fields["update_action"]]
    if kind == "trade_update" and set(selected) - allowed:
        raise DecisionFailure("update_role_conflict")
    for group, keys in (("entry_prices", ["entry0", "entry1"]), ("take_profits", [f"target_{i + 1}" for i in range(20)])):
        chosen = [key for key in keys if key in selected]
        if chosen and chosen != keys[:len(chosen)]:
            raise DecisionFailure("numeric_sequence_gap")
        if chosen:
            fields[group] = [Decimal(selected[key].value) for key in chosen]
        for i, key in enumerate(chosen):
            span = selected[key]
            evidence.append(Evidence(field=f"{group}.{i}", source_id=span.source_id, quote=span.quote))
            refs.add(span.source_id)
    tp_spans = [span for key, span in selected.items() if key.startswith("target_")]
    if tp_spans:
        markers = {"%" in span.quote for span in tp_spans}
        if len(markers) != 1:
            raise DecisionFailure("mixed_target_units")
        fields["take_profit_unit"] = "roe_percent" if True in markers else "price"
    for key in ("stop_loss", "leverage", "reduce_percent"):
        if key in selected:
            span = selected[key]
            fields[key] = Decimal(span.value)
            evidence.append(Evidence(field=key, source_id=span.source_id, quote=span.quote))
            refs.add(span.source_id)
    if kind == "trade_update":
        required = {"move_stop": "stop_loss", "replace_targets": "take_profits", "reduce": "reduce_percent"}.get(fields["update_action"])
        if required and not fields.get(required):
            raise DecisionFailure("update_value_missing")
    if kind == "new_signal" and fields.get("entry") == "unspecified":
        from .semantic_contract import (
            bounded_live_entry_range,
            one_sided_live_entry_limit,
            strict_deferred_price,
        )
        anchor = require_first(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
        if bounded_live_entry_range(packet, [selected.get("entry0"), selected.get("entry1")]):
            fields["entry"] = "range"
        elif one_sided_live_entry_limit(packet, selected.get("entry0"), fields.get("side")):
            fields["entry"] = "limit"
        elif (selected.get("entry0") and selected["entry0"].source_id == packet["anchor_source_id"]
                and strict_deferred_price(anchor["text"], fields.get("entry_timing"), fields.get("entry_prices", []))):
            # An expressly strict deferred price gate is one price-bound entry,
            # unlike an ordinary reference price or a time-delayed market order.
            fields["entry"] = "limit"
    interpretation = Interpretation(kind=kind, source_refs=sorted(refs), evidence=evidence, **fields)
    validate_interpretation(packet, interpretation)
    return interpretation






def first_decision(packet, profile, response, *, model, revision, collect=False, observation=None, spans=None):
    if not isinstance(response, dict) or response.get("model") != revision:
        raise DecisionFailure("decision_model_changed")
    if profile.model_revision and (profile.model != model or profile.model_revision != revision):
        raise DecisionFailure("decision_calibration_model_changed")
    spans = candidates(packet) if spans is None else spans
    questions = questions_for(packet, spans)
    values, probabilities = checked_answers(response, questions, profile)
    required = []
    if observation is not None:
        observation.update(values=values, probabilities=probabilities, required_questions=required,
                           questions_hash=digest(questions), question_contract_version=QUESTION_CONTRACT_VERSION)
    try:
        interpretation = assemble(packet, spans, values, probabilities, profile, collect=collect, required_questions=required)
        validate_interpretation(packet, interpretation)
    except DecisionFailure:
        raise
    except AqlaError as error:
        raise DecisionFailure("decision_evidence_invalid") from error
    except ValueError as error:
        raise DecisionFailure("decision_schema_invalid") from error
    return interpretation, probabilities, required


def replay_decision(packet, profile, responses, *, model, revision, collect=False, usage=None, observation=None, spans=None):
    """One production gate shared by HTTP execution and order-free calibration replay."""
    if not responses:
        raise DecisionFailure("decision_response_missing")
    interpretation, probabilities, required = first_decision(packet, profile, responses[0], model=model,
        revision=revision, collect=collect, observation=observation, spans=spans)
    actionable = interpretation.kind in ("new_signal", "trade_update")
    if actionable:
        if len(responses) != 2 or responses[1].get("model") != revision:
            raise DecisionFailure("verification_missing_or_model_changed")
        _, checks = checked_answers(responses[1], verification_questions(), profile)
        probabilities.update(checks)
        required += list(checks)
        if not collect and any(profile.question_disabled(key) or p < profile.threshold(key) for key, p in checks.items()):
            raise DecisionFailure("verification_failed")
    return DecisionResult(interpretation=interpretation, model=model, model_revision=revision,
        verified=actionable and not collect, collection_only=collect, probabilities=probabilities,
        required_questions=required, usage=(usage or []).copy())


class OpenRouterDecisionAdapter:
    def __init__(self, api_key, *, client=None, timeout=5, model=DEFAULT_DECISION_MODEL, model_revision=None):
        self.api_key, self.timeout, self.model = api_key, timeout, model
        self.client = client
        self.model_revision = model_revision
        self.pinned_revision = model_revision is not None
        self.catalog_checked_at = 0.0

    async def identity(self):
        if self.pinned_revision or self.model_revision and time.monotonic() - self.catalog_checked_at < 300:
            return self.model_revision
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False)
        try:
            response = await client.get(OPENROUTER_MODELS_URL, params={"output_modalities": "decisions"}, timeout=self.timeout)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise DecisionFailure("decision_catalog_limit")
            catalog = response.json()
            models = catalog.get("data", []) if isinstance(catalog, dict) else []
            selected = next((row for row in models if isinstance(row, dict) and row.get("id") == self.model), None)
            if selected is None or not isinstance(selected.get("canonical_slug"), str) or not selected["canonical_slug"]:
                raise DecisionFailure("decision_model_not_available")
            self.model_revision = selected["canonical_slug"]
            self.catalog_checked_at = time.monotonic()
            return self.model_revision
        except DecisionFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise DecisionFailure("decision_catalog_unavailable") from error
        finally:
            if owned:
                await client.aclose()

    def pin_revision(self, revision):
        self.model_revision, self.pinned_revision = revision, True

    async def evaluate(self, state, questions, *, usage=None, phase="decision", expected_revision=None,
                       max_context_characters=None):
        body = {"model": self.model, "state": state, "questions": questions}
        if max_context_characters is not None and len(json.dumps(body, ensure_ascii=False)) > max_context_characters:
            raise DecisionFailure("source_context_limit")
        if not self.api_key:
            raise DecisionFailure("decision_key_missing")
        revision = expected_revision or await self.identity()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False)
        started = time.monotonic()
        recorded = False
        received_ok = False
        try:
            response = await client.post(OPENROUTER_DECISIONS_URL,
                headers={"Authorization": "Bearer " + self.api_key},
                json=body, timeout=self.timeout)
            response.raise_for_status()
            received_ok = True
            if len(response.content) > 2_000_000:
                raise DecisionFailure("decision_response_limit")
            value = response.json()
            if not isinstance(value, dict) or value.get("model") != revision:
                raise DecisionFailure("decision_model_changed")
            raw = value.get("usage")
            measured = {"phase": phase, "model": self.model, "model_revision": revision, "provider": "openrouter", "failed": False,
                     "response_success": True, "contract_valid": True, "request_hash": digest(body), "question_contract_version": QUESTION_CONTRACT_VERSION,
                     "duration_seconds": time.monotonic() - started, "usage_available": isinstance(raw, dict)}
            if isinstance(raw, dict):
                for key in ("input_tokens", "output_tokens"):
                    n = raw.get(key)
                    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                        raise DecisionFailure("decision_usage_invalid")
                    measured[key] = n
            cost = raw.get("cost") if isinstance(raw, dict) else None
            if cost is not None:
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
                    raise DecisionFailure("decision_cost_invalid")
                measured.update(cost_usd=cost, estimated_cost_usd=cost, cost_status="actual", cost_source="openrouter_usage")
            else:
                measured.update(cost_usd=None, cost_status="unknown")
            if usage is not None:
                usage.append(measured)
            recorded = True
            return value, measured
        except DecisionFailure:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise DecisionFailure("decision_api_unavailable") from error
        finally:
            if usage is not None and not recorded:
                usage.append({"phase": phase, "model": self.model, "provider": "openrouter", "failed": True,
                              "response_success": received_ok, "request_hash": digest(body),
                              "contract_valid": False if received_ok else None,
                              "usage_available": False, "duration_seconds": time.monotonic() - started})
            if owned:
                await client.aclose()

    async def interpret(self, packet, profile, *, collect=False, usage=None, observation=None, spans=None,
                        max_context_characters=None):
        usage = usage if usage is not None else []
        spans = candidates(packet) if spans is None else spans
        questions = questions_for(packet, spans)
        if max_context_characters is not None and len(json.dumps(
                {"model": self.model, "state": packet, "questions": questions}, ensure_ascii=False)) > max_context_characters:
            raise DecisionFailure("source_context_limit")
        revision = await self.identity()
        response, _ = await self.evaluate(packet, questions, usage=usage, expected_revision=revision,
                                         max_context_characters=max_context_characters)
        responses = [response]
        if observation is not None:
            observation["responses"] = responses
        try:
            interpretation, _, _ = first_decision(packet, profile, response, model=self.model,
                revision=revision, collect=collect, observation=observation, spans=spans)
            if interpretation.kind in ("new_signal", "trade_update"):
                response, _ = await self.evaluate({"original": packet, "interpretation": interpretation.model_dump(mode="json")},
                    verification_questions(), usage=usage, phase="decision_verification", expected_revision=revision,
                    max_context_characters=max_context_characters)
                responses.append(response)
            result = replay_decision(packet, profile, responses, model=self.model, revision=revision,
                                   collect=collect, usage=usage, observation=observation, spans=spans)
            for row in usage:
                if row.get("phase") in ("decision", "decision_verification") and row.get("contract_valid"):
                    row["interpretation_valid"] = True
            return result
        except DecisionFailure as error:
            mark_decision_failure(usage, str(error))
            raise
