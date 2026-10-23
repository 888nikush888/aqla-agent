"""Reproducible response-size comparison; optional same-model, order-free replay."""

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from aqla_hyperliquid_mcp.models import require_first

ROOT = Path(__file__).resolve().parents[1]
BASE = "1c4e4ce674920a7cac558e23ddedffd9623c5b00"


async def packets():
    import httpx
    from conftest import FakeExchange
    from eth_account import Account
    from test_notifications import lifecycle_fixture

    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.models import Settings, SourceEvent, now_ms
    from aqla_hyperliquid_mcp.server import close_server, create_server
    from aqla_hyperliquid_mcp.state import State

    with tempfile.TemporaryDirectory() as directory:
        account = Account.create()
        private = Path(directory) / "synthetic.secret"
        private.write_text(account.key.hex())
        integration = Path(directory) / "synthetic-integration.secret"
        integration.write_text("synthetic-benchmark-key-not-a-user-credential" * 2)
        settings = Settings(network="testnet", account=account.address, private_key_file=private,
                            integration_key_file=integration, sources=["channel"], state_path=Path(directory) / "state.json")
        state = State(settings)
        fake = FakeExchange(account.address)
        exchange = Hyperliquid(settings, state, httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(fake.handle)))
        server = create_server(settings, state=state, exchange=exchange)
        async def read(name, **arguments):
            response = await server.call_tool(name, arguments)
            return response.structured_content or json.loads(response.content[0].text)
        try:
            state.update(lambda data: lifecycle_fixture(data, settings, count=3, fills=20))
            # Original text remains long and verbatim. No content is classified by a heuristic.
            from aqla_agent.trading import Trading
            trading = Trading(exchange, state)
            sources = [SourceEvent(chat_id="channel", message_id=100 + i, sent_at=now_ms() + i,
                                   text=(f"Результат {i}: цель достигнута, нового входа нет. " * 35)) for i in range(20)]
            for source in sources:
                trading.ingest(source)
            original = [await read("source_context", event_id=source.event_id) for source in sources[:8]]
            reduced = await read("source_context_batch", event_ids=[source.event_id for source in sources[:8]])
            notification = await read("notifications_read")
            rendered = await read("notifications_read", view="rendered")
            if not (notification['telegram_view'] == rendered['telegram_view']):
                raise AssertionError
            for item in reduced["items"]:
                for identity in item["message_ids"]:
                    if not (reduced['messages'][identity]['source']['text'] == require_first(row['source']['text'] for context in original for row in context['messages'] if row['event_id'] == identity)):
                        raise AssertionError
            pairs = {"status": (await read("status"), await read("status", view="summary")),
                     "eight_source_contexts": (original, reduced), "notifications": (notification, rendered)}
            output = {}
            for name, (old, new) in pairs.items():
                old_bytes = len(json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode())
                new_bytes = len(json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode())
                output[name] = {"before_bytes": old_bytes, "after_bytes": new_bytes,
                                "reduction_percent": round(100 * (old_bytes - new_bytes) / old_bytes, 2)}
            output["measurement"] = "serialized_UTF8_bytes_not_provider_tokens"
            output["original_source_bodies_equal"] = True
            return output
        finally:
            await close_server(server)
            state.close()


def replay(hermes_source, output_home, key, *, limit=8, selected_ids=None):
    """Fresh native agents, no tools/state database/trajectories or real orders."""
    sys.path.insert(0, str(hermes_source))
    import yaml

    os.environ["HERMES_HOME"] = str(output_home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    output_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    (output_home / "config.yaml").write_text(yaml.safe_dump({"skills": {"auto_load": []},
        "auxiliary": {"background_review": {"enabled": False}}, "compression": {"threshold_tokens": 64000,
        "proactive_prune_tokens": 48000}, "agent": {"reasoning_effort": "ultra"},
        "providers": {"openrouter": {"request_timeout_seconds": 90}}}))
    from run_agent import AIAgent

    from aqla_agent.efficiency import decision_context
    from aqla_hyperliquid_mcp.models import AqlaError, Interpretation, SourceEvent, now_ms
    from aqla_hyperliquid_mcp.rules import verify_evidence

    before = subprocess.check_output(["git", "show", BASE + ":skills/aqla-master/SKILL.md"], cwd=ROOT).decode()
    after = (ROOT / "skills/aqla-master/SKILL.md").read_text(encoding="utf-8")
    selected = {"de-long", "comma-decimals", "reply-completes", "break-even", "edit-update", "result-report", "missing-direction", "injection"}
    if selected_ids:
        selected = set(selected_ids)
    cases = [case for case in json.loads((ROOT / "tests/fixtures/context_cases.json").read_text(encoding="utf-8")) if case["id"] in selected][:limit]
    cases += [{"id": "percent-roe", "messages": ["STRK SHORT x20. Entry 0.1, SL 0.102, TP +50% / +100%."],
               "expected": {"kind": "new_signal", "symbol": "STRK", "side": "short", "leverage": "20", "take_profit_unit": "roe_percent", "take_profits": ["50", "100"]}, "open_trade": None}]
    instruction = "\nReplay only. Interpret the anchor using original source bodies. Return one JSON Interpretation, no Markdown/tools. Context anchor is event or anchor_event_id; messages may be a keyed map. source_refs/evidence.source_id must use source_id, never event_id. Non-actionable classes require an evidence field classification with an exact anchor quote and a reason. No exchange action is possible. Schema:\n" + json.dumps(Interpretation.model_json_schema())
    results = []
    for case in cases:
        events = [SourceEvent(chat_id=case["id"], message_id=i + 1, sent_at=now_ms(), text=text) for i, text in enumerate(case["messages"])]
        rows = [{"event_id": event.event_id, "source_id": event.source_id, "source": event.model_dump(mode="json"), "status": "pending"} for event in events]
        full = {"event": rows[-1], "messages": rows, "trades": [], "open_trade": case["open_trade"]}
        projected = decision_context(full)
        reports = []
        for mode, master, context in (("before", before, full), ("after", after, projected)):
            print(json.dumps({"replay_case": case["id"], "mode": mode, "started": True}), file=sys.__stdout__, flush=True)
            agent = AIAgent(model="deepseek/deepseek-v4.1-flash", provider="openrouter", base_url="https://openrouter.ai/api/v1", api_key=key,
                            enabled_toolsets=[], max_iterations=1, session_db=None, ephemeral_system_prompt=master + instruction,
                            reasoning_config={"enabled": True, "effort": "ultra"}, skip_memory=True, skip_background_review=True,
                            quiet_mode=True, save_trajectories=False)
            result = None
            try:
                response = agent.run_conversation(user_message=json.dumps(context, ensure_ascii=False))["final_response"].strip()
                if response.startswith("```"):
                    response = response.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                result = Interpretation.model_validate_json(response)
                verify_evidence(result, {row["event_id"]: row for row in rows})
                if result.kind in ("report", "unrelated", "incomplete", "ambiguous"):
                    if not (result.reason.strip() and any(proof.field == 'classification' and proof.source_id == rows[-1]['source_id'] and (proof.quote in rows[-1]['source']['text']) for proof in result.evidence)):
                        raise AssertionError('classification_proof')
                for field, expected in case["expected"].items():
                    actual = getattr(result, field)
                    if isinstance(actual, Decimal):
                        expected = Decimal(expected)
                    elif field == "take_profits":
                        expected = [Decimal(value) for value in expected]
                    if not (actual == expected):
                        raise AssertionError(field)
                reports.append({"mode": mode, "passed": True, "kind": result.kind, **{name: getattr(agent, "session_" + name, None)
                                for name in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "api_calls")}})
            except Exception as error:
                reports.append({"mode": mode, "passed": False, "error_type": type(error).__name__,
                                **({"validation_reason": str(error)[:200]} if isinstance(error, (AqlaError, AssertionError)) else {}),
                                **({"synthetic_interpretation": result.model_dump(mode="json")} if result is not None else {})})
            finally:
                agent.close()
        results.append({"case": case["id"], "results": reports})
        print(json.dumps(results[-1]), file=sys.__stdout__, flush=True)
    non_action = {"report", "unrelated", "incomplete", "ambiguous"}
    for row in results:
        previous, current = row["results"]
        # Preserve exact fixture results wherever the baseline meets them.
        # Existing ambiguous/incomplete/report label variation is reported,
        # never silently counted as an exact fixture pass.
        before_kind = previous.get("kind", previous.get("synthetic_interpretation", {}).get("kind"))
        after_kind = current.get("kind", current.get("synthetic_interpretation", {}).get("kind"))
        row["existing_non_action_label_variation"] = (not previous["passed"] and not current["passed"]
            and previous.get("validation_reason") == current.get("validation_reason") == "kind"
            and before_kind == after_kind and before_kind in non_action)
        row["no_regression"] = (previous["passed"] and current["passed"]) or row["existing_non_action_label_variation"]
    return {"model": "deepseek/deepseek-v4.1-flash", "reasoning_effort": "ultra", "order_tools_available": False,
            "cases": results, "exact_fixture_passed": all(report["passed"] for row in results for report in row["results"]),
            "passed": all(row["no_regression"] for row in results)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--replay-home", type=Path)
    parser.add_argument("--replay-only", action="store_true")
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--cases", nargs="+")
    args = parser.parse_args()
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        report = {} if args.replay_only else asyncio.run(packets())
        if args.hermes_source:
            if args.replay_home is None or not os.environ.get(args.key_env):
                raise SystemExit("Replay requires an isolated home and a private key environment variable")
            report["model_replay"] = replay(args.hermes_source, args.replay_home, os.environ[args.key_env], selected_ids=args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if report.get("model_replay", {}).get("passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
