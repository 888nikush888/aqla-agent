"""Native archive/prune and owned receipt recovery; no model or exchange calls."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    home = Path(args.home).absolute()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    sys.path[:0] = [str(Path(args.hermes_source).absolute()), str(Path(__file__).resolve().parents[1] / "src")]
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.tool_outputs import OutputStore, capture_scope, register_reader
    undo = install_strict_profile()
    from agent.context_compressor import ContextCompressor
    from hermes_constants import hermes_home_key
    from hermes_state import SessionDB
    from tools.registry import registry

    register_reader(SimpleNamespace(register_tool=lambda **kwargs: registry.register(**kwargs, scope=hermes_home_key())))
    db = SessionDB(home / "state.db")
    db.create_session("compression-fixture", "telegram")
    messages, bodies = [], []
    for index in range(12):
        identity = "old-call-" + str(index)
        body = json.dumps({"stored_original": str(index) + "ä" * 14000})
        bodies.append(body)
        messages.extend([{"role": "user", "content": "Old completed request " + str(index)},
                         {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function", "function": {"name": "mcp__aqla__status", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": identity, "tool_name": "mcp__aqla__status", "content": body},
                         {"role": "assistant", "content": "Completed " + str(index)}])
    # Current work and operator intent are deliberately in the protected tail.
    messages.extend({"role": "user", "content": "Current intent " + str(index)} for index in range(19))
    current = {"event_id": "current-anchor", "source_read_receipt": "synthetic-current-receipt", "source": "Original current ROE 25% / 50%"}
    messages.append({"role": "tool", "tool_call_id": "current-call", "content": json.dumps(current), "tool_name": "mcp__aqla__source_context"})
    db.append_messages_batch("compression-fixture", messages)
    live = db.get_messages_as_conversation("compression-fixture")
    compressor = ContextCompressor(model="deepseek/deepseek-v4.1-flash", config_context_length=1048576,
                                   threshold_tokens_cap=128000, proactive_prune_tokens=48000,
                                   protect_last_n=20, proactive_prune_min_result_chars=8000,
                                   proactive_prune_min_reclaim_tokens=4096, quiet_mode=True)
    compressor._session_db, compressor._session_id = db, "compression-fixture"
    try:
        with capture_scope("compression-fixture", "native-prune", home):
            pruned, count = compressor.prune_tool_results_only(live, current_tokens=80000)
        assert count > 0 and len(json.dumps(pruned)) < len(json.dumps(live))
        assert [row["content"] for row in pruned[-20:]] == [row["content"] for row in live[-20:]]
        assert compressor.threshold_tokens == 128000
        # The reported production context must no longer hit the soft trigger.
        assert compressor.should_compress_info(74537) == (False, None)
        recovered = 0
        for row in pruned:
            if row.get("role") != "tool":
                continue
            value = json.loads(row["content"])
            if "output_id" in value:
                restored = OutputStore(home).receipt(value["output_id"], "compression-fixture")["content"]
                assert restored in bodies
                recovered += 1
        assert recovered > 0
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM messages WHERE content=?", (bodies[3],)).fetchone()[0] > 0
        assert [row.get("tool_calls") for row in pruned] == [row.get("tool_calls") for row in live]
        print(json.dumps({"native_archived_prune": True, "protected_tail_unchanged": True,
                          "canonical_tool_arguments_unchanged": True, "lossless_owned_recovery": True,
                          "threshold_tokens": compressor.threshold_tokens, "pruned_results": count}))
    finally:
        db.close()
        undo()


if __name__ == "__main__":
    main()
