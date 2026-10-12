"""Native source-turn cutoff, explicit saved-plan execution and durable retry.

The model is a loopback script and the exchange uses MockTransport exclusively.
--project-root selects the integrated implementation, independently of this test.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import yaml

BOOTSTRAP = '''import json
import sys
import time
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(sys.argv[2]) / "tests"))
from conftest import FakeExchange
from aqla_agent.observations import TemporaryObservation
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import Settings
from aqla_hyperliquid_mcp.server import create_server
from aqla_hyperliquid_mcp.state import State
home = Path(sys.argv[1]).parent
control_path = home / "fixture-control.json"
audit_path = home / "fixture-exchange.json"
def control():
    return json.loads(control_path.read_text())
# Model/default-factory/adapter clocks all use the real models.now_ms path.
time.time_ns = lambda: control()["time_ms"] * 1000000
settings = Settings.load(sys.argv[1])
state = State(settings)
fake = FakeExchange(settings.account)
if audit_path.exists():
    for key, value in json.loads(audit_path.read_text()).items():
        setattr(fake, key, value)
def persist():
    temporary = audit_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({key: getattr(fake, key) for key in
        ("actions", "orders", "fills", "position", "oid")}), encoding="utf-8")
    temporary.replace(audit_path)
def response(request):
    assert request.url.path in ("/info", "/exchange")
    result = fake.handle(request)
    persist()
    return result
persist()
exchange = Hyperliquid(settings, state,
    httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(response)))
original_account = exchange.account_snapshot
async def account(market):
    flags = control()
    if flags["arm_account_fault"] and not flags["fault_consumed"]:
        flags["fault_consumed"] = True
        control_path.write_text(json.dumps(flags), encoding="utf-8")
        raise TemporaryObservation("read_unavailable", "account", "transport")
    return await original_account(market)
exchange.account_snapshot = account
create_server(settings, state=state, exchange=exchange).run(transport="stdio")
'''


def entries(destination):
    audit = json.loads((destination / "fixture-exchange.json").read_text())
    return [row for row in audit["actions"] if row["type"] == "order"
        and any(not order["r"] for order in row["orders"])]


def fixture_control(destination, **changes):
    path = destination / "fixture-control.json"
    value = json.loads(path.read_text())
    value.update(changes)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--mcp-python", required=True)
    args = parser.parse_args()
    project, destination = Path(args.project_root).resolve(), Path(args.destination).resolve()
    os.environ["HERMES_HOME"] = str(destination / "hermes-home")
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_SKIP_UPDATE_CHECK"] = "1"
    os.environ.pop("HL_PRIVATE_KEY", None)
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(Path(args.hermes_source).resolve()))
    from aqla_agent.install import prepare
    from aqla_hyperliquid_mcp.models import Policy, SourceEvent, now_ms

    prepare(project, destination, "12345", "qa-model", "http://127.0.0.1/v1")
    config = yaml.safe_load((destination / "config.yaml").read_text())
    config.update(sources=["12345"], telegram_session_file=None, ranking_enabled=False,
        venues={}, execution_accounts={}, policy=Policy(leverage_mode="signal").model_dump(mode="json"))
    (destination / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(project / "tests"), env.get("PYTHONPATH", "")])
    # Generate only a disposable local signer. Never load a configured key.
    subprocess.run([args.mcp_python, "-B", "-", str(destination)], check=True, text=True, env=env, input='''
from pathlib import Path
import sys
import yaml
from eth_account import Account
home = Path(sys.argv[1])
signer = Account.create()
secret = home / "secrets" / "synthetic-test.secret"
secret.write_text(signer.key.hex())
config_path = home / "config.yaml"
config = yaml.safe_load(config_path.read_text())
config.update(account=signer.address, private_key_file=str(secret))
config_path.write_text(yaml.safe_dump(config))
''')
    initial_time = now_ms()
    (destination / "fixture-control.json").write_text(json.dumps({"time_ms": initial_time,
        "arm_account_fault": False, "fault_consumed": False}), encoding="utf-8")
    bootstrap = destination / "synthetic-entry-retry-mcp.py"
    bootstrap.write_text(BOOTSTRAP, encoding="utf-8")
    native_config_path = destination / "hermes-home" / "config.yaml"
    native = yaml.safe_load(native_config_path.read_text())
    native["mcp_servers"] = {"aqla": native["mcp_servers"]["aqla"]}
    native["mcp_servers"]["aqla"].update(command=str(Path(args.mcp_python).absolute()),
        args=[str(bootstrap), str(destination / "config.yaml"), str(project)], env={"PYTHONPATH": env["PYTHONPATH"]})
    native["model"]["api_key"] = "local-test-only"
    native["agent"]["stream"] = False
    native["display"]["tool_progress"] = "off"
    native_config_path.write_text(yaml.safe_dump(native), encoding="utf-8")

    from hermes_cli.plugins import discover_plugins
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    from aqla_agent.events import EXTENSIONS
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_hyperliquid_mcp.storage import read_snapshot

    undo = install_strict_profile()
    discover_plugins(force=True)
    assert "mcp__aqla__trade_execute" in discover_mcp_tools()
    assert len(EXTENSIONS) == 1
    extension = next(iter(EXTENSIONS))
    original = SourceEvent(chat_id="12345", message_id=42, sent_at=initial_time,
        forward_from="native-owner-forward", text="BTC LONG X35. Entry market. SL 90. TP 110; 120.")
    interpretation = {"kind": "new_signal", "symbol": "BTC", "side": "long", "entry": "market", "leverage": 35,
        "stop_loss": 90, "take_profits": [110, 120], "source_refs": [original.source_id], "evidence": [
            {"field": field, "source_id": original.source_id, "quote": quote} for field, quote in (
                ("symbol", "BTC"), ("side", "LONG"), ("leverage", "X35"), ("stop_loss", "SL 90"),
                ("take_profits.0", "TP 110;"), ("take_profits.1", "120"))]}
    script = {"phase": "preview", "index": 0, "steps": [
        ("source_context", {"event_id": original.event_id}),
        ("interpret_source", {"event_id": original.event_id, "interpretation": interpretation}),
        ("trade_preview", {"event_id": original.event_id, "interpretation": interpretation})]}
    requests, issued, failures = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            message = {"role": "assistant", "content": "Saved plan retained; no further tool request."}
            has_tool = bool(request.get("tools")) and script["index"] < len(script["steps"])
            if has_tool:
                suffix, parameters = script["steps"][script["index"]]
                script["index"] += 1
                issued.append((script["phase"], suffix, parameters))
                available = {tool["function"]["name"] for tool in request["tools"]}
                target = "mcp__aqla__" + suffix
                if target in available:
                    name = target
                else:
                    if "tool_call" not in available:
                        failures.append("Native source schema did not expose the requested tool")
                    name, parameters = "tool_call", {"calls": [{"name": target, "arguments": parameters}]}
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "retry-local-" + str(len(issued)),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(parameters)}}]}
            response = {"id": "retry-local", "object": "chat.completion", "created": initial_time // 1000,
                "model": "qa-model", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if has_tool else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if request.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": index, **row} for index, row in enumerate(delta["tool_calls"])]
                chunk = {**response, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0]["finish_reason"]}]}
                body = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(done) + "\n\ndata: [DONE]\n\n").encode()
            else:
                body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    model = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=model.serve_forever, daemon=True).start()

    async def run():
        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.run_turn import GatewayTurnMixin
        from gateway.session import SessionSource, SessionStore
        from run_agent import AIAgent

        source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345")
        store = SessionStore(destination / "hermes-home" / "sessions", GatewayConfig())
        # This deliberately small synthetic budget reproduces the real cutoff.
        agent = AIAgent(model="qa-model", provider="custom", base_url=f"http://127.0.0.1:{model.server_port}/v1",
            api_key="local-test-only", platform="telegram", enabled_toolsets=["aqla-source"], max_iterations=3,
            session_db=store._db, skip_memory=True, skip_background_review=True, quiet_mode=True, save_trajectories=False)
        history = []
        async def handler(event):
            nonlocal history
            result = await asyncio.to_thread(agent.run_conversation, user_message=event.text, conversation_history=history)
            history = result["messages"]
            return result
        adapter = SimpleNamespace(_message_handler=handler)
        adapter.set_message_handler = lambda callback: setattr(adapter, "_message_handler", callback)
        gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, session_store=store,
            _resolve_enabled_toolsets_for_source=lambda *_: ["skills", "mcp-aqla"])
        extension.boot(gateway)
        with extension.claim_scope(("source", "fixture-schema")):
            enabled, _ = GatewayTurnMixin._resolve_turn_toolsets(gateway, {}, source, "telegram")
            assert enabled == ["aqla-source"]
        forwarded = MessageEvent(text=original.text, source=source, message_type=MessageType.TEXT, message_id="42",
            metadata={}, raw_message={"forward_origin": True, "date": datetime.fromtimestamp(initial_time / 1000, UTC)})
        try:
            assert extension.inbound(forwarded, gateway) is None
            first = await adapter._message_handler(forwarded)
            assert agent.iteration_budget.used == 3 and script["index"] == 3
            assert first["final_response"] and any(not request.get("tools") for request in requests)
            data = read_snapshot(destination / "data" / "state.sqlite3")
            assert len(data["plans"]) == 1 and not data["trades"] and not entries(destination)
            identity, saved = next(iter(data["plans"].items()))
            immutable = saved["plan"]
            assert not saved.get("execution_requested") and saved["event_id"] == original.event_id
            assert immutable["effective_leverage"] == 20
            # A real MCP restart runs backend recovery, but preview alone grants
            # no execution authority, even with a later valid fixture clock.
            shutdown_mcp_servers()
            fixture_control(destination, time_ms=initial_time + 30001)
            assert "mcp__aqla__trade_execute" in discover_mcp_tools()
            assert not entries(destination) and not read_snapshot(destination / "data" / "state.sqlite3")["trades"]
            fixture_control(destination, arm_account_fault=True)
            script.update(phase="execute", index=0, steps=[("trade_execute", {"plan_id": identity})])
            wake = MessageEvent(text="Continue the saved pending plan", source=source, message_id="wake-2", internal=True,
                metadata={"hermes_plugin_injection": True, "hermes_plugin_id": "aqla-events"})
            assert extension.inbound(wake, gateway) is None
            await adapter._message_handler(wake)
            pending = read_snapshot(destination / "data" / "state.sqlite3")
            trade = pending["trades"][immutable["trade_id"]]
            marker = trade["observation_retry"]
            assert trade["status"] == "preparing" and trade["filled_quantity"] == "0"
            assert marker["attempts"] == 1 and marker["authority"] == "execute"
            assert (marker["code"], marker["stage"], marker["category"]) == ("read_unavailable", "account", "transport")
            assert marker["deadline_at"] == immutable["deadline_at"] and not marker["exhausted"]
            assert pending["plans"][identity]["execution_requested"] and pending["plans"][identity]["plan"] == immutable
            assert trade["reserved_risk"] == immutable["risk_usdc"] and not entries(destination)
            before_recovery_calls = len(requests)
            shutdown_mcp_servers()
            fixture_control(destination, time_ms=marker["next_retry_at"] + 1)
            assert "mcp__aqla__trade_execute" in discover_mcp_tools()
            recovered = read_snapshot(destination / "data" / "state.sqlite3")
            current = recovered["trades"][immutable["trade_id"]]
            assert current["status"] == "open" and current["observation_retry"]["completed"]
            assert current["observation_retry"]["exhausted"]
            assert len(entries(destination)) == 1 and len(requests) == before_recovery_calls
            assert recovered["plans"][identity]["plan"] == immutable
            script.update(phase="repeat", index=0, steps=[("trade_execute", {"plan_id": identity})])
            wake.message_id = "wake-3"
            await adapter._message_handler(wake)
            assert len(entries(destination)) == 1 and not failures
            assert [row[1] for row in issued] == ["source_context", "interpret_source", "trade_preview", "trade_execute", "trade_execute"]
            assert issued[-1][2]["plan_id"] == issued[-2][2]["plan_id"] == identity
        finally:
            agent.close()

    try:
        asyncio.run(run())
        print(json.dumps({"native_plugin_registry": True, "source_claim_hooks": True, "preview_cutoff": True,
            "preview_restart_does_not_execute": True, "saved_plan_continued": True,
            "typed_account_retry_durable": True, "backend_restart_recovers_without_model": True,
            "entry_dispatches": 1, "repeated_id_no_duplicate": True, "real_external_calls": 0,
            "scripted_model_requests": len(requests)}))
    finally:
        shutdown_mcp_servers()
        model.shutdown()
        undo()


if __name__ == "__main__":
    main()
