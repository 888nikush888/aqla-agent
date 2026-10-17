"""Original-only assistance for the native source agent; no model or write access."""
from __future__ import annotations

import json
import re

from aqla_hyperliquid_mcp.models import EVIDENCE_FIELDS, AqlaError, Interpretation, digest
from aqla_hyperliquid_mcp.rules import number_is_ordinal, numeric_evidence_values, quoted_numbers

from .decisions import (
    DecisionFailure,
    build_packet,
    candidates,
    linked_candidate_refs,
    questions_for,
    validate_interpretation,
    verification_questions,
)

VERSION = "source-assistance-v1"
CONTRACT_FIELDS = ("kind", "symbol", "side", "entry", "entry_timing", "update_action",
                   "source_exchange", "single_entry_form", "single_management_action")
CONTEXT_CANDIDATE_LIMIT = 64
REPAIR_GUIDANCE = {
    "source_percentage_targets_missing_or_changed": "The original explicitly lists percent-only TP targets. Under source-roe-v1 these are nominal ROE targets, not position allocations. Preserve every listed target in order with take_profit_unit=roe_percent and exact percent quotes. A separate deposit/margin percent is not a TP. Do not silently substitute backup targets.",
    "live_anchor_missing": "Use anchor_source_id in source_refs, not event_id/chat_id/message_id. Evidence.source_id uses the same original source IDs. Do not remove a correctly named symbol while correcting references.",
    "update_identity_missing": "Include the managed market in the symbol FIELD when the anchor names it; symbol evidence alone does not populate the field. Otherwise use a real reply/own binding, never guess a market.",
    "source_example_requires_report_review": "The original explicitly scopes the preceding setup as an example. Inspect any independent instruction AFTER that framing; examples alone are reports, never new orders.",
    "classification_omits_management_instruction": "An unconditional stop instruction remains current management even when a parent setup prescribed a future TP/BE rule. Channel results are not proof of our fills or an already applied stop. Recheck trade_update and its market; Python owns eligibility.",
    "source_leverage_interval_requires_review": "The source specifies a leverage interval, not one source value. Do not pick an endpoint or pretend the field is absent. Recheck ambiguous; a fixed execution profile does not change the original communication.",
    "source_price_boundary_requires_limit": "One SHORT price+ / >=price or LONG price- / <=price is a single limit price. Preserve that one original value; do not invent a second endpoint or default to market.",
    "source_market_form_not_explicit_use_unspecified": "Current-price wording is not an explicitly named market order. Use unspecified for the source form; Python separately decides any permitted execution default.",
    "source_limit_form_not_explicit_use_unspecified": "One reference price does not invent a limit order. Use unspecified unless the original names a limit, a directional price boundary or a supported strict deferred price.",
    "signal_contains_management_instruction": "Re-read the entire original: current management belongs to trade_update, several current actions require review. Do not stuff update_action into new_signal or execute only part. Explicitly framed examples/past results do not authorize an entry.",
    "unbound_setup_edit_is_not_management": "Python reports no accepted own setup binding. An edit/publication alone is not execution. This anchor still presents a complete entry setup, not an explicit management instruction: recheck corrected new_signal, or ambiguous when all original entry legs cannot be represented.",
}
REPAIR_FIELDS = {
    "source_percentage_targets_missing_or_changed": ["take_profits", "take_profit_unit"],
    "live_anchor_missing": ["source_refs", "evidence"],
    "update_identity_missing": ["symbol", "source_refs"],
    "source_example_requires_report_review": ["kind"],
    "classification_omits_management_instruction": ["kind", "update_action"],
    "source_leverage_interval_requires_review": ["kind", "leverage"],
    "source_price_boundary_requires_limit": ["entry", "entry_prices.0"],
    "source_market_form_not_explicit_use_unspecified": ["entry"],
    "source_limit_form_not_explicit_use_unspecified": ["entry"],
    "signal_contains_management_instruction": ["kind", "update_action"],
    "unbound_setup_edit_is_not_management": ["kind", "update_action", "source_refs"],
}


def context_packet(context):
    rows = context["messages"]
    rows = rows.values() if isinstance(rows, dict) else rows
    messages = {row["event_id"]: row for row in [*rows, context["event"]]}
    return build_packet({**context, "messages": list(messages.values())})


def source_assistance(context):
    packet = context_packet(context)
    version = digest(packet)
    candidate_error = None
    try:
        spans = candidates(packet)
    except DecisionFailure as error:
        spans, candidate_error = [], str(error)
    questions = questions_for(packet, spans)
    contract = {key: questions[key] for key in CONTRACT_FIELDS}
    for key in ("symbol", "source_exchange"):
        contract[key] = {"instructions": contract[key]["instructions"]}
    entry = json.loads(contract["entry"]["instructions"])
    contract["entry"] = {**contract["entry"], "instructions": entry["question"]}
    contract["verification"] = verification_questions()
    from .semantic_contract import supported_action_failure, unbound_setup_update_failure
    anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    vetoes = {}
    for kind in ("new_signal", "trade_update", "report", "unrelated"):
        draft = Interpretation(kind=kind, source_refs=[packet["anchor_source_id"]])
        failure = supported_action_failure(anchor["text"], draft) or unbound_setup_update_failure(packet, draft)
        if failure:
            vetoes[kind] = failure
    return {"version": VERSION, "source_version": version, "contract": contract,
            "original_structure_vetoes": vetoes, "vetoes_are_not_classification_or_permission": True,
            "anchor_setup_bound": packet["anchor_setup_bound"],
            "linked_source_ids": sorted(linked_candidate_refs(packet)),
            "order_form_mentions": entry["original_order_form_mentions"],
            "candidates": [span.model_dump(mode="json") for span in spans
                           if span.kind != "text" and span.notation != "ordinal"][:CONTEXT_CANDIDATE_LIMIT],
            "candidates_complete": candidate_error is None and sum(span.kind != "text" and span.notation != "ordinal" for span in spans) <= CONTEXT_CANDIDATE_LIMIT,
            **({"candidate_error": candidate_error, "originals_required": True} if candidate_error else {}),
            "candidate_limit": CONTEXT_CANDIDATE_LIMIT,
            "candidate_roles_are_not_inferred": True,
            "workflow": "Use source_check_interpretation before saving or planning. Copy original candidate quotes. "
                        "Only Python's anchor_setup_bound proves an accepted own setup; publication/edited_at never does. "
                        "Check the complete anchor, including new management instructions in reports. "
                        "Non-actionable results need only kind, source_refs, classification evidence, reason and reason_codes; "
                        "omit all unused trading fields. Correct at most three drafts per processing turn. "
                        "valid only means the automated checks passed: before saving, independently recheck the complete "
                        "original against verification.intent, verification.values, verification.setup and the kind criteria; "
                        "correct a semantic mistake even when the tool found no structural error. "
                        "A valid semantic check is not execution permission; historical sources remain non-executable."}


def precise_numeric_evidence(packet, interpretation):
    """Only shorten an already original quote around one unambiguous matching value."""
    result = interpretation.model_copy(deep=True)
    originals = {row["source_id"]: row["text"] for row in packet["messages"]}
    numeric = {"stop_loss": result.stop_loss}
    numeric.update({f"entry_prices.{i}": value for i, value in enumerate(result.entry_prices)})
    numeric.update({f"take_profits.{i}": value for i, value in enumerate(result.take_profits)})
    for proof in result.evidence:
        value = numeric.get(proof.field)
        original = originals.get(proof.source_id, "")
        if (value is None or proof.source_id not in result.source_refs or proof.quote not in original
                or value not in set(quoted_numbers(proof.quote))):
            continue
        occurrences = list(re.finditer(re.escape(proof.quote), original))
        if len(occurrences) != 1:
            continue
        numbers = list(re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w.])(?:\s*%)?", proof.quote))
        matching = [number for number in numbers if set(quoted_numbers(number.group())) == {value}]
        if len(matching) == 1:
            number = matching[0]
            offset = occurrences[0].start()
            if number_is_ordinal(original, offset + number.start(), offset + number.end()):
                continue
            if proof.quote[:number.start()].rstrip().endswith(("-", "−", "/")):
                continue
            if original.count(number.group()) != 1 or numeric_evidence_values(original, number.group()) != {value}:
                continue  # Keep unique original context when a bare number would be ambiguous.
            # The source unit is checked again by verify_evidence after shortening.
            proof.quote = number.group()
    return result


def check_interpretation(context, interpretation: Interpretation):
    packet = context_packet(context)
    result = precise_numeric_evidence(packet, interpretation)
    errors = []
    from .semantic_contract import supported_action_failure, unbound_setup_update_failure
    anchor = next(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    for failure in (supported_action_failure(anchor["text"], result), unbound_setup_update_failure(packet, result)):
        if failure:
            errors.append({"code": failure, "field_paths": REPAIR_FIELDS.get(failure, ["kind"]), "message": failure,
                           **({"repair": REPAIR_GUIDANCE[failure]} if failure in REPAIR_GUIDANCE else {})})
    try:
        if context.get("source_read_error"):
            raise DecisionFailure("source_context_changed_reread_required")
        validate_interpretation(packet, result, own_setup_refs=context.get("bound_source_refs", ()))
        if result.kind == "new_signal" and result.entry in ("market", "limit"):
            from .semantic_contract import LIMIT, MARKET, source_price_boundary_failure, strict_deferred_price
            text = next(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
            if result.entry == "market" and not MARKET.search(text):
                raise DecisionFailure("source_market_form_not_explicit_use_unspecified")
            if (result.entry == "limit" and not LIMIT.search(text)
                    and not source_price_boundary_failure(text, result.model_copy(update={"entry": "unspecified"}))
                    and not strict_deferred_price(text, result.entry_timing, result.entry_prices)):
                raise DecisionFailure("source_limit_form_not_explicit_use_unspecified")
    except AqlaError as error:
        message = str(error)
        numeric_context = "Numeric source evidence must isolate one number" in message
        field_message = message.split("; use one exact original quote", 1)[0]
        detail = {"code": message if isinstance(error, DecisionFailure) else "source_evidence_invalid",
                       "field_paths": REPAIR_FIELDS.get(message, sorted(field for field in set(EVIDENCE_FIELDS) | set(Interpretation.model_fields)
                                       if re.search(r"(?<![\w.])" + re.escape(field) + r"(?![\w.])", field_message))),
                       "message": message, **({"repair": REPAIR_GUIDANCE[message]} if message in REPAIR_GUIDANCE else {})}
        if numeric_context:
            detail.update(code="source_numeric_proof_needs_context", repair="Use a longer UNIQUE exact original span, including the target label and its value if needed. Verified numbering labels are context, not prices. Do not resubmit a bare repeated number or crop digits from a decimal. Preserve the actual source value and unit.")
        if message.startswith("Non-actional source_refs"):
            detail.update(code="classification_scope_requires_anchor_only", repair="Use ONLY anchor_source_id in source_refs and classification evidence. Remove parent/context refs and their proofs from this non-actional result; they remain readable through the reply chain. Do not add parent proofs merely to retain an extra classification target.")
        if not any(row["message"] == message for row in errors):
            errors.append(detail)
    return {"event_id": packet["anchor_event_id"], "anchor_source_id": packet["anchor_source_id"],
            "available_source_ids": sorted({row["source_id"] for row in packet["messages"]}),
            "source_version": digest(packet), "valid": not errors,
            "interpretation": result.model_dump(mode="json"), "errors": errors,
            "reread_required": bool(context.get("source_read_error")), "execution_authorized": False}
