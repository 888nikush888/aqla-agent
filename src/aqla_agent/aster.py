"""Aster V3 futures, following the official MCP/API contracts, MIT upstream reference."""
from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_hyperliquid_mcp.models import AqlaError, Market, digest, now_ms

from .read_budget import observed_request
from .venues import VenueAdapter

DOMAIN = {"name": "AsterSignTransaction", "version": "1", "chainId": 1666, "verifyingContract": "0x" + "0" * 40}
BASE_URLS = {"mainnet": "https://fapi.asterdex.com", "testnet": "https://fapi.asterdex-testnet.com"}


def sign_parameters(wallet, parameters, *, network="mainnet"):
    message = "&".join(f"{key}={value}" for key, value in sorted(parameters.items()))
    domain = {**DOMAIN, "chainId": 714 if network == "testnet" else 1666}
    typed = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
               {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
               "Message": [{"name": "msg", "type": "string"}]}, "primaryType": "Message", "domain": domain, "message": {"msg": message}}
    return "0x" + wallet.sign_message(encode_typed_data(full_message=typed)).signature.hex().removeprefix("0x")


class Aster(VenueAdapter):
    async def price_snapshot(self, market):
        from .entry import PriceSnapshot
        started = now_ms()
        contract = await self.read("ticker/price", {"symbol": market.native_symbol})
        book = await self.read("ticker/bookTicker", {"symbol": market.native_symbol})
        if any(row.get("symbol") != market.native_symbol for row in (contract, book)):
            raise AqlaError("Aster price snapshot belongs to another market")
        bid, ask = Decimal(book["bidPrice"]), Decimal(book["askPrice"])
        return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                             stop_reference=contract["price"], stop_reference_kind="contract", exchange_time=min(contract["time"], book["time"]),
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
        payload = {**params, "nonce": str(nonce), "user": self.account, "signer": self.wallet.address.lower()}
        payload["signature"] = sign_parameters(self.wallet, payload, network=self.settings.network)
        return {"method": method, "path": path, "params": payload, "nonce": nonce}

    async def send(self, request):
        kwargs = {"params": request["params"]} if request["method"] == "GET" else {"content": urlencode(request["params"]), "headers": {"Content-Type": "application/x-www-form-urlencoded"}}
        response = await self.client.request(request["method"], request["path"], **kwargs)
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise AqlaError("Aster response is uncertain; reconcile before any further send")
        result = response.json()
        if response.status_code >= 400 and not self.rejected(result):
            raise AqlaError("Aster returned an unclassified exchange response")
        return result

    async def read(self, path, params=None, *, private=False):
        with observed_request(self.client, "GET", "/fapi/v3/" + path):
            if private:
                result = await self.send(self.signed("GET", "/fapi/v3/" + path, params or {}))
            else:
                response = await self.client.get("/fapi/v3/" + path, params=params)
                response.raise_for_status()
                result = response.json()
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
        name = symbol.upper().replace("/", "")
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
            return [self.signed("POST", "/fapi/v3/marginType", {"symbol": market.native_symbol, "marginType": "CROSSED" if action["isCross"] else "ISOLATED"}),
                    self.signed("POST", "/fapi/v3/leverage", {"symbol": market.native_symbol, "leverage": str(action["leverage"])})]
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
                    parameters.update(type="LIMIT", price=order["p"], timeInForce="IOC" if order["t"]["limit"]["tif"] == "Ioc" else "GTC")
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

    async def prove_configuration(self, record):
        action = record["wire_action"]
        if action["type"] != "updateLeverage":
            return False
        market = self.native_market(action["asset"])
        rows = await self.read("positionRisk", {"symbol": market.native_symbol}, private=True)
        return bool(rows) and all(int(row["leverage"]) == action["leverage"] and row.get("marginType", "").lower() == ("cross" if action["isCross"] else "isolated") for row in rows)

    async def order_status(self, identity, user=None):
        market = next((market for record in self.state.snapshot()["operations"].values() for order in record["wire_action"].get("orders", [])
                       if order.get("c") == identity for market in self.market_cache.values() if market.asset_id == order["a"]), None)
        if market is None:
            trade = next((trade for trade in self.state.snapshot()["trades"].values() if identity == trade["entry_cloid"]
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
        result = []
        markets = (market,) if market is not None else tuple(self.market_cache.values())
        for market in markets:
            async def window(low, high, budget, selected=market):
                if budget <= 0:
                    raise AqlaError("Aster fill history exceeded its bounded request budget")
                rows = await self.read("userTrades", {"symbol": selected.native_symbol, "startTime": str(low), "endTime": str(high), "limit": "1000"}, private=True)
                if len(rows) >= 1000:
                    if low >= high:
                        raise AqlaError("Aster fills are saturated within one millisecond")
                    middle = (low + high) // 2
                    return await window(low, middle, budget - 1) + await window(middle + 1, high, budget - 1)
                return rows
            for row in await window(start, end, 6):
                if row.get("symbol") is not None and row["symbol"] != market.native_symbol:
                    raise AqlaError("Aster returned a fill from another native market")
                result.append({"oid": int(row["orderId"]), "tid": row["id"], "coin": market.symbol, "sz": row["qty"], "px": row["price"],
                               "time": int(row["time"]), "fee": row["commission"], "feeToken": row["commissionAsset"]})
        return result
