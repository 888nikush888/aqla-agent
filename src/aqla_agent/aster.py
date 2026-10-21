"""Aster V3 futures, following the official MCP/API contracts, MIT upstream reference."""
from __future__ import annotations

import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_hyperliquid_mcp.models import AqlaError, Market, digest, now_ms

from .read_budget import observation_auth, observed_request
from .venues import VenueAdapter

DOMAIN = {"name": "AsterSignTransaction", "version": "1", "chainId": 1666, "verifyingContract": "0x" + "0" * 40}
BASE_URLS = {"mainnet": "https://fapi.asterdex.com", "testnet": "https://fapi.asterdex-testnet.com"}


def sign_parameters(wallet, parameters, *, network="mainnet"):
    message = urlencode(sorted(parameters.items()))
    domain = {**DOMAIN, "chainId": 714 if network == "testnet" else 1666}
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
               {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
               "Message": [{"name": "msg", "type": "string"}]}, "primaryType": "Message", "domain": domain, "message": {"msg": message}}
    return "0x" + wallet.sign_message(encode_typed_data(full_message=typed)).signature.hex().removeprefix("0x")


class Aster(VenueAdapter):
    supports_maker_orders = True

    async def depth_snapshot(self, market):
        from .entry import PriceSnapshot
        started = now_ms()
        contract = await self.read("ticker/price", {"symbol": market.native_symbol}, fresh=True)
        book = await self.read("depth", {"symbol": market.native_symbol, "limit": 100})
        if contract.get("symbol") != market.native_symbol or not book.get("bids") or not book.get("asks"):
            raise AqlaError("Aster depth does not identify an executable requested market")
        bid, ask = Decimal(book["bids"][0][0]), Decimal(book["asks"][0][0])
        return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
            stop_reference=contract["price"], stop_reference_kind="contract", exchange_time=book["T"],
            started_at=started, observed_at=now_ms(), bids=book["bids"], asks=book["asks"]).fresh()

    async def price_snapshot(self, market):
        from .entry import PriceSnapshot
        started = now_ms()
        contract = await self.read("ticker/price", {"symbol": market.native_symbol}, fresh=True)
        book = await self.read("ticker/bookTicker", {"symbol": market.native_symbol})
        if any(row.get("symbol") != market.native_symbol for row in (contract, book)):
            raise AqlaError("Aster price snapshot belongs to another market")
        if type(contract.get("time")) is not int or not 0 <= contract["time"] <= now_ms():
            from .observations import TemporaryObservation
            raise TemporaryObservation("price_stale", "price")
        bid, ask = Decimal(book["bidPrice"]), Decimal(book["askPrice"])
        return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                             stop_reference=contract["price"], stop_reference_kind="contract", exchange_time=book["time"],
                             started_at=started, observed_at=now_ms()).fresh()

    async def funding_payments(self, market, start, end):
        from .funding import aster_funding
        return await aster_funding(self, market, start, end)

    def __init__(self, settings, state, client=None):
        super().__init__(settings, state)
        if not settings.signer:
            raise AqlaError("Configure the Aster Pro API signer address")
        try:
            self.wallet = Account.from_key(settings.private_key_file.read_text().strip())
        except Exception:
            raise AqlaError("Aster API signer key is unavailable or invalid") from None
        if self.wallet.address.lower() != settings.signer.lower():
            raise AqlaError("Aster signer address differs from the configured API key")
        self.client = client or httpx.AsyncClient(base_url=BASE_URLS[settings.network], timeout=settings.timeout_seconds)

    def signed(self, method, path, params):
        def reserve(data):
            nonce = max(time.time_ns() // 1000, int(data["nonces"].get("aster", 0)) + 1)
            data["nonces"]["aster"] = nonce
            return nonce
        nonce = self.state.update(reserve)
        payload = dict(sorted({**params, "nonce": str(nonce), "user": self.account, "signer": self.wallet.address.lower()}.items()))
        payload["signature"] = sign_parameters(self.wallet, payload, network=self.settings.network)
        return {"method": method, "path": path, "params": payload, "nonce": nonce, "wire_query": urlencode(payload)}

    async def send(self, request):
        # Persist the exact signed encoding for new requests. Legacy records retain
        # their saved parameter order and are never re-signed or implicitly sent.
        wire = request.get("wire_query") or urlencode(request["params"])
        url = self.client.base_url.join(request["path"]).copy_with(query=wire.encode("ascii")) if request["method"] == "GET" else request["path"]
        kwargs = {} if request["method"] == "GET" else {"content": wire, "headers": {"Content-Type": "application/x-www-form-urlencoded"}}
        if request["method"] == "GET":
            kwargs["auth"] = observation_auth(self.client)
        response = await self.client.request(request["method"], url, **kwargs)
        if response.status_code >= 500 or response.status_code in (408, 429):
            error = AqlaError("Aster response is uncertain; reconcile before any further send")
            error.http_status = response.status_code
            raise error
        result = response.json()
        if response.status_code >= 400 and not VenueAdapter.rejected(result):
            raise AqlaError("Aster returned an unclassified exchange response")
        return result

    async def read(self, path, params=None, *, private=False, fresh=False):
        with observed_request(self.client, "GET", "/fapi/v3/" + path):
            if private:
                result = await self.send(self.signed("GET", "/fapi/v3/" + path, params or {}))
            else:
                response = await self.client.get("/fapi/v3/" + path, params=params, auth=observation_auth(self.client))
                response.raise_for_status()
                result = response.json()
                if fresh:
                    from .observations import TemporaryObservation
                    # Ticker `time` is the last trade, not when this current-price response was served.
                    try:
                        served = int(parsedate_to_datetime(response.headers["date"]).timestamp() * 1000)
                        valid = 0 <= now_ms() - served <= 2000 and int(response.headers.get("age", "0")) == 0
                    except (KeyError, ValueError, TypeError, OverflowError):
                        valid = False
                    if not valid:
                        raise TemporaryObservation("price_stale", "price")
        if self.rejected(result):
            raise AqlaError("Aster rejected the requested account/market observation")
        return result

    async def verify_signer(self):
        mode = await self.read("positionSide/dual", private=True)
        if mode.get("dualSidePosition") is not False:
            raise AqlaError("Aster requires one-way position mode; do not switch a funded account automatically")
        await self.read("account", private=True)

    async def market(self, symbol, product="perp"):
        if product != "perp":
            raise AqlaError("Automatic Aster routing supports perpetuals only")
        meta = await self.read("exchangeInfo")
        name = symbol.upper().removesuffix(".P").replace("/", "")
        candidates = [row for row in meta["symbols"] if row.get("status") == "TRADING" and row.get("contractType") == "PERPETUAL"
                      and name in (row["symbol"], row["baseAsset"])]
        if len(candidates) != 1:
            if not candidates:
                from aqla_hyperliquid_mcp.models import SymbolUnavailable
                raise SymbolUnavailable("Aster symbol is unavailable or delisted in this network")
            raise AqlaError("Aster symbol is unavailable or ambiguous")
        row = candidates[0]
        filters = {item["filterType"]: item for item in row["filters"]}
        size = filters["LOT_SIZE"]
        quote = row.get("marginAsset")
        if quote not in ("USDT", "USDC") or row["quoteAsset"] != quote:
            raise AqlaError("Unsupported Aster settlement currency")
        bracket = await self.read("leverageBracket", {"symbol": row["symbol"]}, private=True)
        bracket = bracket[0] if isinstance(bracket, list) else bracket
        leverage = max(int(item["initialLeverage"]) for item in bracket["brackets"])
        tiers = [{"notional_floor": item["notionalFloor"], "notional_cap": item["notionalCap"], "max_leverage": item["initialLeverage"]}
                 for item in bracket["brackets"] if "notionalFloor" in item and "notionalCap" in item]
        mid = await self.read("ticker/price", {"symbol": row["symbol"]})
        step = Decimal(size["stepSize"])
        market = Market(symbol=row["baseAsset"], native_symbol=row["symbol"], asset_id=int(digest(row["symbol"])[:13], 16),
                        product="perp", size_decimals=max(0, -step.normalize().as_tuple().exponent), mid=mid["price"], max_leverage=leverage,
                        price_tick=filters["PRICE_FILTER"]["tickSize"], quantity_step=step, min_quantity=size["minQty"],
                        max_quantity=size["maxQty"], min_notional=filters.get("MIN_NOTIONAL", {}).get("notional", "0.00000001"), quote_token=quote, leverage_tiers=tiers)
        if any(cached.asset_id == market.asset_id and cached.native_symbol != market.native_symbol for cached in self.market_cache.values()):
            raise AqlaError("Aster native market identity is ambiguous")
        self.market_cache[market.symbol] = market
        return market

    async def positions(self):
        raw = await self.read("account", private=True)
        positions = []
        for row in raw.get("positions", []):
            if Decimal(row["positionAmt"]):
                market = next((item for item in self.market_cache.values() if item.native_symbol == row["symbol"]), None)
                positions.append({"position": {"coin": market.symbol if market else row["symbol"], "szi": row["positionAmt"], "entryPx": row.get("entryPrice")}})
        return {"assetPositions": positions, "marginSummary": {"accountValue": raw["totalMarginBalance"]}, "withdrawable": raw["availableBalance"]}

    async def build_requests(self, action, deadline):
        if action["type"] == "updateLeverage":
            market = self.native_market(action["asset"])
            current = await self.configuration_position(market)
            requests = []
            if current is None or current["marginType"] != ("cross" if action["isCross"] else "isolated"):
                requests.append(self.signed("POST", "/fapi/v3/marginType", {"symbol": market.native_symbol, "marginType": "CROSSED" if action["isCross"] else "ISOLATED"}))
            if current is None or int(current["leverage"]) != action["leverage"]:
                requests.append(self.signed("POST", "/fapi/v3/leverage", {"symbol": market.native_symbol, "leverage": str(action["leverage"])}))
            return requests
        if action["type"] == "order":
            requests = []
            for order in action["orders"]:
                market = self.native_market(order["a"])
                parameters = {"symbol": market.native_symbol, "side": "BUY" if order["b"] else "SELL", "quantity": order["s"],
                              "reduceOnly": "true" if order["r"] else "false", "newClientOrderId": order["c"]}
                trigger = order["t"].get("trigger")
                if trigger:
                    parameters.update(type="STOP_MARKET" if trigger["tpsl"] == "sl" else "TAKE_PROFIT_MARKET", stopPrice=trigger["triggerPx"], workingType="CONTRACT_PRICE")
                else:
                    tif = {"Ioc": "IOC", "Gtc": "GTC", "Alo": "GTX"}.get(order["t"]["limit"]["tif"])
                    if tif is None:
                        raise AqlaError("Unsupported Aster limit time in force")
                    parameters.update(type="LIMIT", price=order["p"], timeInForce=tif)
                requests.append(self.signed("POST", "/fapi/v3/order", parameters))
            return requests
        if action["type"] in ("cancel", "cancelByCloid"):
            requests = []
            for item in action["cancels"]:
                market = self.native_market(item["asset"])
                field, value = ("origClientOrderId", item["cloid"]) if "cloid" in item else ("orderId", item["o"])
                if field == "origClientOrderId":
                    observed = await self.order_status(value)
                    if observed.get("order", {}).get("status") in ("filled", "canceled", "rejected"):
                        continue
                    rejected = any(leg.get("c") == value and (record["phase"] == "rejected" or any(result["index"] == index and result["disposition"] == "rejected" for result in record.get("legs", [])))
                                   for record in self.state.snapshot()["operations"].values() for index, leg in enumerate(record["wire_action"].get("orders", [])))
                    if rejected and observed.get("status") == "unknownOid":
                        continue
                requests.append(self.signed("DELETE", "/fapi/v3/order", {"symbol": market.native_symbol, field: str(value)}))
            return requests
        raise AqlaError("Unsupported Aster trade intent")

    @staticmethod
    def rejected(response):
        # -4046 means the requested margin type is already effective; no change is necessary.
        return VenueAdapter.rejected(response) and response.get("code") != -4046

    async def configuration_position(self, market):
        started = now_ms()
        rows = await self.read("positionRisk", {"symbol": market.native_symbol}, private=True)
        if not 0 <= now_ms() - started <= 10000:
            from .observations import TemporaryObservation
            raise TemporaryObservation("account_stale", "configuration")
        if rows == []:
            return None
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get("symbol") != market.native_symbol or rows[0].get("positionSide") != "BOTH"
                or rows[0].get("marginType") not in ("cross", "isolated")
                or isinstance(rows[0].get("leverage"), bool) or not str(rows[0].get("leverage", "")).isdigit()
                or int(rows[0]["leverage"]) < 1):
            raise AqlaError("Aster configuration observation has no unique one-way market identity")
        return rows[0]

    async def configuration_outcome(self, record):
        action = record["wire_action"]
        if action["type"] != "updateLeverage":
            return None
        market = self.native_market(action["asset"])
        started = now_ms()
        current = await self.configuration_position(market)
        if current is None:
            return None
        evidence = {"symbol": market.native_symbol, "leverage": int(current["leverage"]), "margin_type": current["marginType"], "observed_at": started}
        if int(current["leverage"]) == action["leverage"] and current["marginType"] == ("cross" if action["isCross"] else "isolated"):
            return {"outcome": "accepted", "evidence": [evidence]}
        # Nonce expiry is only a cooldown for an idempotent correction: an
        # already accepted request may still be queued. Keep it unresolved.
        requests = record.get("signed_requests", [])
        if record.get("signed_requests") == []:
            return {"outcome": "retry_configuration", "evidence": [evidence]}
        if not requests or any(r.get("method") != "POST" or r.get("path") not in ("/fapi/v3/marginType", "/fapi/v3/leverage")
                               or r.get("params", {}).get("symbol") != market.native_symbol or type(r.get("nonce")) is not int for r in requests):
            return None
        clock = await self.read("time")
        server = clock.get("serverTime") if isinstance(clock, dict) else None
        if (type(server) is int and 0 <= now_ms() - server <= 2000 and 0 <= now_ms() - started <= 10000
                and server * 1000 > max(r["nonce"] for r in requests) + 60_000_000):
            evidence.update(server_time=server, nonce_window_ended_at=max(r["nonce"] for r in requests) // 1000 + 60000)
            return {"outcome": "retry_configuration", "evidence": [evidence]}
        return None

    async def prove_configuration(self, record):
        decision = await self.configuration_outcome(record)
        return decision is not None and decision["outcome"] == "accepted"

    async def configuration_entry_clear(self, market):
        rows = await self.read("openOrders", {"symbol": market.native_symbol}, private=True)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("symbol") != market.native_symbol
                                             or type(row.get("reduceOnly")) is not bool for row in rows):
            raise AqlaError("Aster configuration correction needs complete native order evidence")
        if any(not row["reduceOnly"] for row in rows):
            raise AqlaError("Manual entry orders now reserve this market")

    async def order_status(self, identity, user=None):
        market = next((market for record in self.state.snapshot()["operations"].values() for order in record["wire_action"].get("orders", [])
                       if order.get("c") == identity for market in self.market_cache.values() if market.asset_id == order["a"]), None)
        if market is None:
            trade = next((trade for trade in self.state.snapshot()["trades"].values() if identity == trade["entry_cloid"]
                          or any(order["cloid"] == identity for order in trade.get("entry_orders", []))
                          or any(order["cloid"] == identity for order in [*trade["orders"], *trade.get("exit_orders", [])])), None)
            if trade:
                market = Market.model_validate(trade["plan"]["market"])
                self.market_cache[market.symbol] = market
        if market is None:
            raise AqlaError("Aster order must be bound to an immutable known market")
        row = await self.send(self.signed("GET", "/fapi/v3/order", {"symbol": market.native_symbol, "origClientOrderId": str(identity)}))
        if isinstance(row, dict) and row.get("code") == -2013:
            return {"status": "unknownOid"}
        if self.rejected(row):
            raise AqlaError("Aster order observation unavailable")
        if row.get("clientOrderId") != identity or row.get("symbol") != market.native_symbol:
            raise AqlaError("Aster returned another order or market identity")
        status = {"NEW": "open", "PARTIALLY_FILLED": "open", "FILLED": "filled", "CANCELED": "canceled", "EXPIRED": "canceled", "REJECTED": "rejected"}.get(row["status"])
        if status is None:
            raise AqlaError("Unrecognized Aster order status")
        trigger = row.get("origType", row["type"]) in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
        return {"status": "order", "order": {"status": status, "statusTimestamp": row.get("updateTime"), "order": {
            "oid": int(row["orderId"]), "cloid": row["clientOrderId"], "coin": market.symbol,
            "side": "B" if row["side"] == "BUY" else "A", "sz": str(Decimal(row["origQty"]) - Decimal(row["executedQty"])),
            "origSz": row["origQty"], "reduceOnly": row.get("reduceOnly") is True, "isTrigger": trigger,
            "triggerPx": row.get("stopPrice", "0"), "triggerCondition": "Price below" if row["side"] == "SELL" else "Price above"}}}

    async def fill_history(self, market: Market, start: int, end: int):
        if not market.native_symbol:
            raise AqlaError("Aster fill history requires the immutable native market identity")
        return await self.fills(start, end, market=market)

    async def fills(self, start, end, *, market: Market | None = None):
        from .funding import bounded_history
        result = []
        markets = (market,) if market is not None else tuple(self.market_cache.values())
        for market in markets:
            async def fetch(low, high, selected=market):
                return await self.read("userTrades", {"symbol": selected.native_symbol, "startTime": str(low), "endTime": str(high), "limit": "1000"}, private=True)
            for row in await bounded_history(fetch, start, end, limit=1000):
                if row.get("symbol") is not None and row["symbol"] != market.native_symbol:
                    raise AqlaError("Aster returned a fill from another native market")
                result.append({"oid": int(row["orderId"]), "tid": row["id"], "coin": market.symbol, "sz": row["qty"], "px": row["price"],
                               "time": int(row["time"]), "fee": row.get("commission"), "feeToken": row.get("commissionAsset"),
                               "execution_role": "maker" if row.get("maker") is True else "taker" if row.get("maker") is False else "unknown"})
        return result
