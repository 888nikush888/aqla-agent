import pytest

from aqla_hyperliquid_mcp.catalog import INFO_ADAPTER, INFO_MODELS, SDK_ADAPTER, SDK_MODELS, validate_info


def test_all_catalog_schemas_are_strict_objects():
    for adapter in (INFO_ADAPTER, SDK_ADAPTER):
        schema = adapter.json_schema()
        assert schema["discriminator"]
        for model in schema["$defs"].values():
            if model.get("type") == "object":
                assert model["additionalProperties"] is False
    assert len(INFO_MODELS) == 59
    assert len(SDK_MODELS) == 50


def test_info_contexts_and_invalid_fields():
    assert validate_info({"type": "meta", "dex": "xyz"}) == {"type": "meta", "dex": "xyz"}
    with pytest.raises(ValueError):
        validate_info({"type": "meta", "method": "arbitrary"})
    with pytest.raises(ValueError):
        validate_info({"type": "orderStatus", "user": "wrong", "oid": 1})
    with pytest.raises(ValueError):
        validate_info({"type": "userFillsByTime", "user": "0x" + "0" * 40, "startTime": 20, "endTime": 10})


def test_unknown_sdk_attributes_are_not_callable():
    with pytest.raises(ValueError):
        SDK_ADAPTER.validate_python({"operation": "post", "url_path": "/exchange", "payload": {}})
    with pytest.raises(ValueError):
        SDK_ADAPTER.validate_python({"operation": "update_leverage", "name": "BTC", "leverage": "a lot"})
