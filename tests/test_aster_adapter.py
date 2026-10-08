import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_agent.aster import DOMAIN, Aster
from aqla_agent.pipelines import VenueState
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import AqlaError, Market, TradePlan


class AsterBoundary:
    def __init__(self, wallet):
        self.wallet, self.orders, self.trades = wallet, {}, []
        self.position = Decimal(0)
        self.requests, self.posts = [], []
        self.oid = 100
        self.lost = self.reject_stop = False
        self.leverage = 2

    def handle(self, request):
        parameters = dict(request.url.params) if request.method == "GET" else {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.requests.append((request.method, request.url.path, parameters))
        if "signature" in parameters:
            signature = parameters.pop("signature")
            assert abs(int(parameters["nonce"]) - time.time_ns() // 1000) < 60_000_000
            assert parameters["user"] == self.wallet.address.lower()
            assert parameters["signer"] == self.wallet.address.lower()
            typed = {"types": {"EIP712Domain": [{"name": name, "type": kind} for name, kind in [("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address")]], "Message": [{"name": "msg", "type": "string"}]},
                     "primaryType": "Message", "domain": {**DOMAIN, "chainId": 714 if "testnet" in request.url.host else 1666}, "message": {"msg": "&".join(f"{key}={value}" for key, value in sorted(parameters.items()))}}
            assert Account.recover_message(encode_typed_data(full_message=typed), signature=signature) == self.wallet.address
        path = request.url.path.rsplit("/", 1)[-1]
        if request.method != "GET":
            self.posts.append((path, dict(parameters)))
        if path == "dual":
            result = {"dualSidePosition": False}
        elif path == "account":
            result = {"totalMarginBalance": "10000", "availableBalance": "10000", "positions": [{"symbol": "BTCUSDT", "positionAmt": str(self.position), "entryPrice": "100"}]}
        elif path == "exchangeInfo":
            result = {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "marginAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL",
                                   "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}, {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100"}, {"filterType": "MIN_NOTIONAL", "notional": "10"}]}]}
        elif path == "leverageBracket":
            result = [{"brackets": [{"initialLeverage": 20}]}]
        elif path == "price":
            result = {"symbol": "BTCUSDT", "price": "100", "time": time.time_ns() // 1_000_000}
        elif path == "bookTicker":
            result = {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100", "time": time.time_ns() // 1_000_000}
        elif path == "leverage":
            self.leverage = int(parameters["leverage"])
            result = {"leverage": self.leverage, "symbol": "BTCUSDT"}
        elif path == "marginType":
            result = {"code": -4046, "msg": "No need to change margin type"}
        elif path == "positionRisk":
            result = [{"symbol": "BTCUSDT", "leverage": self.leverage, "marginType": "isolated"}]
        elif path == "userTrades":
            result = [row for row in self.trades if int(parameters["startTime"]) <= row["time"] <= int(parameters["endTime"])]
        elif path == "order":
            identity = parameters.get("newClientOrderId", parameters.get("origClientOrderId"))
            if request.method == "POST":
                if parameters["type"] == "STOP_MARKET" and self.reject_stop:
                    return httpx.Response(400, json={"code": -2010, "msg": "Stop rejected"})
                self.oid += 1
                row = {"orderId": self.oid, "clientOrderId": identity, "symbol": "BTCUSDT", "status": "NEW", "type": parameters["type"],
                       "side": parameters["side"], "origQty": parameters["quantity"], "executedQty": "0", "reduceOnly": parameters["reduceOnly"] == "true", "stopPrice": parameters.get("stopPrice", "0"), "price": parameters.get("price", "100")}
                self.orders[identity] = row
                if parameters.get("timeInForce") == "IOC":
                    self.fill(identity)
                if self.lost:
                    self.lost = False
                    raise httpx.ReadTimeout("Lost acknowledgement")
                result = row
            elif identity not in self.orders:
                return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})
            else:
                if request.method == "DELETE":
                    self.orders[identity]["status"] = "CANCELED"
                result = self.orders[identity]
        else:
            raise AssertionError((request.method, request.url.path))
        return httpx.Response(200, json=result)

    def fill(self, identity):
        row = self.orders[identity]
        quantity = Decimal(row["origQty"]) - Decimal(row["executedQty"])
        self.position += quantity * (1 if row["side"] == "BUY" else -1)
        row.update(executedQty=row["origQty"], status="FILLED")
        self.trades.append({"orderId": row["orderId"], "id": len(self.trades) + 1, "qty": str(quantity), "price": row["price"], "time": time.time_ns() // 1_000_000, "commission": "0.01", "commissionAsset": "USDT"})


@pytest.fixture
def aster(setup):
    settings, state, *_ = setup
    wallet = Account.from_key(settings.private_key_file.read_text())
    selected = SimpleNamespace(**{**settings.model_dump(), "venue": "aster", "signer": wallet.address, "private_key_file": settings.private_key_file})
    state.update(lambda data: data["venue_limits"].__setitem__("aster", data["venue_limits"]["hyperliquid"]))
    state.update(lambda data: data["venue_enabled"].__setitem__("aster", True))
    view = VenueState(state, "aster", selected)
    boundary = AsterBoundary(wallet)
    adapter = Aster(selected, view, httpx.AsyncClient(base_url="https://fapi.asterdex-testnet.com", transport=httpx.MockTransport(boundary.handle)))
    return adapter, boundary, Trading(adapter, view)


@pytest.mark.asyncio
async def test_ten_trade_fill_reads_use_only_their_immutable_aster_markets(aster, signal, monkeypatch):
    adapter, _, engine = aster
    source, interpretation = signal
    plan = TradePlan.model_validate(await engine.preview(source.event_id, interpretation))
    markets = [plan.market.model_copy(update={"symbol": "M" + str(index), "native_symbol": "M" + str(index) + "USDT", "asset_id": 1000 + index}) for index in range(100)]
    adapter.market_cache = {market.symbol: market for market in markets}
    calls = []
    async def read(path, params=None, **kwargs):
        assert path == "userTrades" and kwargs == {"private": True}
        calls.append(params["symbol"])
        return []
    monkeypatch.setattr(adapter, "read", read)
    for market in markets[:10]:
        assert await engine._fills(plan.model_copy(update={"market": market})) == []
    assert calls == [market.native_symbol for market in markets[:10]]
    await adapter.close()


@pytest.mark.asyncio
async def test_aster_selected_fill_read_preserves_saturation_guard_and_general_read(aster, signal, monkeypatch):
    adapter, _, engine = aster
    source, interpretation = signal
    plan = TradePlan.model_validate(await engine.preview(source.event_id, interpretation))
    calls = []
    async def saturated(path, params=None, **kwargs):
        calls.append(params["symbol"])
        return [{}] * 1000
    monkeypatch.setattr(adapter, "read", saturated)
    with pytest.raises(AqlaError, match="saturated within one millisecond"):
        await adapter.fills(1, 1)
    assert calls == [plan.market.native_symbol]
    other = plan.market.model_copy(update={"symbol": "ETH", "native_symbol": "ETHUSDT", "asset_id": 1000})
    adapter.market_cache[other.symbol] = other
    calls.clear()
    async def complete(path, params=None, **kwargs):
        calls.append(params["symbol"])
        return [{"orderId": 1, "id": 2, "qty": "1", "price": "100", "time": 1, "commission": ".01", "commissionAsset": "USDT"}]
    monkeypatch.setattr(adapter, "read", complete)
    rows = await adapter.fills(1, 2)
    assert calls == [plan.market.native_symbol, other.native_symbol]
    assert [row["coin"] for row in rows] == [plan.market.symbol, other.symbol]
    assert all(row["fee"] == ".01" and row["feeToken"] == "USDT" for row in rows)
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("native_symbol", [None, "BTCUSDT", "ETHUSDT"])
async def test_aster_fill_response_cannot_relabel_an_explicit_foreign_market(scoped, native_symbol):
    market = Market(symbol="BTC", native_symbol="BTCUSDT", asset_id=0, product="perp", size_decimals=2, mid="100")
    adapter = object.__new__(Aster)
    adapter.market_cache = {market.symbol: market}
    async def read(path, params, **kwargs):
        assert path == "userTrades" and params["symbol"] == "BTCUSDT"
        return [{"orderId": 42, "id": 7, "qty": "1", "price": "100", "time": 50,
            "commission": ".1", "commissionAsset": "USDT", **({"symbol": native_symbol} if native_symbol is not None else {})}]
    adapter.read = read
    if native_symbol == "ETHUSDT":
        with pytest.raises(AqlaError, match="another native market"):
            await (adapter.fill_history(market, 0, 100) if scoped else adapter.fills(0, 100))
    else:
        rows = await (adapter.fill_history(market, 0, 100) if scoped else adapter.fills(0, 100))
        assert len(rows) == 1 and rows[0]["coin"] == "BTC" and rows[0]["oid"] == 42


@pytest.mark.asyncio
async def test_aster_signed_entry_stop_targets_fills_and_cleanup(aster, signal):
    adapter, boundary, engine = aster
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    assert plan["market"]["quote_token"] == "USDT"
    trade = await engine.execute(plan["plan_id"])
    assert trade["status"] == "open"
    assert boundary.position > 0
    for order in trade["orders"]:
        if order["kind"] == "tp":
            boundary.fill(order["cloid"])
    await engine.recover()
    assert engine.state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
    assert boundary.position == 0
    assert all(row["reduceOnly"] is True for row in boundary.orders.values() if row["clientOrderId"] != trade["entry_cloid"])
    assert len({int(parameters["nonce"]) for _, _, parameters in boundary.requests if "nonce" in parameters}) == len([row for row in boundary.requests if "nonce" in row[2]])
    await adapter.close()


@pytest.mark.asyncio
async def test_aster_lost_response_is_never_resent(aster, signal):
    adapter, boundary, engine = aster
    source, interpretation = signal
    await engine.preview(source.event_id, interpretation)
    market = adapter.market_cache["BTC"]
    order = engine.order(SimpleNamespace(market=market, side="long", entry_kind="market"), Decimal("1"), Decimal("100"), cloid=adapter.cloid("lost"))
    action = {"type": "order", "orders": [order], "grouping": "na"}
    boundary.lost = True
    result = await adapter.mutate("lost", action)
    assert result["phase"] == "unknown"
    count = len(boundary.posts)
    assert (await adapter.mutate("lost", action))["phase"] == "unknown"
    assert len(boundary.posts) == count
    assert (await adapter.reconcile("lost"))["resolved"] is True
    assert "signed_requests" not in adapter.public_operation(result)
    with pytest.raises(AqlaError, match="different request"):
        await adapter.mutate("lost", {"type": "cancel", "cancels": []})
    await adapter.close()


@pytest.mark.asyncio
async def test_aster_stop_rejection_closes_owned_trade_and_pauses_entries(aster, signal):
    adapter, boundary, engine = aster
    boundary.reject_stop = True
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    result = await engine.execute(plan["plan_id"])
    assert boundary.position == 0
    assert result["status"] in ("closing", "closed", "cleanup")
    await engine.recover()
    assert engine.state.snapshot()["trades"][result["trade_id"]]["status"] == "closed"
    assert engine.state.root.snapshot()["venue_enabled"]["aster"] is False
    await adapter.close()
