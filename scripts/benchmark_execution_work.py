"""Offline counters and timings: mock exchange, isolated state, no real transports."""

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

INVARIANTS = TestCase()

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


async def run(cycles, history):
    import httpx
    from conftest import FakeExchange
    from eth_account import Account

    from aqla_agent.source_worker import SourceWorker
    from aqla_agent.trading import Trading
    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.models import Evidence, Interpretation, Policy, Settings, SourceEvent, now_ms
    from aqla_hyperliquid_mcp.server import close_server, create_server
    from aqla_hyperliquid_mcp.state import State

    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        signer = Account.create()
        secret = directory / "synthetic.secret"
        secret.write_text(signer.key.hex(), encoding="utf-8")
        key = directory / "synthetic-integration.secret"
        key.write_text("isolated-benchmark-integration-only-" * 3, encoding="utf-8")
        settings = Settings(network="testnet", account=signer.address, signer_role="master", telegram_owner_id="42",
            private_key_file=secret, integration_key_file=key, state_path=directory / "state.sqlite3",
            sources=["fixture"], policy=Policy(entry_range_choice="split"))
        settings.waverider_replay.enabled = False
        state = State(settings)
        fake, reads = FakeExchange(signer.address), Counter()
        def transport(request):
            INVARIANTS.assertTrue(request.url.host == 'fixture.invalid', 'Only the local mock transport is permitted')
            if request.url.path == "/info":
                reads[json.loads(request.content)["type"]] += 1
            return fake.handle(request)
        exchange = Hyperliquid(settings, state, httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(transport)))
        engine = Trading(exchange, state)
        source = SourceEvent(chat_id="fixture", message_id=1, sent_at=now_ms(), text="BTC LONG MARKET. SL 90. Targets 110 120.")
        engine.ingest(source)
        signal = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", entry="market",
            stop_loss=90, take_profits=[110, 120], evidence=[Evidence(field=field, source_id=source.source_id, quote=quote)
                for field, quote in (("symbol", "BTC"), ("side", "LONG"), ("entry", "MARKET"), ("stop_loss", "90"), ("take_profits.0", "110"), ("take_profits.1", "120"))])
        plan = await engine.preview(source.event_id, signal)
        trade = await engine.execute(plan["plan_id"])
        def seed(data):
            for number in range(history):
                event = SourceEvent(chat_id="fixture", message_id=number + 10, sent_at=source.sent_at - 1000,
                    text="Historical synthetic report. " + "x" * 1024, historical=True)
                data["events"][event.event_id] = {"event_id": event.event_id, "source_id": event.source_id,
                    "source": event.model_dump(mode="json"), "status": "ignored"}
        state.update(seed)
        before = len(fake.actions)
        reads.clear()
        durations = []
        for _ in range(cycles):
            started = time.perf_counter()
            await engine.refresh_trade(trade["trade_id"])
            durations.append((time.perf_counter() - started) * 1000)
        INVARIANTS.assertTrue(len(fake.actions) == before, 'A normal no-op refresh must not issue another fake order')
        whole_bytes = len(json.dumps(state.snapshot()).encode())
        patched = []
        # Isolated benchmark instruments SQLite writes without changing the production writer.
        # skipcq: PYL-W0212
        original = state._store.save_roots
        def patch(changed):
            patched.append(len(json.dumps(changed).encode()))
            return original(changed)
        # Isolated benchmark instruments SQLite writes without changing the production writer.
        # skipcq: PYL-W0212
        state._store.save_roots = patch
        state.update_metadata(("cursors",), lambda part: part["cursors"].update(benchmark=1))
        INVARIANTS.assertTrue(patched and patched[-1] < whole_bytes / 10)
        sparse_bytes = patched[-1]
        server = create_server(settings, state=state, exchange=exchange)
        sent, model_calls = [], []
        async def mcp_call(_, tool, arguments):
            response = await server.call_tool(tool, arguments)
            INVARIANTS.assertTrue(not response.is_error)
            return response.structured_content or json.loads(response.content[0].text)
        async def send(owner, text):
            sent.append((owner, text))
            return SimpleNamespace(success=True)
        async def forbidden(*args, **kwargs):
            model_calls.append(1)
            raise AssertionError("A frozen notification must not use a model")
        master = directory / "home/skills/aqla-master/SKILL.md"
        master.parent.mkdir(parents=True)
        master.write_text("Synthetic benchmark policy", encoding="utf-8")
        extension = SimpleNamespace(authority=server.aqla_authority, server="aqla", mcp_call=mcp_call,
            gateway_identity="isolated-benchmark", owner="42", master_path=master,
            skill_hash=lambda: "fixture", binding=(SimpleNamespace(send=send), None, None))
        try:
            state.update_metadata(("notifications",), lambda part: part["notifications"].clear())
            state.update_metadata(("notifications",), lambda part: part["notifications"].update(benchmark={"error": "Synthetic system notice"}))
            worker = SourceWorker(extension, {"mode": "legacy"}, adapter=SimpleNamespace(decide=forbidden), fallback=forbidden)
            await worker.deliver()
            await worker.deliver()
            INVARIANTS.assertTrue(len(sent) == 1 and (not model_calls))
            report = {"fake_exchange_only": True, "real_transports": 0, "cycles": cycles, "synthetic_history_events": history,
                "refresh_p50_ms": statistics.median(durations), "refresh_max_ms": max(durations),
                "info_calls_per_cycle": {key: value / cycles for key, value in sorted(reads.items())},
                "whole_state_serialized_bytes": whole_bytes, "metadata_sql_payload_bytes": sparse_bytes,
                "native_notification_deliveries": len(sent), "model_calls_for_notification": len(model_calls),
                "timing_scope": "local fake exchange; SQL payload bytes are not physical disk write bytes"}
            return report
        finally:
            await close_server(server)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--history", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 100 or not 10 <= args.history <= 10000:
        parser.error("Use 1–100 cycles and 10–10000 synthetic history events")
    report = asyncio.run(run(args.cycles, args.history))
    body = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body + "\n", encoding="utf-8")
    print(body)


if __name__ == "__main__":
    main()
