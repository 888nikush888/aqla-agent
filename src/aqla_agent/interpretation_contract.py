"""Provider-independent production interpretation and original-evidence checks."""

import re

from aqla_hyperliquid_mcp.models import AqlaError, require_first
from aqla_hyperliquid_mcp.rules import (
    quoted_leverage_numbers,
    verify_entry_shape,
    verify_evidence,
    verify_setup_references,
    verify_update_shape,
)

from .evidence import position_fractions

ENTRY_FORM_MARKERS = re.compile(
    r"(?<!\w)(?:market|markt|рынок|маркет(?:у|ом)?|limit|лимит(?:ный|ная|ка)?)\b"
    r"(?:\s+(?:entry|order|reference|price|вход|einstieg))?\s*[:@=-]?\s*\d"
    r"|(?<!\w)(?:entry|enter|buy|sell|einstieg|вход)\s+(?:at\s+)?(?:market|markt|рынок|limit|лимит)\b"
    r"|(?<!\w)(?:entry|einstieg|вход)\s+(?:range|zone|диапазон|зона)\b"
    r"|(?<!\w)по\s+(?:рынку|маркету)(?!\w)"
    r"|(?<!\w)(?:market|markt)[-\s]+(?:entry|einstieg)(?!\w)", re.I)


class DecisionFailure(AqlaError):
    """Safe failure code, never a provider response or model-supplied exception."""



def validate_reduction_quantity(packet, interpretation):
    if interpretation.reduce_percent is not None:
        supported = False
        for proof in interpretation.evidence:
            if proof.field != "reduce_percent":
                continue
            original = require_first(row["text"] for row in packet["messages"] if row["source_id"] == proof.source_id)
            for occurrence in re.finditer(re.escape(proof.quote), original):
                for percentage in re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)*\s*%", proof.quote):
                    before = original[:occurrence.start() + percentage.start()].rstrip()
                    after = original[occurrence.start() + percentage.end():]
                    if (before.endswith(("-", "−")) or re.search(r"(?:at|на|в)\s*\+\s*$", before, re.I)
                            or re.match(r"\s*(?:ROI|ROE|PNL)\b", after, re.I)
                            or re.search(r"\b(?:ROI|ROE|PNL)\s*[:=@]?\s*$", before, re.I)):
                        raise DecisionFailure("pnl_percentage_is_not_reduction_quantity")
            quantities = [(match.span(), {value}) for match, value in position_fractions(proof.quote)]
            for match in re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)*\s*%", proof.quote):
                quantities.append((match.span(), set(quoted_leverage_numbers(match.group()))))
            if len(quantities) == 1 and interpretation.reduce_percent in quantities[0][1]:
                supported = True
        if not supported:
            raise DecisionFailure("reduction_quantity_proof_missing")


def validate_interpretation(packet, interpretation, *, own_setup_refs=()):
    from .semantic_contract import (
        supported_action_failure,
        unbound_setup_update_failure,
        update_identity_failure,
    )

    if packet["anchor_source_id"] not in interpretation.source_refs:
        raise DecisionFailure("live_anchor_missing")
    events = {row["event_id"]: {"event_id": row["event_id"], "source_id": row["source_id"],
              "source": {key: value for key, value in row.items() if key not in ("event_id", "source_id")}}
              for row in packet["messages"]}
    verify_evidence(interpretation, events)
    verify_setup_references(interpretation, packet["anchor_source_id"],
                            [*own_setup_refs, *(ref for trade in packet.get("trades", []) for ref in trade["source_refs"])])
    anchor = require_first(row for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"])
    if not anchor["text"].strip() and interpretation.kind != "incomplete":
        raise DecisionFailure("empty_source_requires_incomplete")
    if interpretation.kind == "new_signal" and packet.get("anchor_setup_bound"):
        raise DecisionFailure("bound_setup_cannot_reenter")
    if interpretation.kind == "new_signal" and interpretation.entry == "unspecified" and ENTRY_FORM_MARKERS.search(anchor["text"]):
        raise DecisionFailure("explicit_entry_form_requires_review")
    from aqla_hyperliquid_mcp.rules import management_steps
    for child in management_steps(interpretation) if interpretation.management_steps else [interpretation]:
        validate_reduction_quantity(packet, child)
    failure = supported_action_failure(anchor["text"], interpretation)
    failure = failure or unbound_setup_update_failure(packet, interpretation)
    failure = failure or update_identity_failure(packet, interpretation)
    if failure:
        raise DecisionFailure(failure)
    verify_entry_shape(interpretation)
    verify_update_shape(interpretation)
    if interpretation.kind not in ("new_signal", "trade_update"):
        from .source_disposition import verify_classification_policy
        if anchor["text"].strip() and (not interpretation.reason.strip() or not any(
                p.field == "classification" and p.source_id == anchor["source_id"] for p in interpretation.evidence)):
            raise DecisionFailure("classification_proof_missing")
        verify_classification_policy(interpretation, anchor["text"])
    if interpretation.bound_trade_id is not None:
        raise DecisionFailure("source_cannot_choose_operator_trade")
