import copy
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_setup_mcp import setup_server  # noqa: F401

from aqla_agent.conflict_review import observe_conflict
from aqla_agent.messages import number, recovery_block, telegram_view
from aqla_agent.recovery_notices import reader_notice_signature, record_recovery_notices, record_source_notice
from aqla_agent.sources import change_sources, supported_source
from aqla_hyperliquid_mcp.models import AqlaError, digest, now_ms
from aqla_hyperliquid_mcp.server import close_server


def test_recovery_notices_transitions_survive_ack_restart_and_keep_accounts_distinct():
    data = {"source_status": {}, "notifications": {}}
    rows = [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "error": "temporary"},
            {"account_key": "hyperliquid:testnet", "trade_id": "t", "error": "temporary"}]
    record_recovery_notices(data, rows)
    assert len(data["notifications"]) == 2
    data["notifications"].clear()
    restarted = copy.deepcopy(data)
    record_recovery_notices(restarted, rows * 2)
    assert restarted["notifications"] == {}
    rows[0]["replacement_unconfirmed"] = True
    record_recovery_notices(restarted, rows)
    assert len(restarted["notifications"]) == 1
    assert next(iter(restarted["notifications"].values()))["severity"] == "urgent"
    restarted["notifications"].clear()
    stamp = now_ms()
    complete = {account: {"category": "complete", "last_attempt_at": stamp, "last_success_at": stamp, "duration_ms": 0, "error_count": 0}
                for account in ("hyperliquid:mainnet", "hyperliquid:testnet")}
    record_recovery_notices(restarted, [], complete)
    assert len(restarted["notifications"]) == 2
    assert all(row["resolved"] for row in restarted["notifications"].values())


@pytest.mark.parametrize("category", ["timeout", "unavailable", "cancelled", "incomplete"])
def test_partial_account_pass_never_claims_unobserved_trade_protection_recovered(category):
    data = {"source_status": {}, "notifications": {}}
    protection = {"account_key": "hyperliquid:mainnet", "trade_id": "protected", "error": "No confirmed effective stop loss"}
    record_recovery_notices(data, [protection])
    data["notifications"].clear()
    stamp = now_ms()
    account_error = {"account_key": "hyperliquid:mainnet", "error": "Account recovery timed out; protection reconciliation is unconfirmed"}
    incomplete = {"hyperliquid:mainnet": {"category": category, "last_attempt_at": stamp, "last_success_at": stamp - 1,
                                        "duration_ms": 1, "error_count": 1}}
    record_recovery_notices(data, [account_error], incomplete)
    assert data["notifications"] and not any(row["resolved"] for row in data["notifications"].values())
    assert all(row["severity"] == "urgent" for row in data["notifications"].values())
    assert any(any(fact["code"] == "recovery_timeout" for fact in notice["recovery_reports"]) for notice in data["notifications"].values())
    prior = [row for row in data["source_status"]["recovery"].values() if any(fact.get("trade_id") == "protected" for fact in row["reports"])]
    assert prior[0]["active"] and prior[0]["reports"][0]["code"] == "protection_unconfirmed"
    data["notifications"].clear()
    complete = {"hyperliquid:mainnet": {"category": "complete", "last_attempt_at": stamp, "last_success_at": stamp,
                                      "duration_ms": 0, "error_count": 0}}
    record_recovery_notices(data, [], complete)
    assert len(data["notifications"]) == 2 and all(row["resolved"] for row in data["notifications"].values())
    data["notifications"].clear()
    record_recovery_notices(data, [], complete)
    assert data["notifications"] == {}


def test_missing_or_stale_account_success_never_resolves_a_prior_notice():
    data = {"source_status": {}, "notifications": {}}
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "error": "No confirmed effective stop loss"}])
    data["notifications"].clear()
    record_recovery_notices(data, [])
    assert data["notifications"] == {}
    old = now_ms() - 120001
    stale = {"hyperliquid:mainnet": {"category": "complete", "last_attempt_at": old, "last_success_at": old, "duration_ms": 0, "error_count": 0}}
    record_recovery_notices(data, [], stale)
    assert data["notifications"] == {} and all(row["active"] for row in data["source_status"]["recovery"].values())


def test_success_on_another_account_does_not_clear_unobserved_mainnet_protection():
    data = {"source_status": {}, "notifications": {}}
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "error": "No confirmed effective stop loss"}])
    data["notifications"].clear()
    stamp = now_ms()
    complete = {"hyperliquid:testnet": {"category": "complete", "last_attempt_at": stamp, "last_success_at": stamp,
                                      "duration_ms": 0, "error_count": 0}}
    record_recovery_notices(data, [], complete)
    assert data["notifications"] == {} and all(row["active"] for row in data["source_status"]["recovery"].values())


@pytest.mark.asyncio
async def test_router_current_pass_diagnostics_replace_reused_or_stale_sink(setup, monkeypatch):
    from aqla_agent.router import Router
    settings, state, _, exchange, _ = setup
    router = Router(settings, state, exchange)
    sink = {"old:account": {"category": "complete"}}
    async def healthy(engine):
        engine.recovery_checked = True
        return []
    monkeypatch.setattr(router, "_recover_account", healthy)
    await router.recover(diagnostics_sink=sink)
    assert set(sink) == {"hyperliquid:testnet"}
    assert sink["hyperliquid:testnet"]["category"] == "complete"
    assert sink["hyperliquid:testnet"]["last_success_at"] >= sink["hyperliquid:testnet"]["last_attempt_at"]
    async def lost_stop(engine):
        engine.recovery_checked = True
        return [{"trade_id": "t", "replacement_unconfirmed": True}]
    monkeypatch.setattr(router, "_recover_account", lost_stop)
    await router.recover(diagnostics_sink=sink)
    assert sink["hyperliquid:testnet"]["category"] == "incomplete" and sink["hyperliquid:testnet"]["error_count"] == 1


@pytest.mark.asyncio
async def test_actual_router_timeout_cannot_publish_false_trade_recovery(setup, monkeypatch):
    from aqla_agent.router import Router
    settings, state, _, exchange, _ = setup
    router = Router(settings, state, exchange)
    data = {"source_status": {}, "notifications": {}}
    sink = {}
    async def unprotected(engine):
        engine.recovery_checked = False
        return [{"trade_id": "protected", "error": "No confirmed effective stop loss"}]
    monkeypatch.setattr(router, "_recover_account", unprotected)
    record_recovery_notices(data, await router.recover(diagnostics_sink=sink), sink)
    data["notifications"].clear()
    async def timeout(engine):
        raise TimeoutError
    monkeypatch.setattr(router, "_recover_account", timeout)
    record_recovery_notices(data, await router.recover(diagnostics_sink=sink), sink)
    assert len(data["notifications"]) == 1
    assert not next(iter(data["notifications"].values()))["resolved"]
    assert sink["hyperliquid:testnet"]["category"] == "timeout"
    data["notifications"].clear()
    async def protected(engine):
        engine.recovery_checked = True
        return []
    monkeypatch.setattr(router, "_recover_account", protected)
    record_recovery_notices(data, await router.recover(diagnostics_sink=sink), sink)
    assert len(data["notifications"]) == 2 and all(row["resolved"] for row in data["notifications"].values())
    data["notifications"].clear()
    record_recovery_notices(data, await router.recover(diagnostics_sink=sink), sink)
    assert data["notifications"] == {}


def test_safe_recovery_renderer_discards_rpc_secrets_and_names_real_account():
    secret = "supersecret-provider-token"
    data = {"source_status": {}, "notifications": {}}
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "abc", "market": "BTC", "error": secret}])
    notice = next(iter(data["notifications"].values()))
    rendered = recovery_block(notice)
    assert secret not in str(notice) and secret not in rendered
    assert "hyperliquid:mainnet" in rendered and "BTC" in rendered and "abc" in rendered
    data.update(events={}, trades={}, paused=False)
    output = telegram_view(data, {}, include_status=False)
    assert "Gespeichertes Ereignis" not in str(output)


def test_reader_retry_counter_is_not_an_outage_transition():
    first = {"code": "reader_unavailable", "sources": ["-102", "-101"], "attempt": 1, "next_retry_at": 10}
    assert reader_notice_signature(first) == reader_notice_signature({**first, "sources": ["-101", "-102"], "attempt": 8, "next_retry_at": 500})
    assert reader_notice_signature(None) != reader_notice_signature(first)
    assert reader_notice_signature({**first, "code": "sources_access_denied"}) != reader_notice_signature(first)


def test_reader_outage_survives_ack_restart_and_recovers_before_same_new_outage():
    data = {"source_status": {}, "notifications": {}}
    record_source_notice(data, "reader_recovered", reader_notice_signature(None))
    assert data["notifications"] == {}
    outage = {"code": "sources_unavailable", "sources": ["-1001"], "attempt": 1}
    record_source_notice(data, outage["code"], reader_notice_signature(outage), outage["sources"])
    assert len(data["notifications"]) == 1
    data["notifications"].clear()
    restarted = copy.deepcopy(data)
    outage["attempt"] = 4
    record_source_notice(restarted, outage["code"], reader_notice_signature(outage), outage["sources"])
    assert restarted["notifications"] == {}
    record_source_notice(restarted, "reader_recovered", reader_notice_signature(None))
    assert len(restarted["notifications"]) == 1
    record_source_notice(restarted, outage["code"], reader_notice_signature(outage), outage["sources"])
    assert len(restarted["notifications"]) == 2


def test_missing_protection_is_urgent_and_changed_replacement_identity_is_not_lost():
    from aqla_agent.notifications import notification_sort_key
    data = {"source_status": {}, "notifications": {}}
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "error": "No confirmed effective stop loss"}])
    identity, notice = next(iter(data["notifications"].items()))
    assert notice["severity"] == "urgent" and notification_sort_key(identity, notice)[0] == 0
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "replacement_unconfirmed": True, "operation": {"operation_id": "stop-1"}}])
    count = len(data["notifications"])
    record_recovery_notices(data, [{"account_key": "hyperliquid:mainnet", "trade_id": "t", "replacement_unconfirmed": True, "operation": {"operation_id": "stop-2"}}])
    assert len(data["notifications"]) > count


@pytest.mark.asyncio
async def test_source_mcp_controls_require_setup_revision_and_keep_other_sources(setup_server):  # noqa: F811
    server, state, _, token, _ = setup_server
    state.update(lambda data: data.update(sources=[str(-1000 - i) for i in range(14)]))
    previous = set(state.snapshot()["sources"])
    revision = state.snapshot()["configuration_revision"]
    ordinary = server.aqla_authority.sign("operator", "without-setup")
    for action in ("add_sources", "remove_sources", "resolve_conflict"):
        with pytest.raises(ToolError, match="Setup grant"):
            await server.call_tool("control", {"action": action, "context_auth": ordinary})
    await server.call_tool("control", {"action": "add_sources", "sources": ["-9991", "-9992", "-9993"], "expected_revision": revision, "context_auth": token})
    assert set(state.snapshot()["sources"]) == previous | {"-9991", "-9992", "-9993"}
    with pytest.raises(ToolError, match="revision"):
        await server.call_tool("control", {"action": "remove_sources", "sources": ["-9991"], "expected_revision": revision, "context_auth": token})
    with pytest.raises(ToolError, match="Unknown source title"):
        await server.call_tool("control", {"action": "add_sources", "sources": ["Unregistered free title"], "expected_revision": revision + 1, "context_auth": token})
    assert set(state.snapshot()["sources"]) == previous | {"-9991", "-9992", "-9993"}
    await close_server(server)


def selection():
    return {"configuration_revision": 7, "sources": [str(-1000 - index) for index in range(14)],
            "source_names": {"-8888": "Registrierter Kanal"}, "source_aliases": {}, "source_ranks": {}}


def test_add_three_sources_preserves_fourteen_then_remove_only_explicit_selection():
    data = selection()
    original = set(data["sources"])
    result = change_sources(data, "add_sources", ["-8888", "@NewChannel", "https://t.me/otherchannel"], 7)
    assert set(result["sources"]) == original | {"-8888", "@newchannel", "@otherchannel"}
    assert result["configuration_revision"] == 8
    result = change_sources(data, "remove_sources", ["Registrierter Kanal"], 8)
    assert set(result["sources"]) == original | {"@newchannel", "@otherchannel"}
    assert result["configuration_revision"] == 9


@pytest.mark.parametrize("selectors,revision", [(["Unknown title"], 7), (["-9999"], 6), (["*"], 7), (["@x"], 7), (["-9999"], True)])
def test_source_changes_reject_stale_unknown_and_unsupported_without_mutation(selectors, revision):
    data = selection()
    before = copy.deepcopy(data)
    with pytest.raises(AqlaError):
        change_sources(data, "add_sources", selectors, revision)
    assert data == before


def test_selector_registered_label_numeric_and_username():
    data = selection()
    assert supported_source(data, "Registrierter Kanal") == "-8888"
    assert supported_source(data, "-100123") == "-100123"
    assert supported_source(data, "@PUBLIC_Name") == "@public_name"


def test_percentage_display_rounding_does_not_change_financial_decimal():
    value = Decimal(1) / 3 * 100
    original = value
    assert number(value, places=2) == "33,33"
    assert value == original and str(value).startswith("33.333333")
    assert number("0.0000123") == "0,0000123"


async def conflicted(setup, signal):
    _, state, fake, _, trading = setup
    source, value = signal
    plan = await trading.preview(source.event_id, value)
    trade = await trading.execute(plan["plan_id"])
    trading._patch(trade["trade_id"], status="conflicted", external_conflict="manual")
    return state, fake, trading, trade["trade_id"]


def manual_fill(fake, size, *, start, direction="Close Long", side="A", tid=9999):
    fake.fills.append({"oid": tid, "tid": tid, "coin": "BTC", "time": now_ms() + 1, "sz": str(size), "px": "110",
                       "fee": "0.01", "feeToken": "USDC", "startPosition": str(start), "dir": direction, "side": side})


def terminal_orders(fake):
    for row in fake.orders.values():
        if row["status"] == "open":
            row["status"] = "canceled"


@pytest.mark.asyncio
async def test_flat_manual_closure_requires_full_original_fill_path_and_terminal_orders(setup, signal):
    state, fake, trading, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    manual_fill(fake, own, start=own)
    fake.position = 0
    before = copy.deepcopy(fake.actions)
    review, _ = await observe_conflict(trading, identity)
    assert not review["resolvable"] and "owned_orders_not_terminal" in review["reasons"]
    terminal_orders(fake)
    review, evidence = await observe_conflict(trading, identity)
    assert review["resolvable"] and review["pnl_complete"] is False
    assert any(row["kind"] == "manual_close" for row in evidence["fills"].values())
    assert fake.actions == before and state.snapshot()["trades"][identity]["status"] == "conflicted"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["partial", "foreign_reopen", "foreign_add_then_close", "missing_fill", "unknown_operation"])
async def test_ownership_review_never_adopts_mixed_or_unproved_exposure(setup, signal, case):
    state, fake, trading, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    terminal_orders(fake)
    if case == "partial":
        manual_fill(fake, own / 2, start=own)
        fake.position = float(own / 2)
    elif case == "foreign_reopen":
        manual_fill(fake, own, start=own)
        manual_fill(fake, 1, start=0, direction="Open Long", side="B", tid=99999)
        fake.position = 1
    elif case == "foreign_add_then_close":
        manual_fill(fake, 1, start=own, direction="Open Long", side="B")
        manual_fill(fake, own + 1, start=own + 1, tid=99999)
        fake.position = 0
    elif case == "unknown_operation":
        manual_fill(fake, own, start=own)
        fake.position = 0
        operation = next(iter(state.snapshot()["operations"]))
        state.update(lambda data: data["operations"][operation].update(phase="unknown"))
    else:
        fake.position = 0
    before = copy.deepcopy(fake.actions)
    review, _ = await observe_conflict(trading, identity)
    assert not review["resolvable"] and fake.actions == before


@pytest.mark.asyncio
async def test_delayed_fill_and_stale_fingerprint_cannot_release_reservation(setup, signal):
    from aqla_agent.router import Router
    settings, state, fake, exchange, trading = setup
    _, _, _, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    fake.position = 0
    terminal_orders(fake)
    router = Router(settings, state, exchange)
    old = await router.conflict_review(identity)
    assert not old["resolvable"]
    manual_fill(fake, own, start=own)
    with pytest.raises(AqlaError, match="changed"):
        await router.resolve_conflict(identity, old["fingerprint"], lambda: None)
    fresh = await router.conflict_review(identity)
    original = copy.deepcopy(state.snapshot()["trades"][identity]["fill_events"])
    result = await router.resolve_conflict(identity, fresh["fingerprint"], lambda: None)
    assert result["resolved"] and result["pnl_complete"] is False
    row = state.snapshot()["trades"][identity]
    assert row["status"] == "closed" and row["reserved_risk"] == "0" and row["cleanup_complete"]
    assert all(row["fill_events"][key] == value for key, value in original.items())
    assert digest(row["plan"]) == digest(trading.state.snapshot()["trades"][identity]["plan"])


@pytest.mark.asyncio
async def test_review_expiry_cross_account_and_commit_gate_never_resolve(setup, signal):
    from aqla_agent.router import Router
    settings, state, fake, exchange, _ = setup
    _, _, _, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    manual_fill(fake, own, start=own)
    fake.position = 0
    terminal_orders(fake)
    router = Router(settings, state, exchange)
    review = await router.conflict_review(identity)
    def denied():
        raise AqlaError("Setup expired at commit")
    with pytest.raises(AqlaError, match="Setup expired"):
        await router.resolve_conflict(identity, review["fingerprint"], denied)
    assert state.snapshot()["trades"][identity]["status"] == "conflicted"
    state.update_metadata(("source_status",), lambda data: data["source_status"]["ownership_reviews"][identity].update(expires_at=0))
    with pytest.raises(AqlaError, match="current ownership review"):
        await router.resolve_conflict(identity, review["fingerprint"], lambda: None)
    settings.network = "mainnet"
    with pytest.raises(AqlaError, match="network|original exchange account"):
        await router.conflict_review(identity)
    assert state.snapshot()["trades"][identity]["status"] == "conflicted"


@pytest.mark.asyncio
async def test_saved_review_survives_restart_without_exchange_action(setup, signal):
    from aqla_agent.router import Router
    from aqla_hyperliquid_mcp.state import State
    settings, state, fake, exchange, _ = setup
    _, _, _, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    manual_fill(fake, own, start=own)
    fake.position = 0
    terminal_orders(fake)
    first = Router(settings, state, exchange)
    review = await first.conflict_review(identity)
    before = copy.deepcopy(fake.actions)
    state.close()
    reopened = State(settings)
    try:
        restarted = Router(settings, reopened, exchange)
        result = await restarted.resolve_conflict(identity, review["fingerprint"], lambda: None)
        assert result["resolved"] and fake.actions == before
        assert reopened.snapshot()["trades"][identity]["manual_closure"]["pnl_complete"] is False
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_removed_source_cannot_execute_a_saved_plan(setup, signal):
    from aqla_hyperliquid_mcp.models import SourceEvent
    _, state, fake, _, trading = setup
    original, interpretation = signal
    state.update(lambda data: data.update(sources=["-101"]))
    source = SourceEvent(chat_id="-101", message_id=2, sent_at=now_ms(), text=original.text)
    trading.ingest(source)
    value = interpretation.model_copy(update={"source_refs": [source.source_id],
        "evidence": [row.model_copy(update={"source_id": source.source_id}) for row in interpretation.evidence]})
    plan = await trading.preview(source.event_id, value)
    state.update(lambda data: change_sources(data, "remove_sources", ["-101"], data["configuration_revision"]))
    with pytest.raises(AqlaError, match="subscri|Source"):
        await trading.execute(plan["plan_id"])
    assert fake.actions == []


@pytest.mark.asyncio
async def test_new_entry_receipt_requires_existing_quantity_reconciliation(setup, signal):
    state, fake, trading, identity = await conflicted(setup, signal)
    entry = next(row for row in fake.fills if row["oid"] == fake.orders[state.snapshot()["trades"][identity]["entry_cloid"]]["order"]["oid"])
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    fake.fills.append({**entry, "tid": 2000, "sz": "0.01", "time": now_ms()})
    manual_fill(fake, own + Decimal("0.01"), start=own + Decimal("0.01"))
    fake.position = 0
    terminal_orders(fake)
    review, _ = await observe_conflict(trading, identity)
    assert not review["resolvable"] and "entry_quantity_not_reconciled" in review["reasons"]
    assert state.snapshot()["trades"][identity]["filled_quantity"] == str(own)


@pytest.mark.asyncio
async def test_same_millisecond_native_fill_ids_keep_numeric_chronology(setup, signal):
    state, fake, trading, identity = await conflicted(setup, signal)
    own = Decimal(state.snapshot()["trades"][identity]["filled_quantity"])
    entry = fake.fills[0]
    manual_fill(fake, own, start=own, tid=1000)
    fake.fills[-1]["time"] = entry["time"]
    fake.position = 0
    terminal_orders(fake)
    review, _ = await observe_conflict(trading, identity)
    assert review["resolvable"]
