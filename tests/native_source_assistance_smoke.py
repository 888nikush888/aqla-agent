"""Actual native high tool loop, local scripted model and isolated production MCP."""
import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--mcp-python", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from evaluate_high_tools import native_worker

    from aqla_agent.decision_evaluation import write_private_json
    from aqla_agent.decisions import build_packet
    from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent

    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    source = SourceEvent(chat_id="synthetic-source", message_id=1, sent_at=1000,
        text="BTC LONG. Entry 100. Stop 90. Target 1: 110.", historical=True)
    row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
    packet = build_packet({"event": row, "messages": [row], "trades": []})
    value = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[100], stop_loss=90, take_profits=[110],
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
            ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "Entry 100"), ("stop_loss", "Stop 90"), ("take_profits.0", "Target 1: 110"))])
    fixture = home / "fixture.private.json"
    write_private_json(fixture, {"packet": packet, "master": "Synthetic historical interpretation, no orders.", "session_id": "native-smoke"})
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.path.endswith("/chat/completions"):
                self.send_response(404)
                self.end_headers()
                return
            calls.append(body)
            assert body.get("reasoning_effort") == "high", {"model": body.get("model"), "keys": list(body), "messages_count": len(body.get("messages", []))}
            index = len(calls) - 1
            sequence = [("source_context", {"event_id": source.event_id, "view": "decision"}),
                        ("source_check_interpretation", {"event_id": source.event_id, "interpretation": value.model_dump(mode="json")})]
            message = {"role": "assistant", "content": "Checked; no orders."}
            if index < len(sequence):
                name, arguments = sequence[index]
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "fixture-" + str(index),
                    "type": "function", "function": {"name": "mcp__aqla__" + name, "arguments": json.dumps(arguments)}}]}
            response = {"id": "native-high-fixture", "object": "chat.completion", "created": 1000,
                "model": "deepseek-v4.1-flash", "choices": [{"index": 0, "message": message,
                    "finish_reason": "tool_calls" if index < len(sequence) else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 10}}}
            streaming = bool(body.get("stream"))
            if streaming:
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
                chunk = {**response, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0]["finish_reason"]}]}
                raw = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(done) + "\n\ndata: [DONE]\n\n").encode()
            else:
                raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    model = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        result = native_worker(SimpleNamespace(worker_fixture=fixture, hermes_source=args.hermes_source.resolve(),
            mcp_python=args.mcp_python.absolute()), model_url=f"http://127.0.0.1:{model.server_port}/v1", model_key="synthetic-only", local_fixture=True)
        assert result["completed"] and result["draft_count"] == 1 and result["checks"][0]["valid"], result
        assert result["checks"][0]["interpretation"]["evidence"][-1]["quote"] == "110"
        assert not result["forbidden_tools"] and result["order_requests"] == 0
        assert len(calls) == len(result["request_attempts"]) == len(result["api_responses"]) == 3, {
            "calls": [{"model": b.get("model"), "effort": b.get("reasoning_effort"), "keys": list(b)} for b in calls],
            "requests": result["request_attempts"], "responses": result["api_responses"]}
        print(json.dumps({"native_high_mcp_loop": True, "original_quote_shortened": True, "model_calls": len(calls), "order_requests": 0}))
    finally:
        model.shutdown()


if __name__ == "__main__":
    main()
