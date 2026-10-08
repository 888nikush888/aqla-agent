"""Lighter official SDK, explicit nonces and integer signing; no SDK auto-send helpers."""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

import httpx

from aqla_hyperliquid_mcp.models import AqlaError, Market, now_ms

from .venues import VenueAdapter

BASE_URLS = {"mainnet": "https://mainnet.zklighter.elliot.ai", "testnet": "https://testnet.zklighter.elliot.ai"}


def integer_amount(value, step):
    scaled = Decimal(str(value)) / Decimal(str(step))
    if scaled != scaled.to_integral_value():
        raise AqlaError("Lighter order number is not exactly on the native integer grid")
    if not 0 <= scaled <= 2**63 - 1:
        raise AqlaError("Lighter order number exceeds the native signed integer range")
    return int(scaled)


def integer_price(value, step):
    result = integer_amount(value, step)
    if result > 2**31 - 1:
        raise AqlaError("Lighter price exceeds the supported native ABI integer range")
    return result


def cacheable_terminal_order(row) -> bool:
    if not isinstance(row, dict) or row.get("status") not in ("filled", "canceled", "cancelled", "expired", "rejected"):
        return False
    required = {"order_index", "client_order_index", "market_index", "owner_account_index", "is_ask",
                "reduce_only", "initial_base_amount", "remaining_base_amount"}
    if not required <= row.keys():
        return False
    if any(type(row[key]) is not int for key in ("order_index", "client_order_index", "market_index")):
        return False
    if type(row["is_ask"]) is not bool or type(row["reduce_only"]) is not bool or type(row["owner_account_index"]) not in (int, str):
        return False
    try:
        numbers = [Decimal(str(row[key])) for key in ("initial_base_amount", "remaining_base_amount")]
        numbers.append(Decimal(str(row.get("trigger_price", "0"))))
        return all(value.is_finite() and value >= 0 for value in numbers)
    except (InvalidOperation, ValueError):
        return False


@dataclass
class TerminalOrderIndex:
    generation: int
    rows: list[dict] = field(default_factory=list)
    by_client: dict[int, tuple[int, dict]] = field(default_factory=dict)
    by_oid: dict[int, tuple[int, dict]] = field(default_factory=dict)

    def add(self, rows: list[dict]) -> None:
        for row in rows:
            if not cacheable_terminal_order(row):
                continue
            if row["order_index"] in self.by_oid:
                continue
            fact = copy.deepcopy(row)
            item = (len(self.rows), fact)
            self.rows.append(fact)
            self.by_oid.setdefault(fact["order_index"], item)
            if fact.get("client_order_index") not in (None, 0):
                self.by_client.setdefault(fact["client_order_index"], item)

    def lookup(self, identity, index) -> dict | None:
        candidates = [item for item in (self.by_client.get(index), self.by_oid.get(identity) if isinstance(identity, int) else None) if item is not None]
        return min(candidates, key=lambda item: item[0])[1] if candidates else None


class Lighter(VenueAdapter):
    async def price_snapshot(self, market):
        from .entry import PriceSnapshot
        started = now_ms()
        # Explicitly timestamped REST responses can be used; missing mark price or
        # timestamps require the exchange's fresh public WebSocket snapshots.
        raw = await self.read("orderBookDetails", {"market_id": market.asset_id})
        row = next((row for row in raw.get("order_book_details", []) if row["market_id"] == market.asset_id), None)
        if row and all(row.get(key) is not None for key in ("mark_price", "timestamp", "best_bid", "best_ask")):
            bid, ask = Decimal(row["best_bid"]), Decimal(row["best_ask"])
            return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                                 stop_reference=row["mark_price"], stop_reference_kind="mark", exchange_time=row["timestamp"],
                                 started_at=started, observed_at=now_ms()).fresh()
        import websockets
        url = BASE_URLS[self.settings.network].replace("https://", "wss://") + "/stream"
        async with asyncio.timeout(2):
            async with websockets.connect(url, open_timeout=2, max_size=1_000_000) as socket:
                for channel in ("market_stats", "order_book"):
                    await socket.send(json.dumps({"type": "subscribe", "channel": f"{channel}/{market.asset_id}"}))
                frames = {}
                for _ in range(20):
                    frame = json.loads(await socket.recv())
                    if frame.get("type") == "ping":
                        await socket.send(json.dumps({"type": "pong"}))
                        continue
                    kind = frame.get("type", "").removeprefix("subscribed/")
                    if kind in ("market_stats", "order_book") and frame.get("channel") in (f"{kind}:{market.asset_id}", f"{kind}/{market.asset_id}"):
                        frames[kind] = frame
                    if set(frames) == {"market_stats", "order_book"}:
                        stats = frames["market_stats"]["market_stats"]
                        stats = stats.get(str(market.asset_id), stats)
                        book = frames["order_book"]["order_book"]
                        bid, ask = Decimal(book["bids"][0]["price"]), Decimal(book["asks"][0]["price"])
                        timestamp = min(frames["order_book"]["timestamp"], frames["market_stats"]["timestamp"])
                        timestamp = timestamp * 1000 if timestamp < 10**12 else timestamp
                        return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                                             stop_reference=stats["mark_price"], stop_reference_kind="mark", exchange_time=timestamp,
                                             started_at=started, observed_at=now_ms()).fresh()
        raise AqlaError("Lighter has no fresh timestamped mark-price/order-book pair")

    async def funding_payments(self, market, start, end):
        from .funding import lighter_funding
        return await lighter_funding(self, market, start, end)

    def __init__(self, settings, state, client=None, signer=None):
        super().__init__(settings, state)
        if settings.api_key_index is None:
            raise AqlaError("Configure a dedicated Lighter API key index from 4 through 254")
        self.client = client or httpx.AsyncClient(base_url=BASE_URLS[settings.network], timeout=settings.timeout_seconds)
        self.signer = signer
        self._order_generation = 0
        self._order_context: ContextVar[TerminalOrderIndex | None] = ContextVar("lighter_order_observations", default=None)

    @contextmanager
    def order_observations(self):
        if self._order_context.get() is not None:
            yield
            return
        token = self._order_context.set(TerminalOrderIndex(self._order_generation))
        try:
            yield
        finally:
            self._order_context.reset(token)

    def invalidate_order_observations(self) -> None:
        self._order_generation += 1

    def sdk(self):
        if self.signer is None:
            from lighter import SignerClient

            self.signer = SignerClient(url=BASE_URLS[self.settings.network], account_index=self.settings.account_index,
                                       api_private_keys={self.settings.api_key_index: self.settings.private_key_file.read_text().strip()})
        return self.signer

    def auth(self):
        token, error = self.sdk().create_auth_token_with_expiry(api_key_index=self.settings.api_key_index)
        if error:
            raise AqlaError("Lighter authentication token could not be created")
        return token

    async def read(self, path, params=None, *, private=False):
        from .read_budget import observation_auth, observed_request
        with observed_request(self.client, "GET", "/api/v1/" + path):
            headers = {"Authorization": self.auth()} if private else {}
            response = await self.client.get("/api/v1/" + path, params=params, headers=headers, auth=observation_auth(self.client))
            response.raise_for_status()
            result = response.json()
        if result.get("code", 200) != 200:
            raise AqlaError("Lighter rejected the requested observation")
        return result

    async def verify_signer(self):
        from lighter.signer_client import CreateOrderTxReq

        self.grouped_order_type = CreateOrderTxReq
        error = self.sdk().check_client()
        if error:
            raise AqlaError("Lighter API key does not match its configured account/key index")
        await self.read("account", {"by": "index", "value": self.account})

    async def market(self, symbol, product="perp"):
        if product != "perp":
            raise AqlaError("Automatic Lighter routing supports perpetuals only")
        name = symbol.upper()
        for suffix in ("/USDC", "USDC", "/USDT", "USDT"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        raw = await self.read("orderBookDetails", {"filter": "perp"})
        rows = [row for row in raw.get("order_book_details", []) if row["symbol"] == name and row.get("status") == "active"]
        if len(rows) != 1:
            if not rows:
                from aqla_hyperliquid_mcp.models import SymbolUnavailable
                raise SymbolUnavailable("Lighter symbol is unavailable or delisted in this network")
            raise AqlaError("Lighter symbol is unavailable or ambiguous")
        row = rows[0]
        if Decimal(str(row.get("multiplier", "1"))) != 1:
            raise AqlaError("Lighter instrument multiplier requires an explicit price conversion")
        market = Market(symbol=name, native_symbol=name, asset_id=row["market_id"], product="perp", size_decimals=row["supported_size_decimals"],
                        mid=row.get("mark_price") or row["last_trade_price"], price_tick=Decimal(1).scaleb(-row["supported_price_decimals"]),
                        quantity_step=Decimal(1).scaleb(-row["supported_size_decimals"]), min_quantity=row["min_base_amount"],
                        min_notional=row["min_quote_amount"], max_leverage=int((Decimal(10000) / Decimal(str(row["min_initial_margin_fraction"]))).to_integral_value(rounding=ROUND_FLOOR)), quote_token="USDC", margin_leverage_scale=10000)
        self.market_cache[name] = market
        return market

    async def positions(self):
        raw = await self.read("account", {"by": "index", "value": self.account})
        rows = raw.get("accounts", [])
        if len(rows) != 1 or str(rows[0]["index"]) != self.account:
            raise AqlaError("Lighter account observation does not match its immutable account index")
        row = rows[0]
        return {"assetPositions": [{"position": {"coin": item["symbol"], "szi": str(Decimal(item["position"]) * int(item["sign"])),
                                                   "entryPx": item["avg_entry_price"]}} for item in row.get("positions", []) if Decimal(item["position"])],
                "marginSummary": {"accountValue": row["total_asset_value"]}, "withdrawable": row["available_balance"]}

    async def next_nonce(self):
        remote = await self.read("nextNonce", {"account_index": self.account, "api_key_index": self.settings.api_key_index})
        nonce = max(int(remote["nonce"]), self._build_next_nonce or 0)
        unresolved = [chunk["nonce"] for row in self.state.snapshot()["operations"].values() if row["phase"] in ("prepared", "sending", "unknown")
                      for request in row.get("signed_requests", []) for chunk in request.get("batch", [request])]
        if any(value >= int(remote["nonce"]) for value in unresolved):
            raise AqlaError("Lighter nonce outcome is unresolved; do not reuse or skip it")
        self._build_next_nonce = nonce + 1
        return nonce

    @staticmethod
    def client_index(identity):
        return int(identity.removeprefix("0x"), 16) & ((1 << 48) - 1)

    async def build_requests(self, action, deadline):
        self._build_next_nonce = None
        sdk = self.sdk()
        requests = []
        if action["type"] == "order" and action.get("grouping") == "normalTpsl":
            orders = []
            for item in action["orders"]:
                market = self.native_market(item["a"])
                trigger = item["t"].get("trigger")
                orders.append(self.grouped_order_type(MarketIndex=market.asset_id, ClientOrderIndex=0 if trigger else self.client_index(item["c"]),
                    BaseAmount=0 if trigger else integer_amount(item["s"], market.quantity_step), Price=integer_price(item["p"], market.price_tick),
                    IsAsk=int(not item["b"]), Type=sdk.ORDER_TYPE_STOP_LOSS if trigger else sdk.ORDER_TYPE_LIMIT,
                    TimeInForce=sdk.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL if trigger or item["t"]["limit"]["tif"] == "Ioc" else sdk.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    ReduceOnly=int(item["r"]), TriggerPrice=integer_price(trigger["triggerPx"], market.price_tick) if trigger else 0,
                    OrderExpiry=sdk.DEFAULT_28_DAY_ORDER_EXPIRY if trigger or item["t"]["limit"]["tif"] != "Ioc" else sdk.DEFAULT_IOC_EXPIRY))
            nonce = await self.next_nonce()
            signed = sdk.sign_create_grouped_orders(sdk.GROUPING_TYPE_ONE_TRIGGERS_THE_OTHER, orders, nonce=nonce, api_key_index=self.settings.api_key_index)
            return [self.signed_request(signed, nonce)]
        intents = action.get("orders", action.get("cancels", [action]))
        for intent in intents:
            nonce = await self.next_nonce()
            kwargs = {"nonce": nonce, "api_key_index": self.settings.api_key_index}
            if action["type"] == "updateLeverage":
                fraction = int((Decimal(10000) / Decimal(action["leverage"])).to_integral_value(rounding=ROUND_CEILING))
                signed = sdk.sign_update_leverage(action["asset"], fraction, sdk.CROSS_MARGIN_MODE if action["isCross"] else sdk.ISOLATED_MARGIN_MODE, **kwargs)
            elif action["type"] == "order":
                market = self.native_market(intent["a"])
                trigger = intent["t"].get("trigger")
                signed = sdk.sign_create_order(market_index=market.asset_id, client_order_index=self.client_index(intent["c"]),
                    base_amount=integer_amount(intent["s"], market.quantity_step), price=integer_price(intent["p"], market.price_tick), is_ask=not intent["b"],
                    order_type=(sdk.ORDER_TYPE_STOP_LOSS if trigger["tpsl"] == "sl" else sdk.ORDER_TYPE_TAKE_PROFIT) if trigger else sdk.ORDER_TYPE_LIMIT,
                    time_in_force=sdk.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL if trigger or intent["t"]["limit"]["tif"] == "Ioc" else sdk.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=intent["r"], trigger_price=integer_price(trigger["triggerPx"], market.price_tick) if trigger else 0,
                    order_expiry=sdk.DEFAULT_28_DAY_ORDER_EXPIRY if trigger or intent["t"]["limit"]["tif"] != "Ioc" else sdk.DEFAULT_IOC_EXPIRY, **kwargs)
            elif action["type"] in ("cancel", "cancelByCloid"):
                identity = intent.get("cloid", intent.get("o"))
                row = await self.order_status(identity)
                if row.get("status") != "order":
                    raise AqlaError("Lighter cancel requires the exact observed order index")
                signed = sdk.sign_cancel_order(market_index=intent["asset"], order_index=row["order"]["order"]["oid"], **kwargs)
            else:
                raise AqlaError("Unsupported Lighter trade intent")
            requests.append(self.signed_request(signed, nonce))
        return [{"nonce": requests[0]["nonce"], "batch": requests}] if len(requests) > 1 else requests

    @staticmethod
    def signed_request(signed, nonce):
        kind, body, tx_hash, error = signed
        if error:
            raise AqlaError("Lighter refused to sign the validated intent")
        return {"tx_type": kind, "tx_info": body, "tx_hash": tx_hash, "nonce": nonce}

    async def send(self, request):
        self.invalidate_order_observations()
        chunks = request.get("batch", [request])
        if "batch" in request:
            response = await self.client.post("/api/v1/sendTxBatch", data={"tx_types": json.dumps([item["tx_type"] for item in chunks]), "tx_infos": json.dumps([item["tx_info"] for item in chunks])})
        else:
            response = await self.client.post("/api/v1/sendTx", data={"tx_type": request["tx_type"], "tx_info": request["tx_info"]})
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise AqlaError("Lighter response is uncertain; reconcile exact identities")
        result = response.json()
        if type(result.get("code")) is not int:
            raise AqlaError("Lighter returned an unclassified transaction response")
        hashes = result.get("tx_hashes") if "batch" in request else [result.get("tx_hash")]
        if result.get("code") == 200 and hashes != [item["tx_hash"] for item in chunks]:
            raise AqlaError("Lighter transaction acknowledgement does not match the signed transaction")
        if result["code"] == 200:
            for attempt in range(12):
                if await self.transactions_executed(chunks):
                    break
                if attempt < 11:
                    await asyncio.sleep(0.25)
            else:
                result["pending"] = True
        return result

    async def transactions_executed(self, chunks):
        for request in chunks:
            raw = await self.read("tx", {"by": "hash", "value": request["tx_hash"]})
            tx = raw.get("tx", raw)
            if (tx.get("hash") != request["tx_hash"] or str(tx.get("account_index")) != self.account
                    or tx.get("api_key_index") != self.settings.api_key_index or tx.get("nonce") != request["nonce"]
                    or tx.get("status") != 2 or not tx.get("executed_at") or not tx.get("block_height")):
                return False
        return True

    async def prove_configuration(self, record):
        return await self.transactions_executed([item for request in record["signed_requests"] for item in request.get("batch", [request])])

    @staticmethod
    def rejected(response):
        return isinstance(response, dict) and type(response.get("code")) is int and response["code"] != 200

    async def order_status(self, identity, user=None):
        index = self.client_index(identity) if isinstance(identity, str) else identity
        group = next((row["wire_action"] for row in self.state.snapshot()["operations"].values()
                      if row["wire_action"].get("grouping") == "normalTpsl" and any(order.get("c") == identity for order in row["wire_action"].get("orders", [])[1:])), None)
        parent_oid = None
        if group:
            parent = await self.order_status(group["orders"][0]["c"])
            if parent.get("status") != "order":
                return {"status": "unknownOid"}
            parent_oid = parent["order"]["order"]["oid"]
            child = next(order for order in group["orders"][1:] if order["c"] == identity)
        context = getattr(self, "_order_context", None)
        selected = context.get() if context else None
        if selected is not None:
            row = await self._scoped_order_row(identity, index, group, parent_oid, child if group else None)
        else:
            row = await self._fresh_order_row(identity, index, group, parent_oid, child if group else None)
        if row is None:
            return {"status": "unknownOid"}
        market = self.native_market(row["market_index"])
        if str(row.get("owner_account_index")) != self.account:
            raise AqlaError("Lighter order belongs to another account")
        status = {"open": "open", "pending": "open", "filled": "filled", "canceled": "canceled", "cancelled": "canceled", "expired": "canceled", "rejected": "rejected"}.get(row["status"])
        if status is None:
            raise AqlaError("Unrecognized Lighter order status")
        return {"status": "order", "order": {"status": status, "statusTimestamp": row.get("updated_at"), "order": {
            "oid": row["order_index"], "cloid": identity if isinstance(identity, str) else str(row["client_order_index"]), "coin": market.symbol,
            "side": "A" if row["is_ask"] else "B", "sz": row["remaining_base_amount"], "origSz": row["initial_base_amount"],
            "reduceOnly": row["reduce_only"], "isTrigger": Decimal(row.get("trigger_price", "0")) != 0,
            "triggerPx": row.get("trigger_price", "0"), "triggerCondition": "Price below" if row["is_ask"] else "Price above"}}}

    @staticmethod
    def _matching_order(rows, identity, index, group, parent_oid, child) -> dict | None:
        if group:
            return next((row for row in rows if row.get("parent_order_index") == parent_oid and row["market_index"] == child["a"]
                and Decimal(row.get("trigger_price", "0")) == Decimal(child["t"]["trigger"]["triggerPx"]) and row.get("reduce_only") is True), None)
        return next((row for row in rows if row["client_order_index"] == index or row["order_index"] == identity), None)

    async def _fresh_order_row(self, identity, index, group, parent_oid, child) -> dict | None:
        active = await self.read("accountActiveOrders", {"account_index": self.account}, private=True)
        rows = active.get("orders", [])
        cursor = None
        for page in range(64):
            row = self._matching_order(rows, identity, index, group, parent_oid, child)
            if row:
                break
            if page and not cursor:
                return None
            historical = await self.read("accountInactiveOrders", {"account_index": self.account, "limit": 100, **({"cursor": cursor} if page else {})}, private=True)
            rows, cursor = historical.get("orders", []), historical.get("next_cursor")
        else:
            raise AqlaError("Lighter order history exceeded the bounded cursor budget")
        return row

    async def _scoped_order_row(self, identity, index, group, parent_oid, child) -> dict | None:
        selected = self._order_context.get()
        if selected.generation != self._order_generation:
            selected = TerminalOrderIndex(self._order_generation)
            self._order_context.set(selected)
        cached = (self._matching_order(selected.rows, identity, index, group, parent_oid, child) if group else selected.lookup(identity, index))
        if cached is not None:
            return cached
        generation = self._order_generation
        active = await self.read("accountActiveOrders", {"account_index": self.account}, private=True)
        rows = active.get("orders", [])
        found = self._matching_order(rows, identity, index, group, parent_oid, child)
        if found is not None:
            if generation != self._order_generation:
                raise AqlaError("Lighter order evidence changed during observation")
            return found
        all_rows, cursor, seen = list(rows), None, set()
        for _ in range(64):
            historical = await self.read("accountInactiveOrders", {"account_index": self.account, "limit": 100, **({"cursor": cursor} if cursor else {})}, private=True)
            rows = historical.get("orders", [])
            if not isinstance(rows, list):
                raise AqlaError("Lighter returned no complete order history list")
            all_rows.extend(rows)
            if found is None:
                found = self._matching_order(rows, identity, index, group, parent_oid, child)
            cursor = historical.get("next_cursor")
            if found is not None or not cursor:
                if generation != self._order_generation:
                    raise AqlaError("Lighter order evidence changed during observation")
                fresh = TerminalOrderIndex(generation)
                fresh.add(all_rows)
                fresh.add(selected.rows)
                self._order_context.set(fresh)
                return found
            if cursor in seen:
                raise AqlaError("Lighter order history cursor repeated; coverage is incomplete")
            seen.add(cursor)
        raise AqlaError("Lighter order history exceeded the bounded cursor budget")

    async def fills(self, start, end):
        result, cursor = [], None
        for _ in range(64):
            raw = await self.read("trades", {"account_index": self.account, "sort_by": "timestamp", "sort_dir": "desc", "limit": 100, **({"cursor": cursor} if cursor else {})}, private=True)
            rows = raw.get("trades", [])
            for row in rows:
                stamp = int(row["timestamp"])
                stamp = stamp * 1000 if stamp < 10**12 else stamp
                if stamp < start:
                    return result
                if stamp > end:
                    continue
                is_ask = str(row["ask_account_id"]) == self.account
                if not is_ask and str(row["bid_account_id"]) != self.account:
                    raise AqlaError("Lighter returned another account's fill")
                market = self.native_market(row["market_id"])
                result.append({"oid": int(row["ask_id"] if is_ask else row["bid_id"]), "tid": row["trade_id"], "coin": market.symbol,
                               "sz": row["size"], "px": row["price"], "time": stamp, "fee": "0" if row.get("maker_fee") == row.get("taker_fee") == 0 else None, "feeToken": "USDC",
                               "native_fee": {"maker_fee": row.get("maker_fee"), "taker_fee": row.get("taker_fee"), "is_maker_ask": row.get("is_maker_ask")}})
            cursor = raw.get("next_cursor")
            if not cursor:
                return result
        raise AqlaError("Lighter fill history exceeded the bounded cursor budget")

    async def close(self):
        await super().close()
        if self.signer is not None:
            await self.signer.close()
