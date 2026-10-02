import pytest

from aqla_hyperliquid_mcp.auth import Authority
from aqla_hyperliquid_mcp.models import AqlaError, now_ms


def test_source_text_cannot_claim_operator_rights():
    authority = Authority("ephemeral-test-integration-key" * 2)
    source = authority.sign("source", "genuine-source")
    assert authority.verify(source)["kind"] == "source"
    with pytest.raises(AqlaError, match="operator authority"):
        authority.verify(source, operator=True)
    forged = source.replace("source", "operator")
    with pytest.raises(AqlaError):
        authority.verify(forged, operator=True)


def test_expired_native_claim_is_rejected():
    authority = Authority("ephemeral-test-integration-key" * 2)
    with pytest.raises(AqlaError):
        authority.verify(authority.sign("operator", "native-message", expires_at=now_ms() - 1))
