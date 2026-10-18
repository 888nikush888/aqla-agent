# ruff: noqa: F811
import copy
import json
import time
from decimal import Decimal

import httpx
import pytest
from test_aster_adapter import aster  # noqa: F401

from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.models import AqlaError


@pytest.fixture
def clock(monkeypatch):
    stamp = [time.time_ns() // 1_000_000]
    monkeypatch.setattr(time, "time_ns", lambda: stamp[0] * 1_000_000)
    monkeypatch.setattr(time, "time", lambda: stamp[0] / 1000)
    return stamp


@pytest.mark.asyncio
async def test_http_400_margin_already_effective_is_a_recognized_noop(aster):
    adapter, boundary, _ = aster
    request = adapter.signed("POST", "/fapi/v3/marginType", {"symbol": "BTCUSDT", "marginType": "ISOLATED"})
    response = await adapter.send(request)
    assert response["code"] == -4046 and not adapter.rejected(response)
    assert boundary.margin_type == "isolated" and not boundary.orders
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("margin,leverage,paths", [("isolated", 2, []), ("cross", 2, ["marginType"]),
    ("isolated", 3, ["leverage"]), ("cross", 3, ["marginType", "leverage"])])
async def test_configuration_sends_only_settings_that_actually_need_change(aster, margin, leverage, paths):
    adapter, boundary, _ = aster
    market = await adapter.market("BTC")
    boundary.margin_type, boundary.leverage = margin, leverage
    result = await adapter.mutate("configuration", {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 2})
    assert result["phase"] == "acknowledged"
    assert [path for path, _ in boundary.posts] == paths
    assert await adapter.prove_configuration(result)
    assert not boundary.orders
    await adapter.close()


@pytest.mark.asyncio
async def test_empty_cold_market_configuration_is_not_assumed_effective(aster, monkeypatch):
    adapter, boundary, _ = aster
    market = await adapter.market("BTC")
    original = adapter.read
    async def cold(path, *args, **kwargs):
        return [] if path == "positionRisk" else await original(path, *args, **kwargs)
    monkeypatch.setattr(adapter, "read", cold)
    result = await adapter.mutate("cold-config", {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 2})
    assert [path for path, _ in boundary.posts] == ["marginType", "leverage"]
    assert await adapter.prove_configuration(result) is False
    assert not boundary.orders
    await adapter.close()


def lose_configuration(adapter, boundary, *, failures=1, applied=False):
    original = boundary.handle
    def handle(request):
        nonlocal failures
        if request.method == "POST" and request.url.path.endswith("/leverage") and failures:
            failures -= 1
            if applied:
                original(request)
            return httpx.Response(503, json={"code": -1000, "msg": "PRIVATE_RESPONSE_SECRET"})
        if request.method == "POST" and request.url.path.endswith("/order"):
            assert all(row["phase"] in ("acknowledged", "reconciled") for row in adapter.state.snapshot()["operations"].values()
                       if row["wire_action"]["type"] == "updateLeverage")
        return original(request)
    adapter.client._transport = httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [False, True])
async def test_lost_configuration_ack_is_recovered_without_replaying_original_signature(aster, signal, clock, applied):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary, applied=applied)
    trade = await engine.execute(plan["plan_id"])
    identity = "leverage:" + trade["trade_id"]
    old = copy.deepcopy(engine.state.snapshot()["operations"][identity])
    assert trade["status"] == "unknown" and not boundary.orders
    assert old["result"]["http_status"] == 503 and "PRIVATE_RESPONSE_SECRET" not in json.dumps(old)
    if not applied:
        assert (await adapter.reconcile(identity))["resolved"] is False
        clock[0] += 61001
        assert (await adapter.reconcile(identity))["resolved"] is False
        assert engine.state.snapshot()["operations"][identity]["phase"] == "unknown"
    await engine.recover()
    snapshot = engine.state.snapshot()
    trade = snapshot["trades"][trade["trade_id"]]
    assert trade["status"] == "open"
    assert snapshot["operations"][identity]["signed_requests"] == old["signed_requests"]
    assert snapshot["operations"][identity]["outcome"] == "accepted"
    assert bool(trade.get("configuration_retry")) is not applied
    assert len([r for r in boundary.orders.values() if not r["reduceOnly"]]) == 1
    terminal = copy.deepcopy(snapshot["operations"][identity])
    await adapter.reconcile(identity)
    assert engine.state.snapshot()["operations"][identity] == terminal
    count = len(boundary.posts)
    await engine.recover()
    await engine.execute(plan["plan_id"])
    assert len(boundary.posts) == count
    await adapter.close()


@pytest.mark.asyncio
async def test_configuration_noop_interrupted_before_completion_has_read_only_recovery(aster, monkeypatch):
    adapter, boundary, _ = aster
    market = await adapter.market("BTC")
    action = {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 2}
    result = await adapter.mutate("no-config-send", action)
    assert result["signed_requests"] == [] and not boundary.posts
    adapter.state.update(lambda d: d["operations"]["no-config-send"].update(phase="sending", outcome=None))
    assert (await adapter.reconcile("no-config-send"))["resolved"]
    assert not boundary.posts
    await adapter.close()


@pytest.mark.asyncio
async def test_second_configuration_failure_stays_unresolved_without_more_writes_or_entry(aster, signal, clock):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary, failures=2)
    trade = await engine.execute(plan["plan_id"])
    clock[0] += 61001
    await engine.recover()
    clock[0] += 61001
    await engine.recover()
    current = engine.state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "unknown" and Decimal(current["reserved_risk"]) > 0
    assert current["configuration_retry"] and len(current["leverage_attempts"]) == 1
    assert not boundary.orders
    assert len([r for r in engine.state.snapshot()["operations"] if r.startswith("leverage:")]) == 2
    operations = copy.deepcopy(engine.state.snapshot()["operations"])
    await engine.recover()
    assert all(r["phase"] == "unknown" for r in engine.state.snapshot()["operations"].values())
    assert {k:r["signed_requests"] for k,r in operations.items()} == {k:r["signed_requests"] for k,r in engine.state.snapshot()["operations"].items()}
    await adapter.close()


@pytest.mark.asyncio
async def test_manual_position_after_configuration_loss_is_never_adopted(aster, signal, clock):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary)
    trade = await engine.execute(plan["plan_id"])
    boundary.position = Decimal(1)
    clock[0] += 61001
    await engine.recover()
    current = engine.state.snapshot()["trades"][trade["trade_id"]]
    assert current["status"] == "unknown" and boundary.position == 1 and not boundary.orders
    assert not current.get("configuration_retry")
    await adapter.close()


@pytest.mark.asyncio
async def test_manual_resting_entry_blocks_configuration_correction(aster, signal, clock):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary)
    trade = await engine.execute(plan["plan_id"])
    manual = {"symbol": "BTCUSDT", "status": "NEW", "reduceOnly": False}
    boundary.orders["manual"] = copy.deepcopy(manual)
    clock[0] += 61001
    await engine.recover()
    current = engine.state.snapshot()["trades"][trade["trade_id"]]
    assert not current.get("configuration_retry") and current["status"] == "unknown"
    assert boundary.orders == {"manual": manual} and boundary.position == 0
    await adapter.close()


@pytest.mark.asyncio
async def test_configuration_ack_without_effect_cannot_unblock_entry(aster, signal, clock):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary)
    trade = await engine.execute(plan["plan_id"])
    original = boundary.handle
    def ack_only(request):
        if request.method == "POST" and request.url.path.endswith("/leverage"):
            return httpx.Response(200, json={"symbol": "BTCUSDT", "leverage": 2})
        return original(request)
    adapter.client._transport = httpx.MockTransport(ack_only)
    clock[0] += 61001
    await engine.recover()
    await engine.recover()
    snapshot = engine.state.snapshot()
    assert snapshot["trades"][trade["trade_id"]]["status"] == "unknown" and not boundary.orders
    assert snapshot["operations"]["leverage:"+trade["trade_id"]]["phase"] == "unknown"
    assert snapshot["operations"]["leverage:"+trade["trade_id"]+":configuration:1"]["phase"] == "acknowledged"
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["pause", "ranking", "source", "deadline", "budget", "other_operation"])
async def test_configuration_correction_keeps_every_original_admission_fence(aster, signal, clock, monkeypatch, changed):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    boundary.leverage = 3
    lose_configuration(adapter, boundary)
    trade = await engine.execute(plan["plan_id"])
    clock[0] += 61001
    if changed == "pause":
        engine.state.root.update(lambda data: data.update(paused=True))
    elif changed == "ranking":
        engine.state.root.update(lambda data: data.update(ranking_revision=data["ranking_revision"]+1))
    elif changed == "source":
        Trading(adapter, engine.state.root).ingest(source.model_copy(update={"edited_at":clock[0],"text":source.text+" Edit"}))
    elif changed == "deadline":
        clock[0] = plan["deadline_at"]+1
    elif changed == "budget":
        account = adapter.account_snapshot
        async def low(market):
            return (await account(market)).model_copy(update={"equity":Decimal(1),"available_margin":Decimal(1)})
        monkeypatch.setattr(adapter,"account_snapshot",low)
    else:
        def unresolved(data):
            row=copy.deepcopy(data["operations"]["leverage:"+trade["trade_id"]])
            row["operation_id"]="other"
            data["operations"]["other"]=row
        engine.state.update(unresolved)
    await engine.recover()
    assert not engine.state.snapshot()["trades"][trade["trade_id"]].get("configuration_retry")
    assert not boundary.orders and boundary.position == 0
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong_market", "hedged", "duplicate", "stale_clock"])
async def test_bad_configuration_evidence_cannot_release_unknown_operation(aster, clock, monkeypatch, invalid):
    adapter, boundary, _ = aster
    market = await adapter.market("BTC")
    boundary.leverage = 3
    lose_configuration(adapter, boundary)
    result = await adapter.mutate("config-proof", {"type": "updateLeverage", "asset": market.asset_id, "isCross": False, "leverage": 2})
    assert result["phase"] == "unknown"
    clock[0] += 61001
    read = adapter.read
    async def bad(path, *args, **kwargs):
        data = await read(path, *args, **kwargs)
        if path == "positionRisk":
            if invalid == "wrong_market":
                data[0]["symbol"] = "ETHUSDT"
            if invalid == "hedged":
                data[0]["positionSide"] = "LONG"
            if invalid == "duplicate":
                data.append(copy.deepcopy(data[0]))
        if path == "time" and invalid == "stale_clock":
            data["serverTime"] -= 2001
        return data
    monkeypatch.setattr(adapter, "read", bad)
    if invalid == "stale_clock":
        assert (await adapter.reconcile("config-proof"))["resolved"] is False
    else:
        with pytest.raises(AqlaError, match="configuration observation"):
            await adapter.reconcile("config-proof")
    assert adapter.state.snapshot()["operations"]["config-proof"]["phase"] == "unknown"
    assert not boundary.orders
    await adapter.close()


@pytest.mark.asyncio
async def test_expired_unknown_order_is_never_treated_as_configuration_failure(aster, signal, clock):
    adapter, boundary, engine = aster
    source, value = signal
    plan = await engine.preview(source.event_id, value)
    original = boundary.handle
    def handle(request):
        if request.method == "POST" and request.url.path.endswith("/order"):
            return httpx.Response(503, json={"code": -1000})
        return original(request)
    adapter.client._transport = httpx.MockTransport(handle)
    trade = await engine.execute(plan["plan_id"])
    assert trade["status"] == "unknown"
    clock[0] += 61001
    assert (await adapter.reconcile(trade["entry_operation_id"]))["resolved"] is False
    await engine.recover()
    assert engine.state.snapshot()["operations"][trade["entry_operation_id"]]["phase"] == "unknown"
    assert not boundary.orders and not engine.state.snapshot()["trades"][trade["trade_id"]].get("configuration_retry")
    await adapter.close()
