"""Pinned Hermes client, local model server, no exchange/MCP/order tools."""
import argparse
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    if args.home.exists() and any(args.home.iterdir()):
        raise SystemExit("Use a new, empty native evaluation home")
    args.home.mkdir(parents=True, exist_ok=True)
    os.environ.update(HERMES_HOME=str(args.home.resolve()), HERMES_SKIP_CONFIG_MIGRATION="1",
                      HERMES_SKIP_UPDATE_CHECK="1")
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "src"), str(args.hermes_source.resolve())]
    import yaml

    from aqla_agent.decisions import build_packet
    from aqla_agent.source_worker import SourceWorker
    from aqla_hyperliquid_mcp.models import SourceEvent
    source = SourceEvent(chat_id="fixture-channel", message_id=1, sent_at=1000, text="Result report, no new trade.")
    packet = build_packet({"event": {"event_id": source.event_id}, "messages": [
        {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}], "trades": []})
    interpretation = {"kind": "report", "source_refs": [source.source_id], "reason": "Result report",
                      "evidence": [{"field": "classification", "source_id": source.source_id, "quote": source.text}]}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            response = {"id": "local-decision-fixture", "object": "chat.completion", "created": 1000,
                        "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(interpretation)}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:" + str(server.server_port) + "/v1"
    (args.home / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "fixture-model", "provider": "custom", "base_url": base_url},
        "agent": {"reasoning_effort": "ultra"}, "auxiliary": {"transient_retries": 0}}))
    master = args.home / "skills/aqla-master/SKILL.md"
    master.parent.mkdir(parents=True)
    master.write_text("Use original source evidence; do not infer new trading intent from reports.")
    runtime = {"provider": "custom", "base_url": base_url, "api_key": "local-fixture-only", "api_mode": "chat_completions"}
    extension = SimpleNamespace(master_path=master, binding=None, session_key="fixture-source",
        gateway=SimpleNamespace(_resolve_session_agent_runtime=lambda **_: ("fixture-model", runtime)))
    try:
        usage = []
        result = asyncio.run(SourceWorker(extension, {"mode": "hybrid"}).generative_fallback(packet, "synthetic_uncertain", usage))
        assert result.fallback and result.interpretation.kind == "report"
        assert len(requests) == 1 and len(requests[0]["messages"]) == 2
        assert not requests[0].get("tools")
        assert requests[0].get("max_tokens",requests[0].get("max_completion_tokens")) == 8192
        schema = json.loads(requests[0]["messages"][0]["content"].split("Schema:\n", 1)[1])
        assert schema["properties"]["source_refs"]["items"]["enum"] == [source.source_id]
        assert schema["properties"]["source_refs"]["contains"] == {"const": source.source_id}
        assert schema["$defs"]["Evidence"]["properties"]["source_id"]["enum"] == [source.source_id]
        assert schema["properties"]["bound_trade_id"]["type"] == "null"
        assert usage[0]["input_tokens"] == 100 and usage[0]["output_tokens"] == 20
        assert usage[0]["cost_status"] == "unknown" and usage[0]["cost_usd"] is None
        print(json.dumps({"passed": True, "native_model_calls": 1, "history_messages": 0, "order_tools_available": False}))
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


if __name__ == "__main__":
    main()
