"""Private frozen source replay for express; no model, broker or credential access."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from unittest import TestCase

from aqla_hyperliquid_mcp.models import require_first

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aqla_agent.decision_evaluation import case_context, write_private_json  # noqa: E402
from aqla_agent.express import VERSION, compile_template  # noqa: E402
from aqla_agent.source_assistance import check_interpretation  # noqa: E402
from aqla_agent.source_replay import mismatches  # noqa: E402
from aqla_hyperliquid_mcp.models import Interpretation, SourceEvent, digest  # noqa: E402

INVARIANTS = TestCase()


def evaluate(case):
    context = case_context(case)
    context["event"] = require_first(row for row in context["messages"] if row["event_id"] == context["event"]["event_id"])
    source = SourceEvent.model_validate(context["event"]["source"])
    started = time.perf_counter()
    draft = compile_template(source)
    checked = check_interpretation(context, draft) if draft else None
    accepted = bool(checked and checked["valid"])
    errors = []
    if accepted:
        if case.get("express_expected") is False:
            errors.append("false_express_admission")
        errors += mismatches(Interpretation.model_validate(checked["interpretation"]), case["expected"])
    elif case.get("express_expected") is True:
        errors.append("expected_complete_template_deferred")
    return {"case_id": case["id"], "accepted": accepted, "passed": not errors, "errors": errors,
            "duration_seconds": time.perf_counter() - started, "model_calls": 0,
            "interpretation": checked["interpretation"] if accepted else None}


def percentile(values, fraction):
    return sorted(values)[max(0, min(len(values)-1, int(len(values)*fraction + .999999)-1))]


async def _paired_case(case, row, output, pair_index):
    from express_benchmark_fixture import apply_fake
    context = case_context(case)
    source = SourceEvent.model_validate(require_first(r["source"] for r in context["messages"] if r["event_id"] == case["anchor_event_id"]))
    fast = evaluate(case)
    INVARIANTS.assertTrue(fast['accepted'] and fast['passed'] and row['passed'])
    values = {"express": Interpretation.model_validate(fast["interpretation"]),
              "high": Interpretation.model_validate(row["interpretation"])}
    results = {}
    order = ("high", "express") if pair_index % 2 else ("express", "high")
    for route in order:
        results[route] = await apply_fake(source, values[route], output / (digest([row["case_id"], row["run"], route])[:24]), express=route == "express")
    INVARIANTS.assertTrue(results['express']['outcome'] == results['high']['outcome'], 'Paired financial outcome differs')
    results["high"]["seconds"] += row["duration_seconds"]
    return {"case_id": row["case_id"], "run": row["run"], **results}


def _paired_metrics(pairs):
    metrics = {}
    for route in ("express", "high"):
        seconds = [p[route]["seconds"] for p in pairs]
        metrics[route] = {"mean_seconds": statistics.mean(seconds), "median_seconds": statistics.median(seconds),
                          "p95_seconds": percentile(seconds, .95)}
    return metrics


def _paired_report(reference, observations, pairs, output):
    metrics = _paired_metrics(pairs)
    passed = all(metrics["express"][key] < metrics["high"][key] for key in ("median_seconds", "p95_seconds"))
    paired_ids = {(p["case_id"], p["run"]) for p in pairs}
    comparable = [r for r in observations if (r["case_id"], r["run"]) in paired_ids]
    responses = [response for r in comparable for response in r.get("api_responses", [])]
    write_private_json(output / "paired-rows.private.json", pairs)
    return {"paired_cases": len({p["case_id"] for p in pairs}), "observations": len(pairs), "same_financial_outcomes": True,
            "native_baseline_cases": reference["cases"], "native_baseline_observations": reference["observations"],
            "baseline_caveman_sha256": reference["identity"]["caveman_sha256"], "metrics": metrics, "speed_gate_passed": passed,
            "scope": "native high+tools+Caveman read/check conversation + common warmed fake executor, including immediate fake limit fill/protection; excludes process start, live exchange/fill waiting and subsequent model-driven trade/notification calls",
            "express_model_calls": 0, "baseline_model_calls": sum(len(r.get("request_attempts", [])) for r in comparable),
            "baseline_usage_totals": {key: sum(response.get(key, 0) for response in responses) for key in
                ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")},
            "baseline_quota_usd_estimate": sum(r.get("quota_usd_estimate", 0) for r in comparable),
            "native_baseline_all_model_calls": reference["model_request_attempts"],
            "native_baseline_all_quota_usd_estimate": reference["quota_usd_estimate"],
            "real_order_requests": 0, "dex_cases": "covered by interpretation grading and existing DEX regression suite, excluded from this simple fake execution fixture"}


async def paired_fake_benchmark(cases, baseline, output):
    """Native read/check interpretation followed by the same isolated executor."""
    sys.path.insert(0, str(ROOT / "tests"))
    from aqla_agent.source_replay import canonical_symbol

    reference = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
    INVARIANTS.assertTrue(reference['all_passed'] and reference['identity']['native_auto_load'] == ['caveman', 'aqla-master'])
    observations = [json.loads(p.read_text(encoding="utf-8")) for p in baseline.glob("*.private.json") if p.stat().st_size]
    by_id = {case["id"]: case for case in cases}
    pairs = []
    for row in observations:
        if ":" in (canonical_symbol(row["interpretation"].get("symbol")) or ""):
            continue
        pairs.append(await _paired_case(by_id[row["case_id"]], row, output, len(pairs)))
    INVARIANTS.assertTrue(pairs and len(pairs) == len({p['case_id'] for p in pairs}) * 3)
    return _paired_report(reference, observations, pairs, output)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Completed native high+tools+Caveman results for a paired fake execution replay")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = payload["cases"]
    manifest = payload.get("protocol", payload.get("manifest"))
    INVARIANTS.assertTrue(manifest and manifest['dataset_sha256'] == digest(cases), 'Frozen originals/reference hash changed')
    identity = {"template_version": VERSION, "dataset_sha256": digest(cases),
                "source_sha256": digest([(p.relative_to(ROOT).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in sorted((ROOT / "src").rglob("*.py"))]), "model_calls": 0, "order_requests": 0}
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    seal = args.output / "identity.json"
    if seal.exists():
        INVARIANTS.assertTrue(json.loads(seal.read_text(encoding='utf-8')) == identity, 'Frozen replay implementation changed')
    else:
        write_private_json(seal, identity)
    rows = [evaluate(case) for case in cases]
    durations = [row["duration_seconds"] for row in rows]
    report = {"identity": identity, "cases": len(rows), "accepted": sum(row["accepted"] for row in rows),
              "passed": sum(row["passed"] for row in rows), "all_passed": all(row["passed"] for row in rows),
              "mean_seconds": statistics.mean(durations), "p95_seconds": percentile(durations, .95),
              "latency_scope": "whole-template parse plus common original evidence/semantic check; no exchange I/O",
              "model_calls": 0, "order_requests": 0,
              "failures": [{"case_id": row["case_id"], "errors": row["errors"]} for row in rows if not row["passed"]]}
    write_private_json(args.output / "rows.private.json", rows)
    if args.baseline:
        report["paired_benchmark"] = asyncio.run(paired_fake_benchmark(cases, args.baseline, args.output))
        report["all_passed"] &= report["paired_benchmark"]["speed_gate_passed"]
    write_private_json(args.output / "report.json", report)
    print(json.dumps(report))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
