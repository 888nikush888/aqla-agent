import pytest

from aqla_hyperliquid_mcp.models import AqlaError


@pytest.mark.asyncio
async def test_entry_bracket_and_targets_are_not_repeated(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    result = await trading.execute(plan["plan_id"])
    assert result["status"] == "open"
    assert fake.position > 0
    count = len(fake.actions)
    again = await trading.execute(plan["plan_id"])
    assert again["trade_id"] == result["trade_id"]
    assert len(fake.actions) == count
    assert any(order["kind"] == "sl" for order in result["orders"])
    assert sum(order["kind"] == "tp" for order in result["orders"]) == 2
    assert state.snapshot()["bindings"][source.source_id]["trade_id"] == result["trade_id"]


@pytest.mark.asyncio
async def test_unknown_send_keeps_identity_and_is_not_retried(setup):
    _, state, fake, exchange, _ = setup
    fake.lost_response = True
    action = {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2}
    first = await exchange.mutate("stable", action)
    assert first["phase"] == "unknown"
    assert len(fake.actions) == 1
    second = await exchange.mutate("stable", action)
    assert second["phase"] == "unknown"
    assert len(fake.actions) == 1
    with pytest.raises(AqlaError, match="different request"):
        await exchange.mutate("stable", {**action, "leverage": 3})
    assert len(fake.actions) == 1
    assert state.snapshot()["nonces"]


@pytest.mark.asyncio
async def test_policy_change_invalidates_unsent_plan(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    state.update(lambda row: row.update(policy_revision=2))
    with pytest.raises(AqlaError, match="policy changed"):
        await trading.execute(plan["plan_id"])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_raw_sdk_uses_same_durable_dispatch(setup):
    _, _, fake, exchange, _ = setup
    request = {"operation": "update_leverage", "name": "BTC", "leverage": 2, "is_cross": False}
    result = await exchange.sdk_action("sdk-test", request)
    assert result["phase"] == "acknowledged"
    assert fake.actions[-1] == {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2}
    await exchange.sdk_action("sdk-test", request)
    assert len(fake.actions) == 1


def test_batch_ack_is_not_mistaken_for_acceptance_of_all_legs():
    from aqla_hyperliquid_mcp.api import Hyperliquid

    action = {"type": "order", "orders": [{}, {}]}
    mixed = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}, {"error": "invalid stop"}]}}}
    phase, outcome, legs = Hyperliquid.decode_result(action, mixed)
    assert (phase, outcome) == ("acknowledged", "partial")
    assert [leg["disposition"] for leg in legs] == ["accepted", "rejected"]
    rejected = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "one"}, {"error": "two"}]}}}
    assert Hyperliquid.decode_result(action, rejected)[:2] == ("rejected", "rejected")
    assert Hyperliquid.decode_result(action, {"status": "ok", "response": {"type": "default"}})[:2] == ("unknown", "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "cancelByCloid"])
async def test_cancel_reconciliation_requires_exact_terminal_proof_for_every_leg(setup, kind):
    _, state, fake, exchange, _ = setup
    identities = [exchange.cloid("owned", index) for index in range(2)]
    for index, identity in enumerate(identities):
        fake.orders[identity] = {"status": "open", "order": {"oid": 500 + index, "cloid": identity}}
    cancels = [{"a": 0, "o": 500 + index} if kind == "cancel" else {"asset": 0, "cloid": identity} for index, identity in enumerate(identities)]
    fake.lost_response = True
    action = {"type": kind, "cancels": cancels}
    result = await exchange.mutate("cancel-proof", action)
    assert result["phase"] == "unknown"
    for identity in identities:
        fake.orders[identity]["status"] = "open"
    assert not (await exchange.reconcile("cancel-proof"))["resolved"]
    fake.orders[identities[0]]["status"] = "canceled"
    assert not (await exchange.reconcile("cancel-proof"))["resolved"]
    fake.orders[identities[1]]["status"] = "filled"
    assert (await exchange.reconcile("cancel-proof"))["resolved"]
    assert state.snapshot()["operations"]["cancel-proof"]["phase"] == "reconciled"
    assert fake.actions.count(action) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "cancelByCloid"])
async def test_cancel_reconciliation_does_not_accept_another_terminal_order(setup, monkeypatch, kind):
    _, state, fake, exchange, _ = setup
    identity = exchange.cloid("owned")
    action = {"type": kind, "cancels": [{"a": 0, "o": 500} if kind == "cancel" else {"asset": 0, "cloid": identity}]}
    fake.lost_response = True
    await exchange.mutate("cancel-proof", action)

    async def wrong_order(*args):
        return {"status": "order", "order": {"status": "canceled", "order": {"oid": 501, "cloid": exchange.cloid("foreign")}}}

    monkeypatch.setattr(exchange, "order_status", wrong_order)
    assert not (await exchange.reconcile("cancel-proof"))["resolved"]
    assert state.snapshot()["operations"]["cancel-proof"]["phase"] == "unknown"


@pytest.mark.asyncio
async def test_mutation_deadline_is_checked_again_after_signer_await(setup, monkeypatch):
    _, state, fake, exchange, _ = setup
    from aqla_hyperliquid_mcp import api
    deadline = api.now_ms() + 10000
    original = exchange.verify_signer

    async def expired():
        await original()
        monkeypatch.setattr(api, "now_ms", lambda: deadline)

    monkeypatch.setattr(exchange, "verify_signer", expired)
    with pytest.raises(AqlaError, match="deadline has expired before preparation"):
        await exchange.mutate("expires-during-await", {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2}, deadline=deadline)
    assert fake.actions == []
    assert "expires-during-await" not in state.snapshot()["operations"]


@pytest.mark.parametrize("action,response", [
    ({"type": "order", "orders": [{}]}, {"type": "cancel", "data": {"statuses": ["success"]}}),
    ({"type": "cancel", "cancels": [{}]}, {"type": "order", "data": {"statuses": ["success"]}}),
    ({"type": "updateLeverage"}, {"type": "unsupported", "data": {"statuses": []}}),
    ({"type": "trailingStop"}, {"type": "default"}),
    ({"type": "updateLeverage"}, {"type": "unsupported"}),
])
def test_malformed_non_default_acknowledgment_does_not_claim_acceptance(action, response):
    from aqla_hyperliquid_mcp.api import Hyperliquid
    assert Hyperliquid.decode_result(action, {"status": "ok", "response": response})[:2] == ("unknown", "unknown")
