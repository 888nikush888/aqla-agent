"""Lossless decision projections and progress-aware native wake scheduling."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from aqla_hyperliquid_mcp.models import AqlaError

from .execution_outcome import diagnostic_view, trade_outcome
from .source_queue import is_source_row

SOURCE_TOOLS = tuple("mcp__aqla__" + name for name in (
    "status", "source_lookup", "source_context", "source_context_batch", "source_check_interpretation", "interpret_source", "interpret_source_batch",
    "trade_preview", "trade_execute", "trade_update", "trade_diagnostics", "operation_status", "operation_reconcile",
    "notifications_read", "notifications_details", "notifications_ack", "ranking_status", "monthly_report")) + ("aqla_read_tool_result",)


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass
class WakeSchedule:
    observed: str | None = None
    delivered: str | None = None
    interval: int = 30
    queued_at: float | None = None

    def due(self, work, now, last_wake, *, busy=False):
        current = signature(work)
        if current != self.observed:
            self.observed = current
            self.interval = 30
        if self.queued_at is not None:
            # Scheduling acceptance is not delivery. Recover a lost admission
            # after five minutes only when no actual native turn is running.
            if busy or now - self.queued_at < 300:
                return False
            self.queued_at = None
        if busy:
            return False
        return self.delivered != current or now - last_wake >= self.interval

    def accepted(self, now):
        repeated = self.delivered == self.observed
        self.delivered = self.observed
        self.queued_at = now
        self.interval = {30: 60, 60: 120, 120: 300, 300: 300}[self.interval] if repeated else 30

    def consumed(self):
        self.queued_at = None


def decision_context(context):
    """Keep original bodies/revisions once, with explicit stored-detail links."""
    anchor = context["event"]
    messages = {row["event_id"]: {key: row[key] for key in
                ("event_id", "source_id", "source", "status", "interpretation", "native_forward", "last_error") if key in row}
                for row in [*context["messages"], anchor]}
    trades = []
    for row in context["trades"]:
        plan = row["plan"]
        trades.append({"trade_id": row["trade_id"], "status": row["status"], "execution_outcome": row["execution_outcome"] if "execution_outcome" in row else trade_outcome(row),
                       "entry_diagnostic": row["execution_outcome"].get("entry_diagnostic") if "execution_outcome" in row else diagnostic_view(row),
                       "plan": {key: plan[key] for key in
                                ("source_refs", "source_revisions", "market", "side", "account", "account_key", "exchange",
                                 "network", "entry_price", "entry_limit", "entry_kind", "entry_timing", "quantity",
                                 "deadline_at", "stop_loss", "take_profits", "policy", "created_at") if key in plan},
                       **{key: row[key] for key in ("entry_vwap", "filled_quantity", "remaining_quantity", "stop_price",
                                                   "targets", "tp_filled", "management_revision", "entry_stop_confirmed",
                                                   "execution_incident", "maker_target_rejection",
                                                   "percent_finalization", "percent_updates") if key in row},
                       "detail": {"tool": "trade_diagnostics", "trade_id": row["trade_id"]}})
    result = {key: value for key, value in context.items() if key not in
              ("event", "messages", "trades", "source_names")}
    from .source_assistance import source_assistance
    assistance = source_assistance(context)
    result.update(view="decision", anchor_event_id=anchor["event_id"], anchor_source_id=anchor["source_id"],
                  message_ids=list(messages), messages=messages, trades=trades,
                  percentage_rule="Absolute targets without %: price. Explicit source TP percentages: roe_percent on own initial margin before costs.",
                  source_name=context.get("source_name"),
                  projection={"source_bodies": "lossless", "stored_trade_details_omitted": True},
                  interpretation_assistance=assistance, anchor_setup_bound=assistance["anchor_setup_bound"])
    return result


def decision_batch(contexts):
    if not 1 <= len(contexts) <= 8:
        raise AqlaError("Source context batch requires 1–8 distinct event IDs")
    messages, trades, configurations, items, contracts, candidates = {}, {}, {}, [], {}, {}
    for context in contexts:
        result = decision_context(context)
        assistance = result["interpretation_assistance"]
        assistance.pop("anchor_setup_bound")  # The item's authoritative field is already present.
        if not assistance["original_structure_vetoes"]:
            assistance.pop("original_structure_vetoes")
        if "candidates" in assistance:
            spans = assistance.pop("candidates")
            candidates.update({span["candidate_id"]: span for span in spans})
            assistance["candidate_ids"] = [span["candidate_id"] for span in spans]
        if "contract" in assistance:
            contract = assistance.pop("contract")
            for key in ("workflow", "candidate_roles_are_not_inferred", "version", "candidate_limit",
                        "vetoes_are_not_classification_or_permission"):
                contract[key] = assistance.pop(key)
            identity = signature(contract)[:24]
            contracts[identity] = contract
            assistance["contract_id"] = identity
        percentage_rule = result.pop("percentage_rule")
        result.pop("projection")
        source_messages = result.pop("messages")
        for identity, row in source_messages.items():
            if identity in messages and messages[identity] != row:
                raise AqlaError("Source context changed during batch read; reread current anchors")
            messages[identity] = row
        trade_keys = []
        for row in result.pop("trades"):
            identity = row["trade_id"]
            if identity in trades and trades[identity] != row:
                raise AqlaError("Trade binding changed during batch read; reread current anchors")
            trades[identity] = row
            trade_keys.append(identity)
        config = {key: result.pop(key) for key in ("policy", "policy_revision", "backup_profile", "profiles", "pipelines",
                  "configuration_revision", "venue_limits", "execution_accounts") if key in result}
        config_id = signature(config)[:24]
        configurations[config_id] = config
        items.append({**result, "message_ids": list(source_messages),
                      "trade_ids": trade_keys, "configuration_id": config_id})
    return {"view": "decision_batch", "items": items, "messages": messages, "trades": trades,
            "configurations": configurations, "interpretation_contracts": contracts, "interpretation_candidates": candidates,
            "source_bodies": "lossless", "stored_trade_details_omitted": True, "percentage_rule": percentage_rule}


def classification_ack(row, changed):
    return {"event_id": row["event_id"], "source_id": row["source_id"], "status": row["status"],
            "changed": changed, "event_status_is_fill_proof": False,
            "detail": {"tool": "source_context", "event_id": row["event_id"], "view": "full"}}


def diagnostic_sections(result, snapshot, sections):
    groups = {
        "plan": ("plan",), "orders": ("orders", "exit_orders", "order_observations"),
        "fills": ("fill_events", "entry_vwap"),
        "accounting": ("entry_accounting", "initial_sl_risk", "source_tp_finalization", "source_tp_updates"),
        "operations": ("operations", "execution_incident", "observation_retry", "entry_diagnostic"), "lifecycle": ("lifecycle_events",),
        "prices": ("price_checks", "waiting_snapshot", "waiting_error", "unsent_reason", "observation_retry", "entry_diagnostic"), "sources": (),
    }
    if not sections or len(sections) > 8 or any(section not in groups for section in sections):
        raise AqlaError("Choose 1–8 diagnostic sections")
    plan = result["plan"]
    keys = {key for section in sections for key in groups[section]}
    output = {key: result[key] for key in keys}
    output.update(trade_id=result["trade_id"], status=result["status"], close_reason=result["close_reason"],
                  execution_outcome=result["execution_outcome"],
                  scope={key: plan.get(key) for key in ("account", "account_key", "exchange", "network")},
                  selected_sections=list(dict.fromkeys(sections)), details_omitted=True,
                  historical_missing_quotes_are_not_inferred=True)
    if "sources" in sections:
        refs = set(plan["source_refs"])
        output["source_messages"] = {row["event_id"]: {"source_id": row["source_id"], "source": row["source"]}
                                      for row in snapshot["events"].values() if is_source_row(row) and row["source_id"] in refs}
        output["source_revisions"] = result["source_revisions"]
        output["missing_original_sources"] = result["missing_original_sources"]
        output["original_sources"] = {key: {"event_id": row["event_id"]} for key, row in result["original_sources"].items()}
        for key in ("later_source_edits", "retained_revisions_with_missing_original"):
            output[key] = [{name: value for name, value in row.items() if name != "source"} for row in result[key]]
    return output
