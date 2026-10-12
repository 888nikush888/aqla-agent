"""Export real MCP schemas and exercise pinned Hermes tool-search validation.

No model, exchange, Telegram, credentials or production state is used.
"""
import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--mcp-python", required=True)
    args = parser.parse_args()
    home = Path(args.home).absolute()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_SKIP_UPDATE_CHECK"] = "1"
    (home / "config.yaml").write_text("mcp_servers: {}\nplugins: {enabled: []}\n", encoding="utf-8")
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(Path(args.hermes_source)))
    env = dict(os.environ)
    env.pop("HL_PRIVATE_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join([str(project / "src"), env.get("PYTHONPATH", "")])
    exported = subprocess.run([args.mcp_python, "-B", "-", str(home)], input='''
import asyncio
import json
import sys
from pathlib import Path
from aqla_hyperliquid_mcp.models import Settings
from aqla_hyperliquid_mcp.server import create_server, close_server
async def export():
    settings = Settings(network="testnet", account="0x" + "1" * 40,
        state_path=Path(sys.argv[1]) / "synthetic-schema.sqlite3")
    server = create_server(settings)
    try:
        print(json.dumps([tool.model_dump(mode="json", by_alias=True) for tool in await server.list_tools()
            if tool.name in ("interpret_source", "interpret_source_batch")]))
    finally:
        await close_server(server)
asyncio.run(export())
''', text=True, capture_output=True, check=True, env=env)
    schemas = json.loads(exported.stdout)
    assert len(schemas) == 2
    from tools.registry import registry
    from tools.tool_search import dispatch_tool_describe
    from tools.tool_search_validation import validate_deferred_call_args

    from aqla_agent.hermes_runtime import install_strict_profile

    undo = install_strict_profile()
    try:
        definitions = []
        for tool in schemas:
            name = "mcp__aqla__" + tool["name"]
            schema = {"name": name, "description": tool["description"], "parameters": tool["inputSchema"]}
            registry.register(name=name, toolset="mcp-aqla", schema=schema,
                handler=lambda _args, **_: (_ for _ in ()).throw(AssertionError("validation never dispatches")))
            definitions.append({"type": "function", "function": schema})
        described = json.loads(dispatch_tool_describe({"names": [row["function"]["name"] for row in definitions]},
            current_tool_defs=definitions))
        assert set(described["tools"]) == {"mcp__aqla__interpret_source", "mcp__aqla__interpret_source_batch"}
        source = "synthetic-source"
        value = {"kind": "new_signal", "source_refs": [source], "symbol": "AXS", "side": "long", "leverage": 35,
            "entry": "market", "take_profits": [1.3, 1.33], "evidence": [
                {"field": field, "source_id": source, "quote": quote} for field, quote in (
                    ("symbol", "AXS"), ("side", "LONG"), ("leverage", "X35"),
                    ("take_profits.0", "1.3;"), ("take_profits.1", "1,33"))]}
        name = "mcp__aqla__interpret_source"
        valid = {"event_id": "synthetic-event", "interpretation": value}
        assert validate_deferred_call_args(name, valid) is None
        bad = copy.deepcopy(valid)
        bad["interpretation"]["evidence"][-1]["field"] = "take_profits"
        error = validate_deferred_call_args(name, bad)
        assert error is not None and "take_profits.0" in error and "take_profits.1" in error
        assert "NOT invoked" in error
        bad = copy.deepcopy(valid)
        bad["interpretation"]["take_profits"][-1] = "1,33"
        error = validate_deferred_call_args(name, bad)
        assert error is not None and "dot-decimal" in error and "NOT invoked" in error
        batch = {"items": [{"event_id": "synthetic-event", "kind": "ambiguous", "source_refs": [source],
            "reason": "Percentage basis unclear", "reason_codes": ["percentage_basis"],
            "source_read_receipt": "synthetic-receipt", "evidence": [
                {"field": "classification", "source_id": source, "quote": "50% / 100%"}]}]}
        assert validate_deferred_call_args("mcp__aqla__interpret_source_batch", batch) is None
    finally:
        undo()
    print(json.dumps({"real_mcp_schema": True, "native_tool_describe": True,
        "indexed_price_guidance": True, "dot_decimal_validation": True, "batch_reason_codes": True,
        "model_or_exchange_calls": 0}))


if __name__ == "__main__":
    main()
