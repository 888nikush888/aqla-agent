"""Independent wire and persisted-scope regressions from the final API review."""
import copy
import json
from decimal import Decimal

import httpx
import pytest
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from test_sdk_actions import CASES

from aqla_hyperliquid_mcp.api import SDK_PRINCIPAL_L1, SDK_TRADING_L1, SDK_USER_SIGNED, Hyperliquid
from aqla_hyperliquid_mcp.catalog import SDK_METHODS
from aqla_hyperliquid_mcp.models import AqlaError, digest
from aqla_hyperliquid_mcp.state import State

VAULT = "0x" + "4" * 40
OTHER = "0x" + "5" * 40


def scoped_exchange(setup, vault=VAULT):
    settings, _, _, exchange, _ = setup
    settings = settings.model_copy(update={"vault_address": vault, "signer_role": "master",
                                           "state_path": settings.state_path.with_name("scoped.json")})
    state = State(settings)
    return settings, state, Hyperliquid(settings, state, exchange.client)


def test_every_sdk_handler_has_exactly_one_signing_contract():
    assert SDK_PRINCIPAL_L1 | SDK_TRADING_L1 | SDK_USER_SIGNED == set(SDK_METHODS)
    assert not SDK_PRINCIPAL_L1 & SDK_TRADING_L1
    assert not SDK_PRINCIPAL_L1 & SDK_USER_SIGNED
    assert not SDK_TRADING_L1 & SDK_USER_SIGNED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,params", CASES.items())
async def test_every_sdk_wire_scope_from_a_vault_and_l1_recovered_signer(setup, operation, params):
    settings, state, exchange = scoped_exchange(setup)
    setup[2].position = 1.0
    payloads = []

    async def capture(payload):
        payloads.append(payload)
        return {"status": "ok", "response": {"type": "default"}}

    try:
        request = {"operation": operation, **copy.deepcopy(params)}
        record = await exchange.sdk_action("vault:" + operation, request, sender=capture)
        payload = payloads[0]
        assert record["request"]["principal"] == settings.account.lower()
        assert record["request"]["account"] == VAULT
        if "signatureChainId" in payload["action"]:
            assert operation in SDK_USER_SIGNED
            assert "vaultAddress" not in payload and "expiresAfter" not in payload
            assert record["request"]["signing_scope"] == "user"
            if operation == "usd_class_transfer":
                assert payload["action"]["amount"].endswith(" subaccount:" + VAULT)
            elif operation == "send_asset":
                assert payload["action"]["fromSubAccount"] == VAULT
        else:
            expected = None if operation in SDK_PRINCIPAL_L1 else VAULT
            assert payload["vaultAddress"] == expected
            assert record["wire_vault_address"] == expected
            recovered = recover_agent_or_user_from_l1_action(
                payload["action"], payload["signature"], payload["vaultAddress"], payload["nonce"],
                payload["expiresAfter"], False)
            assert recovered.lower() == exchange.wallet.address.lower()
            wrong = recover_agent_or_user_from_l1_action(
                payload["action"], payload["signature"], VAULT if expected is None else None,
                payload["nonce"], payload["expiresAfter"], False)
            assert wrong.lower() != exchange.wallet.address.lower()
        again = await exchange.sdk_action("vault:" + operation, request, sender=capture)
        assert again == record and len(payloads) == 1
        state.close()
        with State(settings) as restored:
            assert restored.snapshot()["operations"]["vault:" + operation] == record
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["scope", "principal", "wire_vault", "target"])
async def test_forged_sdk_scope_is_rejected_on_reopen_without_rewriting(setup, tamper):
    settings, state, exchange = scoped_exchange(setup)
    try:
        await exchange.sdk_action("referrer", {"operation": "set_referrer", "code": "CODE"})
        data = state.snapshot()
        record = data["operations"]["referrer"]
        if tamper == "scope":
            record["request"]["signing_scope"] = "trading"
        elif tamper == "principal":
            record["request"]["principal"] = OTHER
        elif tamper == "wire_vault":
            record["wire_vault_address"] = VAULT
        else:
            record["request"]["account"] = OTHER
        state._write(data)
        state.close()
        original = state.path.read_bytes()
        with pytest.raises(AqlaError, match="Saved"):
            State(settings)
        assert state.path.read_bytes() == original
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("vault,operation", [(None, "set_referrer"), (VAULT, "order"), (VAULT, "set_referrer")])
async def test_legacy_sdk_records_preserve_valid_scope_and_refuse_ambiguous_vault_principal(setup, vault, operation):
    settings, state, exchange = scoped_exchange(setup, vault)
    request = {"operation": operation, **copy.deepcopy(CASES[operation])}
    try:
        await exchange.sdk_action("legacy", request)
        data = state.snapshot()
        record = data["operations"]["legacy"]
        record["request"].pop("principal")
        record["request"].pop("signing_scope")
        record.pop("wire_vault_address")
        record["request_hash"] = digest(record["request"])
        state._write(data)
        state.close()
        if vault and operation == "set_referrer":
            original = state.path.read_bytes()
            with pytest.raises(AqlaError, match="ambiguous vault signing scope"):
                State(settings)
            assert state.path.read_bytes() == original
        else:
            with State(settings) as restored:
                replay = Hyperliquid(settings, restored, exchange.client)
                before = len(setup[2].actions)
                assert await replay.sdk_action("legacy", request) == record
                assert len(setup[2].actions) == before
    finally:
        state.close()


def metadata_transport(fake, *, collateral=0, include_collateral=True):
    def handle(request):
        query = json.loads(request.content)
        if request.url.path == "/info":
            if query.get("type") == "perpDexs":
                return httpx.Response(200, json=[None, {"name": "xyz"}])
            if query.get("type") == "spotMeta":
                return httpx.Response(200, json={
                    "tokens": [{"index": 0, "name": "USDC", "szDecimals": 8},
                               {"index": 360, "name": "USDH", "szDecimals": 2},
                               {"index": 2, "name": "LOW", "szDecimals": 2}],
                    "universe": [{"index": 2, "name": "@2", "tokens": [2, 0]}]})
            if query.get("type") == "allMids":
                return httpx.Response(200, json={"xyz:LOW": "0.0123"} if query.get("dex") else
                                      {"BTC": "0.0123", "@2": "0.0123"})
            if query.get("type") == "meta" and query.get("dex") == "xyz":
                meta = {"universe": [{"name": "xyz:LOW", "szDecimals": 2, "maxLeverage": 20}]}
                if include_collateral:
                    meta["collateralToken"] = collateral
                return httpx.Response(200, json=meta)
            if query.get("type") == "clearinghouseState" and query.get("dex") == "xyz":
                return httpx.Response(200, json={"assetPositions": [
                    {"position": {"coin": "xyz:LOW", "szi": "1000", "entryPx": "0.0123"}}]})
        return fake.handle(request)
    return httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("collateral,include,quote", [(0, True, "USDC"), (360, True, "USDH"),
                                                      (999, True, "UNKNOWN"), (None, False, "UNKNOWN"),
                                                      (True, True, "UNKNOWN")])
async def test_hip3_collateral_identity_controls_auto_route_but_full_sdk_stays_available(setup, collateral, include, quote):
    settings, state, fake, exchange, _ = setup
    exchange.client = httpx.AsyncClient(base_url=settings.base_url,
                                       transport=metadata_transport(fake, collateral=collateral, include_collateral=include))
    try:
        market = (await exchange.markets("perp", "xyz"))["xyz:LOW"]
        assert market.quote_token == quote
        if collateral == 0 and type(collateral) is int:
            assert (await exchange.market("xyz:LOW")).collateral_token == 0
        else:
            with pytest.raises(AqlaError, match="USDC"):
                await exchange.market("xyz:LOW")
        assert (await exchange.market("BTC")).collateral_token == 0  # Legacy native metadata is protocol USDC.
        payloads = []

        async def capture(payload):
            payloads.append(payload)
            return {"status": "ok", "response": {"type": "default"}}

        await exchange.sdk_action("full-hip3", {"operation": "market_open", "name": "xyz:LOW", "is_buy": True,
                                               "sz": 1000.0}, sender=capture)
        assert payloads[0]["action"]["orders"][0]["a"] == 110000
        assert len(state.snapshot()["operations"]) == 1
    finally:
        await exchange.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,operation,is_buy,expected", [
    ("xyz:LOW", "market_open", True, "0.0129"), ("xyz:LOW", "market_open", False, "0.0117"),
    ("xyz:LOW", "market_close", False, "0.0117"), ("BTC", "market_open", True, "0.0129"),
    ("@2", "market_open", True, "0.012915"),
])
async def test_sdk_market_helpers_use_product_precision_in_actual_signed_wire(setup, name, operation, is_buy, expected):
    settings, _, fake, exchange, _ = setup
    exchange.client = httpx.AsyncClient(base_url=settings.base_url, transport=metadata_transport(fake))
    try:
        payloads = []

        async def capture(payload):
            payloads.append(payload)
            return {"status": "ok", "response": {"type": "default"}}

        request = {"operation": operation, "coin": name, "sz": 1000.0} if operation == "market_close" else {
            "operation": operation, "name": name, "is_buy": is_buy, "sz": 1000.0}
        await exchange.sdk_action("precision", request, sender=capture)
        order = payloads[0]["action"]["orders"][0]
        assert order["p"] == expected
        decimals = max(0, -Decimal(order["p"]).as_tuple().exponent)
        assert decimals <= (6 if name != "@2" else 8) - 2
        assert order["b"] == is_buy
    finally:
        await exchange.close()
