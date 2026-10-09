import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from aqla_agent.aster import DOMAIN, Aster, sign_parameters


def typed_message(unsigned, network):
    return {"types": {"EIP712Domain": [{"name": name, "type": kind} for name, kind in (
        ("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address"))],
        "Message": [{"name": "msg", "type": "string"}]}, "primaryType": "Message",
        "domain": {**DOMAIN, "chainId": 714 if network == "testnet" else 1666}, "message": {"msg": unsigned}}


class Nonces:
    def __init__(self):
        self.data = {"nonces": {}}

    def update(self, callback):
        return callback(self.data)


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["testnet", "mainnet"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("parameters", [{}, {"symbol": "BTCUSDT", "limit": "1000"},
    {"symbol": "xyz:GOLD", "newClientOrderId": "space + plus&percent%/utf8-ä",
     "batchOrders": json.dumps([{"symbol": "BTCUSDT", "note": "a+b & c% / Zürich"}], ensure_ascii=False)}])
async def test_aster_recovers_signer_from_exact_transmitted_unsigned_bytes(network, method, parameters):
    wallet = Account.create()
    captured = []
    def handle(request):
        wire = request.url.query.decode("ascii") if method == "GET" else request.content.decode("ascii")
        parts = wire.split("&")
        assert parts[-1].startswith("signature=")
        unsigned = "&".join(parts[:-1])
        parsed = parse_qs(wire, keep_blank_values=True)
        recovered = Account.recover_message(encode_typed_data(full_message=typed_message(unsigned, network)), signature=parsed["signature"][0])
        assert recovered == wallet.address
        assert unsigned == urlencode(sorted({**parameters, "nonce": parsed["nonce"][0], "user": wallet.address.lower(), "signer": wallet.address.lower()}.items()))
        assert all(parsed[key] == [str(value)] for key, value in parameters.items())
        if method != "GET":
            assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
            assert request.url.query == b""
        else:
            assert request.content == b""
        captured.append(wire)
        return httpx.Response(200, json={"code": 200})
    adapter = object.__new__(Aster)
    adapter.settings = SimpleNamespace(network=network)
    adapter.state, adapter.wallet, adapter.account = Nonces(), wallet, wallet.address.lower()
    adapter.client = httpx.AsyncClient(base_url="https://fapi.asterdex.com", transport=httpx.MockTransport(handle))
    try:
        signed = adapter.signed(method, "/fapi/v3/order", parameters)
        before = copy.deepcopy(signed)
        assert await adapter.send(signed) == {"code": 200}
        assert signed == before and captured == [signed["wire_query"]]
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_aster_sign_parameters_normal_vector_uses_canonical_encoded_query(network):
    wallet = Account.create()
    values = {"user": wallet.address.lower(), "nonce": "123456789", "symbol": "BTCUSDT", "signer": wallet.address.lower()}
    signature = sign_parameters(wallet, values, network=network)
    message = encode_typed_data(full_message=typed_message(urlencode(sorted(values.items())), network))
    assert Account.recover_message(message, signature=signature) == wallet.address


@pytest.mark.asyncio
async def test_aster_legacy_saved_request_is_not_reordered_or_resigned():
    wallet = Account.create()
    legacy = {"method": "POST", "path": "/fapi/v3/order", "nonce": 1,
              "params": {"symbol": "BTCUSDT", "nonce": "1", "user": wallet.address.lower(), "signer": wallet.address.lower(), "signature": "legacy-saved-signature"}}
    before, captured = copy.deepcopy(legacy), []
    def handle(request):
        captured.append(request.content.decode("ascii"))
        return httpx.Response(200, json={"code": 200})
    adapter = object.__new__(Aster)
    adapter.client = httpx.AsyncClient(base_url="https://fapi.asterdex.com", transport=httpx.MockTransport(handle))
    try:
        await adapter.send(legacy)
        assert legacy == before and captured == [urlencode(before["params"])]
    finally:
        await adapter.client.aclose()
