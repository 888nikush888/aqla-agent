"""Actual native high tool loop, local scripted model and isolated production MCP."""
import argparse
import json
import socket
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
    parser.add_argument("--transport-failure", action="store_true")
    parser.add_argument("--numbered-integer-target", action="store_true")
    parser.add_argument("--rate-limit-failure", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from evaluate_high_tools import native_worker

    from aqla_agent.decision_evaluation import write_private_json
    from aqla_agent.decisions import build_packet
    from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent

    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    text = ("BTC SHORT. Entry 2. Stop 3.\nTarget 1: 1.8\nTarget 2: 1.4\nTarget 3: 1\nRisk 1%"
        if args.numbered_integer_target else "BTC LONG. Entry 100. Stop 90. Target 1: 110.")
    source = SourceEvent(chat_id="synthetic-source", message_id=1, sent_at=1000, text=text, historical=True)
    unrelated = SourceEvent(chat_id="synthetic-source", message_id=2, sent_at=900,
        text="ETH SHORT. Entry 200. Stop 210. Target 180.", historical=True)
    row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json")}
    packet = build_packet({"event": row, "messages": [row], "trades": []})
    value = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[100], stop_loss=90, take_profits=[110],
        source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
            ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "Entry 100"), ("stop_loss", "Stop 90"), ("take_profits.0", "Target 1: 110"))])
    if args.numbered_integer_target:
        value = Interpretation(kind="new_signal", symbol="BTC", side="short", entry_prices=[2], stop_loss=3,
            take_profits=["1.8", "1.4", 1], source_refs=[source.source_id], evidence=[
                Evidence(field=f, source_id=source.source_id, quote=q) for f, q in (
                    ("symbol", "BTC"), ("side", "SHORT"), ("entry_prices.0", "Entry 2"), ("stop_loss", "Stop 3"),
                    ("take_profits.0", "1.8"), ("take_profits.1", "1.4"), ("take_profits.2", "1"))])
    corrected = value.model_copy(deep=True)
    if args.numbered_integer_target:
        corrected.evidence[-1].quote = "Target 3: 1"
    fixture = home / "fixture.private.json"
    write_private_json(fixture, {"packet": packet, "context_messages": [row, {"event_id": unrelated.event_id,
        "source_id": unrelated.source_id, "source": unrelated.model_dump(mode="json")}],
        "master": "Synthetic historical interpretation, no orders.", "session_id": "native-smoke"})
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
            failures = 2 if args.transport_failure or args.rate_limit_failure else 0
            index = len(calls) - 1 - failures
            if index < 0:
                if args.rate_limit_failure:
                    raw = json.dumps({"error": {"code": "rate_limit_exceeded", "type": "rate_limit_exceeded",
                        "message": "rate limit exceeded, retry later"}}).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            sequence = [("source_context", {"event_id": source.event_id, "view": "decision"}),
                        ("source_check_interpretation", {"event_id": source.event_id, "interpretation": value.model_dump(mode="json")})]
            if args.numbered_integer_target:
                sequence.append(("source_check_interpretation", {"event_id": source.event_id, "interpretation": corrected.model_dump(mode="json")}))
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
        waits = []
        result = native_worker(SimpleNamespace(worker_fixture=fixture, hermes_source=args.hermes_source.resolve(),
            mcp_python=args.mcp_python.absolute()), model_url=f"http://127.0.0.1:{model.server_port}/v1", model_key="synthetic-only", local_fixture=True,
            retry_sleep=waits.append)
        expected_drafts = 2 if args.numbered_integer_target else 1
        responses = 2 + expected_drafts
        assert result["completed"] and result["draft_count"] == expected_drafts and result["checks"][-1]["valid"], result
        if args.numbered_integer_target:
            assert not result["checks"][0]["valid"] and result["checks"][0]["errors"][0]["code"] == "source_numeric_proof_needs_context"
        assert result["checks"][-1]["interpretation"]["evidence"][-1]["quote"] == ("Target 3: 1" if args.numbered_integer_target else "110")
        assert not result["forbidden_tools"] and result["order_requests"] == 0
        from aqla_hyperliquid_mcp.storage import read_snapshot
        retained = read_snapshot(home / "state.sqlite3")
        assert set(retained["events"]) == {source.event_id, unrelated.event_id}
        assert waits == ([15, 30] if args.transport_failure or args.rate_limit_failure else [])
        assert len(calls) == len(result["request_attempts"]) == responses + (2 if args.transport_failure or args.rate_limit_failure else 0)
        assert len(result["api_responses"]) == responses, {
            "calls": [{"model": b.get("model"), "effort": b.get("reasoning_effort"), "keys": list(b)} for b in calls],
            "requests": result["request_attempts"], "responses": result["api_responses"]}
        print(json.dumps({"native_high_mcp_loop": True, "original_quote_shortened": not args.numbered_integer_target,
            "integer_target_context_kept": args.numbered_integer_target, "model_calls": len(calls),
            "transport_backoff_verified": waits, "order_requests": 0}))
    finally:
        model.shutdown()


if __name__ == "__main__":
    main()
