"""Owner wave requests consume the native reconciliation wrapper without resending."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aqla_agent.operator_management import dispatch_operator_wave
from aqla_hyperliquid_mcp.models import AqlaError


@pytest.mark.parametrize("resolved", [True, False])
@pytest.mark.asyncio
async def test_owner_pause_reconciles_existing_wave_receipt_without_another_order(monkeypatch, resolved):
    identity, wave_id = "operator:pause", "waverider:trade:0:reduce"
    operation = {"operation_id": wave_id, "phase": "unknown"}
    data = {"operations": {wave_id: operation}, "operator_requests": {identity: {"status": "pending"}}}

    class Root:
        @staticmethod
        def read(*_args, **_kwargs):
            return copy.deepcopy(data)

        @staticmethod
        def update(callback):
            return callback(data)

    root = Root()
    phase = "reconciled" if resolved else "unknown"
    wrapper = {"operation": {**operation, "phase": phase}, "evidence": [], "resolved": resolved, "resubmitted": False}
    exchange = SimpleNamespace(reconcile=AsyncMock(return_value=wrapper), mutate=AsyncMock(),
        price_snapshot=AsyncMock(return_value=SimpleNamespace(fresh=lambda: None)))
    changes = []
    engine = SimpleNamespace(state=SimpleNamespace(root=root), exchange=exchange,
        patch_trade=lambda trade_id, **values: changes.append((trade_id, values)))
    request = SimpleNamespace(trade_id="trade")
    trade = {"waverider": {"operation_id": wave_id}}
    plan = SimpleNamespace(policy=SimpleNamespace(management_mode="waverider"), market=object())
    monkeypatch.setattr("aqla_agent.waverider.ended", lambda *_args: False)

    if resolved:
        result = await dispatch_operator_wave(engine, identity, root, request, trade, plan, "pause_waverider")
        assert result == {"request_id": identity, "status": "done"}
        assert changes == [("trade", {"waverider_paused": True})]
    else:
        with pytest.raises(AqlaError):
            await dispatch_operator_wave(engine, identity, root, request, trade, plan, "pause_waverider")
        assert data["operator_requests"][identity]["status"] == "pending" and changes == []
    exchange.reconcile.assert_awaited_once_with(wave_id)
    exchange.mutate.assert_not_awaited()
