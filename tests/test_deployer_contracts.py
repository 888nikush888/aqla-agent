import pytest

from aqla_hyperliquid_mcp.actions import OutcomeOperation, PerpDeploy, StarProxy, native_payload

A = "0x" + "1" * 40
PERP = {
    "registerAsset": {"assetRequest": {"coin": "xyz:BTC", "szDecimals": 2, "oraclePx": "100", "marginTableId": 1, "onlyIsolated": True}, "dex": "xyz"},
    "registerAsset2": {"assetRequest": {"coin": "xyz:BTC", "szDecimals": 2, "oraclePx": "100", "marginTableId": 1, "marginMode": "normal"}, "dex": "xyz"},
    "setOracle": {"dex": "xyz", "oraclePxs": [["xyz:BTC", "100"]], "markPxs": [[["xyz:BTC", "100"]]], "externalPerpPxs": [["xyz:BTC", "100"]]},
    "setFundingMultipliers": [["xyz:BTC", "1"]],
    "setFundingInterestRates": [["xyz:BTC", "-0.001"]],
    "setFundingClamps": [["xyz:BTC", "0.001"]],
    "haltTrading": {"coin": "xyz:BTC", "isHalted": False},
    "insertMarginTable": {"dex": "xyz", "marginTable": {"description": "test", "marginTiers": [{"lowerBound": 0, "maxLeverage": 10}, {"lowerBound": 10000, "maxLeverage": 5}]}},
    "setMarginTableIds": [["xyz:BTC", 1]],
    "setFeeRecipient": {"dex": "xyz", "feeRecipient": A},
    "setOpenInterestCaps": [["xyz:BTC", None]],
    "setSubDeployers": {"dex": "xyz", "subDeployers": [{"variant": "setOracle", "user": A, "allowed": True}]},
    "setMarginModes": [["xyz:BTC", "normal"]],
    "setDeployerFees": [["xyz", {"scale": "1", "growthMode": False}]],
    "setPerpAnnotation": {"coin": "xyz:BTC", "category": "Crypto", "description": "BTC", "displayName": None, "keywords": ["BTC"]},
    "disableDex": "xyz",
    "star": {"dex": "xyz", "operation": {"proxy": [A, {"modifyApproval": True}]}},
}
TEMPLATE = {"id": "template", "keywordToValue": [["a", "one"]]}
FEE_TEMPLATE = {**TEMPLATE, "deployerFeeScale": "1"}
SETTLEMENT = {"outcome": 1, "settleFraction": "1", "details": "", "nameAndDescription": ["template", "description"], "sideNames": ["Yes", "No"]}
OUTCOME = {
    "registerStandaloneOutcomeFromTemplate": FEE_TEMPLATE,
    "registerQuestionFromTemplate": {"questionTemplateInstance": FEE_TEMPLATE, "namedOutcomeTemplateInstances": [TEMPLATE]},
    "registerAndAssociateNamedOutcomeFromTemplate": {"question": 1, "namedOutcomeTemplateInstance": TEMPLATE},
    "settleOutcome": SETTLEMENT,
    "settleQuestion2": {"question": 1, "outcomeSettlements": [SETTLEMENT], "nameAndDescription": ["template", "description"]},
    "setSubDeployers": [{"variant": "settleQuestion", "user": A, "allowed": True}],
}
PROXY = {
    "modifyApproval": False,
    "modifyBackstopLiquidatorApproval": True,
    "setReduceOnly": True,
    "cancel": {"cancels": [{"a": 100000, "o": 1}]},
    "cancelAll": {"assets": None},
    "order": {"orders": [{"a": 100000, "b": True, "p": "100", "s": "1", "r": True, "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"},
    "sendAsset": {"destination": A, "amount": "100"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,value", PERP.items())
async def test_every_documented_perp_deployer_variant_reaches_the_signed_boundary(setup, variant, value):
    settings, _, fake, exchange, _ = setup
    settings.signer_role = "master"
    result = await exchange.native_action("perp:" + variant, {"type": "perpDeploy", variant: value})
    assert result["phase"] == "acknowledged"
    assert variant in fake.actions[0]
    assert set(fake.actions[0]) == {"type", variant}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,value", OUTCOME.items())
async def test_every_current_outcome_deployer_variant_preserves_its_native_payload(setup, variant, value):
    settings, _, fake, exchange, _ = setup
    settings.signer_role = "master"
    result = await exchange.native_action("outcome:" + variant, {"type": "outcomeDeploy", "venue": "ab", "operation": {variant: value}})
    assert result["phase"] == "acknowledged"
    assert fake.actions[0]["operation"] == {variant: value}


@pytest.mark.parametrize("variant,value", PROXY.items())
def test_every_star_proxy_variant_is_typed_and_preserves_false_and_null(variant, value):
    payload = native_payload({"type": "perpDeploy", "star": {"dex": "xyz", "operation": {"proxy": [A, {variant: value}]}}})
    assert payload["star"]["operation"]["proxy"][1] == {variant: value}
    assert payload["testnet_only"] is True


def test_variant_fixture_sets_are_complete_and_multiple_variants_are_forbidden():
    assert set(PERP) == set(PerpDeploy.variants)
    assert set(OUTCOME) == set(OutcomeOperation.variants)
    assert set(PROXY) == set(StarProxy.variants)
    with pytest.raises(ValueError):
        native_payload({"type": "perpDeploy", "haltTrading": PERP["haltTrading"], "disableDex": "xyz"})
    with pytest.raises(ValueError):
        native_payload({"type": "perpDeploy", "star": {"dex": "xyz", "operation": {"proxy": [A, {"order": {"orders": [{**PROXY['order']['orders'][0], "r": False}]}}]}}})
