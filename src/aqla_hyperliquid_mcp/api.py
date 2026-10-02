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

from .models import AccountSnapshot, AqlaError, Market, Settings, digest, now_ms
from .state import State


def wire_number(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise AqlaError("Non-finite exchange number")
    return format(number, "f").rstrip("0").rstrip(".") if number % 1 else format(number.quantize(Decimal(1)), "f")


class Hyperliquid:
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
            offset = 0
            if dex:
                dexes = await self.info("perpDexs")
                index = next((i for i, item in enumerate(dexes) if item and item["name"] == dex), None)
                if index is None:
                    raise AqlaError("Unknown Hyperliquid DEX namespace")
                offset = 100000 + (index - 1) * 10000
            for index, row in enumerate(meta["universe"]):
                if row.get("isDelisted") or row["name"] not in mids:
                    continue
                name = row["name"]
                result[name] = Market(symbol=name, asset_id=offset + index, product="perp",
                                      size_decimals=row["szDecimals"], max_leverage=row["maxLeverage"],
                                      mid=mids[name], dex=dex)
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

    async def market(self, symbol: str, product="perp"):
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
            raise AqlaError("Source market cannot be resolved uniquely; no substitute market is used")
        market = next(iter(unique.values()))
        if market.quote_token != "USDC":
            raise AqlaError("Automatic risk sizing currently requires USDC quote currency")
        return market

    async def account_snapshot(self, market: Market):
        if market.product == "spot":
            raw = await self.info("spotClearinghouseState", user=self.account)
            balance = next((row for row in raw["balances"] if row["coin"] == "USDC"), None)
            if not balance:
                raise AqlaError("No USDC balance for spot entry")
            available = Decimal(balance["total"]) - Decimal(balance.get("hold", "0"))
            return AccountSnapshot(equity=Decimal(balance["total"]), available_margin=available)
        abstraction = await self.info("userAbstraction", user=self.account)
        if abstraction not in ("disabled", None):
            raise AqlaError("Automatic sizing requires standard account abstraction; other modes remain available through the full MCP")
        raw = await self.info("clearinghouseState", user=self.account, **({"dex": market.dex} if market.dex else {}))
        return AccountSnapshot(equity=raw["marginSummary"]["accountValue"], available_margin=raw["withdrawable"])

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

    async def mutate(self, operation_id: str, action: dict, *, deadline=None, sign_method="l1", signer=None):
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

        return await self._dispatch(operation_id, request, build, deadline)

    async def _dispatch(self, operation_id, request, build, deadline=None, sender=None, nonce_override=None, allow_existing_nonce=False):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 160:
            raise AqlaError("Operation identity must contain 1..160 characters")
        request_hash = digest(request)
        async with self.mutations:
            previous = self.state.snapshot()["operations"].get(operation_id)
            if previous:
                if previous["request_hash"] != request_hash:
                    raise AqlaError("Operation ID already identifies a different request")
                return previous
            if deadline is not None and now_ms() >= deadline:
                raise AqlaError("Operation deadline has expired")
            await self.verify_signer()
            nonce = self._nonce(nonce_override, allow_existing=allow_existing_nonce)
            expires = min(now_ms() + 30000, deadline) if deadline is not None else now_ms() + 30000
            payload = json.loads(json.dumps(build(nonce, expires), allow_nan=False))
            record = {"operation_id": operation_id, "request_hash": request_hash, "request": request,
                      "wire_action": payload["action"], "nonce": nonce, "expires_after": payload.get("expiresAfter"),
                      "phase": "prepared", "created_at": now_ms(), "result": None}
            self.state.update(lambda state: state["operations"].__setitem__(operation_id, record))
            self.state.update(lambda state: state["operations"][operation_id].update(phase="sending"))
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
        response = result.get("response", {})
        statuses = response.get("data", {}).get("statuses")
        expected = len(action.get("orders", action.get("modifies", action.get("cancels", []))))
        if action.get("type") == "modify":
            expected = 1
        if statuses is None:
            if expected:
                return "unknown", "unknown", []
            return "acknowledged", "accepted", []
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
        request_binding = {"network": self.settings.network, "account": self.account.lower(), "operation": operation, "params": params}
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

            def capture(action, signature, timestamp):
                if timestamp != nonce:
                    raise AqlaError("SDK signing timestamp differs from the durable nonce; action was not sent")
                payload = {"action": action, "signature": signature, "nonce": timestamp}
                if "signatureChainId" not in action:
                    payload["vaultAddress"] = self.settings.vault_address if action["type"] not in ("usdClassTransfer", "sendAsset") else None
                    payload["expiresAfter"] = expires
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

    async def order_status(self, identity):
        return await self.info("orderStatus", user=self.account, oid=identity)

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
        record = self.state.snapshot()["operations"].get(operation_id)
        if not record:
            raise AqlaError("Unknown operation")
        action = record["wire_action"]
        identities = [order["c"] for order in action.get("orders", []) if order.get("c")]
        if action.get("type") == "cancelByCloid":
            identities = [order["cloid"] for order in action["cancels"]]
        if action.get("type") == "cancel":
            identities = [order["o"] for order in action["cancels"]]
        observations = [await self.order_status(identity) for identity in identities]
        proven = bool(identities) and all(row.get("status") == "order" and row.get("order", {}).get("status") not in (None, "unknownOid") for row in observations)
        if proven:
            self.state.update(lambda state: state["operations"][operation_id].update(phase="reconciled", evidence=observations))
        return {"operation": self.state.snapshot()["operations"][operation_id], "evidence": observations,
                "resolved": proven, "resubmitted": False, "absence_is_proof": False}

    async def close(self):
        await self.client.aclose()
