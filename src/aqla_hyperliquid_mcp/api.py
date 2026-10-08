from __future__ import annotations

import asyncio
import copy
import json
import types
from decimal import Decimal

import httpx
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import signing

from .models import TERMINAL_ORDER, AccountSnapshot, AqlaError, Market, Settings, digest, now_ms
from .state import State

SDK_PRINCIPAL_L1 = frozenset({
    "set_referrer", "create_sub_account", "sub_account_transfer", "sub_account_spot_transfer", "vault_usd_transfer",
    "spot_deploy_register_token", "spot_deploy_user_genesis", "spot_deploy_enable_freeze_privilege",
    "spot_deploy_freeze_user", "spot_deploy_revoke_freeze_privilege", "spot_deploy_enable_quote_token",
    "spot_deploy_genesis", "spot_deploy_register_spot", "spot_deploy_register_hyperliquidity",
    "spot_deploy_set_deployer_trading_fee_share", "perp_deploy_register_asset", "perp_deploy_set_oracle",
    "c_signer_unjail_self", "c_signer_jail_self", "c_validator_register", "c_validator_change_profile",
    "c_validator_unregister", "use_big_blocks",
})
SDK_USER_SIGNED = frozenset({
    "usd_class_transfer", "send_asset", "usd_transfer", "spot_transfer", "token_delegate", "withdraw_from_bridge",
    "approve_builder_fee", "convert_to_multi_sig_user", "user_dex_abstraction", "user_set_abstraction",
})
SDK_TRADING_L1 = frozenset({
    "order", "bulk_orders", "modify_order", "bulk_modify_orders_new", "market_open", "market_close",
    "cancel", "cancel_by_cloid", "bulk_cancel", "bulk_cancel_by_cloid", "schedule_cancel", "update_leverage",
    "update_isolated_margin", "agent_enable_dex_abstraction", "agent_set_abstraction", "noop", "gossip_priority_bid",
})


def sdk_signing_scope(operation):
    if operation in SDK_PRINCIPAL_L1:
        return "principal"
    if operation in SDK_USER_SIGNED:
        return "user"
    if operation in SDK_TRADING_L1:
        return "trading"
    raise AqlaError("No explicit signing scope for this SDK operation")


def wire_number(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise AqlaError("Non-finite exchange number")
    return format(number, "f").rstrip("0").rstrip(".") if number % 1 else format(number.quantize(Decimal(1)), "f")


class Hyperliquid:
    async def price_snapshot(self, market):
        from aqla_agent.entry import PriceSnapshot
        started = now_ms()
        raw = await self.info("metaAndAssetCtxs", **({"dex": market.dex} if market.dex else {}))
        context = next((context for item, context in zip(raw[0]["universe"], raw[1], strict=True) if item["name"] == market.symbol), None)
        if context is None:
            raise AqlaError("Target market has no verified mark-price context")
        book = await self.info("l2Book", coin=market.symbol)
        if book.get("coin") != market.symbol or not all(book.get("levels", [])):
            raise AqlaError("Target market has no executable order book")
        bid, ask = Decimal(book["levels"][0][0]["px"]), Decimal(book["levels"][1][0]["px"])
        return PriceSnapshot(market=market.symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                             stop_reference=context["markPx"], stop_reference_kind="mark", exchange_time=book["time"],
                             started_at=started, observed_at=now_ms()).fresh()

    async def funding_payments(self, market, start, end):
        from aqla_agent.funding import hyperliquid_funding
        return await hyperliquid_funding(self, market, start, end)

    def __init__(self, settings: Settings, state: State, client=None):
        self.settings, self.state = settings, state
        self.client = client or httpx.AsyncClient(base_url=settings.base_url, timeout=settings.timeout_seconds)
        self.mutations = asyncio.Lock()
        key = settings.private_key()
        try:
            self.wallet = Account.from_key(key) if key else None
        except Exception as error:
            raise AqlaError("Invalid configured signing key") from error
        self.market_cache: dict[str, Market] = {}
        self.last_inventory = None

    @property
    def account(self):
        return self.settings.vault_address or self.settings.account

    async def info(self, kind: str, **params):
        # Callers use catalog-validated public inputs or fixed internal requests.
        response = await self.client.post("/info", json={"type": kind, **params})
        response.raise_for_status()
        return response.json()

    async def markets(self, product="perp", dex=""):
        mids = await self.info("allMids", **({"dex": dex} if dex else {}))
        result = {}
        if product == "perp":
            meta = await self.info("meta", **({"dex": dex} if dex else {}))
            # The native perp DEX has protocol USDC collateral. A HIP-3 DEX
            # must identify its collateral; a legacy/missing field proves none.
            collateral = meta.get("collateralToken", 0 if not dex else None)
            if type(collateral) is not int or collateral < 0:
                collateral = None
            quote = "USDC" if collateral == 0 else "UNKNOWN"
            if collateral is not None and collateral != 0:
                spot = await self.info("spotMeta")
                token = next((item for item in spot["tokens"] if item["index"] == collateral), None)
                if token:
                    quote = token["name"]
            offset = 0
            if dex:
                dexes = await self.info("perpDexs")
                index = next((i for i, item in enumerate(dexes) if item and item["name"] == dex), None)
                if index is None:
                    from .models import SymbolUnavailable
                    raise SymbolUnavailable("Unknown Hyperliquid DEX namespace")
                offset = 100000 + index * 10000
            for index, row in enumerate(meta["universe"]):
                if row.get("isDelisted") or row["name"] not in mids:
                    continue
                name = row["name"]
                result[name] = Market(symbol=name, asset_id=offset + index, product="perp",
                                      size_decimals=row["szDecimals"], max_leverage=row["maxLeverage"],
                                      mid=mids[name], dex=dex, collateral_token=collateral, quote_token=quote)
        else:
            meta = await self.info("spotMeta")
            tokens = {item["index"]: item for item in meta["tokens"]}
            for row in meta["universe"]:
                name = row["name"]
                key = name if name in mids else "@" + str(row["index"])
                if key not in mids:
                    continue
                base, quote = (tokens[i] for i in row["tokens"])
                market = Market(symbol=name, asset_id=10000 + row["index"], product="spot", size_decimals=base["szDecimals"],
                                mid=mids[key], base_token=base["name"], quote_token=quote["name"])
                result[name] = market
                result["@" + str(row["index"])] = market
                pair = base["name"] + "/" + quote["name"]
                if pair not in result:
                    result[pair] = market
        self.market_cache.update(result)
        return result

    @staticmethod
    def canonical_symbol(symbol: str, product="perp"):
        if product == "perp" and symbol.upper().replace("/", "") in {"GOLD", "XAU", "XAUUSD", "XAUUSDT", "XAUUSDC"}:
            return "xyz:GOLD"
        return symbol

    async def market(self, symbol: str, product="perp"):
        symbol = self.canonical_symbol(symbol, product)
        dex = symbol.split(":", 1)[0] if ":" in symbol else ""
        markets = await self.markets(product, dex)
        normalized = symbol.upper() if not dex else symbol
        if product == "perp" and not dex:
            for suffix in ("/USDT", "/USDC", "USDT", "USDC"):
                if normalized.endswith(suffix):
                    normalized = normalized[:-len(suffix)]
                    break
        candidates = [market for key, market in markets.items() if key == normalized or key == symbol]
        unique = {item.asset_id: item for item in candidates}
        if len(unique) != 1:
            if not unique and product == "perp":
                from .models import SymbolUnavailable
                meta = await self.info("meta", **({"dex": dex} if dex else {}))
                if any(row["name"] == normalized and not row.get("isDelisted") for row in meta["universe"]):
                    raise AqlaError("Listed market has no usable fresh price data; market absence is not proved")
                raise SymbolUnavailable(f"Market {symbol} is unavailable/delisted on Hyperliquid {self.settings.network}")
            if symbol == "xyz:GOLD":
                raise AqlaError(f"Configured Gold market xyz:GOLD is unavailable on {self.settings.network}; no substitute is used")
            raise AqlaError("Source market cannot be resolved uniquely; no substitute market is used")
        market = next(iter(unique.values()))
        if market.quote_token != "USDC" or market.product == "perp" and market.collateral_token != 0:
            raise AqlaError("Automatic risk sizing currently requires USDC quote currency")
        return market

    async def account_snapshot(self, market: Market):
        if self.settings.shared_account and market.product == "perp":
            return await self.account_inventory(market)
        if market.product == "spot":
            raw = await self.info("spotClearinghouseState", user=self.account)
            balance = next((row for row in raw["balances"] if row["coin"] == "USDC"), None)
            if not balance:
                raise AqlaError("No USDC balance for spot entry")
            available = Decimal(balance["total"]) - Decimal(balance.get("hold", "0"))
            position = next((row for row in raw["balances"] if row["coin"] == market.base_token), None)
            size = Decimal(position["total"]) if position else Decimal(0)
            return AccountSnapshot(equity=Decimal(balance["total"]), available_margin=available, position_size=size)
        abstraction = await self.info("userAbstraction", user=self.account)
        if abstraction not in ("disabled", None):
            raise AqlaError("Automatic sizing requires standard account abstraction; other modes remain available through the full MCP")
        raw = await self.info("clearinghouseState", user=self.account, **({"dex": market.dex} if market.dex else {}))
        size = None
        positions = raw.get("assetPositions")
        if isinstance(positions, list):
            if any(not isinstance(row, dict) or not isinstance(row.get("position"), dict)
                   or not isinstance(row["position"].get("coin"), str) or "szi" not in row["position"] for row in positions):
                raise AqlaError("Account position observation is malformed")
            matching = [row["position"] for row in positions if row["position"]["coin"] == market.symbol]
            if len(matching) > 1:
                raise AqlaError("Account position observation is ambiguous")
            size = Decimal(matching[0]["szi"]) if matching else Decimal(0)
        return AccountSnapshot(equity=raw["marginSummary"]["accountValue"], available_margin=raw["withdrawable"], position_size=size)

    async def account_inventory(self, market: Market | None = None):
        """Complete bounded REST observation; entry calls never use a cached budget."""
        stamp = now_ms()
        mode = await self.info("userAbstraction", user=self.account)
        if mode not in ("disabled", None, "unifiedAccount"):
            raise AqlaError("Shared account entries require Standard or Unified Account mode; portfolio margin is unsupported")
        dexes = await self.info("perpDexs")
        metas = await self.info("allPerpMetas")
        if not isinstance(dexes, list) or not dexes or dexes[0] is not None or len(dexes) > 512:
            raise AqlaError("Complete DEX inventory is unavailable")
        names = [""] + [row["name"] for row in dexes[1:] if isinstance(row, dict) and isinstance(row.get("name"), str)]
        if len(names) != len(dexes) or len(set(names)) != len(names):
            raise AqlaError("DEX inventory is incomplete or ambiguous")
        if not isinstance(metas, list) or len(metas) != len(dexes) or any(not isinstance(meta, dict) or not isinstance(meta.get('universe'), list) for meta in metas):
            raise AqlaError("Complete perpetual market inventory is unavailable")
        perp_markets = {row['name'] for meta in metas for row in meta['universe'] if isinstance(row, dict) and isinstance(row.get('name'), str)}
        limiter = asyncio.Semaphore(3)
        async def observe(dex):
            async with limiter:
                fields = {"dex": dex} if dex else {}
                raw, orders = await asyncio.gather(self.info("clearinghouseState", user=self.account, **fields),
                                                 self.info("frontendOpenOrders", user=self.account, **fields))
                if not isinstance(raw, dict) or not isinstance(raw.get("assetPositions"), list) or not isinstance(orders, list):
                    raise AqlaError("Account positions or orders are incomplete")
                return dex, raw, orders
        if len(names) > 64:
            from aqla_agent.inventory_stream import observe as observe_large_inventory
            observations = await observe_large_inventory(self, names)
        else:
            observations = await asyncio.gather(*(observe(dex) for dex in names))
        positions, entries, seen_positions = {}, [], set()
        states = {}
        for dex, raw, orders in observations:
            states[dex] = raw
            for row in raw["assetPositions"]:
                position = row.get("position", {}) if isinstance(row, dict) else {}
                coin = position.get("coin")
                if not isinstance(coin, str) or "szi" not in position or (dex and not coin.startswith(dex + ":")):
                    raise AqlaError("Account position observation is malformed")
                size = Decimal(str(position["szi"]))
                if not size.is_finite() or coin in seen_positions or coin not in perp_markets:
                    raise AqlaError("Account position observation is ambiguous")
                seen_positions.add(coin)
                if size:
                    positions[coin] = size
            for order in orders:
                if not isinstance(order, dict) or not isinstance(order.get("coin"), str):
                    raise AqlaError("Account order observation is malformed")
                # Spot orders still consume the exchange-reported USDC hold,
                # but do not reserve a perpetual trading slot.
                if order["coin"] not in perp_markets:
                    continue
                if type(order.get("reduceOnly")) is not bool:
                    raise AqlaError("Order reduction scope is unknown")
                if not order["reduceOnly"]:
                    entries.append({"market": order["coin"], "oid": order.get("oid"), "cloid": order.get("cloid")})
        if mode == "unifiedAccount":
            spot = await self.info("spotClearinghouseState", user=self.account)
            balances = [row for row in spot.get("balances", []) if row.get("token") == 0 and row.get("coin") == "USDC"]
            if len(balances) != 1:
                raise AqlaError("Unified USDC balance is unavailable or ambiguous")
            equity, hold = Decimal(balances[0]["total"]), Decimal(balances[0]["hold"])
            available = equity - hold
            # This exchange ceiling includes cross/isolated maintenance. It is
            # an additional ceiling, never a replacement for initial holds.
            ceilings = [Decimal(value) for token, value in spot.get("tokenToAvailableAfterMaintenance", []) if token == 0]
            if len(ceilings) != 1 or hold < 0 or available < 0:
                raise AqlaError("Unified USDC availability is not completely proved")
            available = min(available, max(Decimal(0), ceilings[0]))
        else:
            raw = states.get(market.dex if market else "")
            equity, available = Decimal(raw["marginSummary"]["accountValue"]), Decimal(raw["withdrawable"])
        if now_ms() - stamp > 10000:
            raise AqlaError("Complete account observation is stale")
        snapshot = AccountSnapshot(equity=equity, available_margin=available, position_size=positions.get(market.symbol, Decimal(0)) if market else None,
                                   observed_at=stamp, account_mode=mode or "disabled", positions=positions, entry_orders=entries)
        self.last_inventory = snapshot
        return snapshot

    async def verify_signer(self):
        if self.wallet is None:
            raise AqlaError("Signing key is not configured; public reads remain available")
        if self.settings.signer_role == "master":
            if self.wallet.address.lower() != self.settings.account.lower():
                raise AqlaError("Master signer differs from configured account")
        else:
            role = await self.info("userRole", user=self.wallet.address)
            if role.get("role") != "agent" or role.get("data", {}).get("user", "").lower() != self.settings.account.lower():
                raise AqlaError("API wallet is not bound to the configured account")

    def cloid(self, operation_id, leg=0):
        return "0x" + digest([self.settings.network, self.account.lower(), operation_id, leg])[:32]

    def _nonce(self, requested=None, *, allow_existing=False):
        scope = self.settings.network + ":" + self.wallet.address.lower()

        def advance(state):
            prior = state["nonces"].get(scope, 0)
            value = max(now_ms(), prior + 1) if requested is None else requested
            if requested is not None and (not isinstance(requested, int) or not now_ms() - 172800000 < requested < now_ms() + 86400000):
                raise AqlaError("Requested nonce is outside the exchange time window")
            if requested is not None and not allow_existing and value <= prior:
                raise AqlaError("Prepared nonce was already consumed; prepare and collect signatures again")
            state["nonces"][scope] = max(prior, value)
            return value

        return self.state.update(advance)

    async def mutate(self, operation_id: str, action: dict, *, deadline=None, sign_method="l1", signer=None, prepare_guard=None, async_prepare_guard=None):
        """Durable request binding, no automatic retries, per-leg result preservation."""
        request = {"network": self.settings.network, "account": self.account.lower(), "action": action,
                   "deadline": deadline, "sign_method": sign_method}
        def build(nonce, expires):
            signed_action = copy.deepcopy(action)
            if sign_method == "l1":
                signature = signing.sign_l1_action(self.wallet, signed_action, self.settings.vault_address, nonce, expires, self.settings.network == "mainnet")
            else:
                if self.settings.signer_role != "master":
                    raise AqlaError("This user-signed action requires the master signer")
                if signer is None:
                    raise AqlaError("No documented signing contract for this action")
                signed_action["nonce" if "nonce" in signed_action else "time"] = nonce
                signature = signer(self.wallet, signed_action, self.settings.network == "mainnet")
                expires = None
            payload = {"action": signed_action, "nonce": nonce, "signature": signature}
            if expires is not None:
                payload["expiresAfter"] = expires
                payload["vaultAddress"] = self.settings.vault_address
            return payload

        return await self._dispatch(operation_id, request, build, deadline, prepare_guard=prepare_guard, async_prepare_guard=async_prepare_guard)

    async def _dispatch(self, operation_id, request, build, deadline=None, sender=None, nonce_override=None, allow_existing_nonce=False, prepare_guard=None, async_prepare_guard=None):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 160:
            raise AqlaError("Operation identity must contain 1..160 characters")
        self.state.assert_binding(self.settings)
        request_hash = digest(request)
        async with self.mutations:
            previous = self.state.snapshot()["operations"].get(operation_id)
            if previous:
                self.state.assert_operation_binding(self.settings, previous)
                matches = previous["request_hash"] == request_hash
                if not matches and request.get("operation") and "signing_scope" not in previous["request"]:
                    legacy_request = {key: value for key, value in request.items() if key not in ("principal", "signing_scope")}
                    matches = previous["request"] == legacy_request and previous["request_hash"] == digest(legacy_request)
                if not matches:
                    raise AqlaError("Operation ID already identifies a different request")
                return previous
            if deadline is not None and now_ms() >= deadline:
                raise AqlaError("Operation deadline has expired")
            await self.verify_signer()
            if async_prepare_guard is not None:
                await async_prepare_guard()
            nonce = self._nonce(nonce_override, allow_existing=allow_existing_nonce)
            expires = min(now_ms() + 30000, deadline) if deadline is not None else now_ms() + 30000
            payload = json.loads(json.dumps(build(nonce, expires), allow_nan=False))
            record = {"operation_id": operation_id, "request_hash": request_hash, "request": request,
                      "wire_action": payload["action"], "nonce": nonce, "expires_after": payload.get("expiresAfter"),
                      "wire_vault_address": payload.get("vaultAddress"),
                      "phase": "prepared", "created_at": now_ms(), "result": None}
            def prepare(state):
                self.state.assert_operation_binding(self.settings, record)
                if prepare_guard is not None:
                    prepare_guard(state)
                if deadline is not None and now_ms() >= deadline:
                    raise AqlaError("Operation deadline has expired before preparation")
                state["operations"][operation_id] = record
            self.state.update(prepare)
            if async_prepare_guard is not None:
                try:
                    await async_prepare_guard()
                except BaseException as error:
                    message = str(error) if isinstance(error, AqlaError) else "Entry guard failed before any exchange send"
                    self.state.update(lambda state: state["operations"][operation_id].update(phase="rejected", outcome="not_sent", result={"error": message}))
                    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise
                    return self.state.snapshot()["operations"][operation_id]
            try:
                def begin_send(state):
                    self.state.assert_operation_binding(self.settings, record)
                    if prepare_guard is not None:
                        prepare_guard(state)
                    if deadline is not None and now_ms() >= deadline:
                        raise AqlaError("Entry deadline expired before exchange send")
                    state["operations"][operation_id].update(phase="sending")
                self.state.update(begin_send)
            except AqlaError as error:
                message = str(error)
                self.state.update(lambda state: state["operations"][operation_id].update(phase="rejected", outcome="not_sent", result={"error": message}))
                return self.state.snapshot()["operations"][operation_id]
            try:
                if sender is None:
                    response = await self.client.post("/exchange", json=payload)
                    response.raise_for_status()
                    result = response.json()
                else:
                    result = await sender(payload)
                phase, outcome, legs = self.decode_result(payload["action"], result)
                self.state.update(lambda state: state["operations"][operation_id].update(phase=phase, outcome=outcome, legs=legs, result=result))
            except BaseException as error:
                self.state.update(lambda state: state["operations"][operation_id].update(phase="unknown", result={"error": "Exchange outcome unknown; reconcile exact identities, do not resubmit"}))
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
            return self.state.snapshot()["operations"][operation_id]

    @staticmethod
    def decode_result(action, result):
        if not isinstance(result, dict) or result.get("status") not in ("ok", "err"):
            return "unknown", "unknown", []
        if result["status"] == "err":
            return "rejected", "rejected", []
        response = result.get("response")
        if not isinstance(response, dict):
            return "unknown", "unknown", []
        data = response.get("data", {})
        if not isinstance(data, dict):
            return "unknown", "unknown", []
        status = data.get("status")
        if isinstance(status, dict) and isinstance(status.get("error"), str):
            if response.get("type") != action.get("type"):
                return "unknown", "unknown", []
            return "rejected", "rejected", [{"index": 0, "disposition": "rejected", "status": status}]
        if action.get("type") in ("twapOrder", "twapCancel"):
            if response.get("type") != action["type"]:
                return "unknown", "unknown", []
            if action["type"] == "twapCancel" and status == "success":
                return "acknowledged", "accepted", []
            running = status.get("running") if isinstance(status, dict) else None
            if action["type"] == "twapOrder" and isinstance(running, dict) and type(running.get("twapId")) is int and running["twapId"] > 0:
                return "acknowledged", "accepted", []
            return "unknown", "unknown", []
        if action.get("type") == "trailingStop":
            if response.get("type") == "trailingStop" and type(data.get("oid")) is int and data["oid"] > 0:
                return "acknowledged", "accepted", []
            return "unknown", "unknown", []
        statuses = data.get("statuses")
        expected = len(action.get("orders", action.get("modifies", action.get("cancels", []))))
        if action.get("type") == "modify":
            expected = 1
        if statuses is None:
            if expected:
                return "unknown", "unknown", []
            return ("acknowledged", "accepted", []) if response.get("type") == "default" else ("unknown", "unknown", [])
        response_type = {"order": "order", "modify": "order", "batchModify": "order", "cancel": "cancel", "cancelByCloid": "cancel"}.get(action.get("type"))
        if response_type is None or response.get("type") != response_type:
            return "unknown", "unknown", []
        if not isinstance(statuses, list) or expected and len(statuses) != expected:
            return "unknown", "unknown", []
        legs = []
        for index, status in enumerate(statuses):
            if isinstance(status, dict) and isinstance(status.get("error"), str):
                disposition = "rejected"
            elif isinstance(status, str) and status in ("success", "waitingForFill", "waitingForTrigger"):
                disposition = "accepted"
            elif isinstance(status, dict) and (isinstance(status.get("filled"), dict) or isinstance(status.get("resting"), dict)):
                disposition = "accepted"
            else:
                disposition = "unknown"
            legs.append({"index": index, "disposition": disposition, "status": status})
        outcomes = {leg["disposition"] for leg in legs}
        if "unknown" in outcomes:
            return "unknown", "unknown", legs
        if outcomes == {"rejected"}:
            return "rejected", "rejected", legs
        return "acknowledged", "partial" if "rejected" in outcomes else "accepted", legs

    async def prepare_sdk_action(self, operation_id, request, *, multisig=False, target_account=None):
        from .catalog import SDK_HELPERS, SDK_METHODS, cloid_arguments, validate_sdk

        params = validate_sdk(request)
        operation = params.pop("operation")
        api_wallet_methods = {"order", "bulk_orders", "modify_order", "bulk_modify_orders_new", "market_open", "market_close",
                              "cancel", "cancel_by_cloid", "bulk_cancel", "bulk_cancel_by_cloid", "schedule_cancel",
                              "update_leverage", "update_isolated_margin", "agent_enable_dex_abstraction", "agent_set_abstraction", "noop"}
        if not multisig and self.settings.signer_role != "master" and operation not in api_wallet_methods:
            raise AqlaError("This operation requires a master signer")
        signing_scope = sdk_signing_scope(operation)
        request_binding = {"network": self.settings.network, "account": self.account.lower(), "operation": operation, "params": params,
                           "principal": self.settings.account.lower(), "signing_scope": signing_scope}
        # Fill in optional client identities before SDK signing, never after it.
        if operation in ("order", "market_open", "market_close", "modify_order") and params.get("cloid") is None:
            params["cloid"] = self.cloid(operation_id)
        for i, order in enumerate(params.get("order_requests", [])):
            if order.get("cloid") is None:
                order["cloid"] = self.cloid(operation_id, i)
        market_operations = {"order", "bulk_orders", "modify_order", "bulk_modify_orders_new", "market_open", "market_close",
                             "cancel", "cancel_by_cloid", "bulk_cancel", "bulk_cancel_by_cloid", "update_leverage", "update_isolated_margin"}
        if operation in market_operations:
            await self.markets("perp")
            await self.markets("spot")
        names = []

        def gather(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("name", "coin") and isinstance(item, str):
                        names.append(item)
                    else:
                        gather(item)
            elif isinstance(value, list):
                for item in value:
                    gather(item)

        gather(params)
        for name in names if operation in market_operations else []:
            if ":" in name:
                await self.markets("perp", name.split(":", 1)[0])
        positions = {}
        effective_account = self.settings.vault_address or target_account or self.account
        if operation == "market_close":
            coin = params["coin"]
            dex = coin.split(":", 1)[0] if ":" in coin else ""
            positions[dex] = await self.info("clearinghouseState", user=effective_account, dex=dex)
        metadata = dict(self.market_cache)

        class PreparedInfo:
            name_to_coin = {name: market.symbol for name, market in metadata.items()}
            coin_to_asset = {market.symbol: market.asset_id for market in metadata.values()}
            asset_to_sz_decimals = {market.asset_id: market.size_decimals for market in metadata.values()}

            def name_to_asset(self, name):
                if name not in metadata:
                    raise AqlaError("Unknown market in exchange action")
                return metadata[name].asset_id

            def all_mids(self, dex=""):
                return {market.symbol: str(market.mid) for market in metadata.values() if market.dex == dex}

            def user_state(self, address, dex=""):
                if address.lower() != self_account.lower() or dex not in positions:
                    raise AqlaError("Market-close helper lacks a fresh bound position snapshot")
                return positions[dex]

        self_account = effective_account

        def build(nonce, expires):
            sdk = object.__new__(Exchange)
            sdk.wallet, sdk.base_url = self.wallet, self.settings.base_url
            sdk.vault_address, sdk.account_address = self.settings.vault_address, target_account or self.settings.account
            sdk.info, sdk.expires_after = PreparedInfo(), expires
            # Bind isolated SDK function globals. No global timestamp monkeypatch or second dispatcher.
            for name, method in (SDK_METHODS | SDK_HELPERS).items():
                globals_copy = dict(method.__globals__, get_timestamp_ms=lambda: nonce)
                function = types.FunctionType(method.__code__, globals_copy, method.__name__, method.__defaults__, method.__closure__)
                function.__kwdefaults__ = method.__kwdefaults__
                setattr(sdk, name, types.MethodType(function, sdk))

            def slippage_price(name, is_buy, slippage, px=None):
                market = metadata[name]
                price = float(market.mid) if not px else px
                price *= (1 + slippage) if is_buy else (1 - slippage)
                decimals = (6 if market.product == "perp" else 8) - market.size_decimals
                return round(float(f"{price:.5g}"), decimals)

            # An isolated facade override: HIP-3 IDs are above the spot range,
            # but their price precision still follows the perpetual contract.
            sdk._slippage_price = slippage_price

            def capture(action, signature, timestamp):
                if timestamp != nonce:
                    raise AqlaError("SDK signing timestamp differs from the durable nonce; action was not sent")
                payload = {"action": action, "signature": signature, "nonce": timestamp}
                if "signatureChainId" not in action:
                    if signing_scope == "user":
                        raise AqlaError("SDK action differs from its documented signing scope")
                    payload["vaultAddress"] = None if signing_scope == "principal" else self.settings.vault_address
                    payload["expiresAfter"] = expires
                elif signing_scope != "user":
                    raise AqlaError("SDK action differs from its documented signing scope")
                return payload

            sdk._post_action = capture
            arguments = cloid_arguments(params)
            if operation == "noop":
                arguments["nonce"] = nonce
            payload = vars(sdk)[operation](**arguments)
            if not isinstance(payload, dict) or "action" not in payload or "signature" not in payload:
                raise AqlaError("SDK operation did not produce exactly one signed action")
            return payload

        return request_binding, build

    async def sdk_action(self, operation_id, request, *, sender=None):
        binding, build = await self.prepare_sdk_action(operation_id, request)
        noop = binding["operation"] == "noop"
        requested = binding["params"]["nonce"] if noop else None
        return await self._dispatch(operation_id, binding, build, sender=sender, nonce_override=requested, allow_existing_nonce=noop)

    async def order_status(self, identity, user=None):
        return await self.info("orderStatus", user=user or self.account, oid=identity)

    async def native_action(self, operation_id, request, *, sender=None):
        from .actions import native_payload, sign_native

        action = native_payload(request)
        api_actions = {"order", "cancel", "cancelByCloid", "modify", "batchModify", "twapOrder", "twapCancel", "trailingStop", "reserveRequestWeight", "agentSendAsset"}
        if self.settings.signer_role != "master" and action["type"] not in api_actions:
            raise AqlaError("This native action requires a master signer")
        if action["type"] == "order":
            for i, order in enumerate(action["orders"]):
                order.setdefault("c", self.cloid(operation_id, i))
        request_binding = {"network": self.settings.network, "account": self.account.lower(), "native_action": action}
        return await self._dispatch(operation_id, request_binding,
                                    lambda nonce, expires: sign_native(self.wallet, action, nonce, expires, self.settings), sender=sender)

    async def reconcile(self, operation_id):
        self.state.assert_binding(self.settings)
        record = self.state.snapshot()["operations"].get(operation_id)
        if not record:
            raise AqlaError("Unknown operation")
        self.state.assert_operation_binding(self.settings, record)
        action = record["wire_action"]
        identities = [order["c"] for order in action.get("orders", []) if order.get("c")]
        if action.get("type") == "cancelByCloid":
            identities = [order["cloid"] for order in action["cancels"]]
        if action.get("type") == "cancel":
            identities = [order["o"] for order in action["cancels"]]
        observations = [await self.order_status(identity, record["request"].get("account")) for identity in identities]
        def proves(identity, row):
            detail = row.get("order", {})
            order = detail.get("order", {})
            matches = type(order.get("oid")) is int and order["oid"] == identity if type(identity) is int else str(order.get("cloid", "")).lower() == identity.lower()
            terminal = action.get("type") not in ("cancel", "cancelByCloid") or detail.get("status") in TERMINAL_ORDER
            return row.get("status") == "order" and matches and terminal and detail.get("status") not in (None, "unknownOid")
        proven = bool(identities) and all(proves(identity, row) for identity, row in zip(identities, observations, strict=True))
        if proven:
            self.state.update(lambda state: state["operations"][operation_id].update(phase="reconciled", evidence=observations))
        return {"operation": self.state.snapshot()["operations"][operation_id], "evidence": observations,
                "resolved": proven, "resubmitted": False, "absence_is_proof": False}

    async def close(self):
        await self.client.aclose()
