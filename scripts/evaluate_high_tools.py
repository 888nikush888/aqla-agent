"""Native high + production MCP tools, private historical inputs, no exchange access.

Run with the pinned Hermes interpreter. Model credentials belong in the isolated
process environment; this script never loads a production home or credentials.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aqla_agent.decision_evaluation import case_context, write_private_json  # noqa: E402
from aqla_agent.install import DEFAULT_MODEL, DEFAULT_MODEL_URL  # noqa: E402
from aqla_hyperliquid_mcp.models import Interpretation, digest  # noqa: E402


def source_digest():
    files = sorted((ROOT / "src").rglob("*.py")) + [Path(__file__)]
    return digest([(str(p.relative_to(ROOT)).replace("\\", "/"), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files])


def native_worker(args, *, model_url=DEFAULT_MODEL_URL, model_key=None, local_fixture=False, retry_sleep=time.sleep):
    import yaml

    fixture_path = args.worker_fixture.resolve()
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    directory = fixture_path.parent
    home = directory / "hermes-home"
    home.mkdir(mode=0o700, exist_ok=True)
    os.environ.update(HERMES_HOME=str(home), HERMES_SKIP_CONFIG_MIGRATION="1", HERMES_SKIP_UPDATE_CHECK="1",
                      HERMES_STREAM_RETRIES="0")
    sys.path.insert(0, str(args.hermes_source))
    allowed_host = "127.0.0.1" if local_fixture else "opencode.ai"
    assert local_fixture or model_url == DEFAULT_MODEL_URL
    config = {"model": {"default": DEFAULT_MODEL, "provider": "custom", "base_url": model_url},
              "agent": {"stream": False, "api_max_retries": 1, "auto_recovery_cycles": 0},
              "providers": {"custom": {"models": {DEFAULT_MODEL: {"timeout_seconds": 120, "stale_timeout_seconds": 120}}}},
              "compression": {"enabled": False}, "plugins": {"enabled": []}, "fallback_providers": [],
              "display": {"tool_progress": "off"}, "tools": {"tool_search": {"enabled": False}},
              "mcp_servers": {"aqla": {"command": str(args.mcp_python),
                  "args": ["-m", "aqla_agent.source_replay", str(fixture_path)], "cwd": str(ROOT),
                  "trust": "full", "supports_parallel_tool_calls": False,
                  "env": {"PYTHONPATH": str(ROOT / "src"), "HERMES_MODEL_API_KEY": ""}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    from hermes_constants import parse_reasoning_effort
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import registry

    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.tool_outputs import register_reader
    from aqla_agent.tool_runtime import GUIDANCE
    from aqla_agent.usage_metrics import RUN, UsageRun

    undo = install_strict_profile()
    register_reader(SimpleNamespace(register_tool=registry.register))
    names = discover_mcp_tools()
    assert any(n.endswith("source_check_interpretation") for n in names)
    allowed_tools = {"mcp__aqla__source_context", "mcp__aqla__source_context_batch", "mcp__aqla__source_check_interpretation",
                     "mcp__aqla__get_prompt", "mcp__aqla__list_prompts", "mcp__aqla__list_resources", "mcp__aqla__read_resource",
                     "aqla_read_tool_result", "tool_search", "tool_describe", "tool_call"}
    measured = UsageRun("source_quality_replay")
    tool_events = []
    record_tool = measured.tool

    def observe_tool(name, content, error, duration=None):
        tool_events.append({"name": name, "error": bool(error)})
        record_tool(name, content, error, duration)

    measured.tool = observe_tool
    token = RUN.set(measured)
    requests = []
    agent = None
    result = None
    started = time.monotonic()
    try:
        agent = AIAgent(model=DEFAULT_MODEL, provider="custom", base_url=model_url,
            api_key=model_key or os.environ["HERMES_MODEL_API_KEY"], enabled_toolsets=["mcp-aqla"],
            fallback_model=[], session_id="high-tools-" + fixture["session_id"],
            reasoning_config=parse_reasoning_effort("high"), max_iterations=12,
            skip_context_files=True, skip_memory=True, skip_background_review=True,
            quiet_mode=True, save_trajectories=False, session_db=None,
            request_overrides={"stream": False, "max_tokens": 32768})
        agent.client.max_retries = 0
        # The evaluation owns its three attempts and 15/30s backoff; native's
        # extra last-primary rung would otherwise add an unplanned fourth try.
        agent._try_recover_primary_transport = lambda *_args, **_kwargs: False

        def guard(request):
            assert request.method == "POST" and request.url.host == allowed_host
            assert request.url.path.endswith("/chat/completions")
            body = json.loads(request.content)
            assert body["model"] == DEFAULT_MODEL and body.get("reasoning_effort") == "high"
            assert {t["function"]["name"] for t in body.get("tools", [])} <= allowed_tools
            stamp = datetime.now(UTC)
            peak = stamp.weekday() < 5 and (1 <= stamp.hour < 4 or 6 <= stamp.hour < 10)
            requests.append({"started_utc": stamp.isoformat(), "effort": "high", "price_multiplier": 2 if peak else 1})

        create_client = agent._create_request_openai_client

        def guarded_client(**kwargs):
            client = create_client(**kwargs)
            client.max_retries = 0
            hooks = client._client.event_hooks.setdefault("request", [])
            if guard not in hooks:
                hooks.append(guard)
            return client

        agent._create_request_openai_client = guarded_client
        agent.client._client.event_hooks.setdefault("request", []).append(guard)
        instruction = (fixture["master"] + "\n\n" + GUIDANCE + "\nHistorical quality replay only. "
            "The originals retain original timestamps and historical metadata. Classify communication intent AS OF the anchor publication; "
            "Python alone owns age, ownership, risk and execution permission. No trade, operator action or live write is available. "
            "Read source_context(view=decision), read the provided interpretation contract, and use source_check_interpretation. "
            "Check verification.intent, verification.values and verification.setup against the entire original. "
            "When valid=true, finish briefly; your LAST checked interpretation is the evaluated result. "
            "Do not stop with an unchecked answer. Correct specific tool errors, at most three drafts. "
            "Never use another model. Expected answers are unavailable to you.")
        for attempt in range(3):
            result = agent.run_conversation(user_message="Process historical source anchor " + fixture["packet"]["anchor_event_id"],
                                            system_message=instruction)
            if not result.get("error") or attempt == 2:
                break
            # Only transport recovery may repeat a turn; a semantic failure never does.
            error = str(result.get("error", "")).lower()
            if not any(s in error for s in ("connection", "timeout", "rate limit", "temporar", "503", "502", "empty stream")):
                break
            retry_sleep(15 * 2 ** attempt)
    finally:
        if agent is not None:
            agent.close()
        shutdown_mcp_servers()
        RUN.reset(token)
        undo()
    checks_path = directory / "checks.private.json"
    checks = json.loads(checks_path.read_text(encoding="utf-8")) if checks_path.exists() else []
    return {"completed": bool(result is not None and result.get("completed") is True and not result.get("error")
                               and not result.get("failed") and not result.get("interrupted") and not result.get("partial")),
            "native_exit_reason": result.get("turn_exit_reason") if result else None,
            "served_model": result.get("served_model") if result else None, "checks": checks,
            "draft_count": len(checks), "duration_seconds": time.monotonic() - started,
            "api_responses": measured.api, "request_attempts": requests,
            "tool_events": tool_events,
            "tool_metrics": measured.tools, "forbidden_tools": sorted(set(measured.tools) - allowed_tools),
            "order_requests": 0, "model": DEFAULT_MODEL, "effort": "high"}


def grade(case, observed):
    from aqla_agent.source_replay import mismatches
    checks = observed.get("checks", [])
    def check(row):
        if not row.get("valid"):
            return ["tool_validation"]
        return mismatches(Interpretation.model_validate(row["interpretation"]), case["expected"])
    errors = check(checks[-1]) if checks else ["no_checked_interpretation"]
    if not observed.get("completed"):
        errors.append("native_turn_incomplete")
    if observed.get("forbidden_tools"):
        errors.append("forbidden_tool")
    submissions = [event for event in observed.get("tool_events", []) if event["name"].endswith("source_check_interpretation")]
    if len(submissions) > 3:
        errors.append("draft_limit_exceeded")
    return {"passed": not errors, "first_draft_passed": bool(checks and submissions and not submissions[0]["error"]
            and checks[0].get("submitted_valid") and not check(checks[0])),
            "mismatch_fields": errors, "interpretation": checks[-1]["interpretation"] if checks else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--mcp-python", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--master", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runs", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--worker-fixture", type=Path)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.worker_fixture:
        try:
            observed = native_worker(args)
        except Exception as error:
            observed = {"completed": False, "checks": [], "worker_error_type": type(error).__name__, "order_requests": 0}
        write_private_json(args.worker_fixture.parent / "observed.private.json", observed)
        return
    assert args.dataset and args.master and args.output
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = payload["cases"]
    master = args.master.read_text(encoding="utf-8")
    manifest = payload.get("protocol", payload.get("manifest"))
    assert digest(cases) == manifest["dataset_sha256"]
    identity = {"version": "high-tools-native-v2", "source_sha256": source_digest(),
                "dataset_sha256": digest(cases), "master_sha256": hashlib.sha256(master.encode()).hexdigest(),
                "effort": "high", "model": DEFAULT_MODEL, "draft_limit": 3, "transport_attempt_limit": 3,
                "request_timeout_seconds": 120, "transport_wait_seconds": [15, 30],
                "context_mode": "all_retained_original_messages_no_cached_labels",
                "runs": args.runs, "workers": args.workers, "order_tools_available": False}
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    seal = args.output / "identity.json"
    if seal.exists():
        assert json.loads(seal.read_text(encoding="utf-8")) == identity, "Frozen experiment changed"
    else:
        write_private_json(seal, identity)
    selected = [c for c in cases if not args.case_id or c["id"] in args.case_id]
    assert selected and (not args.case_id or set(args.case_id) <= {c["id"] for c in cases})
    schedule = [(c, run) for run in range(args.runs) for c in sorted(selected, key=lambda c: digest(c["id"]))]
    budget_lock = threading.Lock()
    budget_used = [0.0]

    def cost(observed):
        total = 0.0
        multiplier = max((request["price_multiplier"] for request in observed.get("request_attempts", [])), default=1)
        for row in observed.get("api_responses", []):
            if row.get("usage_available"):
                # Conservatively use the highest observed window for a case crossing peak time.
                total += multiplier * (row["input_tokens"] * .15 + row["cache_read_tokens"] * .003 + row["output_tokens"] * .60) / 1_000_000
        return total

    def evaluate(item):
        case, run = item
        directory = args.output / digest([case["id"], run])[:24]
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "result.private.json"
        if path.exists():
            row = json.loads(path.read_text(encoding="utf-8"))
            with budget_lock:
                budget_used[0] += row.get("quota_usd_estimate", 0)
            return row
        with budget_lock:
            if budget_used[0] >= 5:
                return {"case_id": case["id"], "run": run, "passed": False, "first_draft_passed": False,
                        "mismatch_fields": ["quota_estimate_limit"], "order_requests": 0}
        fixture = {"packet": case["packet"], "context_messages": case_context(case)["messages"],
                   "master": master, "session_id": digest([case["id"], run])[:24]}
        fixture_path = directory / "fixture.private.json"
        write_private_json(fixture_path, fixture)
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--hermes-source", str(args.hermes_source),
                   "--mcp-python", str(args.mcp_python), "--worker-fixture", str(fixture_path)]
        process_started = time.monotonic()
        with (directory / "native-private.log").open("w", encoding="utf-8") as log:
            try:
                subprocess.run(command, stdout=log, stderr=log, timeout=1800, check=False)
            except subprocess.TimeoutExpired:
                write_private_json(directory / "observed.private.json", {"completed": False, "checks": [], "worker_timeout": True})
        observed_path = directory / "observed.private.json"
        observed = json.loads(observed_path.read_text(encoding="utf-8")) if observed_path.exists() else {"completed": False, "checks": []}
        row = {"case_id": case["id"], "run": run, **observed, **grade(case, observed),
               "total_case_seconds": time.monotonic() - process_started, "quota_usd_estimate": cost(observed)}
        with budget_lock:
            budget_used[0] += row["quota_usd_estimate"]
        write_private_json(path, row)
        print(json.dumps({k: row.get(k) for k in ("case_id", "run", "passed", "first_draft_passed", "draft_count", "duration_seconds", "mismatch_fields", "worker_error_type")}), flush=True)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        rows = list(executor.map(evaluate, schedule))
    passed = sum(row["passed"] for row in rows)
    responses = [response for row in rows for response in row.get("api_responses", [])]
    durations = sorted(row["duration_seconds"] for row in rows if "duration_seconds" in row)
    total_durations = sorted(row["total_case_seconds"] for row in rows if "total_case_seconds" in row)
    report = {"identity": identity, "cases": len(selected), "observations": len(rows), "passed": passed,
              "first_draft_passed": sum(row["first_draft_passed"] for row in rows),
              "all_passed": passed == len(rows), "three_runs_complete": args.runs == 3 and len(selected) == len(cases),
              "model_request_attempts": sum(len(row.get("request_attempts", [])) for row in rows),
              "mean_seconds": statistics.mean(durations) if durations else None,
              "median_seconds": statistics.median(durations) if durations else None,
              "mean_total_case_seconds": statistics.mean(total_durations) if total_durations else None,
              "latency_scope": "duration_seconds: native conversation including API/tools; total_case_seconds also includes worker/MCP startup",
              "usage_totals": {key: sum(response.get(key, 0) for response in responses) for key in
                  ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")},
              "quota_usd_estimate": sum(row.get("quota_usd_estimate", 0) for row in rows),
              "quota_measurement": "published-price estimate, not subscription console debit; failed-call usage may be unknown",
              "runs": [{"run": run, "passed": sum(row["passed"] for row in rows if row["run"] == run), "cases": len(selected)} for run in range(args.runs)],
              "order_requests": 0, "failures": [{"case_id": row["case_id"], "run": row["run"], "fields": row["mismatch_fields"]} for row in rows if not row["passed"]]}
    write_private_json(args.output / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "failures"}), flush=True)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
