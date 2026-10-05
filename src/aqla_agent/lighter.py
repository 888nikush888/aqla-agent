"""Lighter official SDK, explicit nonces and integer signing; no SDK auto-send helpers."""
from __future__ import annotations

import asyncio
import json
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import httpx

from aqla_hyperliquid_mcp.models import AqlaError, Market

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


class Lighter(VenueAdapter):
    def __init__(self, settings, state, client=None, signer=None):
        super().__init__(settings, state)
        if settings.api_key_index is None:
            raise AqlaError("Configure a dedicated Lighter API key index from 4 through 254")
        self.client = client or httpx.AsyncClient(base_url=BASE_URLS[settings.network], timeout=settings.timeout_seconds)
        self.signer = signer

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
        headers = {"Authorization": self.auth()} if private else {}
        response = await self.client.get("/api/v1/" + path, params=params, headers=headers)
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
        active = await self.read("accountActiveOrders", {"account_index": self.account}, private=True)
        rows = active.get("orders", [])
        cursor = None
        for page in range(64):
            if group:
                row = next((row for row in rows if row.get("parent_order_index") == parent_oid and row["market_index"] == child["a"]
                            and Decimal(row.get("trigger_price", "0")) == Decimal(child["t"]["trigger"]["triggerPx"]) and row.get("reduce_only") is True), None)
            else:
                row = next((row for row in rows if row["client_order_index"] == index or row["order_index"] == identity), None)
            if row:
                break
            if page and not cursor:
                return {"status": "unknownOid"}
            historical = await self.read("accountInactiveOrders", {"account_index": self.account, "limit": 100, **({"cursor": cursor} if page else {})}, private=True)
            rows, cursor = historical.get("orders", []), historical.get("next_cursor")
        else:
            raise AqlaError("Lighter order history exceeded the bounded cursor budget")
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
