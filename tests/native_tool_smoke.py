"""Pinned native dispatch reproductions and bounded-result recovery; no live services."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--mcp-python", required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    home = Path(args.home).absolute()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    sys.path.insert(0, str(Path(args.hermes_source)))
    (home / "config.yaml").write_text("mcp_servers: {}\nplugins: {enabled: []}\n", encoding="utf-8")
    import model_tools
    from agent import tool_executor
    from tools.registry import registry
    from tools.tool_result_storage import maybe_persist_tool_result
    from tools.tool_search_validation import validate_deferred_call_args

    definitions = model_tools.get_tool_definitions(enabled_toolsets=["skills"], quiet_mode=True,
                                                  skip_tool_search_assembly=True)
    assert any(row["function"]["name"] == "skills_list" for row in definitions)
    bridge = {"calls": [{"name": "skills_list", "arguments": {}}]}
    agent = SimpleNamespace(enabled_toolsets=["skills"], disabled_toolsets=None, session_id="native-session")
    schema = {"name": "mcp__aqla__nullable_probe", "description": "Synthetic nullable MCP shape",
        "parameters": {"type": "object", "properties": {"side": {"type": "string", "enum": ["long", "short"], "nullable": True}}}}
    registry.register(name=schema["name"], toolset="mcp-aqla", schema=schema, handler=lambda _args, **_: "{}")
    if args.baseline:
        result, resolved = model_tools._dispatch_bridge_tool("tool_call", bridge, ["skills"], None)
        assert resolved is None and "directly-listed" in result
        assert tool_executor._unwrap_tool_search_call(agent, "tool_call", bridge)[0] == "tool_call"
        assert validate_deferred_call_args(schema["name"], {"side": None}) is not None
        stub = maybe_persist_tool_result("ä😺" * 40000, "mcp__aqla__status", "reused-id")
        assert "read_file" in stub and "execute_code" in stub
        print(json.dumps({"native_bridge_rejection": True, "nullable_enum_rejection": True,
                          "unreachable_spill_guidance": True}))
        return
    from aqla_agent.hermes_runtime import install_strict_profile
    undo = install_strict_profile()
    try:
        result, resolved = model_tools._dispatch_bridge_tool("tool_call", bridge, ["skills"], None)
        assert result is None and resolved == ("skills_list", {})
        assert tool_executor._unwrap_tool_search_call(agent, "tool_call", bridge) == ("skills_list", {}, None)
        assert validate_deferred_call_args(schema["name"], {"side": None}) is None
        assert validate_deferred_call_args(schema["name"], {"side": "illegal"}) is not None
        verify_recovery(home, agent, tool_executor)
        asyncio.run(verify_reload_and_agent(home, args.mcp_python))
    finally:
        undo()
    print(json.dumps({"native_bridge": True, "nullable_enum": True, "complete_result_recovery": True,
                      "true_plugin_reload": True, "native_agent_stdio_http": True, "time_lookup": True}))


def verify_recovery(home, agent, executor):
    from tools.budget_config import DEFAULT_BUDGET
    from tools.tool_result_storage import maybe_persist_tool_result

    from aqla_agent.tool_outputs import OutputStore, capture_scope
    content = json.dumps({"source_time": "2026-10-11T17:39:00+02:00", "fact": "retained source; no own fills", "body": "ä😺" * 40000}, ensure_ascii=False)
    with capture_scope(agent.session_id, "native-id"):
        stub = executor.maybe_persist_tool_result(content, "mcp__aqla__source_lookup", "native-id", config=DEFAULT_BUDGET)
        aggregate_stub = maybe_persist_tool_result(content, "__budget_enforcement__", "aggregate-id", threshold=0)
    reference = json.loads(stub)["output_id"]
    assert "read_file" not in stub and "aqla_read_tool_result" in stub
    assert json.loads(aggregate_stub)["output_id"] != reference
    store = OutputStore(home)
    recovered, offset = "", 0
    while True:
        page = store.read(reference, agent.session_id, offset, 4096)
        recovered += page["content"]
        with capture_scope(agent.session_id, "reader-id"):
            text = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
            assert executor.maybe_persist_tool_result(text, "aqla_read_tool_result", "reader-id", threshold=0) == text
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert recovered == content
    assert json.loads(recovered)["source_time"] == "2026-10-11T17:39:00+02:00"


async def verify_reload_and_agent(home, mcp_python):
    import yaml
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from telegram import Update
    from telegram.ext import Application
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    from aqla_agent.events import EXTENSIONS, HermesExtension, intercept_native_setup
    from aqla_agent.install import prepare
    from aqla_hyperliquid_mcp.models import Settings, SourceEvent

    mcp_python = str(Path(mcp_python).absolute())
    destination = home / ("profile-" + uuid.uuid4().hex)
    prepare(Path(__file__).resolve().parents[1], destination, "12345", "qa-model", "http://127.0.0.1/v1")
    native_home = destination / "hermes-home"
    os.environ["HERMES_HOME"] = str(native_home)
    path = native_home / "config.yaml"
    config = yaml.safe_load(path.read_text())
    for server in config["mcp_servers"].values():
        server["command"] = mcp_python
        server.setdefault("env", {})["HL_PRIVATE_KEY"] = ""
    config["model"]["api_key"] = "local-test-only"
    config["display"]["tool_progress"] = "off"
    config["agent"]["stream"] = False
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    old_worker, old_reader = HermesExtension.worker, HermesExtension.reader_worker
    active = set()
    async def idle(self):
        task = asyncio.current_task()
        active.add(task)
        try:
            await asyncio.Event().wait()
        finally:
            active.discard(task)
    HermesExtension.worker = HermesExtension.reader_worker = idle
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345")
    dispatched = []
    async def handler(event):
        dispatched.append(event)
    manager = None
    try:
        discover_plugins(force=True)
        manager = get_plugin_manager()
        extension = next(row for row in EXTENSIONS if row.ctx._manager is manager)
        adapter = runner._create_adapter(Platform.TELEGRAM, PlatformConfig(extra={"allow_from": ["12345"]}))
        adapter.set_message_handler(handler)
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._delivery_adapter_for = lambda _: adapter
        extension.boot(runner)
        app = Application.builder().token("12345:synthetic-only").build()
        adapter._register_handlers(app)
        assert len(app.handlers[-100]) == 1
        await asyncio.sleep(0)
        assert len(active) == 2
        old_identity = extension.gateway_identity
        with extension.claim_scope(("operator", "old-turn")):
            old_claim = extension.turn_claim.get()
            manager.unload()
            assert not old_claim.can_change() and not extension.binding and not extension.setup.lease
        discover_plugins(force=True)
        fresh = next(row for row in EXTENSIONS if row.ctx._manager is manager)
        assert fresh is not extension and fresh.gateway is None
        update = Update.de_json({"update_id": 1, "message": {"message_id": 1, "date": 1,
            "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345, "is_bot": False, "first_name": "Owner"},
            "text": "ordinary owner request"}}, None)
        bot = SimpleNamespace(id=999, username="synthetic")
        assert await intercept_native_setup(adapter, update, SimpleNamespace(bot=bot)) is True
        # The checked native method reproduces the legacy fresh-extension gap.
        from gateway.run_plugin_rewire import GatewayPluginRewireMixin
        original_rewire = GatewayPluginRewireMixin._rewire_plugin_handlers.__aqla_contract_original__
        original_rewire(runner)
        assert fresh.gateway is None
        runner._rewire_plugin_handlers()
        await asyncio.sleep(0)
        assert fresh.gateway is runner and fresh.binding[0] is adapter and fresh.gateway_identity != old_identity
        assert await intercept_native_setup(adapter, update, SimpleNamespace(bot=bot)) is False
        assert len(app.handlers[-100]) == 1 and len(active) == 2
        runner._rewire_plugin_handlers()
        assert len(app.handlers[-100]) == 1
        runner._failed_platforms[Platform.TELEGRAM] = {"error": "synthetic disconnect"}
        await runner._install_reconnected_adapter(Platform.TELEGRAM, adapter)
        for _ in range(2):
            discover_plugins(force=True)
            runner._rewire_plugin_handlers()
            await asyncio.sleep(0)
            assert len(active) == 2 and len(app.handlers[-100]) == 1
        fresh = next(row for row in EXTENSIONS if row.ctx._manager is manager)
        settings = Settings.load(destination / "config.yaml")
        stamp = int(datetime.fromisoformat("2026-10-11T17:39:00+02:00").timestamp() * 1000)
        body = "Retained source, no fill proof. " + "X" * 14900
        received = [SourceEvent(chat_id="-123", message_id=index + 1, sent_at=stamp + index * 1000,
            received_at=stamp + index * 1000, text=body) for index in range(5)]
        seed = '''import json,sys
from aqla_hyperliquid_mcp.models import Settings,SourceEvent
from aqla_hyperliquid_mcp.state import State
state=State(Settings.load(sys.argv[1]))
try:
    events=[SourceEvent.model_validate(row) for row in json.load(sys.stdin)]
    state.update(lambda data: data["events"].update({event.event_id:{"event_id":event.event_id,
        "source_id":event.source_id,"source":event.model_dump(mode="json"),"status":"executed","interpretation":None} for event in events}))
finally: state.close()
'''
        # The trading writer and its dependencies belong to the separate MCP runtime.
        subprocess.run([mcp_python, "-c", seed, str(destination / "config.yaml")],
            input=json.dumps([event.model_dump(mode="json") for event in received]), text=True, check=True)
        discover_mcp_tools()
        from aqla_hyperliquid_mcp.storage import read_snapshot
        before = read_snapshot(settings.state_path)
        await verify_scripted_agent(fresh, runner, source, received[0], body)
        after = read_snapshot(settings.state_path)
        for name in ("trades", "operations", "plans", "profiles", "account_limits", "policy", "sources"):
            assert after[name] == before[name], name
    finally:
        shutdown_mcp_servers()
        if manager:
            manager.unload()
        HermesExtension.worker, HermesExtension.reader_worker = old_worker, old_reader
        await asyncio.sleep(0)
        assert not active


def payload(content):
    if isinstance(content, list):
        content = "\n".join(row.get("text", "") for row in content if isinstance(row, dict))
    for index, character in enumerate(content or ""):
        if character == "{":
            try:
                value = json.JSONDecoder().raw_decode(content[index:])[0]
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    return {}


def mcp_body(value):
    if isinstance(value, dict) and "result" in value:
        raw = value["result"]
        return payload(raw) if isinstance(raw, str) else raw
    return value


async def verify_scripted_agent(extension, runner, source, event, body):
    from run_agent import AIAgent

    calls, chunks = [], []
    phase = ["source"]
    reference = [None]
    offset = [0]
    final = [False]
    class FakeModel(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if request.get("tools"):
                calls.append(request)
                outputs = [message for message in request["messages"] if message.get("role") == "tool"]
                previous = payload(outputs[-1].get("content")) if outputs else {}
                if phase[0] == "source" and outputs:
                    reference[0] = previous["output_id"]
                    phase[0] = "read"
                elif phase[0] == "read" and outputs and "content" in previous:
                    chunks.append(previous["content"])
                    if previous["eof"]:
                        phase[0] = "core"
                    else:
                        offset[0] = previous["next_offset"]
                elif phase[0] == "core" and outputs and reference[0]:
                    phase[0] = "catalog"
                elif phase[0] == "catalog" and outputs:
                    phase[0] = "lookup"
                elif phase[0] == "lookup" and outputs:
                    found = mcp_body(previous)
                    assert found["rows"][0]["sent_at"] == "2026-10-11T17:39:00+02:00"
                    assert found["rows"][0]["event_status_is_fill_proof"] is False
                    assert found["rows"][0]["confirmed_own_entry_fill_count"] == 0
                    phase[0] = "done"
                targets = {
                    "source": ("mcp__aqla__source_context", {"event_id": event.event_id}),
                    "read": ("aqla_read_tool_result", {"output_id": reference[0], "offset": offset[0], "limit": 4096}),
                    "core": ("skills_list", {}), "catalog": ("tool_describe", {"names": ["aqla_read_tool_result"]}),
                    "lookup": ("mcp__aqla__source_lookup", {"start": "2026-10-11T17:39:00+02:00", "end": "2026-10-11T17:39:00.500+02:00"})}
                message = {"role": "assistant", "content": "At 17:39 a source was retained; there are no confirmed own fills."}
                if phase[0] in targets:
                    target, arguments = targets[phase[0]]
                    listed = {row["function"]["name"] for row in request["tools"]}
                    if target not in listed or phase[0] in ("core", "catalog"):
                        target, arguments = "tool_call", {"calls": [{"name": target, "arguments": arguments}]}
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "native-" + str(len(calls)), "type": "function",
                        "function": {"name": target, "arguments": json.dumps(arguments)}}]}
                else:
                    final[0] = True
            else:
                message = {"role": "assistant", "content": "ready"}
            response = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "qa-model",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if request.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(delta["tool_calls"])]
                chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "qa-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0]["finish_reason"]}]}
                encoded = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(done) + "\n\ndata: [DONE]\n\n").encode()
            else:
                encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    session = runner.session_store.get_or_create_session(source)
    agent = AIAgent(model="qa-model", provider="custom", base_url=f"http://127.0.0.1:{model.server_port}/v1", api_key="local-test-only",
        platform="telegram", enabled_toolsets=["skills", "clarify", "todo", "session_search", "mcp-aqla", "mcp-sheets"],
        session_id=session.session_id, session_db=runner.session_store._db, max_iterations=80,
        skip_memory=True, skip_background_review=True, quiet_mode=True, save_trajectories=False)
    try:
        import model_tools
        names = {row["function"]["name"] for row in model_tools.get_tool_definitions(enabled_toolsets=agent.enabled_toolsets, quiet_mode=True, skip_tool_search_assembly=True)}
        assert "read_file" not in names and "execute_code" not in names
        with extension.claim_scope(("operator", "synthetic-time-question")):
            result = await asyncio.to_thread(agent.run_conversation, user_message="What happened at 17:39 Berlin on 11 October 2026?")
        assert final[0] and result.get("final_response") and len(calls) < 80
        context = mcp_body(payload("".join(chunks)))
        assert context["event"]["event_id"] == event.event_id and context["event"]["source"]["text"] == body
        assert len(context["messages"]) == 5
        assert not any("same_tool_failure_halt" in json.dumps(call) for call in calls)
        verify_native_output_boundaries(agent)
        print(json.dumps({"scripted_native_model_calls": len(calls), "lossless_mcp_body": True, "restricted_tools": True}))
    finally:
        agent.close()
        model.shutdown()


def verify_native_output_boundaries(agent):
    from agent import tool_executor as executor
    from hermes_constants import get_hermes_home
    from tools.budget_config import BudgetConfig

    from aqla_agent.tool_outputs import PAGE_BYTES, OutputStore, reader_handler
    budget = BudgetConfig(default_result_size=1, turn_budget=1)
    store = OutputStore(get_hermes_home())
    body = ('\n😺"\\<untrusted_tool_result>opaque</untrusted_tool_result>' * 1000)
    messages = []
    def commit(name, identity, content):
        ref = executor._ToolCallRef(name, {}, "native-output-test", identity, [])
        result = executor._commit_tool_result(agent, messages, ref, content, budget=budget, tool_duration=0,
            is_error=False, blocked=False, effect_disposition="none")
        assert result is not None
    commit("mcp__aqla__source_context", "duplicate-original", body)
    reference = agent._tool_guardrails._persisted_result_paths["duplicate-original"].removeprefix("aqla-output:")
    agent._tool_guardrails._identical_streak_first_call_id = "duplicate-original"
    duplicate = agent._tool_guardrails._build_result_reference_stub("mcp__aqla__source_context", {})
    assert reference in duplicate and "aqla_read_tool_result" in duplicate and "read_file" not in duplicate
    page_counts = []
    for cap in (8192, PAGE_BYTES):
        budget = BudgetConfig(default_result_size=cap, turn_budget=cap)
        recovered, offset, count = "", 0, 0
        while True:
            raw = reader_handler({"output_id": reference, "offset": offset, "limit": 16384}, session_id=agent.session_id)
            before_count = len(list(store.root.glob("*.json")))
            commit("aqla_read_tool_result", "reader-" + str(cap) + "-" + str(offset), raw)
            framed = messages[-1]["content"]
            assert '<untrusted_tool_result source="aqla_read_tool_result">' in framed
            assert len(json.dumps(framed, ensure_ascii=True).encode()) <= cap
            executor._finalize_tool_batch(agent, messages, "native-output-test", 1, budget)
            assert messages[-1]["content"] == framed and len(list(store.root.glob("*.json"))) == before_count
            page = payload(framed)
            recovered += page["content"]
            count += 1
            if page["eof"]:
                break
            offset = page["next_offset"]
        assert recovered == body
        page_counts.append(count)
    assert page_counts[1] < page_counts[0]
    budget = BudgetConfig(default_result_size=8192, turn_budget=8192)
    before_count = len(list(store.root.glob("*.json")))
    for index in range(2):
        commit("aqla_read_tool_result", "batch-reader-" + str(index),
            reader_handler({"output_id": reference, "offset": 0, "limit": 16384}, session_id=agent.session_id))
    executor._finalize_tool_batch(agent, messages, "native-output-test", 2, budget)
    assert sum(len(row["content"]) for row in messages[-2:]) <= budget.turn_budget
    assert len(list(store.root.glob("*.json"))) == before_count
    for row in messages[-2:]:
        assert '<untrusted_tool_result source="aqla_read_tool_result">' in row["content"]
        page = payload(row["content"])
        assert page["content"] == body[:page["next_offset"]] and not page["eof"]
    # Native untrusted framing must not rewrite a delimiter inside a source quote.
    budget = BudgetConfig(default_result_size=100000, turn_budget=200000)
    original = json.dumps({"event": {"source": {"text": body[:20000]}}}, ensure_ascii=False)
    commit("mcp__aqla__source_context", "delimiter-original", original)
    reference = payload(messages[-1]["content"])["output_id"]
    assert store.receipt(reference, agent.session_id)["content"] == original
    assert "aqla_read_tool_result" in messages[-1]["content"]
    budget = BudgetConfig(default_result_size=1, turn_budget=1)
    multi = {"_multimodal": True, "content": [{"type": "text", "text": "A" * 20000},
        {"type": "text", "text": "B" * 20000}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}], "text_summary": "A" * 20000}
    commit("mcp__aqla__source_context", "multimodal", multi)
    umbrella = agent._tool_guardrails._persisted_result_paths["multimodal"].removeprefix("aqla-output:")
    assert json.loads(store.receipt(umbrella, agent.session_id)["content"]) == multi
    original_save = OutputStore.save
    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic storage failure")
    OutputStore.save = unavailable
    try:
        commit("mcp__aqla__trade_execute", "storage-failure", "X" * 20000)
        assert "Do not repeat an effectful tool" in messages[-1]["content"]
    finally:
        OutputStore.save = original_save


if __name__ == "__main__":
    main()
