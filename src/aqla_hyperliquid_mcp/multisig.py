"""Typed multisig preparation and submission using the official SDK contracts."""
from __future__ import annotations

from typing import Annotated, Literal

from eth_account import Account
from eth_account.messages import encode_typed_data
from hyperliquid.exchange import _multi_sig_payload_action
from hyperliquid.utils import signing
from pydantic import Field

from .actions import USER_SIGNED_TYPES, NativeRequest, native_payload, sign_native
from .catalog import SdkRequest
from .models import Address, AqlaError, Model, digest, now_ms


class Signature(Model):
    r: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    s: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    v: Literal[27, 28]


SDK_USER_TYPES = {
    "usdSend": ("HyperliquidTransaction:UsdSend", signing.USD_SEND_SIGN_TYPES),
    "spotSend": ("HyperliquidTransaction:SpotSend", signing.SPOT_TRANSFER_SIGN_TYPES),
    "withdraw3": ("HyperliquidTransaction:Withdraw", signing.WITHDRAW_SIGN_TYPES),
    "usdClassTransfer": ("HyperliquidTransaction:UsdClassTransfer", signing.USD_CLASS_TRANSFER_SIGN_TYPES),
    "sendAsset": ("HyperliquidTransaction:SendAsset", signing.SEND_ASSET_SIGN_TYPES),
    "tokenDelegate": ("HyperliquidTransaction:TokenDelegate", signing.TOKEN_DELEGATE_TYPES),
    "convertToMultiSigUser": ("HyperliquidTransaction:ConvertToMultiSigUser", signing.CONVERT_TO_MULTI_SIG_USER_SIGN_TYPES),
    "userDexAbstraction": ("HyperliquidTransaction:UserDexAbstraction", signing.USER_DEX_ABSTRACTION_SIGN_TYPES),
    "userSetAbstraction": ("HyperliquidTransaction:UserSetAbstraction", signing.USER_SET_ABSTRACTION_SIGN_TYPES),
    "approveBuilderFee": ("HyperliquidTransaction:ApproveBuilderFee", [
        {"name": "hyperliquidChain", "type": "string"}, {"name": "maxFeeRate", "type": "string"},
        {"name": "builder", "type": "address"}, {"name": "nonce", "type": "uint64"}]),
}


async def inner_builder(exchange, operation_id, request, multisig_user):
    if isinstance(request, dict):
        from .actions import NATIVE_ADAPTER
        from .catalog import SDK_ADAPTER
        request = SDK_ADAPTER.validate_python(request) if "operation" in request else NATIVE_ADAPTER.validate_python(request)
    if hasattr(request, "operation"):
        _, build = await exchange.prepare_sdk_action(operation_id, request, multisig=True, target_account=multisig_user)
        return build
    action = native_payload(request)
    settings = exchange.settings.model_copy(update={"signer_role": "master"})
    return lambda nonce, expires: sign_native(exchange.wallet, action, nonce, expires, settings)


def signing_message(exchange, inner_action, multisig_user, nonce, expires_after):
    leader = exchange.wallet.address.lower()
    if "signatureChainId" in inner_action:
        if inner_action["type"] in SDK_USER_TYPES:
            primary, fields = SDK_USER_TYPES[inner_action["type"]]
        elif inner_action["type"] in USER_SIGNED_TYPES:
            primary, pairs = USER_SIGNED_TYPES[inner_action["type"]]
            fields = [{"name": name, "type": kind} for name, kind in pairs]
        else:
            raise AqlaError("No typed multisig user-signing contract for this action")
        enriched = signing.add_multi_sig_fields(inner_action, multisig_user, leader)
        return signing.user_signed_payload(primary, signing.add_multi_sig_types(fields), enriched)
    envelope = [multisig_user.lower(), leader, inner_action]
    action_hash = signing.action_hash(envelope, exchange.settings.vault_address, nonce, expires_after)
    return signing.l1_payload(signing.construct_phantom_agent(action_hash, exchange.settings.network == "mainnet"))


async def prepare(exchange, operation_id, multisig_user: Address, request: SdkRequest | NativeRequest, nonce=None, expires_after=None):
    await exchange.verify_signer()
    scope = exchange.settings.network + ":" + exchange.wallet.address.lower()
    nonce = nonce or max(now_ms(), exchange.state.snapshot()["nonces"].get(scope, 0) + 1)
    if expires_after is not None and expires_after <= now_ms():
        raise AqlaError("Multisig expiry has already passed")
    build = await inner_builder(exchange, operation_id, request, multisig_user)
    action = build(nonce, expires_after)["action"]
    message = signing_message(exchange, action, multisig_user, nonce, expires_after)
    return {"multi_sig_user": multisig_user.lower(), "leader": exchange.wallet.address.lower(), "nonce": nonce,
            "expires_after": expires_after, "inner_action": action, "signing_payload": message,
            "sent": False, "nonce_reserved": False}


async def submit(exchange, operation_id, multisig_user, request, nonce, signatures, expires_after=None):
    if nonce is None or not signatures:
        raise AqlaError("Supply the exact prepared nonce and collected multisig signatures")
    if exchange.wallet is None:
        raise AqlaError("Signing key is not configured")
    if isinstance(request, dict):
        from .actions import NATIVE_ADAPTER
        from .catalog import SDK_ADAPTER
        request = SDK_ADAPTER.validate_python(request) if "operation" in request else NATIVE_ADAPTER.validate_python(request)
    signatures = [Signature.model_validate(value).model_dump() for value in signatures]
    request_binding = {"network": exchange.settings.network, "account": multisig_user.lower(), "leader": exchange.wallet.address.lower(),
                       "inner_request": request.model_dump(mode="json", by_alias=True), "signatures": signatures,
                       "nonce": nonce, "expires_after": expires_after}
    existing = exchange.state.snapshot()["operations"].get(operation_id)
    if existing:
        if existing["request_hash"] != digest(request_binding):
            raise AqlaError("Operation ID already identifies a different multisig request")
        return existing
    prepared = await prepare(exchange, operation_id, multisig_user, request, nonce, expires_after)
    group = await exchange.info("userToMultiSigSigners", user=multisig_user)
    if not isinstance(group, dict) or type(group.get("threshold")) is not int or not 1 <= group["threshold"] <= 10 or not group.get("authorizedUsers"):
        raise AqlaError("The target has no verified multisig signer configuration")
    authorized = {address.lower() for address in group["authorizedUsers"]}
    leader_principal = exchange.settings.account.lower()
    if leader_principal not in authorized:
        raise AqlaError("Configured leader is not an authorized multisig user or its API wallet")
    signable = encode_typed_data(full_message=prepared["signing_payload"])
    supplied = []
    recovered = set()
    for value in signatures:
        value = Signature.model_validate(value).model_dump()
        try:
            address = Account.recover_message(signable, vrs=(value["v"], int(value["r"], 16), int(value["s"], 16))).lower()
        except Exception as error:
            raise AqlaError("Invalid collected multisig signature") from error
        if address not in authorized or address in recovered:
            raise AqlaError("Multisig signature is unauthorized, duplicated or belongs to a different payload")
        recovered.add(address)
        supplied.append(value)
    if len(recovered) < group["threshold"]:
        raise AqlaError("Collected signatures do not meet the verified threshold")
    inner = _multi_sig_payload_action(prepared["inner_action"])

    def build(timestamp, _):
        action = {"type": "multiSig", "signatureChainId": "0x66eee", "signatures": supplied,
                  "payload": {"multiSigUser": multisig_user.lower(), "outerSigner": prepared["leader"], "action": inner}}
        signature = signing.sign_multi_sig_action(exchange.wallet, action, exchange.settings.network == "mainnet",
                                                 exchange.settings.vault_address, timestamp, expires_after)
        return {"action": action, "nonce": timestamp, "signature": signature,
                "vaultAddress": exchange.settings.vault_address, "expiresAfter": expires_after}

    return await exchange._dispatch(operation_id, request_binding, build, deadline=expires_after, nonce_override=nonce)
