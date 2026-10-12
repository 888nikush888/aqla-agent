"""Opt-in, order-free Decision Model evaluation and separate held-out calibration evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
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
)

CRITICAL_FIELDS = ("kind", "symbol", "side", "leverage", "entry", "entry_timing", "source_exchange",
                   "entry_prices", "stop_loss", "take_profits", "take_profit_unit", "update_action", "reduce_percent",
                   "source_refs", "evidence")
REQUIRED_SCENARIOS = {"reply", "edit", "percent", "decimal", "injection", "multi_setup", "report_with_action"}


def equal(actual, expected):
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(equal(a, b) for a, b in zip(actual, expected, strict=True))
    if actual is None or expected is None:
        return actual is expected
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except Exception:
        return actual == expected


def calibration_profile(results, dataset, sources, master_hash="", *, model=DEFAULT_DECISION_MODEL, model_revision=DEFAULT_DECISION_MODEL):
    by_id = {case["id"]: case for case in dataset}
    calibration = [row for row in results if by_id[row["id"]].get("split") == "calibration"]
    holdout = [row for row in results if by_id[row["id"]].get("split") == "holdout"]
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
    thresholds = {}
    for row in calibration:
        if not row["passed"]:
            continue
        for key, probability in row.get("probabilities", {}).items():
            key = "number_role" if key.startswith("role.") else key
            if key == "live_action" and row.get("kind") not in ("new_signal", "trade_update"):
                probability = 1 - probability
            thresholds[key] = max(0.99, min(thresholds.get(key, 1.0), probability))
    critical = sum(row["critical_error"] for row in holdout)
    sufficient_annotations = all(case.get("group_id") and case.get("annotation_complete") is True
                                 and set(CRITICAL_FIELDS) <= set(case["expected"])
                                 for case in dataset if case.get("split") in ("calibration", "holdout"))
    coverage = all(
        {"new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous"} <=
        {case["expected"].get("kind") for case in dataset if case.get("split") == split}
        and {"de", "en", "ru"} <= {case.get("language") for case in dataset if case.get("split") == split}
        and REQUIRED_SCENARIOS <= {tag for case in dataset if case.get("split") == split for tag in case.get("tags", [])}
        for split in ("calibration", "holdout"))
    valid = bool(master_hash and sources and thresholds and len(calibration) >= 30 and len(holdout) >= 30 and sufficient_annotations
                 and coverage
                 and all(row["passed"] for row in calibration) and critical == 0
                 and sum(row["passed"] for row in holdout) / len(holdout) >= 0.95)
    return CalibrationProfile(model=model, model_revision=model_revision, thresholds=thresholds, sources=sources, master_hash=master_hash,
        dataset_hash=digest([case for case in dataset if case.get("split") == "calibration"]),
        holdout_hash=digest([case for case in dataset if case.get("split") == "holdout"]),
        holdout_cases=len(holdout), critical_errors=critical, validated=valid)


async def evaluate(dataset, adapter, *, profile=None, master=""):
    profile = profile or CalibrationProfile()
    rows = []
    for case in dataset:
        sources = [SourceEvent.model_validate(text) if isinstance(text, dict) else
                   SourceEvent(chat_id=case.get("chat_id", case["id"]), message_id=i + 1, sent_at=1000 + i,
                    reply_to_id=case.get("reply_to_id") if i == len(case["messages"]) - 1 else None, text=text)
                   for i, text in enumerate(case["messages"])]
        context = {"event": {"event_id": case.get("anchor_event_id", sources[-1].event_id)}, "messages": [
            {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
            for source in sources], "trades": case.get("trades", [])}
        packet, usage = build_packet(context), []
        packet["approved_source_guidance"] = master
        result, failure, mismatches = None, None, []
        try:
            result = await adapter.interpret(packet, profile, collect=True, usage=usage)
            actual = result.interpretation.model_dump(mode="json")
            mismatches = [key for key, expected in case["expected"].items() if not equal(actual.get(key), expected)]
        except Exception as error:
            failure = str(error) if isinstance(error, DecisionFailure) else "evaluation_failed"
        predicted = result.interpretation.kind if result else None
        expected_kind = case["expected"].get("kind")
        actionable = predicted in ("new_signal", "trade_update")
        critical = bool(failure is None and mismatches and
                        (actionable or expected_kind in ("new_signal", "trade_update")))
        rows.append({"id": case["id"], "source_hash": digest(packet), "kind": predicted,
                     "expected_kind": expected_kind, "passed": failure is None and not mismatches,
                     "critical_error": critical, "missed_actionable": expected_kind in ("new_signal", "trade_update") and not actionable,
                     "mismatch_fields": mismatches, "failure_code": failure,
                     "probabilities": result.probabilities if result else {}, "usage": usage})
    return rows


async def evaluate_split(dataset, adapter, sources, master=""):
    calibration_profile([], dataset, sources)
    model = getattr(adapter, "model", DEFAULT_DECISION_MODEL)
    revision = await adapter.identity() if hasattr(adapter, "identity") else model
    if hasattr(adapter, "pin_revision"):
        adapter.pin_revision(revision)
    training = [case for case in dataset if case.get("split") == "calibration"]
    test = [case for case in dataset if case.get("split") == "holdout"]
    other = [case for case in dataset if case.get("split") not in ("calibration", "holdout")]
    rows = await evaluate(training, adapter, profile=CalibrationProfile(model=model, model_revision=revision), master=master) if training else []
    import hashlib
    master_hash = hashlib.sha256(master.encode()).hexdigest() if master else ""
    frozen = calibration_profile(rows, dataset, sources, master_hash, model=model, model_revision=revision)
    # Held-out results must be measured with the fitted thresholds, never retrofitted.
    rows += await evaluate(test, adapter, profile=frozen, master=master) if test else []
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
    adapter = OpenRouterDecisionAdapter(key, model=args.model)
    rows = asyncio.run(evaluate_split(dataset, adapter, args.sources, master))
    profile = calibration_profile(rows, dataset, args.sources, master_hash, model=args.model, model_revision=adapter.model_revision)
    passed = sum(row["passed"] for row in rows)
    report = {"version": VERSION, "model": profile.model, "model_revision": profile.model_revision, "cases": rows, "order_tools_available": False,
              "passed_cases": passed, "without_fallback_share": passed / len(rows),
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
        total = sum(r["input_tokens"] + r.get("cache_read_tokens", 0) for r in baseline)
        report["baseline_generative_input_tokens"] = total
        # Unresolved cases conservatively retain the FULL old baseline spend.
        unresolved = {r["id"] for r in rows if not r["passed"]}
        retained = sum(r["input_tokens"] + r.get("cache_read_tokens", 0) for r in baseline if r["id"] in unresolved)
        report["conservative_projected_generative_input_reduction"] = 1 - retained / total if total else None
        report["generative_reduction_is_measured"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(args.output, 0o600)
    if args.write_profile:
        args.write_profile.parent.mkdir(parents=True, exist_ok=True)
        args.write_profile.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(args.write_profile, 0o600)
    print(json.dumps({key: report[key] for key in ("passed_cases", "without_fallback_share", "critical_errors", "calibration_validated", "input_tokens", "estimated_cost_usd")}))


if __name__ == "__main__":
    main()
