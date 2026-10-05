import json
import time
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from aqla_agent.lighter import Lighter, integer_amount
from aqla_agent.pipelines import VenueState
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import AqlaError


class Signer:
    ORDER_TYPE_LIMIT = 0
    ORDER_TYPE_STOP_LOSS = 2
    ORDER_TYPE_TAKE_PROFIT = 4
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
    ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
    DEFAULT_28_DAY_ORDER_EXPIRY = -1
    DEFAULT_IOC_EXPIRY = 0
    GROUPING_TYPE_ONE_TRIGGERS_THE_OTHER = 1
    CROSS_MARGIN_MODE = 0
    ISOLATED_MARGIN_MODE = 1

    def __init__(self):
        self.calls = []

    def signed(self, kind, values):
        self.calls.append((kind, values))
        return 14, json.dumps({"kind": kind, **values}), "hash-" + str(values["nonce"]), None

    def sign_create_order(self, **values):
        return self.signed("order", values)

    def sign_create_grouped_orders(self, grouping, orders, **values):
        rows = [{name: getattr(order, name) for name, _ in order._fields_} for order in orders]
        return self.signed("group", {"orders": rows, **values})

    def sign_update_leverage(self, market, fraction, margin, **values):
        return self.signed("leverage", {"market": market, "fraction": fraction, "margin": margin, **values})

    def sign_cancel_order(self, **values):
        return self.signed("cancel", values)

    def create_auth_token_with_expiry(self, **_):
        return "unit-test-only-token", None

    def check_client(self):
        return None

    async def close(self):
        pass


class Boundary:
    def __init__(self):
        self.orders, self.trades, self.transactions = {}, [], {}
        self.position = Decimal(0)
        self.nonce = 0
        self.posts = []
        self.lost = False

    def order(self, values):
        grouped = "MarketIndex" in values
        index = values["ClientOrderIndex"] if grouped else values["client_order_index"]
        quantity = Decimal(values["BaseAmount"] if grouped else values["base_amount"]) / 100
        price = Decimal(values["Price"] if grouped else values["price"]) / 10
        is_ask = bool(values["IsAsk"] if grouped else values["is_ask"])
        trigger = Decimal(values["TriggerPrice"] if grouped else values.get("trigger_price", 0)) / 10
        immediate = (values["TimeInForce"] if grouped else values["time_in_force"]) == 0 and not trigger
        row = {"order_index": index + 100, "client_order_index": index, "owner_account_index": 42, "market_index": 1,
               "initial_base_amount": str(quantity), "remaining_base_amount": "0" if immediate else str(quantity),
               "is_ask": is_ask, "price": str(price), "trigger_price": str(trigger), "reduce_only": bool(values["ReduceOnly"] if grouped else values["reduce_only"]),
               "status": "filled" if immediate else "open"}
        self.orders[index] = row
        if immediate:
            self.position += quantity * (-1 if is_ask else 1)
            self.trades.append({"trade_id": len(self.trades) + 1, "market_id": 1, "ask_account_id": 42 if is_ask else 99, "bid_account_id": 99 if is_ask else 42,
                                "ask_id": row["order_index"] if is_ask else 1000, "bid_id": 1000 if is_ask else row["order_index"], "timestamp": time.time_ns() // 1_000_000,
                                "size": str(quantity), "price": str(price), "maker_fee": 0, "taker_fee": 0, "is_maker_ask": not is_ask})

    def handle(self, request):
        from urllib.parse import parse_qs

        path = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)
        if path == "nextNonce":
            result = {"code": 200, "nonce": self.nonce}
        elif path == "orderBookDetails":
            result = {"code": 200, "order_book_details": [{"symbol": "BTC", "market_id": 1, "status": "active", "market_type": "perp", "multiplier": "1.000000000000000000",
                                                           "supported_size_decimals": 2, "supported_price_decimals": 1, "min_base_amount": "0.01", "min_quote_amount": "10",
                                                           "mark_price": "100", "last_trade_price": "100", "min_initial_margin_fraction": 500}]}
        elif path == "account":
            result = {"code": 200, "accounts": [{"index": 42, "total_asset_value": "10000", "available_balance": "10000", "positions": [{"symbol": "BTC", "position": str(abs(self.position)), "sign": 1 if self.position >= 0 else -1, "avg_entry_price": "100"}]}]}
        elif path in ("sendTx", "sendTxBatch"):
            self.posts.append(request)
            params = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            bodies = json.loads(params["tx_infos"]) if path == "sendTxBatch" else [params["tx_info"]]
            hashes = []
            for body in bodies:
                values = json.loads(body)
                assert values["nonce"] == self.nonce
                self.nonce += 1
                tx_hash = "hash-" + str(values["nonce"])
                hashes.append(tx_hash)
                self.transactions[tx_hash] = {"code": 200, "hash": tx_hash, "account_index": 42, "api_key_index": 4, "nonce": values["nonce"], "status": 2, "executed_at": 1, "block_height": 1}
                if values["kind"] == "order":
                    self.order(values)
                elif values["kind"] == "group":
                    parent = None
                    for item in values["orders"]:
                        if item["Type"] == 2:
                            assert item["BaseAmount"] == item["ClientOrderIndex"] == 0
                            item["BaseAmount"] = values["orders"][0]["BaseAmount"]
                        self.order(item)
                        created = self.orders[item["ClientOrderIndex"]]
                        if parent is None:
                            parent = created
                        else:
                            created["parent_order_index"] = parent["order_index"]
                elif values["kind"] == "cancel":
                    row = next(row for row in self.orders.values() if row["order_index"] == values["order_index"])
                    row["status"] = "canceled"
            if self.lost:
                self.lost = False
                raise httpx.ReadTimeout("Response lost after acceptance")
            result = {"code": 200, **({"tx_hashes": hashes} if path == "sendTxBatch" else {"tx_hash": hashes[0]})}
        elif path == "tx":
            result = self.transactions.get(params["value"], {"code": 200, "status": 1})
        elif path in ("accountActiveOrders", "accountInactiveOrders"):
            result = {"code": 200, "orders": [row for row in self.orders.values() if (row["status"] == "open") == (path == "accountActiveOrders")]}
        elif path == "trades":
            result = {"code": 200, "trades": list(reversed(self.trades))}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=result)


@pytest.fixture
def lighter(setup):
    settings, state, *_ = setup
    selected = SimpleNamespace(**{**settings.model_dump(), "account": "42", "account_index": 42, "api_key_index": 4, "venue": "lighter", "private_key_file": settings.private_key_file})
    state.update(lambda data: data["venue_limits"].__setitem__("lighter", data["venue_limits"]["hyperliquid"]))
    state.update(lambda data: data["venue_enabled"].__setitem__("lighter", True))
    view = VenueState(state, "lighter", selected)
    signer, boundary = Signer(), Boundary()
    adapter = Lighter(selected, view, httpx.AsyncClient(base_url="https://testnet.zklighter.elliot.ai", transport=httpx.MockTransport(boundary.handle)), signer=signer)
    return adapter, boundary, signer, Trading(adapter, view)


@pytest.mark.asyncio
async def test_lighter_grouped_entry_stop_targets_integer_sizes_and_close(lighter, signal):
    adapter, boundary, signer, engine = lighter
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    trade = await engine.execute(plan["plan_id"])
    assert trade["status"] == "open"
    assert any(kind == "group" for kind, _ in signer.calls)
    assert any(request.url.path.endswith("sendTxBatch") for request in boundary.posts)
    assert all(isinstance(values["base_amount"], int) for kind, values in signer.calls if kind == "order")
    result = await engine.close_position(trade["trade_id"], Decimal(100), "owner-close")
    assert boundary.position == 0
    assert result
    assert await adapter.prove_configuration(next(row for row in engine.state.snapshot()["operations"].values() if row["wire_action"]["type"] == "updateLeverage"))
    await adapter.close()


@pytest.mark.asyncio
async def test_lighter_accepted_lost_response_reconciles_without_signing_again(lighter, signal):
    adapter, boundary, signer, engine = lighter
    source, interpretation = signal
    await engine.preview(source.event_id, interpretation)
    market = adapter.market_cache["BTC"]
    boundary.lost = True
    action = {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 3}
    result = await adapter.mutate("lost", action)
    assert result["phase"] == "unknown"
    count = len(signer.calls)
    await adapter.mutate("lost", action)
    assert len(signer.calls) == count
    assert (await adapter.reconcile("lost"))["resolved"]
    assert signer.calls[-1][1]["fraction"] == 3334
    await adapter.close()


def test_lighter_integer_conversion_never_rounds_or_overflows():
    assert integer_amount("0.12345", "0.00001") == 12345
    with pytest.raises(AqlaError, match="grid"):
        integer_amount("0.123456", "0.00001")
    with pytest.raises(AqlaError, match="range"):
        integer_amount(2**64, 1)


@pytest.mark.asyncio
async def test_actual_native_sdk_signs_oto_parent_with_nil_child_quantity_and_id():
    from lighter import SignerClient
    from lighter.signer_client import CreateOrderTxReq, create_api_key

    key, _, error = create_api_key()
    assert error is None
    signer = SignerClient(url="https://testnet.zklighter.elliot.ai", account_index=42, api_private_keys={4: key})
    try:
        parent = CreateOrderTxReq(MarketIndex=1, ClientOrderIndex=12345, BaseAmount=10000, Price=100000, IsAsk=0, Type=0, TimeInForce=0, ReduceOnly=0, TriggerPrice=0, OrderExpiry=0)
        stop = CreateOrderTxReq(MarketIndex=1, ClientOrderIndex=0, BaseAmount=0, Price=85000, IsAsk=1, Type=2, TimeInForce=0, ReduceOnly=1, TriggerPrice=90000, OrderExpiry=-1)
        result = signer.sign_create_grouped_orders(1, [parent, stop], nonce=1, api_key_index=4)
        assert result[3] is None and result[1] and result[2]
        body = json.loads(result[1])
        assert body["AccountIndex"] == 42 and body["ApiKeyIndex"] == 4 and body["Nonce"] == 1
    finally:
        await signer.close()
