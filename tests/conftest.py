import json
from decimal import Decimal

import httpx
import pytest
from eth_account import Account

from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, Policy, Settings, SourceEvent, now_ms
from aqla_hyperliquid_mcp.state import State
from aqla_hyperliquid_mcp.trades import Trading


class FakeExchange:
    """A stateful exchange boundary, including accepted-but-lost responses."""

    def __init__(self, signer):
        self.signer = signer
        self.actions, self.orders, self.fills = [], {}, []
        self.position = 0.0
        self.lost_response = False
        self.reject_stop = False
        self.oid = 100
        self.multisig_users = []
        self.multisig_threshold = 1
        self.partial_limit_fraction = 0
        self.lose_entry_response = False
        self.reject_targets = False
        self.mid = "100"

    def handle(self, request):
        payload = json.loads(request.content)
        kind = payload.get("type")
        if request.url.path == "/info":
            if kind == "allMids":
                data = {"BTC": self.mid}
            elif kind == "meta":
                data = {"universe": [{"name": "BTC", "szDecimals": 2, "maxLeverage": 20}]}
            elif kind == "spotMeta":
                data = {"universe": [], "tokens": []}
            elif kind == "userRole":
                data = {"role": "agent", "data": {"user": self.signer}}
            elif kind == "userAbstraction":
                data = "disabled"
            elif kind == "userToMultiSigSigners":
                data = {"authorizedUsers": self.multisig_users, "threshold": self.multisig_threshold}
            elif kind == "clearinghouseState":
                positions = [{"position": {"coin": "BTC", "szi": str(self.position), "entryPx": "100"}}] if self.position else []
                data = {"assetPositions": positions, "marginSummary": {"accountValue": "10000"}, "withdrawable": "10000"}
            elif kind == "userFillsByTime":
                data = self.fills
            elif kind == "orderStatus":
                row = self.orders.get(payload["oid"])
                if row is None and isinstance(payload["oid"], int):
                    row = next((row for row in self.orders.values() if row["order"]["oid"] == payload["oid"]), None)
                data = {"status": "order", "order": row} if row else {"status": "unknownOid"}
            else:
                data = {}
            return httpx.Response(200, json=data)
        action = payload["action"]
        self.actions.append(action)
        statuses = []
        if action["type"] == "order":
            for order in action["orders"]:
                self.oid += 1
                trigger = order["t"].get("trigger")
                row = {"coin": "BTC", "oid": self.oid, "cloid": order.get("c"), "sz": order["s"],
                       "origSz": order["s"], "limitPx": order["p"], "side": "B" if order["b"] else "A",
                       "reduceOnly": order["r"], "isTrigger": bool(trigger), "triggerPx": trigger["triggerPx"] if trigger else "0"}
                phase = "open"
                if trigger and trigger["tpsl"] == "sl" and self.reject_stop:
                    phase = "rejected"
                    statuses.append({"error": "Simulated stop rejection"})
                elif trigger and trigger["tpsl"] == "tp" and self.reject_targets:
                    phase = "rejected"
                    statuses.append({"error": "Simulated target rejection"})
                elif not order["r"] and not trigger and order["t"].get("limit", {}).get("tif") == "Gtc" and self.partial_limit_fraction:
                    size = Decimal(order["s"]) * Decimal(str(self.partial_limit_fraction))
                    self.position = float(Decimal(str(self.position)) + (size if order["b"] else -size))
                    row["sz"] = str(Decimal(order["s"]) - size)
                    self.fills.append({"oid": self.oid, "sz": str(size), "px": "100", "tid": self.oid, "coin": "BTC", "fee": "0.01", "time": now_ms()})
                    statuses.append({"resting": {"oid": self.oid}})
                elif order["t"].get("limit", {}).get("tif") == "Ioc":
                    phase = "filled"
                    size = Decimal(order["s"])
                    if order["r"]:
                        size = min(size, abs(Decimal(str(self.position))))
                    self.position = float(Decimal(str(self.position)) + (size if order["b"] else -size))
                    self.fills.append({"oid": self.oid, "sz": order["s"], "px": "100", "tid": self.oid, "coin": "BTC", "fee": "0.01", "time": now_ms()})
                    statuses.append({"filled": {"oid": self.oid, "totalSz": order["s"], "avgPx": "100"}})
                else:
                    statuses.append({"resting": {"oid": self.oid}})
                self.orders[order["c"]] = {"status": phase, "order": row}
            data = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
        elif action["type"] in ("modify", "batchModify"):
            count = 1 if action["type"] == "modify" else len(action["modifies"])
            data = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 999}}] * count}}}
        elif action["type"] in ("cancel", "cancelByCloid"):
            for cancel in action["cancels"]:
                row = self.orders.get(cancel.get("cloid"))
                if row and row["status"] == "open":
                    row["status"] = "canceled"
            data = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"] * len(action["cancels"])}}}
        else:
            data = {"status": "ok", "response": {"type": "default"}}
        if self.lost_response or self.lose_entry_response and action["type"] == "order" and any(not order["r"] for order in action["orders"]):
            self.lost_response = False
            self.lose_entry_response = False
            raise httpx.ReadTimeout("Simulated accepted action with a lost response")
        return httpx.Response(200, json=data)

    def fill(self, cloid, amount, price="100"):
        row = self.orders[cloid]
        size = Decimal(str(amount))
        if size > Decimal(row["order"]["sz"]):
            raise ValueError("Fake fill exceeds the remaining order")
        signed = size if row["order"]["side"] == "B" else -size
        self.position = float(Decimal(str(self.position)) + signed)
        row["order"]["sz"] = str(Decimal(row["order"]["sz"]) - size)
        if Decimal(row["order"]["sz"]) == 0:
            row["status"] = "filled"
        self.fills.append({"oid": row["order"]["oid"], "sz": str(size), "px": str(price), "tid": 1000 + len(self.fills),
                           "coin": "BTC", "fee": "0.01", "time": now_ms()})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Ephemeral test signer, never a user credential or configured live key.
    monkeypatch.delenv("HL_PRIVATE_KEY", raising=False)
    signer = Account.create()
    secret = tmp_path / "test.secret"
    secret.write_text(signer.key.hex())
    integration = tmp_path / "integration.secret"
    integration.write_text("test-integration-only-" * 3)
    settings = Settings(network="testnet", account=signer.address, state_path=tmp_path / "state.json",
                        private_key_file=secret, integration_key_file=integration, sources=["channel"], policy=Policy())
    state = State(settings)
    fake = FakeExchange(signer.address)
    client = httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(fake.handle))
    exchange = Hyperliquid(settings, state, client)
    trading = Trading(exchange, state)
    yield settings, state, fake, exchange, trading
    state.close()


@pytest.fixture
def signal(setup):
    *_, trading = setup
    source = SourceEvent(chat_id="channel", message_id=1, sent_at=now_ms(), text="BTC kaufen. Entry 100. SL 90. Ziele 110 und 120.")
    trading.ingest(source)
    evidence = [Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in (
        ("symbol", "BTC"), ("side", "kaufen"), ("entry_prices.0", "Entry 100"), ("stop_loss", "SL 90"),
        ("take_profits.0", "Ziele 110"), ("take_profits.1", "120"))]
    interpretation = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long",
                                    entry="market", stop_loss="90", take_profits=["110", "120"], evidence=evidence)
    return source, interpretation
