import copy

from aqla_hyperliquid_mcp import coverage


def test_independent_doc_and_sdk_manifest_has_no_missing_contracts():
    result = coverage.audit()
    assert result["complete"] is True, result["missing"]
    assert result["live_exchange_verified"] is False
    assert result["exchange_types"] == 40
    assert result["sdk_operations"] == 52


def test_coverage_gate_detects_a_missing_real_handler(monkeypatch):
    models = copy.copy(coverage.NATIVE_MODELS)
    models.pop("trailingStop")
    monkeypatch.setattr(coverage, "NATIVE_MODELS", models)
    result = coverage.audit()
    assert result["complete"] is False
    assert "trailingStop" in result["missing"]["exchange"]
