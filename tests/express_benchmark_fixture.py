"""Real Python trading paths with ephemeral signers and in-memory exchanges only."""
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
from conftest import FakeExchange
from eth_account import Account

from aqla_agent.source_replay import canonical_symbol
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import ExpressSettings, Policy, Settings, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


async def apply_fake(source, interpretation, directory, *, express):
    directory.mkdir(parents=True, exist_ok=False)
    signer = Account.create()
    secret = directory / "ephemeral-test-only.secret"
    secret.write_text(signer.key.hex())
    symbol = canonical_symbol(interpretation.symbol)
    assert symbol and ":" not in symbol, "DEX fixtures require the separate native DEX suite"
    fake = FakeExchange(signer.address)
    if interpretation.entry_prices:
        fake.mid = str(sum(interpretation.entry_prices) / len(interpretation.entry_prices))
    requests = []

    def project(value, key=None):
        if isinstance(value, dict):
            return {symbol if k == "BTC" else k: project(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [project(v) for v in value]
        if value == "BTC":
            return symbol
        if key == "szDecimals":
            return 5 if Decimal(fake.mid) >= 10000 else 3 if Decimal(fake.mid) >= 1000 else 2
        if key in ("px", "entryPx", "avgPx") and value == "100":
            return fake.mid
        return value

    def transport(request):
        assert request.url.path in ("/info", "/exchange")
        action = json.loads(request.content).get("action", {})
        requests.append({"time_ns": time.perf_counter_ns(), "mutating": request.url.path == "/exchange",
                         "entry": action.get("type") == "order" and any(not order["r"] for order in action["orders"])})
        response = fake.handle(request)
        return httpx.Response(response.status_code, json=project(response.json()))

    settings = Settings(network="testnet", account=signer.address, state_path=directory / "state.sqlite3",
        private_key_file=secret, sources=[source.chat_id], policy=Policy(), express=ExpressSettings(
            mode="live" if express else "off", master_path=Path(__file__).resolve().parents[1] / "skills/aqla-master/SKILL.md"))
    state = State(settings)
    exchange = Hyperliquid(settings, state, httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(transport)))
    server = create_server(settings, state=state, exchange=exchange)
    try:
        # Both processes are initialized/reconciled before the measurement.
        await server.aqla_trading.recover()
        fresh = source.model_copy(update={"sent_at": now_ms(), "received_at": now_ms(), "historical": False})
        assert fresh.source_id == source.source_id and fresh.event_id == source.event_id
        requests.clear()
        started = time.perf_counter_ns()
        server.aqla_trading.ingest(fresh)
        if express:
            await server.aqla_express.tick()
        else:
            preview = await server.aqla_trading.preview(fresh.event_id, interpretation)
            targets = preview.get("targets", {})
            targets = list(targets.values()) if isinstance(targets, dict) else targets
            plans = [t["plan_id"] for t in targets if t.get("status") == "planned"]
            if not targets and preview.get("plan_id"):
                plans = [preview["plan_id"]]
            if plans:
                await server.aqla_trading.execute_many(plans)
        snapshot = state.snapshot()
        trades = list(snapshot["trades"].values())
        assert len(trades) == 1 and trades[0]["status"] in ("open", "opening", "waiting_entry"), {
            "status": [t["status"] for t in trades], "reason": [t.get("unsent_reason") for t in trades]}
        trade = trades[0]
        if trade["status"] in ("opening", "waiting_entry"):
            # A resting limit cannot fill worse than its rounded limit price.
            fake.fill(trade["entry_cloid"], trade["plan"]["quantity"], price=trade["plan"]["entry_limit"])
            await server.aqla_trading.recover()
            trade = state.snapshot()["trades"][trade["trade_id"]]
        seconds = (time.perf_counter_ns() - started) / 1e9
        assert trade["status"] == "open", {"status": trade["status"], "reason": trade.get("error")}
        assert len([order for order in fake.orders.values() if order["status"] == "open" and order["order"]["reduceOnly"]]) >= 2
        plan = trade["plan"]
        outcome = {"status": trade["status"], "symbol": plan["market"]["symbol"], "side": plan["side"],
                   "quantity": str(Decimal(plan["quantity"])), "stop_price": trade["stop_price"], "targets": trade["targets"]}
        first = next((r["time_ns"] for r in requests if r["mutating"]), None)
        entry = next((r["time_ns"] for r in requests if r["entry"]), None)
        return {"seconds": seconds, "first_fake_exchange_action_seconds": (first-started)/1e9 if first else None,
                "entry_dispatch_seconds": (entry-started)/1e9 if entry else None,
                "outcome": outcome, "fake_exchange_actions": len(fake.actions), "real_order_requests": 0}
    finally:
        await close_server(server)
