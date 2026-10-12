"""Opt-in, order-free Decision Model evaluation and separate held-out calibration evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from aqla_hyperliquid_mcp.models import SourceEvent, digest

from .decisions import (
    DEFAULT_DECISION_MODEL,
    VERSION,
    CalibrationProfile,
    DecisionFailure,
    OpenRouterDecisionAdapter,
    build_packet,
    candidates,
    replay_decision,
)

CRITICAL_FIELDS = ("kind", "symbol", "side", "leverage", "entry", "entry_timing", "source_exchange",
                   "entry_prices", "stop_loss", "take_profits", "take_profit_unit", "update_action", "reduce_percent",
                   "source_refs", "evidence")
REQUIRED_SCENARIOS = {"reply", "edit", "percent", "decimal", "injection", "multi_setup", "report_with_action"}
PROVIDER_FAILURES = {"decision_api_unavailable", "decision_catalog_unavailable", "decision_key_missing",
                     "decision_model_changed", "decision_model_not_available", "evaluation_failed"}


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class EvaluationCheckpoint:
    """Resume paid API observations only under the exact frozen input contract."""
    def __init__(self, path, identity, *, retry_provider_failures=False):
        self.path, self.identity, self.retry_provider_failures = Path(path), identity, retry_provider_failures
        self.rows = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("identity") != identity or not isinstance(saved.get("rows"), dict):
                raise ValueError("Checkpoint belongs to another dataset, master, questions or model revision")
            self.rows = saved["rows"]

    def get(self, case_id, source_hash):
        row = self.rows.get(case_id)
        if row is None:
            return None
        if row.get("source_hash") != source_hash:
            raise ValueError("Checkpoint source context changed")
        if self.retry_provider_failures and row.get("failure_code") in PROVIDER_FAILURES:
            return None
        return json.loads(json.dumps(row))

    def put(self, row):
        self.rows[row["id"]] = json.loads(json.dumps(row))
        write_private_json(self.path, {"identity": self.identity, "rows": self.rows})


class EvaluationAdapter(OpenRouterDecisionAdapter):
    def __init__(self, *args, request_interval=4, **kwargs):
        super().__init__(*args, **kwargs)
        if not math.isfinite(request_interval) or not 0 <= request_interval <= 60:
            raise ValueError("Evaluation request interval must be between 0 and 60 seconds")
        self.request_interval, self.last_request_at = request_interval, 0

    async def evaluate(self, *args, **kwargs):
        delay = self.request_interval - (time.monotonic() - self.last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request_at = time.monotonic()
        return await super().evaluate(*args, **kwargs)


def revision_time_context(events, anchor, *, limit=20):
    """As-of semantic replay, explicitly distinct from delayed-ingestion replay."""
    source = anchor["source"]
    revision_at = source.get("edited_at") or source["sent_at"]
    available = [row for row in events if row["source"]["chat_id"] == source["chat_id"]
                 and (row["source"].get("edited_at") or row["source"]["sent_at"]) <= revision_at]
    available.sort(key=lambda row: (row["source"]["sent_at"], row["source"].get("edited_at") or 0))
    selected = available[-limit:]
    if anchor not in selected:
        selected.append(anchor)
    reply, seen = source.get("reply_to_id"), set()
    while reply and reply not in seen:
        seen.add(reply)
        matches = [row for row in available if row["source"]["message_id"] == reply]
        if not matches:
            break
        parent = max(matches, key=lambda row: row["source"].get("edited_at") or row["source"]["sent_at"])
        if parent not in selected:
            selected.insert(0, parent)
        reply = parent["source"].get("reply_to_id")
    return selected


def equal(actual, expected):
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(equal(a, b) for a, b in zip(actual, expected, strict=True))
    if actual is None or expected is None:
        return actual is expected
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except Exception:
        return actual == expected


def complete_fields(interpretation):
    """Compatibility wire serializers must not hide annotation defaults."""
    def value(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, list):
            return [value(child) for child in item]
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return item
    return {key: value(getattr(interpretation, key)) for key in type(interpretation).model_fields}


def case_packet(case):
    sources = [SourceEvent.model_validate({"received_at": text.get("edited_at") or text.get("sent_at"), **text}) if isinstance(text, dict) else SourceEvent(
        chat_id=case.get("chat_id", case["id"]), message_id=i + 1, sent_at=1000 + i, received_at=1000 + i,
        reply_to_id=case.get("reply_to_id") if i == len(case["messages"]) - 1 else None, text=text)
        for i, text in enumerate(case["messages"])]
    event_ids = case.get("message_event_ids", [source.event_id for source in sources])
    if len(event_ids) != len(sources) or len(set(event_ids)) != len(event_ids) or any(
            identity not in (source.event_id, digest([source.source_id, source.edited_at, source.text, source.media])[:32])
            for source, identity in zip(sources, event_ids, strict=True)):
        raise ValueError("Offline event identities must match original revisions or retained legacy identities")
    context = {"event": {"event_id": case.get("anchor_event_id", event_ids[-1])},
        "messages": [{"event_id": identity, "source_id": s.source_id, "source": s.model_dump(mode="json")}
                     for s, identity in zip(sources, event_ids, strict=True)],
        "trades": case.get("trades", [])}
    return build_packet(context)


def annotation_mismatches(interpretation, expected):
    actual = complete_fields(interpretation)
    return [key for key, wanted in expected.items() if not equal(actual.get(key), wanted)]


def question_labels(packet, expected, actual=None):
    """Independent annotated values/quotes map prepared choices to correct answers."""
    spans = candidates(packet)
    by_id = {s.candidate_id: s for s in spans}
    proofs = expected.get("evidence", []) or []
    labels = {"kind": {expected.get("kind")}, "side": {expected.get("side") or "not_present"},
        "entry": {expected.get("entry", "unspecified")}, "entry_timing": {expected.get("entry_timing", "standard")},
        "update_action": {expected.get("update_action") or "not_present"}}
    for field, kind in (("symbol", "symbol"), ("source_exchange", "exchange")):
        wanted = expected.get(field)
        labels[field] = {s.candidate_id for s in spans if s.kind == kind and wanted is not None and
                         str(s.value).casefold() == str(wanted).casefold()} if wanted is not None else {"not_present"}
    for field in ("classification", "side", "entry_timing", "update_action"):
        quotes = [p for p in proofs if p["field"] == field]
        labels["proof." + field] = {s.candidate_id for s in spans if s.kind == "text" and any(
            p["source_id"] == s.source_id and p["quote"] == s.quote for p in quotes)} if quotes else {"not_present"}
    role_fields = {"entry_prices.0": "entry0", "entry_prices.1": "entry1", "stop_loss": "stop_loss",
                   "leverage": "leverage", "reduce_percent": "reduce_percent",
                   **{f"take_profits.{i}": f"target_{i + 1}" for i in range(20)}}
    for identity, span in by_id.items():
        if span.kind != "number":
            continue
        roles = {role_fields[p["field"]] for p in proofs if p["field"] in role_fields and
                 p["source_id"] == span.source_id and p["quote"] == span.quote}
        if roles:
            # Ambiguous normalization alternatives cannot both represent one value.
            roles = {role for role in roles if any(p["field"] in role_fields and role_fields[p["field"]] == role and
                equal(span.value, (expected.get(p["field"].split(".")[0], [])[int(p["field"].split(".")[1])]
                      if "." in p["field"] else expected.get(p["field"]))) for p in proofs)}
        labels["role." + identity] = roles or {"ignore"}
    actionable = expected.get("kind") in ("new_signal", "trade_update")
    labels["live_action"] = {actionable}
    labels["single_entry_form"] = {expected.get("kind") not in ("ambiguous", "incomplete")}
    values_fields = {"leverage", "entry", "entry_timing", "entry_prices", "stop_loss", "take_profits",
                     "take_profit_unit", "update_action", "reduce_percent", "evidence"}
    setup_fields = {"symbol", "side", "source_exchange", "source_refs"}
    mismatches = set(annotation_mismatches(actual, expected)) if actual is not None else set(expected)
    labels.update({"verification.intent": {actionable and actual is not None and actual.kind == expected.get("kind")},
                   "verification.values": {actual is not None and not mismatches & values_fields},
                   "verification.setup": {actual is not None and not mismatches & setup_fields}})
    return labels


def calibration_question_results(packet, expected, observation, result):
    labels = question_labels(packet, expected, result.interpretation if result else None)
    values = observation.get("values", {})
    probabilities = observation.get("probabilities", {})
    required = observation.get("required_questions", [])
    if result:
        probabilities = result.probabilities
        required = result.required_questions
    rows = []
    for key in dict.fromkeys(required):
        if key not in probabilities or key not in labels:
            continue
        probability = probabilities[key]
        if key in ("live_action", "single_entry_form") or key.startswith("verification."):
            prediction = probability >= 0.5
            confidence = probability if prediction else 1 - probability
        else:
            prediction, confidence = values.get(key), probability
        rows.append({"question": key, "family": "number_role" if key.startswith("role.") else key,
                     "correct": prediction in labels[key], "confidence": confidence,
                     "positive_only": key == "single_entry_form" or key.startswith("verification."),
                     "prediction": prediction})
    return rows


def fit_thresholds(rows):
    families = {}
    for row in rows:
        # A structurally rejected/negative-verification case cannot be admitted by
        # any legal threshold (>=.5); do not distort calibration with its unused choices.
        if not row.get("raw_correct") and not row.get("baseline_gate_passed"):
            continue
        for decision in row.get("question_results", []):
            if decision.get("positive_only") and decision.get("prediction") is False:
                continue
            group = families.setdefault(decision["family"], {"correct": [], "wrong": []})
            group["correct" if decision["correct"] else "wrong"].append(decision["confidence"])
    thresholds, disabled = {}, []
    for family, values in families.items():
        wrong = values["wrong"]
        if wrong and max(wrong) >= 1:
            disabled.append(family)
            continue
        threshold = max(0.5, min(values["correct"], default=0.99))
        if wrong:
            threshold = max(threshold, math.nextafter(max(wrong), 1.0))
        thresholds[family] = threshold
    return thresholds, disabled


def validate_splits(dataset, sources=()):
    by_id = {case["id"]: case for case in dataset}
    if len(by_id) != len(dataset):
        raise ValueError("Dataset requires distinct case IDs")
    groups = {split: {case.get("group_id", case["id"]) for case in dataset if case.get("split") == split}
              for split in ("calibration", "holdout")}
    if groups["calibration"] & groups["holdout"]:
        raise ValueError("Calibration and holdout must use different source threads/groups")
    original_sources = {split: {SourceEvent.model_validate(message).source_id
        for case in dataset if case.get("split") == split
        for message in case.get("messages", []) if isinstance(message, dict)}
        for split in ("calibration", "holdout")}
    if original_sources["calibration"] & original_sources["holdout"]:
        raise ValueError("Calibration and holdout must not share original source messages or edits")
    if sources and all(case.get("messages") for case in dataset):
        represented = set()
        for case in dataset:
            if case.get("origin") == "real":
                packet = case_packet(case)
                represented.add(next(row["chat_id"] for row in packet["messages"] if row["event_id"] == packet["anchor_event_id"]))
        if not set(sources) <= represented:
            raise ValueError("Activation sources must have independently annotated real anchor messages")
    return by_id


def calibration_profile(results, dataset, sources, master_hash="", *, model=DEFAULT_DECISION_MODEL, model_revision=DEFAULT_DECISION_MODEL):
    by_id = validate_splits(dataset, sources)
    calibration = [row for row in results if by_id[row["id"]].get("split") == "calibration"]
    holdout = [row for row in results if by_id[row["id"]].get("split") == "holdout"]
    thresholds, disabled = fit_thresholds(calibration)
    accepted = [row for row in holdout if row.get("accepted")]
    critical = sum(row.get("critical_error", False) for row in accepted)
    sufficient_annotations = all(case.get("group_id") and case.get("annotation_complete") is True
                                 and set(CRITICAL_FIELDS) <= set(case["expected"])
                                 for case in dataset if case.get("split") in ("calibration", "holdout"))
    coverage = all(
        {"new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous"} <=
        {case["expected"].get("kind") for case in dataset if case.get("split") == split}
        and {"de", "en", "ru"} <= {case.get("language") for case in dataset if case.get("split") == split}
        and REQUIRED_SCENARIOS <= {tag for case in dataset if case.get("split") == split for tag in case.get("tags", [])}
        for split in ("calibration", "holdout"))
    evidence, permissions = {}, {}
    for source in sources:
        training = [r for r in calibration if r.get("source_id") == source and by_id[r["id"]].get("origin") == "real"]
        testing = [r for r in holdout if r.get("source_id") == source and by_id[r["id"]].get("origin") == "real"]
        source_accepted = [r for r in testing if r.get("accepted")]
        allowed = [kind for kind in ("new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous")
            if any(r["passed"] and r.get("kind") == kind for r in training)
            and sum(r["passed"] and r.get("kind") == kind for r in source_accepted) >= 2]
        proof = {"calibration_groups": len({by_id[r["id"]]["group_id"] for r in training}),
                 "holdout_groups": len({by_id[r["id"]]["group_id"] for r in testing}),
                 "accepted_cases": len(source_accepted),
                 "accepted_accuracy": sum(r["passed"] for r in source_accepted) / len(source_accepted) if source_accepted else 0,
                 "critical_errors": sum(r.get("critical_error", False) for r in source_accepted)}
        if proof["calibration_groups"] >= 3 and proof["holdout_groups"] >= 5 and proof["accepted_accuracy"] >= 0.95 and proof["critical_errors"] == 0 and allowed:
            evidence[source], permissions[source] = proof, allowed
    real_action_coverage = all(sum(r["passed"] and r.get("kind") == kind and by_id[r["id"]].get("origin") == "real"
                                  for r in accepted) >= 4 for kind in ("new_signal", "trade_update"))
    required_ids = {case["id"] for case in dataset if case.get("split") in ("calibration", "holdout")}
    measured_ids = [row["id"] for row in results if row["id"] in required_ids]
    complete_measurement = (set(measured_ids) == required_ids and len(measured_ids) == len(required_ids)
                            and not any(row.get("failure_code") in PROVIDER_FAILURES for row in calibration + holdout))
    valid = bool(master_hash and evidence and thresholds and complete_measurement and len(calibration) >= 30 and len(holdout) >= 30
                 and len({by_id[r["id"]]["group_id"] for r in accepted}) >= 30
                 and sufficient_annotations and coverage and real_action_coverage and critical == 0
                 and sum(row["passed"] for row in accepted) / len(accepted) >= 0.95)
    return CalibrationProfile(model=model, model_revision=model_revision, thresholds=thresholds, disabled_questions=disabled,
        sources=list(evidence), source_evidence=evidence, source_permissions=permissions, master_hash=master_hash,
        dataset_hash=digest([case for case in dataset if case.get("split") == "calibration"]),
        holdout_hash=digest([case for case in dataset if case.get("split") == "holdout"]),
        holdout_cases=len(holdout), critical_errors=critical, validated=valid)


async def evaluate(dataset, adapter, *, profile=None, master="", collect=False, checkpoint=None):
    profile = profile or CalibrationProfile()
    rows = []
    for case in dataset:
        packet, usage = case_packet(case), []
        packet["approved_source_guidance"] = master
        if checkpoint is not None:
            cached = checkpoint.get(case["id"], digest(packet))
            if cached is not None:
                rows.append(cached)
                if cached.get("failure_code") in PROVIDER_FAILURES:
                    raise DecisionFailure(cached["failure_code"])
                continue
        result, failure, mismatches, observation = None, None, [], {}
        try:
            result = await adapter.interpret(packet, profile, collect=collect, usage=usage, observation=observation)
            mismatches = annotation_mismatches(result.interpretation, case["expected"])
        except Exception as error:
            failure = str(error) if isinstance(error, DecisionFailure) else "evaluation_failed"
        predicted = result.interpretation.kind if result else None
        expected_kind = case["expected"].get("kind")
        actionable = predicted in ("new_signal", "trade_update")
        accepted = result is not None and not result.collection_only
        critical = bool(accepted and mismatches and
                        (actionable or expected_kind in ("new_signal", "trade_update")))
        baseline_passed = accepted
        if observation.get("responses"):
            try:
                baseline = CalibrationProfile(model=profile.model, model_revision=profile.model_revision,
                    thresholds={key: 0.5 for key in (result.required_questions if result else observation.get("required_questions", []))})
                replay_decision(packet, baseline, observation["responses"], model=getattr(adapter, "model", DEFAULT_DECISION_MODEL),
                                revision=getattr(adapter, "model_revision", profile.model_revision))
                baseline_passed = True
            except DecisionFailure:
                baseline_passed = False
        rows.append({"id": case["id"], "source_hash": digest(packet), "kind": predicted,
                     "source_id": next(r["chat_id"] for r in packet["messages"] if r["event_id"] == packet["anchor_event_id"]),
                     "expected_kind": expected_kind, "passed": failure is None and not mismatches,
                     "raw_correct": failure is None and not mismatches, "accepted": accepted, "deferred": not accepted,
                     "baseline_gate_passed": baseline_passed,
                     "critical_error": critical, "missed_actionable": expected_kind in ("new_signal", "trade_update") and not actionable,
                     "mismatch_fields": mismatches, "failure_code": failure,
                     "probabilities": result.probabilities if result else observation.get("probabilities", {}), "usage": usage,
                     "question_results": calibration_question_results(packet, case["expected"], observation, result),
                     "responses": observation.get("responses", [])})
        if checkpoint is not None:
            checkpoint.put(rows[-1])
            if failure in PROVIDER_FAILURES:
                raise DecisionFailure(failure)
    return rows


async def evaluate_split(dataset, adapter, sources, master="", *, checkpoint_path=None, retry_provider_failures=False):
    validate_splits(dataset, sources)
    model = getattr(adapter, "model", DEFAULT_DECISION_MODEL)
    revision = await adapter.identity() if hasattr(adapter, "identity") else model
    if hasattr(adapter, "pin_revision"):
        adapter.pin_revision(revision)
    checkpoint = EvaluationCheckpoint(checkpoint_path, {"version": VERSION, "dataset_hash": digest(dataset),
        "master_hash": digest(master), "model": model, "model_revision": revision},
        retry_provider_failures=retry_provider_failures) if checkpoint_path else None
    training = [case for case in dataset if case.get("split") == "calibration"]
    test = [case for case in dataset if case.get("split") == "holdout"]
    other = [case for case in dataset if case.get("split") not in ("calibration", "holdout")]
    rows = await evaluate(training, adapter, profile=CalibrationProfile(model=model, model_revision=revision), master=master,
                          collect=True, checkpoint=checkpoint) if training else []
    import hashlib
    master_hash = hashlib.sha256(master.encode()).hexdigest() if master else ""
    frozen = calibration_profile(rows, dataset, sources, master_hash, model=model, model_revision=revision)
    # Held-out results must be measured with the fitted thresholds, never retrofitted.
    heldout = await evaluate(test, adapter, profile=frozen, master=master, collect=True, checkpoint=checkpoint) if test else []
    test_by_id = {case["id"]: case for case in test}
    for row in heldout:
        case = test_by_id[row["id"]]
        try:
            packet = case_packet(case)
            packet["approved_source_guidance"] = master
            accepted = replay_decision(packet, frozen, row["responses"], model=model, revision=revision)
            # This measures the frozen semantic adapter, without granting source
            # authority. Final permissions additionally require source-specific
            # calibration and held-out evidence; the report checks both again.
            row["accepted"], row["deferred"] = True, False
            row["mismatch_fields"] = annotation_mismatches(accepted.interpretation, case["expected"])
            row["passed"] = not row["mismatch_fields"]
            row["critical_error"] = bool(row["mismatch_fields"] and (accepted.interpretation.kind in ("new_signal", "trade_update")
                or case["expected"]["kind"] in ("new_signal", "trade_update")))
        except DecisionFailure as error:
            row["accepted"], row["deferred"], row["passed"], row["critical_error"] = False, True, False, False
            row["failure_code"] = str(error)
    rows += heldout
    rows += await evaluate(other, adapter, master=master) if other else []
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--model", default=DEFAULT_DECISION_MODEL)
    parser.add_argument("--sources", nargs="+", default=[])
    parser.add_argument("--write-profile", type=Path)
    parser.add_argument("--master", type=Path)
    parser.add_argument("--baseline", type=Path, help="Saved per-case generative baseline usage; same distinct case IDs")
    parser.add_argument("--checkpoint", type=Path, help="Default: output filename plus .checkpoint.json; resume exact raw observations")
    parser.add_argument("--retry-provider-failures", action="store_true", help="Explicitly retry only saved provider failures, not semantic deferrals")
    parser.add_argument("--request-interval", type=float, default=4, help="Minimum seconds between decision API calls, 0–60")
    parser.add_argument("--timeout", type=float, default=30, help="Per-request evaluation timeout, 0–30 seconds")
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    if not key:
        raise SystemExit("Set the decision API key environment variable; no calls were made")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not isinstance(dataset, list) or not dataset or len({c["id"] for c in dataset}) != len(dataset):
        raise SystemExit("Dataset must contain distinct annotated case IDs")
    calibration_profile([], dataset, args.sources)
    master = args.master.read_text(encoding="utf-8") if args.master else ""
    import hashlib
    master_hash = hashlib.sha256(master.encode()).hexdigest() if master else ""
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("--timeout must be positive and at most 30 seconds")
    adapter = EvaluationAdapter(key, model=args.model, timeout=args.timeout, request_interval=args.request_interval)
    checkpoint = args.checkpoint or args.output.with_name(args.output.name + ".checkpoint.json")
    try:
        rows = asyncio.run(evaluate_split(dataset, adapter, args.sources, master, checkpoint_path=checkpoint,
                                         retry_provider_failures=args.retry_provider_failures))
    except DecisionFailure as error:
        raise SystemExit("Evaluation interrupted: " + str(error) + "; saved observations remain in the checkpoint") from None
    profile = calibration_profile(rows, dataset, args.sources, master_hash, model=args.model, model_revision=adapter.model_revision)
    by_id = {case["id"]: case for case in dataset}
    for row in rows:
        row["production_eligible"] = bool(profile.validated and row["accepted"] and row["passed"]
            and by_id[row["id"]].get("origin") == "real" and row["source_id"] in profile.sources
            and profile.permits_kind(row["source_id"], row["kind"]))
    heldout = [row for row in rows if by_id[row["id"]].get("split") == "holdout"]
    real_heldout = [row for row in heldout if by_id[row["id"]].get("origin") == "real"]
    passed = sum(row["passed"] for row in heldout)
    report = {"version": VERSION, "model": profile.model, "model_revision": profile.model_revision, "cases": rows, "order_tools_available": False,
              "passed_cases": passed, "raw_correct_cases": sum(row["raw_correct"] for row in rows),
              "accepted_holdout_cases": sum(row["accepted"] for row in heldout),
              "deferred_holdout_cases": sum(row["deferred"] for row in heldout),
              "without_fallback_share": sum(row["production_eligible"] for row in real_heldout) / len(real_heldout) if real_heldout else None,
              "without_fallback_share_is_live_measurement": False,
              "activated_sources": profile.sources, "source_permissions": profile.source_permissions,
              "critical_errors": sum(row["critical_error"] for row in rows),
              "calibration_validated": profile.validated,
              "input_tokens": sum(u.get("input_tokens", 0) for row in rows for u in row["usage"]),
              "estimated_cost_usd": sum(u.get("estimated_cost_usd", 0) for row in rows for u in row["usage"])}
    report["missing_usage_calls"] = sum(not u.get("usage_available", False) for row in rows for u in row["usage"])
    report["estimated_cost_complete"] = all("estimated_cost_usd" in u for row in rows for u in row["usage"])
    if not report["estimated_cost_complete"]:
        report["estimated_cost_usd"] = None
    durations = sorted(sum(u.get("duration_seconds", 0) for u in row["usage"]) for row in rows)
    report["api_latency_p50_seconds"] = durations[max(0, math.ceil(len(durations) * 0.5) - 1)]
    report["api_latency_p95_seconds"] = durations[max(0, math.ceil(len(durations) * 0.95) - 1)]
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if not isinstance(baseline, list) or {r["id"] for r in baseline} != {r["id"] for r in rows} or len(baseline) != len(rows):
            raise SystemExit("Baseline must cover exactly the same distinct case IDs")
        benchmark_ids = {r["id"] for r in real_heldout}
        total = sum(r["input_tokens"] + r.get("cache_read_tokens", 0) for r in baseline if r["id"] in benchmark_ids)
        report["baseline_generative_input_tokens"] = total
        report["baseline_comparison_scope"] = "real_holdout_only"
        report["baseline_comparison_cases"] = len(benchmark_ids)
        # Unresolved cases conservatively retain the FULL old baseline spend.
        unresolved = {r["id"] for r in real_heldout if not r["production_eligible"]}
        retained = sum(r["input_tokens"] + r.get("cache_read_tokens", 0) for r in baseline if r["id"] in unresolved)
        report["conservative_projected_generative_input_reduction"] = 1 - retained / total if total else None
        report["generative_reduction_is_measured"] = False
    write_private_json(args.output, report)
    if args.write_profile:
        write_private_json(args.write_profile, profile.model_dump(mode="json"))
    print(json.dumps({key: report[key] for key in ("passed_cases", "without_fallback_share", "critical_errors", "calibration_validated", "input_tokens", "estimated_cost_usd")}))


if __name__ == "__main__":
    main()
