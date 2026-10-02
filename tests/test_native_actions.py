import copy

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_hyperliquid_mcp.actions import NATIVE_ADAPTER, NATIVE_MODELS, native_payload
from aqla_hyperliquid_mcp.models import AqlaError

ADDRESS = "0x" + "1" * 40
ORDER = {"a": 0, "b": True, "p": "100", "s": "1", "r": True, "t": {"limit": {"tif": "Gtc"}}}
CASES = {
    "order": {"orders": [ORDER]},
    "cancel": {"cancels": [{"a": 0, "o": 1}], "f": True},
    "cancelByCloid": {"cancels": [{"asset": 0, "cloid": "0x" + "2" * 32}]},
    "modify": {"oid": 1, "order": ORDER, "a": True},
    "batchModify": {"modifies": [{"oid": 1, "order": ORDER}]},
    "twapOrder": {"twap": {"a": 0, "b": True, "s": "1", "r": True, "m": 5, "t": False}},
    "twapCancel": {"a": 0, "t": 1},
    "trailingStop": {"asset": 0, "isBuy": False, "sz": "1", "reduceOnly": True, "retracement": {"pct": "1%"}, "activationPx": None},
    "reserveRequestWeight": {"weight": 1, "destination": None},
    "cDeposit": {"wei": 100000000},
    "cWithdraw": {"wei": 100000000},
    "approveAgent": {"agentAddress": ADDRESS},
    "agentSendAsset": {"destination": ADDRESS, "sourceDex": "", "destinationDex": "xyz", "token": "USDC", "amount": "10"},
    "sendToEvmWithData": {"token": "USDC", "amount": "10", "sourceDex": "", "destinationRecipient": ADDRESS,
                          "addressEncoding": "hex", "destinationChainId": 999, "gasLimit": 100000, "data": "0x0001"},
    "hip3LiquidatorTransfer": {"dex": "xyz", "ntl": 1000000000, "isDeposit": True},
    "validatorL1Stream": {"riskFreeRate": "0.04"},
    "authorizeAqav2Role": {"token": 0, "role": "technical"},
    "claimRewards": {},
    "perpDeploy": {"haltTrading": {"coin": "xyz:BTC", "isHalted": True}},
    "userOutcome": {"mergeOutcome": {"outcome": 1, "amount": None}},
    "activateOutcomeDeployer": {"deactivate": None},
    "outcomeDeploy": {"venue": "ab", "operation": {"registerStandaloneOutcomeFromTemplate": {"id": "template", "keywordToValue": [], "deployerFeeScale": "1"}}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,parameters", CASES.items())
async def test_every_native_action_is_validated_signed_and_deduplicated(setup, kind, parameters):
    settings, state, fake, exchange, _ = setup
    settings.signer_role = "master"
    request = {"type": kind, **copy.deepcopy(parameters)}
    result = await exchange.native_action("native:" + kind, request)
    assert result["phase"] == "acknowledged"
    assert fake.actions[-1]["type"] == kind
    assert result["nonce"] == state.snapshot()["nonces"]["testnet:" + exchange.wallet.address.lower()]
    assert "testnet_only" not in fake.actions[-1]
    count = len(fake.actions)
    await exchange.native_action("native:" + kind, request)
    assert len(fake.actions) == count


def test_catalog_covers_all_native_cases_and_rejects_extra_fields():
    assert set(CASES) == set(NATIVE_MODELS)
    for kind, parameters in CASES.items():
        with pytest.raises(ValueError):
            NATIVE_ADAPTER.validate_python({"type": kind, **parameters, "arbitrary_signing_payload": {}})


def test_null_variants_and_values_remain_in_native_wire_payload():
    assert native_payload({"type": "userOutcome", "mergeOutcome": {"outcome": 1, "amount": None}})["mergeOutcome"]["amount"] is None
    assert "deactivate" in native_payload({"type": "activateOutcomeDeployer", "deactivate": None})
    assert native_payload({"type": "order", "orders": [ORDER]})["grouping"] == "na"
    with pytest.raises(ValueError):
        native_payload({"type": "cancel", "cancels": [{"a": 0, "o": 1}], "f": False})
    with pytest.raises(ValueError):
        native_payload({"type": "userOutcome", "mergeOutcome": {"outcome": 1, "amount": None}, "splitOutcome": {"outcome": 1, "amount": "1"}})


@pytest.mark.asyncio
async def test_user_signed_staking_contract_has_independent_eip712_fields(setup):
    settings, _, _, exchange, _ = setup
    settings.signer_role = "master"
    captured = []

    async def sender(payload):
        captured.append(payload)
        return {"status": "ok", "response": {"type": "default"}}

    await exchange.native_action("staking-contract", {"type": "cDeposit", "wei": 100000000}, sender=sender)
    payload = captured[0]
    action = payload["action"]
    assert "expiresAfter" not in payload
    assert action["nonce"] == payload["nonce"]
    assert action["hyperliquidChain"] == "Testnet"
    types = {"HyperliquidTransaction:CDeposit": [{"name": "hyperliquidChain", "type": "string"}, {"name": "wei", "type": "uint64"}, {"name": "nonce", "type": "uint64"}]}
    signable = encode_typed_data(domain_data={"name": "HyperliquidSignTransaction", "version": "1", "chainId": int(action["signatureChainId"], 16),
                                             "verifyingContract": "0x" + "0" * 40}, message_types=types, message_data=action)
    signature = payload["signature"]
    signer = Account.recover_message(signable, vrs=(signature["v"], int(signature["r"], 16), int(signature["s"], 16)))
    assert signer == exchange.wallet.address


@pytest.mark.asyncio
async def test_user_signed_actions_require_master_and_testnet_variants_fail_before_dispatch(setup):
    settings, _, fake, exchange, _ = setup
    with pytest.raises(AqlaError, match="master signer"):
        await exchange.native_action("denied-stake", {"type": "cDeposit", "wei": 1})
    settings.signer_role = "master"
    settings.network = "mainnet"
    with pytest.raises(AqlaError, match="testnet-only"):
        await exchange.native_action("denied-outcome", {"type": "activateOutcomeDeployer", "deactivate": None})
    assert fake.actions == []


def test_deployer_sorting_and_range_contracts():
    payload = native_payload({"type": "perpDeploy", "setFundingMultipliers": [["xyz:ETH", "2"], ["xyz:BTC", "1"]]})
    assert payload["setFundingMultipliers"][0][0] == "xyz:BTC"
    with pytest.raises(AqlaError, match="range"):
        native_payload({"type": "perpDeploy", "setFundingClamps": [["xyz:BTC", "0.02"]]})
    with pytest.raises(ValueError):
        native_payload({"type": "hip3LiquidatorTransfer", "dex": "xyz", "ntl": 1, "isDeposit": True})
