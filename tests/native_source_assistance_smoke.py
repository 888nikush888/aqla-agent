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
    parser.add_argument("--transport-exhausted", action="store_true")
    parser.add_argument("--numbered-integer-target", action="store_true")
    parser.add_argument("--rate-limit-failure", action="store_true")
    parser.add_argument("--empty-original", action="store_true")
    parser.add_argument("--two-entry-orders", action="store_true")
    parser.add_argument("--analysis-entry-level", action="store_true")
    parser.add_argument("--managed-position-side", action="store_true")
    parser.add_argument("--conditional-exit-permission", action="store_true")
    parser.add_argument("--target-catchup", action="store_true")
    parser.add_argument("--partial-exit-label", action="store_true")
    parser.add_argument("--caveman", action="store_true")
    args = parser.parse_args()
    correction_needed = any((args.numbered_integer_target, args.empty_original, args.two_entry_orders,
        args.analysis_entry_level, args.managed_position_side, args.conditional_exit_permission, args.target_catchup, args.partial_exit_label))
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from evaluate_high_tools import native_worker

    from aqla_agent.decision_evaluation import write_private_json
    from aqla_agent.decisions import build_packet
    from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEntryLeg, SourceEvent

    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    text = ("BTC SHORT. Entry 2. Stop 3.\nTarget 1: 1.8\nTarget 2: 1.4\nTarget 3: 1\nRisk 1%"
        if args.numbered_integer_target else "BTC LONG. Entry 100. Stop 90. Target 1: 110.")
    if args.empty_original:
        text = ""
    if args.two_entry_orders:
        text = "BTC LONG\nПoкупаем пo текущим ценам = 100\noтлoженный oрдер на пoкупку = 95\nStop 90\nTarget 110"
    if args.analysis_entry_level:
        text = "BTC LONG\nPrice swept the historical low at 85 and reacted.\nStop 80\nTarget 110"
    if args.managed_position_side:
        text = "BTC\nPurchase from 100 we close at current price 110."
    if args.conditional_exit_permission:
        text = "BTC\nWho has not taken profit yet can do it now.\nStop to entry now."
    if args.target_catchup:
        text = "BTC\nTarget 2 reached; you can take profit now if you forgot to place its order."
    if args.partial_exit_label:
        text = "BTC\nЧасть фикс и стоп в бу."
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
    if args.empty_original:
        value = Interpretation(kind="incomplete", source_refs=[source.source_id], reason="Original text is unavailable",
            evidence=[Evidence(field="classification", source_id=unrelated.source_id, quote=unrelated.text)])
    if args.two_entry_orders:
        value = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[95,100],
            source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q)
                for f,q in [("symbol","BTC"),("side","LONG"),("entry_prices.0","95"),("entry_prices.1","100")]])
    if args.analysis_entry_level:
        value = Interpretation(kind="new_signal", symbol="BTC", side="long", entry_prices=[85], stop_loss=80, take_profits=[110],
            source_refs=[source.source_id], evidence=[Evidence(field=f, source_id=source.source_id, quote=q)
                for f,q in [("symbol","BTC"),("side","LONG"),("entry_prices.0","85"),("stop_loss","80"),("take_profits.0","Target 110")]])
    if args.managed_position_side:
        value = Interpretation(kind="trade_update", symbol="BTC", update_action="close", source_refs=[source.source_id],
            evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in [("symbol","BTC"),("update_action",text.splitlines()[1])]])
    if args.conditional_exit_permission or args.partial_exit_label:
        value = Interpretation(kind="trade_update", symbol="BTC", update_action="break_even", source_refs=[source.source_id],
            evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in [("symbol","BTC"),("update_action",text.splitlines()[-1])]])
    if args.target_catchup:
        value = Interpretation(kind="trade_update", symbol="BTC", update_action="close", source_refs=[source.source_id],
            evidence=[Evidence(field=f, source_id=source.source_id, quote=q) for f,q in [("symbol","BTC"),("update_action",text.splitlines()[1])]])
    corrected = value.model_copy(deep=True)
    if args.numbered_integer_target:
        corrected.evidence[-1].quote = "Target 3: 1"
    if args.empty_original:
        corrected.evidence = []
    if args.two_entry_orders or args.conditional_exit_permission or args.partial_exit_label:
        corrected = Interpretation(kind="ambiguous", source_refs=[source.source_id], reason="Original instruction cannot be partially represented by one supported action",
            evidence=[Evidence(field="classification", source_id=source.source_id, quote=text)])
    if args.two_entry_orders:
        corrected = Interpretation(kind="new_signal", symbol="BTC", side="long", entry="split",
            entry_legs=[SourceEntryLeg(kind="unspecified", price=100), SourceEntryLeg(kind="limit", price=95)],
            stop_loss=90, take_profits=[110], source_refs=[source.source_id], evidence=[
                Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
                    ("symbol", "BTC"), ("side", "LONG"), ("entry_legs.0.kind", "Пoкупаем пo текущим ценам = 100"),
                    ("entry_legs.0.price", "100"), ("entry_legs.1.kind", "oтлoженный oрдер на пoкупку = 95"),
                    ("entry_legs.1.price", "95"), ("stop_loss", "90"), ("take_profits.0", "110"))])
    if args.analysis_entry_level:
        corrected.entry_prices = []
        corrected.evidence = [proof for proof in corrected.evidence if proof.field != "entry_prices.0"]
    if args.managed_position_side:
        corrected.side = "long"
        corrected.evidence.append(Evidence(field="side", source_id=source.source_id, quote="Purchase"))
    if args.target_catchup:
        corrected = Interpretation(kind="report", source_refs=[source.source_id], reason="Existing target/result, not a new whole-position close",
            evidence=[Evidence(field="classification", source_id=source.source_id, quote=text)])
    fixture = home / "fixture.private.json"
    fixture_body = {"packet": packet, "context_messages": [row, {"event_id": unrelated.event_id,
        "source_id": unrelated.source_id, "source": unrelated.model_dump(mode="json")}],
        "master": "Synthetic historical interpretation, no orders.", "session_id": "native-smoke"}
    if args.caveman:
        fixture_body.update(caveman=(root / "skills/caveman/SKILL.md").read_text(encoding="utf-8"),
                            master=(root / "skills/aqla-master/SKILL.md").read_text(encoding="utf-8"))
    write_private_json(fixture, fixture_body)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return None

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.path.endswith("/chat/completions"):
                self.send_response(404)
                self.end_headers()
                return
            calls.append(body)
            assert body.get("reasoning_effort") == "high", {"model": body.get("model"), "keys": list(body), "messages_count": len(body.get("messages", []))}
            failures = 3 if args.transport_exhausted else 2 if args.transport_failure or args.rate_limit_failure else 0
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
            if correction_needed:
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
        if args.transport_exhausted:
            from aqla_hyperliquid_mcp.storage import read_snapshot
            retained = read_snapshot(home / "state.sqlite3")
            assert not result["completed"] and not result["checks"] and result["draft_count"] == 0
            assert result["transport_turn_attempts"] == 3 and waits == [15, 30]
            assert len(calls) == len(result["request_attempts"]) == 3 and not result["api_responses"]
            assert not retained["plans"] and not retained["operations"] and not retained["trades"]
            assert not result["forbidden_tools"] and result["order_requests"] == 0
            print(json.dumps({"native_transport_exhaustion": True, "model_calls": 3, "transport_backoff_verified": waits,
                "checked_drafts": 0, "order_requests": 0}))
            return
        expected_drafts = 2 if correction_needed else 1
        responses = 2 + expected_drafts
        assert result["completed"] and result["draft_count"] == expected_drafts and result["checks"][-1]["valid"], result
        if args.numbered_integer_target:
            assert not result["checks"][0]["valid"] and result["checks"][0]["errors"][0]["code"] == "source_numeric_proof_needs_context"
        if args.analysis_entry_level:
            assert result["checks"][0]["errors"][0]["code"] == "source_entry_reference_needs_role_review"
            assert result["checks"][-1]["interpretation"]["entry_prices"] == []
        if args.empty_original:
            assert result["checks"][0]["errors"][0]["field_paths"] == ["evidence.0"]
            assert "evidence=[]" in result["checks"][0]["errors"][0]["repair"]
            assert result["checks"][-1]["interpretation"]["evidence"] == []
        elif args.two_entry_orders:
            assert result["checks"][0]["errors"][0]["code"] == "entry_legs_not_representable"
            assert result["checks"][-1]["interpretation"]["entry"] == "split"
            assert [row["price"] for row in result["checks"][-1]["interpretation"]["entry_legs"]] == ["100", "95"]
        elif args.managed_position_side:
            assert result["checks"][0]["errors"][0]["code"] == "managed_position_direction_missing_or_changed"
            assert result["checks"][-1]["interpretation"]["side"] == "long"
        elif args.partial_exit_label:
            assert result["checks"][0]["errors"][0]["code"] == "management_actions_not_representable"
            assert result["checks"][-1]["interpretation"]["kind"] == "ambiguous"
        elif args.target_catchup:
            assert result["checks"][0]["errors"][0]["code"] == "existing_target_catchup_requires_report_review"
            assert result["checks"][-1]["interpretation"]["kind"] == "report"
        elif args.conditional_exit_permission:
            assert result["checks"][0]["errors"][0]["code"] == "scheduled_management_not_representable"
            assert result["checks"][-1]["interpretation"]["kind"] == "ambiguous"
            assert result["checks"][-1]["interpretation"]["update_action"] is None
        else:
            assert result["checks"][-1]["interpretation"]["evidence"][-1]["quote"] == ("Target 3: 1" if args.numbered_integer_target else "110")
        assert not result["forbidden_tools"] and result["order_requests"] == 0
        if args.caveman:
            prompt = json.dumps(calls[0]["messages"], ensure_ascii=False)
            assert prompt.index('caveman') < prompt.index('aqla-master')
            assert "Numbers and units exact" in prompt
        from aqla_hyperliquid_mcp.storage import read_snapshot
        retained = read_snapshot(home / "state.sqlite3")
        assert set(retained["events"]) == {source.event_id, unrelated.event_id}
        assert waits == ([15, 30] if args.transport_failure or args.rate_limit_failure else [])
        assert result["transport_turn_attempts"] == (3 if waits else 1)
        assert [retry["wait_seconds"] for retry in result["transport_retries"]] == waits
        from agent.error_classifier import classify_api_error
        from evaluate_high_tools import transport_retryable
        broken = classify_api_error(BrokenPipeError(32, "Broken pipe"), provider="custom", model="deepseek-v4.1-flash")
        assert transport_retryable({"completed": False, "failure_reason": broken.reason.value, "failure_retryable": broken.retryable})
        assert len(calls) == len(result["request_attempts"]) == responses + (2 if args.transport_failure or args.rate_limit_failure else 0)
        assert len(result["api_responses"]) == responses, {
            "calls": [{"model": b.get("model"), "effort": b.get("reasoning_effort"), "keys": list(b)} for b in calls],
            "requests": result["request_attempts"], "responses": result["api_responses"]}
        assert not retained["plans"] and not retained["operations"] and not retained["trades"]
        print(json.dumps({"native_high_mcp_loop": True, "original_quote_shortened": not (args.numbered_integer_target or args.empty_original or args.two_entry_orders or args.managed_position_side or args.conditional_exit_permission or args.target_catchup or args.partial_exit_label),
            "caveman_in_model_request": args.caveman,
            "managed_position_side_kept": args.managed_position_side,
            "conditional_exit_not_discarded": args.conditional_exit_permission,
            "target_catchup_not_new_exit": args.target_catchup,
            "partial_exit_not_dropped": args.partial_exit_label,
            "mixed_script_entry_legs_preserved": args.two_entry_orders,
            "analysis_level_not_entry": args.analysis_entry_level,
            "empty_original_quote_exception": args.empty_original,
            "integer_target_context_kept": args.numbered_integer_target, "model_calls": len(calls),
            "transport_backoff_verified": waits, "order_requests": 0}))
    finally:
        model.shutdown()


if __name__ == "__main__":
    main()
