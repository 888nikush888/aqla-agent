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
                                                           "mark_price": "100", "last_trade_price": "100", "min_initial_margin_fraction": 500,
                                                           "timestamp": time.time_ns() // 1_000_000, "best_bid": "100", "best_ask": "100"}]}
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


def terminal_order(index, *, status="filled", owner=42):
    return {"order_index": index + 100, "client_order_index": index + 10, "owner_account_index": owner, "market_index": 1,
        "initial_base_amount": "1", "remaining_base_amount": "0", "is_ask": False, "trigger_price": "0",
        "reduce_only": False, "status": status}


def count_order_reads(adapter, boundary, monkeypatch):
    calls = []
    def observed(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path in ("accountActiveOrders", "accountInactiveOrders"):
            calls.append(path)
        return boundary.handle(request)
    monkeypatch.setattr(adapter.client, "_transport", httpx.MockTransport(observed))
    return calls


@pytest.mark.asyncio
async def test_ten_terminal_order_reads_share_one_explicit_scan_and_new_pass_reads_again(lighter, monkeypatch):
    adapter, boundary, _, _ = lighter
    await adapter.market("BTC")
    boundary.orders = {index + 10: terminal_order(index) for index in range(10)}
    calls = count_order_reads(adapter, boundary, monkeypatch)
    with adapter.order_observations():
        for index in range(10):
            row = await adapter.order_status(index + 100)
            assert row["order"]["status"] == "filled" and row["order"]["order"]["oid"] == index + 100
    assert calls == ["accountActiveOrders", "accountInactiveOrders"]
    with adapter.order_observations():
        assert (await adapter.order_status(100))["order"]["status"] == "filled"
    assert calls == ["accountActiveOrders", "accountInactiveOrders"] * 2
    await adapter.close()


@pytest.mark.asyncio
async def test_order_scope_never_caches_unknown_absence_or_open_state(lighter, monkeypatch):
    adapter, boundary, _, _ = lighter
    await adapter.market("BTC")
    calls = count_order_reads(adapter, boundary, monkeypatch)
    with adapter.order_observations():
        assert (await adapter.order_status(100))["status"] == "unknownOid"
        boundary.orders[10] = terminal_order(0, status="open")
        assert (await adapter.order_status(100))["order"]["status"] == "open"
        boundary.orders[10]["status"] = "canceled"
        assert (await adapter.order_status(100))["order"]["status"] == "canceled"
    assert calls.count("accountActiveOrders") == 3 and calls.count("accountInactiveOrders") == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_mutation_invalidates_scoped_terminal_order_evidence(lighter, monkeypatch):
    adapter, boundary, _, _ = lighter
    market = await adapter.market("BTC")
    boundary.orders[10] = terminal_order(0)
    calls = count_order_reads(adapter, boundary, monkeypatch)
    with adapter.order_observations():
        assert (await adapter.order_status(100))["order"]["status"] == "filled"
        result = await adapter.mutate("scope-invalidation", {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 3})
        assert result["phase"] == "acknowledged"
        boundary.orders[10]["owner_account_index"] = 43
        with pytest.raises(AqlaError, match="another account"):
            await adapter.order_status(100)
    assert calls.count("accountActiveOrders") == 2 and calls.count("accountInactiveOrders") == 2
    await adapter.close()


@pytest.mark.asyncio
async def test_scoped_order_history_repeated_cursor_is_incomplete(lighter, monkeypatch):
    adapter, _, _, _ = lighter
    calls = []
    async def repeated(path, params=None, **kwargs):
        calls.append(path)
        return {"orders": [], **({"next_cursor": "same"} if path == "accountInactiveOrders" else {})}
    monkeypatch.setattr(adapter, "read", repeated)
    with adapter.order_observations(), pytest.raises(AqlaError, match="cursor repeated"):
        await adapter.order_status(100)
    assert calls == ["accountActiveOrders", "accountInactiveOrders", "accountInactiveOrders"]
    await adapter.close()


@pytest.mark.asyncio
async def test_positive_terminal_order_does_not_require_unrelated_later_history_pages(lighter, monkeypatch):
    adapter, _, _, _ = lighter
    await adapter.market("BTC")
    calls = []
    async def positive(path, params=None, **kwargs):
        calls.append(path)
        return {"orders": []} if path == "accountActiveOrders" else {"orders": [terminal_order(0)], "next_cursor": "unrelated-broken-page"}
    monkeypatch.setattr(adapter, "read", positive)
    assert (await adapter.order_status(100))["order"]["status"] == "filled"
    calls.clear()
    with adapter.order_observations():
        assert (await adapter.order_status(100))["order"]["status"] == "filled"
    assert calls == ["accountActiveOrders", "accountInactiveOrders"]
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [{"status": "filled"}, {"status": "filled", "order_index": 999},
    {"status": "filled", "order_index": 999, "client_order_index": []}, "unrelated malformed native row"])
async def test_positive_order_survives_unrelated_nonindexable_rows_but_missing_order_is_not_proved(lighter, monkeypatch, malformed):
    adapter, _, _, _ = lighter
    await adapter.market("BTC")
    calls = []
    async def read(path, params=None, **kwargs):
        calls.append(path)
        return {"orders": [] if path == "accountActiveOrders" else [terminal_order(0), malformed]}
    monkeypatch.setattr(adapter, "read", read)
    assert (await adapter.order_status(100))["order"]["status"] == "filled"
    with adapter.order_observations():
        result = await adapter.order_status(100)
        assert result["order"]["status"] == "filled" and result["order"]["order"]["oid"] == 100
        count = len(calls)
        with pytest.raises((AqlaError, KeyError, TypeError)):
            await adapter.order_status(999)
        assert len(calls) > count
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "parent", "market", "owner", "missing_parent"])
async def test_scoped_nil_client_stop_requires_its_parent_market_and_original_owner(lighter, signal, monkeypatch, damage):
    adapter, boundary, _, engine = lighter
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    trade = await engine.execute(plan["plan_id"])
    stop = next(order for order in trade["orders"] if order["kind"] == "sl")
    child = next(row for row in boundary.orders.values() if row.get("parent_order_index") is not None)
    assert child["client_order_index"] == 0
    parent_oid = child["parent_order_index"]
    if damage == "parent":
        child["parent_order_index"] += 1000
    elif damage == "market":
        child["market_index"] = 999
    elif damage == "owner":
        child["owner_account_index"] = 43
    if damage == "missing_parent":
        original = adapter.read
        async def hidden(path, params=None, **kwargs):
            response = await original(path, params, **kwargs)
            if path in ("accountActiveOrders", "accountInactiveOrders"):
                response["orders"] = [row for row in response["orders"] if row["order_index"] != parent_oid]
            return response
        monkeypatch.setattr(adapter, "read", hidden)
    before = len(boundary.posts)
    with adapter.order_observations():
        if damage == "owner":
            with pytest.raises(AqlaError, match="another account"):
                await adapter.order_status(stop["cloid"])
        else:
            result = await adapter.order_status(stop["cloid"])
            if damage:
                assert result["status"] == "unknownOid"
            else:
                assert result["order"]["order"]["oid"] == child["order_index"]
                assert result["order"]["order"]["cloid"] == stop["cloid"]
    assert len(boundary.posts) == before
    await adapter.close()


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
async def test_uncached_other_market_fill_preserves_recovery_and_owned_close(lighter, setup, signal):
    from aqla_agent.router import Router

    settings, state, _, exchange, _ = setup
    adapter, boundary, _, _ = lighter
    router = Router(settings, state, exchange, adapters={"lighter": adapter})
    engine = router.engine_for("lighter", settings.network)
    try:
        source, interpretation = signal
        plan = await engine.preview(source.event_id, interpretation)
        trade = await engine.execute(plan["plan_id"])
        assert trade["status"] == "open"
        original_fills = trade["fill_events"]
        original_orders = trade["orders"]
        boundary.trades.append({**boundary.trades[0], "market_id": 99, "trade_id": 99999,
                                "ask_id": 99998, "bid_id": 99997, "timestamp": time.time_ns() // 1_000_000})
        before = len(boundary.posts)
        reports = await router.recover()
        assert not any(row.get("error") for row in reports)
        assert engine.recovery_checked is True and len(boundary.posts) == before
        current = state.snapshot()["trades"][trade["trade_id"]]
        assert current["status"] == "open" and current["fill_events"] == original_fills
        assert current["orders"] == original_orders and set(adapter.market_cache) == {"BTC"}
        result = await router.close_trade(trade["trade_id"], "selected-market-close")
        assert result["phase"] == "acknowledged" and boundary.position == 0
        close = state.snapshot()["operations"]["operator-close:selected-market-close"]["wire_action"]["orders"][0]
        assert close["a"] == plan["market"]["asset_id"] == 1
        assert close["r"] is True and close["b"] is False
        assert Decimal(close["s"]) == Decimal(trade["remaining_quantity"])
        assert len(boundary.posts) == before + 1
        await router.recover()
        closed = state.snapshot()["trades"][trade["trade_id"]]
        assert closed["status"] == "closed" and engine.recovery_checked is True
        assert all(row["coin"] == "BTC" and row["tid"] != 99999 for row in closed["fill_events"].values())
        assert all(order["a"] == 1 for operation in state.snapshot()["operations"].values()
                   for order in operation["wire_action"].get("orders", []))
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_selected_fill_history_preserves_general_reader_facts(lighter):
    adapter, boundary, _, _ = lighter
    try:
        market = await adapter.market("BTC")
        adapter.market_cache["ETH"] = market.model_copy(update={"symbol": "ETH", "native_symbol": "ETH", "asset_id": 99})
        stamp = time.time_ns() // 1_000_000
        own = {"trade_id": 1, "market_id": 1, "ask_account_id": 42, "bid_account_id": 77,
               "ask_id": 111, "bid_id": 222, "timestamp": stamp, "size": "1.23", "price": "100.1",
               "maker_fee": 0, "taker_fee": 0, "is_maker_ask": True}
        boundary.trades = [own, {**own, "trade_id": 2, "market_id": 99, "ask_id": 333,
                               "maker_fee": 1, "taker_fee": 2, "is_maker_ask": False}]
        general = await adapter.fills(stamp, stamp)
        assert [row["coin"] for row in general] == ["ETH", "BTC"]
        assert general[0]["fee"] is None and general[0]["execution_role"] == "taker"
        assert general[1] == {"oid": 111, "tid": 1, "coin": "BTC", "sz": "1.23", "px": "100.1",
                              "time": stamp, "fee": "0", "feeToken": "USDC", "execution_role": "maker",
                              "native_fee": {"maker_fee": 0, "taker_fee": 0, "is_maker_ask": True}}
        assert await adapter.fill_history(market, stamp, stamp) == [general[1]]
        adapter.market_cache.pop("ETH")
        with pytest.raises(AqlaError, match="immutable native market"):
            await adapter.fills(stamp, stamp)
        assert await adapter.fill_history(market, stamp, stamp) == [general[1]]
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False], ids=["pages-and-window", "cursor-budget"])
async def test_selected_fill_history_keeps_page_window_and_request_budget(lighter, monkeypatch, complete):
    adapter, boundary, _, _ = lighter
    try:
        market = await adapter.market("BTC")
        stamp = time.time_ns() // 1_000_000
        own = {"trade_id": 1, "market_id": 1, "ask_account_id": 77, "bid_account_id": 42,
               "ask_id": 111, "bid_id": 222, "timestamp": stamp, "size": "1", "price": "100",
               "maker_fee": 0, "taker_fee": 0, "is_maker_ask": False}
        foreign = {**own, "trade_id": 2, "market_id": 99}
        requests = []
        def pages(request):
            if request.url.path.rsplit("/", 1)[-1] != "trades":
                return boundary.handle(request)
            requests.append(dict(request.url.params))
            if complete and request.url.params.get("cursor"):
                return httpx.Response(200, json={"code": 200, "trades": [own, {**foreign, "timestamp": stamp - 1}]})
            return httpx.Response(200, json={"code": 200, "trades": [{**foreign, "timestamp": stamp + 1}, foreign], "next_cursor": "next"})
        monkeypatch.setattr(adapter.client, "_transport", httpx.MockTransport(pages))
        if complete:
            facts = await adapter.fill_history(market, stamp, stamp)
            assert len(facts) == 1 and facts[0]["oid"] == 222 and facts[0]["time"] == stamp
            assert len(requests) == 2
        else:
            with pytest.raises(AqlaError, match="bounded cursor budget"):
                await adapter.fill_history(market, stamp, stamp)
            assert len(requests) == 64
        expected = {"account_index": "42", "sort_by": "timestamp", "sort_dir": "desc", "limit": "100"}
        assert requests[0] == expected
        assert all(row == {**expected, "cursor": "next"} for row in requests[1:])
        assert not boundary.posts and set(adapter.market_cache) == {"BTC"}
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["account", "market_id", "size"])
async def test_selected_relevant_malformed_fill_still_blocks_management(lighter, signal, damage):
    adapter, boundary, _, engine = lighter
    try:
        source, interpretation = signal
        plan = await engine.preview(source.event_id, interpretation)
        trade = await engine.execute(plan["plan_id"])
        before = len(boundary.posts)
        boundary.trades.append({**boundary.trades[0], "market_id": 99, "trade_id": 99999})
        if damage == "account":
            boundary.trades[0].update(ask_account_id=77, bid_account_id=78)
        else:
            boundary.trades[0].pop(damage)
        reports = await engine.recover()
        assert any(row.get("error") for row in reports) and engine.recovery_checked is False
        assert len(boundary.posts) == before and boundary.position > 0
        current = engine.state.snapshot()["trades"][trade["trade_id"]]
        assert current["fill_events"] == trade["fill_events"]
        assert any(row["status"] == "open" and row["reduce_only"] and Decimal(row["trigger_price"]) > 0
                   for row in boundary.orders.values())
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_selected_history_without_own_entry_fill_never_adopts_other_market(lighter, signal, monkeypatch):
    adapter, boundary, _, engine = lighter
    original = boundary.handle
    def missing_own(request):
        response = original(request)
        if request.url.path.rsplit("/", 1)[-1] == "trades":
            return httpx.Response(200, json={"code": 200, "trades": [
                {**row, "market_id": 99, "trade_id": 99999} for row in boundary.trades]})
        return response
    try:
        monkeypatch.setattr(adapter.client, "_transport", httpx.MockTransport(missing_own))
        source, interpretation = signal
        plan = await engine.preview(source.event_id, interpretation)
        trade = await engine.execute(plan["plan_id"])
        assert trade["status"] == "unknown" and trade["fill_evidence_pending"] is True
        assert trade["filled_quantity"] == "0" and trade["fill_events"] == {}
        assert not any(row["kind"] == "tp" for row in trade["orders"])
        assert boundary.position > 0 and any(row["status"] == "open" and row["reduce_only"]
                                              for row in boundary.orders.values())
        before = len(boundary.posts)
        await engine.recover()
        assert len(boundary.posts) == before and engine.state.snapshot()["trades"][trade["trade_id"]]["status"] == "unknown"
    finally:
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


@pytest.mark.asyncio
async def test_lighter_grouped_entry_rechecks_after_last_sending_write(lighter, setup, signal, monkeypatch):
    from aqla_agent import entry, venues
    from aqla_agent import lighter as lighter_module
    from aqla_agent import trading as trading_module
    from aqla_hyperliquid_mcp.models import now_ms
    adapter, boundary, _, engine = lighter
    state = setup[1]
    source, interpretation = signal
    plan = await engine.preview(source.event_id, interpretation)
    clock = [now_ms()]
    for module in (lighter_module, entry, trading_module, venues):
        monkeypatch.setattr(module, "now_ms", lambda: clock[0])
    original_account = adapter.account_snapshot
    async def account(market):
        return (await original_account(market)).model_copy(update={"observed_at": clock[0]})
    monkeypatch.setattr(adapter, "account_snapshot", account)
    original_update, original_transport = state.update, boundary.handle
    delayed = False
    def update(callback):
        nonlocal delayed
        result = original_update(callback)
        row = state.snapshot()["operations"].get("entry:" + plan["trade_id"])
        if not delayed and row and row["phase"] == "sending":
            delayed = True
            clock[0] += 2001
        return result
    def transport(request):
        response = original_transport(request)
        if request.url.path.rsplit("/", 1)[-1] == "orderBookDetails":
            data = response.json()
            for row in data.get("order_book_details", []):
                row["timestamp"] = clock[0]
            return httpx.Response(200, json=data)
        return response
    monkeypatch.setattr(state, "update", update)
    adapter.client._transport = httpx.MockTransport(transport)
    result = await engine.execute(plan["plan_id"])
    assert delayed and result["status"] == "open"
    assert len([row for row in boundary.orders.values() if not row["reduce_only"]]) == 1
    assert any(row["reduce_only"] and row["trigger_price"] != "0" for row in boundary.orders.values())
    await adapter.close()
