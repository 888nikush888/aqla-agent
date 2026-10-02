import copy

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.multisig import prepare, submit

MULTISIG_USER = "0x" + "3" * 40
ORDER_REQUEST = {"type": "order", "orders": [{"a": 0, "b": True, "p": "100", "s": "1", "r": False, "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"}


def signature(wallet, payload):
    signed = wallet.sign_message(encode_typed_data(full_message=payload))
    return {"r": "0x" + signed.r.to_bytes(32, "big").hex(), "s": "0x" + signed.s.to_bytes(32, "big").hex(), "v": signed.v}


@pytest.mark.asyncio
async def test_multisig_prepares_without_sending_then_verifies_threshold_and_submits_once(setup):
    settings, state, fake, exchange, _ = setup
    settings.signer_role = "master"
    other = Account.create()
    fake.multisig_users = [exchange.wallet.address, other.address]
    fake.multisig_threshold = 2
    prepared = await prepare(exchange, "multisig-order", MULTISIG_USER, ORDER_REQUEST)
    assert fake.actions == []
    assert prepared["sent"] is False
    signatures = [signature(exchange.wallet, prepared["signing_payload"]), signature(other, prepared["signing_payload"])]
    with pytest.raises(AqlaError, match="threshold"):
        await submit(exchange, "multisig-order", MULTISIG_USER, ORDER_REQUEST, prepared["nonce"], signatures[:1])
    result = await submit(exchange, "multisig-order", MULTISIG_USER, ORDER_REQUEST, prepared["nonce"], signatures)
    assert result["phase"] == "acknowledged"
    assert fake.actions[0]["payload"]["multiSigUser"] == MULTISIG_USER
    assert fake.actions[0]["payload"]["outerSigner"] == exchange.wallet.address.lower()
    assert result["nonce"] == prepared["nonce"]
    assert len(state.snapshot()["operations"]) == 1
    again = await submit(exchange, "multisig-order", MULTISIG_USER, ORDER_REQUEST, prepared["nonce"], signatures)
    assert again["operation_id"] == result["operation_id"]
    assert len(fake.actions) == 1


@pytest.mark.asyncio
async def test_multisig_payload_change_and_duplicate_signature_are_rejected_before_network_mutation(setup):
    settings, _, fake, exchange, _ = setup
    settings.signer_role = "master"
    fake.multisig_users = [exchange.wallet.address]
    prepared = await prepare(exchange, "multisig-guard", MULTISIG_USER, ORDER_REQUEST)
    signed = signature(exchange.wallet, prepared["signing_payload"])
    with pytest.raises(AqlaError, match="duplicated"):
        await submit(exchange, "multisig-guard", MULTISIG_USER, ORDER_REQUEST, prepared["nonce"], [signed, signed])
    changed = copy.deepcopy(ORDER_REQUEST)
    changed["orders"][0]["s"] = "2"
    with pytest.raises(AqlaError, match="different payload"):
        await submit(exchange, "multisig-guard", MULTISIG_USER, changed, prepared["nonce"], [signed])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_user_signed_multisig_uses_human_signing_value_and_canonical_outer_wire(setup):
    settings, _, fake, exchange, _ = setup
    settings.signer_role = "master"
    fake.multisig_users = [exchange.wallet.address]
    request = {"operation": "user_set_abstraction", "user": MULTISIG_USER, "abstraction": "disabled"}
    prepared = await prepare(exchange, "multisig-abstraction", MULTISIG_USER, request)
    assert prepared["signing_payload"]["message"]["abstraction"] == "disabled"
    signed = signature(exchange.wallet, prepared["signing_payload"])
    await submit(exchange, "multisig-abstraction", MULTISIG_USER, request, prepared["nonce"], [signed])
    assert fake.actions[0]["payload"]["action"]["abstraction"] == "i"


@pytest.mark.asyncio
async def test_noop_keeps_the_explicit_requested_nonce(setup):
    _, _, _, exchange, _ = setup
    from aqla_hyperliquid_mcp.models import now_ms
    nonce = now_ms()
    result = await exchange.sdk_action("explicit-noop", {"operation": "noop", "nonce": nonce})
    assert result["nonce"] == nonce
