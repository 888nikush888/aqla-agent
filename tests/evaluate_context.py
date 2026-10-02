"""Opt-in real Hermes model evaluation. No bot, MCP or exchange tools are enabled."""
import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True, help="New, empty evaluation home")
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-url", required=True)
    parser.add_argument("--key-env", default="HERMES_MODEL_API_KEY")
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    if not key:
        raise SystemExit("Set the requested model key environment variable first")
    root = Path(__file__).resolve().parents[1]
    home = Path(args.home).resolve()
    if home.exists() and any(home.iterdir()):
        raise SystemExit("Evaluation home must be new and empty")
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_SKIP_UPDATE_CHECK"] = "1"
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(Path(args.hermes_source).resolve()))
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_hyperliquid_mcp.models import Interpretation, SourceEvent, now_ms
    from aqla_hyperliquid_mcp.rules import verify_evidence
    undo = install_strict_profile()
    from run_agent import AIAgent

    cases = json.loads((root / "tests" / "fixtures" / "context_cases.json").read_text(encoding="utf-8"))
    instruction = (root / "skills" / "aqla-master" / "SKILL.md").read_text(encoding="utf-8")
    instruction += "\nEvaluation only: classify the last message using the supplied context. Return exactly one JSON Interpretation, no Markdown or tools. Symbol is canonical (BTC rather than $BTC/USDT). Bound edits are updates. Schema:\n" + json.dumps(Interpretation.model_json_schema())
    reports = []
    try:
        for case in cases:
            agent = AIAgent(model=args.model, provider="custom", base_url=args.model_url, api_key=key,
                            enabled_toolsets=[], max_iterations=1, session_db=None, ephemeral_system_prompt=instruction,
                            skip_memory=True, skip_background_review=True, quiet_mode=True, save_trajectories=False)
            events = [SourceEvent(chat_id=case["id"], message_id=i + 1, sent_at=now_ms(), text=text) for i, text in enumerate(case["messages"])]
            context = {"messages": [{"source_id": event.source_id, "text": event.text} for event in events], "open_trade": case["open_trade"]}
            def denied(*_, **__):
                raise RuntimeError("All tools are disabled in this evaluation")
            agent.tools = []
            agent.valid_tool_names = set()
            agent._execute_tool_calls = denied
            try:
                response = agent.run_conversation(user_message=json.dumps(context, ensure_ascii=False))["final_response"].strip()
                if response.startswith("```"):
                    response = response.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                result = Interpretation.model_validate_json(response)
                verify_evidence(result, {event.event_id: {"source_id": event.source_id, "source": event.model_dump(mode="json")} for event in events})
                for field, expected in case["expected"].items():
                    actual = getattr(result, field)
                    if field in ("stop_loss", "reduce_percent"):
                        assert actual == Decimal(expected), field
                    elif field == "take_profits":
                        assert actual == [Decimal(value) for value in expected], field
                    else:
                        assert actual == expected, field
                reports.append({"id": case["id"], "passed": True})
            except Exception as error:
                reports.append({"id": case["id"], "passed": False, "error_type": type(error).__name__})
            finally:
                agent.close()
            print(json.dumps(reports[-1]), flush=True)
        (home / "evaluation.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    finally:
        undo()
    if not all(row["passed"] for row in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
