"""Original-only assistance for the native source agent; no model or write access."""
from __future__ import annotations

import json
import re

from aqla_hyperliquid_mcp.models import EVIDENCE_FIELDS, AqlaError, Interpretation, digest, require_first
from aqla_hyperliquid_mcp.rules import number_is_ordinal, numeric_evidence_values, quoted_numbers

from .interpretation_contract import DecisionFailure, validate_interpretation
from .source_candidates import (
    build_packet,
    candidates,
    linked_candidate_refs,
    questions_for,
    verification_questions,
)

VERSION = "source-assistance-v7"
CONTRACT_FIELDS = ("kind", "symbol", "side", "entry", "entry_timing", "update_action",
                   "source_exchange", "single_entry_form", "single_management_action")
CONTEXT_CANDIDATE_LIMIT = 64
REPAIR_GUIDANCE = {
    "management_action_mismatch": "The selected action omits or changes the original current action. A partial exit is reduce, never close of the whole position. Keep its original fraction; when no fraction is stated this schema cannot execute it and requires ambiguous. Preserve every separate instruction; do not invent a fraction or discard an exit as past without original tense evidence.",
    "existing_target_catchup_requires_report_review": "The source discusses an existing target/result and catching up on a forgotten target order, with no separate current management instruction. Recheck report: this does not newly close the whole position or supply a reduction fraction. Use anchor-only classification evidence; do not borrow the parent's old setup fields. Preserve any genuinely separate new stop/exit instruction in other originals.",
    "scheduled_management_not_representable": "The original management is conditional or scheduled, including permission to take profit now IF not already done. This schema cannot represent that condition. Recheck ambiguous using anchor-only classification evidence; never erase the conditional action as optional background or execute only a separate stop instruction. Do not convert it into an unconditional exit.",
    "management_actions_not_representable": "The original contains several distinct management actions. Use sequence with exactly two management_steps for a current partial reduction followed by break_even or move_stop, with separate original clause/value evidence. Other combinations remain ambiguous with anchor-only classification evidence. Never select just one action or discard a permitted/conditional instruction as optional background.",
    "managed_position_direction_missing_or_changed": "The current anchor expressly identifies the position being managed as a purchase/buy holding or sale/short holding. Preserve that POSITION direction with an exact current-anchor side quote. Closing a purchase is long; an exit sale is not a new short. Do not infer direction from prices, profit, market commentary or old parent values.",
    "multiple_managed_position_directions_require_review": "The source manages different position directions. Re-read the full source; do not select one side or execute a partial instruction. A single supported unambiguous action is required.",
    "completed_stop_move_is_not_current_management": "The original reports an already completed stop move: the past-tense governor follows the destination. Recheck report, preserving any genuinely separate current instruction. An unconditional-looking stop phrase does not override its explicit past tense or prove our own stop changed. Never repeat a completed author action merely because our own position state differs.",
    "update_identity_needs_current_quote": "This unbound update explicitly names its market in the current original. Preserve the symbol, quote it from anchor_source_id and remove a parent used only for that duplicate identity. Keep genuine other field contributions; Python's existing own setup bindings remain authoritative. Never change the market to avoid this evidence correction.",
    "source_setup_topic_needs_original_context": "A bare scenario/setup/plan topic tag does not establish market-outlook/report framing or reveal the missing media's instructions. Recheck incomplete when the original instruction is unavailable. Do not infer video contents or borrow neighbour instructions.",
    "source_percentage_target_interval_not_representable": "The original specifies a binding profit/TP interval, not an ordered list of exits. Recheck ambiguous; do not select an endpoint, invent several targets or erase the explicit target to use backup values. This is an interval problem, not missing percentage basis.",
    "entry_legs_not_representable": "Re-read contract.single_entry_form: one setup can now have TWO separate original entries. Use entry=split, entry_legs with both kind/price/weight_percent/timing and indexed original evidence. Never flatten separate orders into range or drop a leg. More than two entries or independent setups remain ambiguous.",
    "split_entry_evidence_needs_role_review": "Each entry_legs.i.kind needs its own distinct original entry clause, including its price when present. A current-price purchase without a named MARKET form stays unspecified, never invent market. Quote each price/position weight separately. Never use the whole message for both form proofs or borrow another leg's prices.",
    "split_entry_weights_missing_or_changed": "Copy the COMPLETE original entry clause into EACH entry_legs.i.kind proof, INCLUDING its price AND allocation (for example: Entry 1 LIMIT 100: 25% position). A form/price quote ending before the allocation cannot associate that weight with this leg. Also keep separate exact entry_legs.i.weight_percent evidence (for example: 25%). Preserve both weights, total 100; only an unstated allocation defaults to 50/50. Deposit, leverage and TP percentages are not entry weights. Never invent a decimal convention for an unclear fraction.",
    "source_percentage_targets_missing_or_changed": "The original explicitly lists percent-only TP targets. Under source-roe-v1 these are nominal ROE targets, not position allocations. Preserve every listed target in order with take_profit_unit=roe_percent and exact percent quotes. A separate deposit/margin percent is not a TP. Do not silently substitute backup targets.",
    "live_anchor_missing": "Use anchor_source_id in source_refs, not event_id/chat_id/message_id. Evidence.source_id uses the same original source IDs. Do not remove a correctly named symbol while correcting references.",
    "update_identity_missing": "An unbound update needs both its symbol FIELD and literal symbol evidence from this anchor or its actual reply chain. Unlinked neighbours and matching prices cannot identify the market. Without original identity or Python's own binding, classify incomplete; never retain a guessed symbol after removing its proof.",
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
    "management_action_mismatch": ["kind", "update_action", "reduce_percent"],
    "existing_target_catchup_requires_report_review": ["kind", "update_action", "source_refs"],
    "scheduled_management_not_representable": ["kind", "update_action"],
    "management_actions_not_representable": ["kind", "update_action"],
    "managed_position_direction_missing_or_changed": ["side", "evidence"],
    "multiple_managed_position_directions_require_review": ["kind", "side", "update_action"],
    "completed_stop_move_is_not_current_management": ["kind", "update_action"],
    "update_identity_needs_current_quote": ["source_refs", "evidence"],
    "source_setup_topic_needs_original_context": ["kind"],
    "source_percentage_target_interval_not_representable": ["kind", "take_profits", "take_profit_unit"],
    "entry_legs_not_representable": ["kind", "entry", "entry_prices", "entry_legs"],
    "split_entry_evidence_needs_role_review": ["entry_legs", "evidence"],
    "split_entry_weights_missing_or_changed": ["entry_legs", "evidence"],
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
    contract["management_steps"] = {"instructions": Interpretation.model_fields["management_steps"].description}
    contract["update_action"]["criteria"]["sequence"] = "Exactly one current partial reduction followed by break_even or move_stop: emit both typed management_steps with separate original action/value evidence; never perform just one action."
    for key in ("symbol", "source_exchange"):
        contract[key] = {"instructions": contract[key]["instructions"]}
    entry = json.loads(contract["entry"]["instructions"])
    contract["entry"] = {**contract["entry"], "instructions": entry["question"]}
    contract["entry"]["criteria"] = {**contract["entry"]["criteria"],
        "split": "Two discrete original entries of ONE setup: entry=split, entry_prices=[], parent entry_timing=standard, entry_legs=[{kind,price,weight_percent,timing},{kind,price,weight_percent,timing}]. ALL leg prices/timings belong INSIDE entry_legs, never in parent entry_prices. Preserve original forms/prices/weights; never flatten into range or select one leg."}
    contract["verification"] = verification_questions()
    from .semantic_contract import (
        supported_action_failure,
        unbound_setup_update_failure,
        unsupported_structure,
    )
    anchor = require_first(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    vetoes = {}
    for kind in ("new_signal", "trade_update", "report", "unrelated"):
        draft = Interpretation(kind=kind, source_refs=[packet["anchor_source_id"]])
        failure = supported_action_failure(anchor["text"], draft) or unbound_setup_update_failure(packet, draft)
        if failure:
            if kind == "trade_update" and failure == "management_actions_not_representable" and unsupported_structure(packet) is None:
                vetoes["single_management_action"] = "Use the supported complete two-step sequence; no partial instruction is authorized"
                continue
            if kind == "new_signal" and failure in ("entry_legs_not_representable", "additional_entry_legs_not_representable"):
                vetoes["flattened_entry"] = failure
                continue
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
                        "Inspect original_structure_vetoes before drafting; a listed kind/shape is rejected, never authorized. "
                        "Only Python's anchor_setup_bound proves an accepted own setup; publication/edited_at never does. "
                        "Missing source SL/TP/leverage stays null/[]; trade_preview applies configured Backup or rejects execution. "
                        "Explicit TP percentages use roe_percent; do not demand absolute prices or a new operator stop rule. "
                        "TWO original entries of ONE setup use entry=split: each kind proof quotes its FULL original entry clause INCLUDING price and any stated allocation; price and weight also get their own indexed proofs. More than two or independent setups remain ambiguous. "
                        "Check the complete anchor, including new management instructions in reports. "
                        "Non-actionable results need only kind, source_refs, classification evidence and reason; "
                        "reason_codes are optional (use only declared values or omit them); "
                        "for an empty original use incomplete with evidence=[] (there is no original quote); never borrow a context quote. "
                        "omit all unused trading fields. At most THREE checker calls total per processing turn, including schema-invalid calls. "
                        "Do not resubmit a valid draft just to reword its reason or remove optional reason_codes. "
                        "If the third call is valid and semantic review finds no material error, finish using that checked draft unchanged. "
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
    for index, leg in enumerate(result.entry_legs):
        numeric[f"entry_legs.{index}.price"] = leg.price
        numeric[f"entry_legs.{index}.weight_percent"] = leg.weight_percent
    numeric.update({f"take_profits.{i}": value for i, value in enumerate(result.take_profits)})
    for proof in result.evidence:
        value = numeric.get(proof.field)
        original = originals.get(proof.source_id, "")
        if value is None or proof.source_id not in result.source_refs or proof.quote not in original:
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
    anchor = require_first(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
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
            text = require_first(row["text"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
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
        if message.startswith("Missing source SL/TP/leverage alone"):
            detail.update(code="missing_execution_fields_are_not_ambiguity", field_paths=["kind", "reason"],
                repair="Re-read the kind criteria and original. Missing source SL/TP/leverage alone is not ambiguity. Preserve absent fields as null/[], explicit target percentages as roe_percent and exact original quotes. Use trade_preview for configured Backup and unchanged risk checks; never invent prices, ignore a genuine independent ambiguity or execute without authorization.")
        if message.startswith("Trade update reduce requires reduce_percent"):
            detail.update(code="source_reduce_fraction_missing", field_paths=["kind", "update_action", "reduce_percent"],
                repair="A partial exit requires an original fraction. Never replace it with a whole-position close or invent a fraction. Recheck ambiguous when the original does not specify one; preserve separate stop instructions without executing only that part.")
        if numeric_context:
            detail.update(code="source_numeric_proof_needs_context", repair="Use a longer UNIQUE exact original span, including the target label and its value if needed. Verified numbering labels are context, not prices. Do not resubmit a bare repeated number or crop digits from a decimal. Preserve the actual source value and unit.")
        if message.startswith("TP evidence selects a percentage-interval endpoint:"):
            detail.update(code="source_percentage_interval_needs_role_review", repair="An interval endpoint is not one discrete TP. Re-read the original financial role: a nonbinding descriptive expected-move range alone is not a TP instruction; a binding profit span (only/max/no more) or explicit TP interval requires ambiguous, even without a TP label. Never erase a binding span to use backup targets. Never pick one bound or split a continuous range into invented exits. Use a unique original quote if a separate real target has the same number.")
        if message.startswith("Entry evidence selects only an analysis level:"):
            detail.update(code="source_entry_reference_needs_role_review", repair="The quoted number describes a historical/technical market level, not a stated entry. Re-read its original sentence and quote an actual current entry assignment if one exists. Otherwise leave entry_prices absent; preserve the valid market/direction/stop/targets. Do not invent a price or reject the whole setup merely because its optional entry reference is unstated.")
        if message == "Evidence does not occur in its original source":
            originals = {row["source_id"]: row["text"] for row in packet["messages"]}
            detail.update(field_paths=[f"evidence.{i}" for i, proof in enumerate(result.evidence)
                if proof.source_id not in result.source_refs or proof.quote not in originals.get(proof.source_id, "")],
                repair="Each proof must quote that same source_id's exact original and the source must be in source_refs. An EMPTY anchor is incomplete with evidence=[]; remove borrowed quotes, not the incomplete classification. Never probe other kinds or invent a quote to satisfy evidence.")
        if message.startswith("Non-actional source_refs"):
            detail.update(code="classification_scope_requires_anchor_only", repair="Use ONLY anchor_source_id in source_refs and classification evidence. Remove parent/context refs and their proofs from this non-actional result; they remain readable through the reply chain. Do not add parent proofs merely to retain an extra classification target.")
        if not any(row["message"] == message for row in errors):
            errors.append(detail)
    return {"event_id": packet["anchor_event_id"], "anchor_source_id": packet["anchor_source_id"],
            "available_source_ids": sorted({row["source_id"] for row in packet["messages"]}),
            "source_version": digest(packet), "valid": not errors,
            "interpretation": result.model_dump(mode="json"), "errors": errors,
            "reread_required": bool(context.get("source_read_error")), "execution_authorized": False}
