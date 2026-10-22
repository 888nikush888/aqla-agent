import copy
from decimal import Decimal

import httpx
import pytest

from aqla_agent.pipelines import AccountLimits, Parameters, Pipeline, TradingProfile, migrate, route_control
from aqla_agent.router import Router
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms


@pytest.fixture
def routed(setup):
    settings, state, fake, exchange, _ = setup
    boundaries = {"hyperliquid": fake}
    router = Router(settings, state, exchange)
    profiles = [TradingProfile(id=name, name=name.upper(), parameters=Parameters(risk_percent=risk, max_notional_quote=notional))
                for name, risk, notional in (("a", "0.25", "500"), ("b", "1", "1000"))]
    for selected in profiles:
        state.update(lambda data, selected=selected: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=selected))
    pipeline = Pipeline(id="source-one", name="Quelle Eins", sources=["channel"], enabled=True,
                        targets=[{"exchange": "hyperliquid", "profile_id": "a"}])
    state.update(lambda data: route_control(data, "set_pipeline", expected_revision=data["configuration_revision"], pipeline=pipeline))
    return router, boundaries


@pytest.mark.parametrize('exchange', ['hyperliquid'])
def test_projected_snapshots_never_share_mutable_profiles_with_writer_or_preview_basis(routed, setup, exchange):
    router, _ = routed
    view = router.engines['hyperliquid'].state
    original = setup[1].snapshot()
    selected = view.snapshot()
    selected['profiles']['a']['parameters']['risk_percent'] = '99'
    assert setup[1].snapshot()['profiles'] == original['profiles']
    with view.scope({'pipeline_id': 'source-one', 'profile_id': 'a'}, preview_snapshot=original):
        preview = view.preview_snapshot()
        preview['profiles']['a']['parameters']['risk_percent'] = '98'
    assert original['profiles']['a']['parameters']['risk_percent'] == '0.25'


@pytest.mark.asyncio
async def test_admission_checks_the_current_transaction_snapshot(routed, setup, signal):
    from aqla_agent.pipelines import ConfigurationChanged
    from aqla_hyperliquid_mcp.models import TradePlan
    router, _ = routed
    source, value = signal
    raw = await router.preview(source.event_id, value)
    plan = TradePlan.model_validate(next(row["plan"] for row in raw["targets"] if row["exchange"] == "hyperliquid"))
    engine = router.engines["hyperliquid"]
    current = engine.state.snapshot()
    current["paused"] = True
    assert not setup[1].snapshot()["paused"]
    with pytest.raises(ConfigurationChanged, match="paused"):
        engine.validate_configuration(current, plan)


@pytest.mark.parametrize('exchange', ['hyperliquid'])
def test_fresh_account_limits_do_not_copy_financial_and_source_history(routed, setup, monkeypatch, exchange):
    router, _ = routed
    root = setup[1]
    view = router.engines['hyperliquid'].state
    before = view.limits()
    monkeypatch.setattr(root, 'snapshot', lambda: pytest.fail('Reading limits copied the complete financial state'))
    assert view.limits() == before
    root.update(lambda data: data['venue_limits']['hyperliquid'].update(max_open_trades=before.max_open_trades + 1))
    assert view.limits().max_open_trades == before.max_open_trades + 1




@pytest.mark.asyncio
async def test_fresh_fallback_signal_cannot_inherit_unquoted_old_channel_context(routed, setup, signal, monkeypatch):
    from aqla_hyperliquid_mcp.models import SymbolUnavailable

    router, boundaries = routed
    state = setup[1]
    state.update(lambda data: data["ranking_policy"].update(enabled=True))
    state.update(lambda data: data["source_ranks"]["channel"].update(tier="D"))
    profile = TradingProfile(id="a", name="A", parameters=Parameters(
        **{**state.snapshot()["profiles"]["a"]["parameters"], "max_signal_age_seconds": 14400}))
    state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=profile))
    state.update(lambda data: data["pipelines"]["source-one"].update(targets=data["pipelines"]["source-one"]["targets"][:1]))
    old = SourceEvent(chat_id="channel", message_id=999, sent_at=now_ms() - 20 * 86400 * 1000,
                      historical=True, text="Old unrelated channel result")
    router.ingest(old)
    source, value = signal
    reads = []
    async def missing(*_):
        reads.append("hyperliquid")
        raise SymbolUnavailable("Fresh metadata confirms missing BTC market")
    monkeypatch.setattr(router.engines["hyperliquid"].exchange, "market", missing)
    before = state.snapshot()
    with pytest.raises(AqlaError, match="source_refs include context without setup evidence") as failure:
        await router.preview(source.event_id, value.model_copy(update={"source_refs": [old.source_id, source.source_id]}))
    assert "deadline expired" not in str(failure.value)
    assert state.snapshot() == before and not reads
    result = await router.preview(source.event_id, value)
    target = result["targets"][0]
    assert target["status"] == "rejected" and target["exchange"] == "hyperliquid"
    assert target["error"] == "Fresh metadata confirms missing BTC market"
    assert not state.snapshot()["plans"] and not state.snapshot()["operations"]
    assert state.snapshot()["events"][old.event_id] == before["events"][old.event_id]
    assert reads == ["hyperliquid"] and all(not boundary.actions for boundary in boundaries.values())
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Fresh price data unavailable", "Insufficient margin", "Timeout: result unknown"])
async def test_ranked_non_listing_errors_never_fallback(routed, setup, signal, monkeypatch, reason):
    router, boundaries = routed
    setup[1].update(lambda data: data["ranking_policy"].update(enabled=True))
    setup[1].update(lambda data: data["source_ranks"]["channel"].update(tier="D"))
    async def unavailable(*_):
        raise AqlaError(reason)
    monkeypatch.setattr(router.engines["hyperliquid"].exchange, "market", unavailable)
    source, interpretation = signal
    result = await router.preview(source.event_id, interpretation)
    assert result["targets"][0]["status"] == "rejected" and result["targets"][0]["error"] == reason
    assert all(row["requested_exchange"] == row["exchange"] for row in result["targets"])
    assert all(not boundary.actions for boundary in boundaries.values())
    await router.close()


@pytest.mark.asyncio
async def test_ranked_fallback_no_mainnet_escape_when_testnet_unavailable(routed, setup, signal, monkeypatch):
    from aqla_hyperliquid_mcp.models import SymbolUnavailable
    router, _ = routed
    setup[1].update(lambda data: data["ranking_policy"].update(enabled=True))
    async def missing(*_):
        raise SymbolUnavailable("Confirmed absent")
    monkeypatch.setattr(router.engines["hyperliquid"].exchange, "market", missing)
    source, interpretation = signal
    result = await router.preview(source.event_id, interpretation)
    assert result["targets"][0]["status"] == "rejected"
    assert result["targets"][0]["network"] == "testnet" and result["targets"][0]["error"] == "Confirmed absent"
    assert not setup[1].snapshot()["operations"]
    await router.close()






@pytest.mark.asyncio
async def test_profile_edit_blocks_unsent_plan_but_preserves_open_policy(routed, setup, signal):
    router, _ = routed
    source, interpretation = signal
    result = await router.preview(source.event_id, interpretation)
    other = source.model_copy(update={"message_id": 77})
    router.ingest(other)
    second = await router.preview(other.event_id, interpretation.model_copy(update={"source_refs": [other.source_id],
        "evidence": [row.model_copy(update={"source_id": other.source_id}) for row in interpretation.evidence]}))
    first = await router.execute(result["targets"][0]["plan_id"])
    state = setup[1]
    changed = TradingProfile(id="a", name="A", parameters=Parameters(risk_percent="0.9"))
    state.update(lambda data: route_control(data, "set_profile", expected_revision=data["configuration_revision"], profile=changed))
    assert state.snapshot()["trades"][first["trade_id"]]["plan"]["policy"]["risk_percent"] == "0.25"
    outcome = await router.execute(second["targets"][0]["plan_id"])
    assert "changed" in outcome["error"]
    assert len(state.snapshot()["trades"]) == 1
    await router.close()






def test_routing_changes_are_revision_guarded_and_profiles_do_not_set_account_limits(setup):
    state = setup[1]
    with pytest.raises(AqlaError, match="revision"):
        state.update(lambda data: route_control(data, "set_profile", expected_revision=-1, profile=TradingProfile(id="a", name="A", parameters=Parameters())))
    with pytest.raises(ValueError):
        Parameters(max_open_trades=100)
    with pytest.raises(ValueError):
        Pipeline(id="p", name="P", sources=["channel"], targets=[{"exchange": "aster", "profile_id": "a"}] * 2)
    with pytest.raises(AqlaError, match="Hyperliquid"):
        state.update(lambda data: route_control(data, "set_exchange_enabled", expected_revision=data["configuration_revision"], exchange="aster", enabled=True))


@pytest.mark.asyncio
async def test_reply_update_reaches_original_setup_family_on_all_venues(routed, setup, signal):
    router, _ = routed
    source, interpretation = signal
    previews = await router.preview(source.event_id, interpretation)
    await router.execute_many([row["plan_id"] for row in previews["targets"]])
    update = SourceEvent(chat_id="channel", message_id=2, reply_to_id=source.message_id, sent_at=now_ms(), text="SL 95")
    router.ingest(update)
    classified = Interpretation(kind="trade_update", update_action="move_stop", stop_loss="95", source_refs=[update.source_id],
                                evidence=[Evidence(field="stop_loss", source_id=update.source_id, quote="SL 95")])
    result = await router.update_trade(update.event_id, classified)
    assert result.get("trade_id") and not result.get("error")
    assert all(Decimal(trade["stop_price"]) == 95 for trade in setup[1].snapshot()["trades"].values())
    before = len(setup[1].snapshot()["operations"])
    await router.update_trade(update.event_id, classified)
    assert len(setup[1].snapshot()["operations"]) == before
    await router.close()




@pytest.mark.parametrize("entry,tp", [("standard", "standard"), ("maker_only", "standard"), ("standard", "maker_only"), ("maker_only", "maker_only")])
def test_profile_parameters_preserve_independent_maker_modes_and_legacy_json(entry, tp):
    value = Parameters(entry_execution=entry, take_profit_execution=tp)
    dumped = value.model_dump(mode="json")
    assert ("entry_execution" in dumped) == (entry != "standard")
    assert ("take_profit_execution" in dumped) == (tp != "standard")
    policy = value.policy(AccountLimits(max_open_trades=3, max_total_risk_percent="2"))
    assert policy.entry_execution == entry and policy.take_profit_execution == tp
    assert Parameters.from_policy(policy) == value
    assert Parameters.model_validate(dumped) == value


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,tp", [("maker_only", "standard"), ("standard", "maker_only"), ("maker_only", "maker_only")])
async def test_profile_maker_modes_through_real_mcp_control_router_plan_and_native_wire(setup, signal, tmp_path, monkeypatch, entry, tp):
    from dataclasses import asdict

    from test_maker_execution import MakerBoundary

    from aqla_agent.setup import SetupBinding, write_password_hash
    from aqla_hyperliquid_mcp.server import close_server, create_server

    settings, state, old, exchange, _ = setup
    verifier = tmp_path / "fixture-verifier.json"
    write_password_hash(verifier, "fixture-only")
    settings.setup_password_hash_file, settings.telegram_owner_id = verifier, "12345"
    fake = MakerBoundary(old.signer)
    fake.partial = Decimal(".5")
    monkeypatch.setattr(exchange.client, "_transport", httpx.MockTransport(fake.handle))
    server = create_server(settings, state=state, exchange=exchange)
    binding = SetupBinding("12345", "12345", "fixture-gateway", "fixture-session")
    gate = server._aqla_setup_gate
    generation = gate.bind(binding)
    identity = "a" * 64
    gate.register(binding, identity, None, generation)
    auth = server.aqla_authority.sign("operator", "native-command", setup={"identity": identity, "binding": asdict(binding)})
    parameters = {**state.snapshot()["profiles"]["standard"]["parameters"], "entry_execution": entry, "take_profit_execution": tp}
    result = await server.call_tool("control", {"action": "set_profile", "expected_revision": state.snapshot()["configuration_revision"],
        "profile": {"id": "standard", "name": "Standard", "parameters": parameters}, "context_auth": auth})
    assert not result.is_error
    source, interpretation = signal
    router = server.aqla_trading
    plan = await router.preview(source.event_id, interpretation)
    assert plan.get("entry_execution", "standard") == entry
    assert plan.get("take_profit_execution", "standard") == tp
    trade = await router.execute(plan["plan_id"])
    assert trade["status"] == "open"
    snapshot = state.snapshot()
    entry_wire = snapshot["operations"][trade["entry_operation_id"]]["wire_action"]["orders"][0]
    assert (entry_wire["t"].get("limit", {}).get("tif") == "Alo") == (entry == "maker_only")
    target_wire = snapshot["operations"]["targets:" + trade["trade_id"] + ":0"]["wire_action"]["orders"]
    assert all((order["t"].get("limit", {}).get("tif") == "Alo") == (tp == "maker_only") for order in target_wire)
    assert snapshot["trades"][trade["trade_id"]]["plan"] == plan["targets"][0]["plan"]
    await close_server(server)


def test_migration_keeps_existing_execution_identifiers_and_requires_no_new_limits(setup):
    _, state, *_ = setup
    data = state.snapshot()
    for key in ("profiles", "pipelines", "venue_limits", "venue_enabled", "venue_bindings", "venue_events", "venue_accounts"):
        data.pop(key)
    data["schema"] = 1
    data["operations"]["old-operation"] = {"nonce": 99, "wire_action": {"orders": [{"c": "existing-order"}]}}
    before = copy.deepcopy(data)
    assert migrate(data)
    assert data["operations"] == before["operations"]
    assert data["bindings"] == before["bindings"]
    assert data["profiles"]["standard"]["name"] == "Standard"
    assert data["venue_enabled"] == {"hyperliquid": True}
    assert set(data["venue_limits"]) == {"hyperliquid"}
    assert migrate(data) is False


@pytest.mark.asyncio
async def test_account_limits_apply_to_other_profiles_and_unknown_reservations(routed, setup, signal):
    router, _ = routed
    state = setup[1]
    source, interpretation = signal
    def reserved(data):
        data["venue_limits"]["hyperliquid"]["max_open_trades"] = 1
        data["trades"]["other-profile-reservation"] = {"trade_id": "other-profile-reservation", "chat_id": "another-source", "status": "unknown", "reserved_risk": "20",
            "plan": {"exchange": "hyperliquid", "network": setup[0].network, "account": setup[0].account.lower(), "market": {"asset_id": 999, "quote_token": "USDC"}, "source_refs": []}}
    state.update(reserved)
    result = await router.preview(source.event_id, interpretation)
    target = next(row for row in result["targets"] if row["exchange"] == "hyperliquid")
    assert "Maximum open trade count" in target["error"]
    assert not any(row.get("plan_id") for row in result["targets"])
    await router.close()
