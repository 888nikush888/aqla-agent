import pytest

from aqla_hyperliquid_mcp.models import AqlaError, require_first


def test_required_lookup_preserves_first_item_and_iterator_position():
    values = iter([0, 1])
    assert require_first(values) == 0
    assert require_first(values) == 1
    with pytest.raises(AqlaError, match="Required state item"):
        require_first(values)


async def test_missing_lookup_is_a_controlled_error_in_async_management():
    async def update():
        require_first(iter(()), "Owned order is missing")
        raise AssertionError("No mutation may follow a missing owned order")
    with pytest.raises(AqlaError, match="Owned order is missing"):
        await update()
