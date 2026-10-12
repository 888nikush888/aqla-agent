import copy
from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, TradePlan, digest, now_ms


def change_admission(state, trading, source, plan, change):
    if change == "source":
        trading.ingest(source.model_copy(update={"edited_at": source.sent_at + 1, "text": source.text + " Edit"}))
    elif change == "binding":
        state.update(lambda row: row["bindings"].__setitem__(source.source_id, {"trade_id": "other", "kind": "entry"}))
    elif change in ("capacity", "risk"):
        other = copy.deepcopy(plan)
        if change == "risk":
            other["market"]["asset_id"] = 1
        state.update(lambda row: row["trades"].__setitem__("other", {
            "status": "open", "plan": other, "reserved_risk": "150" if change == "risk" else "0"}))
    elif change == "subscription":
        state.update(lambda row: row.update(sources=[], source_aliases={}))
    elif change == "policy":
        state.update(lambda row: row.update(policy_revision=row["policy_revision"] + 1))
    elif change == "pause":
        state.update(lambda row: row.update(paused=True))
    elif change in ("ranking", "ranking_status"):
        def toggle(row):
            row["ranking_policy"]["enabled"] = not row["ranking_policy"]["enabled"]
            if change == "ranking":
                row["ranking_revision"] += 1
        state.update(toggle)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ranking", "ranking_status", "ranking_revision"])
async def test_direct_trading_checks_ranking_binding(setup, signal, change):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    preview = await trading.preview(source.event_id, interpretation)
    plan = TradePlan.model_validate(preview)
    assert plan.ranking_enabled is False
    assert plan.ranking_policy_revision == state.snapshot()["ranking_revision"]
    assert plan.plan_id == digest(plan.model_dump(mode="json", exclude={"plan_id"}))
    assert trading.validate_admission(state.snapshot(), plan) is None
    if change == "ranking_revision":
        state.update(lambda row: row.update(ranking_revision=row["ranking_revision"] + 1))
    else:
        change_admission(state, trading, source, preview, change)
    with pytest.raises(AqlaError, match="Ranking policy changed"):
        trading.validate_admission(state.snapshot(), plan)
    with pytest.raises(AqlaError, match="Ranking policy changed"):
        await trading.execute(plan.plan_id)
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [("ranking_enabled",), ("ranking_policy_revision",), ("ranking_enabled", "ranking_policy_revision")])
async def test_partial_legacy_ranking_binding_is_rejected(setup, signal, missing):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    preview = await trading.preview(source.event_id, interpretation)
    legacy = {key: value for key, value in preview.items() if key not in missing}
    plan = TradePlan.model_validate(legacy)
    assert all(key not in plan.model_dump(mode="json") for key in missing)
    assert plan.plan_id == preview["plan_id"]
    state.update(lambda data: data["plans"][plan.plan_id].update(plan=legacy))
    with pytest.raises(AqlaError, match="Legacy ranking binding.*fresh preview"):
        await trading.execute(plan.plan_id)
    assert state.snapshot()["plans"][plan.plan_id]["plan"] == legacy
    assert not state.snapshot()["trades"]
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "policy", "source", "binding", "capacity", "risk", "subscription", "ranking", "ranking_status"])
async def test_final_admission_rechecks_state_changed_during_account_read(setup, signal, monkeypatch, change):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.account_snapshot

    async def changed(market):
        account = await original(market)
        change_admission(state, trading, source, plan, change)
        return account

    monkeypatch.setattr(exchange, "account_snapshot", changed)
    with pytest.raises(AqlaError):
        await trading.execute(plan["plan_id"])
    assert plan["trade_id"] not in state.snapshot()["trades"]
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["equity", "available_margin", "stale"])
async def test_final_admission_requires_fresh_account_budget(setup, signal, monkeypatch, change):
    _, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.account_snapshot

    async def changed(market):
        account = await original(market)
        values = {"observed_at": account.observed_at - 10001} if change == "stale" else {change: Decimal("1")}
        return account.model_copy(update=values)

    monkeypatch.setattr(exchange, "account_snapshot", changed)
    if change == "stale":
        pending = await trading.execute(plan["plan_id"])
        assert pending["observation_retry"]["attempts"] == 1
        assert pending["observation_retry"]["code"] == "account_stale"
        assert pending["reserved_risk"] != "0"
    else:
        with pytest.raises(AqlaError):
            await trading.execute(plan["plan_id"])
        assert plan["trade_id"] not in state.snapshot()["trades"]
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "policy", "source", "binding", "capacity", "risk", "subscription", "vault", "principal", "stale", "deadline", "ranking", "ranking_status"])
async def test_unsent_entry_rechecks_after_dispatch_signer_await(setup, signal, monkeypatch, change):
    settings, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = exchange.verify_signer
    calls = 0

    async def changed():
        nonlocal calls
        await original()
        calls += 1
        if calls == 3:  # Execute, leverage dispatch, then the still-unsent entry dispatch.
            if change == "vault":
                settings.vault_address = "0x" + "1" * 40
            elif change == "principal":
                settings.account = "0x" + "2" * 40
            elif change == "stale":
                # Dispatch now re-reads the account after signing awaits. Supply
                # an actually stale fresh-read result instead of aging a cached
                # observation through a one-millisecond clock boundary.
                observation = exchange.account_snapshot
                async def stale_account(market):
                    value = await observation(market)
                    value.observed_at = now_ms() - 10001
                    return value
                monkeypatch.setattr(exchange, "account_snapshot", stale_account)
            elif change == "deadline":
                from aqla_hyperliquid_mcp import trades
                stamp = trades.now_ms()
                monkeypatch.setattr(trades, "now_ms", lambda: stamp + 300001)
            else:
                change_admission(state, trading, source, plan, change)

    monkeypatch.setattr(exchange, "verify_signer", changed)
    if change == "stale":
        pending = await trading.execute(plan["plan_id"])
        assert pending["observation_retry"]["code"] == "account_stale"
        assert pending["observation_retry"]["attempts"] == 1
    else:
        with pytest.raises(AqlaError):
            await trading.execute(plan["plan_id"])
    assert [row["type"] for row in fake.actions] == ["updateLeverage"]
    assert "entry:" + plan["trade_id"] not in state.snapshot()["operations"]


@pytest.mark.asyncio
async def test_revoked_subscription_cancels_admitted_but_unsent_recovery(setup, signal, monkeypatch):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    original = trading._entry_steps

    class SimulatedProcessCrash(BaseException):
        pass

    async def interrupted(*args):
        raise SimulatedProcessCrash("Crash after admission")

    monkeypatch.setattr(trading, "_entry_steps", interrupted)
    with pytest.raises(SimulatedProcessCrash):
        await trading.execute(plan["plan_id"])
    state.update(lambda row: row.update(sources=[], source_aliases={}))
    monkeypatch.setattr(trading, "_entry_steps", original)
    await trading.recover()
    trade = state.snapshot()["trades"][plan["trade_id"]]
    assert trade["status"] == "cancelled" and trade["reserved_risk"] == "0"
    assert "subscribed" in trade["unsent_reason"]
    assert fake.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_only_trusted_owner_native_forward_survives_source_revocation(setup, signal, native):
    settings, state, fake, _, trading = setup
    source, interpretation = signal
    settings.telegram_owner_id = "owner"
    source = source.model_copy(update={"chat_id": "owner"})
    state.update(lambda row: row["sources"].append("owner"))
    trading.ingest(source, native_forward=native)
    interpretation = interpretation.model_copy(update={
        "source_refs": [source.source_id],
        "evidence": [proof.model_copy(update={"source_id": source.source_id}) for proof in interpretation.evidence]})
    plan = await trading.preview(source.event_id, interpretation)
    state.update(lambda row: row.update(sources=[]))
    if native:
        assert (await trading.execute(plan["plan_id"]))["status"] == "open"
    else:
        with pytest.raises(AqlaError, match="subscribed"):
            await trading.execute(plan["plan_id"])
        assert fake.actions == []


@pytest.mark.asyncio
async def test_revocation_and_pause_leave_funded_exits_available(setup, signal):
    _, state, fake, _, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    trade = await trading.execute(plan["plan_id"])
    state.update(lambda row: row.update(sources=[], paused=True, policy_revision=2))
    result = await trading.close_position(trade["trade_id"])
    assert result["phase"] == "acknowledged"
    await trading.recover()
    assert fake.position == 0
    assert state.snapshot()["trades"][trade["trade_id"]]["status"] == "closed"
