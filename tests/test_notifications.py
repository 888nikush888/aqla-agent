import copy
import json
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aqla_agent.lifecycle import record_lifecycle
from aqla_agent.messages import summary_block
from aqla_agent.notifications import NATIVE_BYTE_BUDGET, _summary, canonical_json, native_response_bytes
from aqla_hyperliquid_mcp.models import Market, Policy, SourceEvent, TradePlan, digest
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


@pytest.fixture
async def notification_server(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    yield server
    await close_server(server)


async def call(server, tool, **arguments):
    result = await server.call_tool(tool, arguments)
    payload = result_payload(result)
    if tool in ("notifications_read", "notifications_details"):
        text = next(block.text for block in result.content if block.type == "text")
        assert len(json.dumps({"result": text}, ensure_ascii=False).encode("utf-8")) == native_response_bytes(payload)
    return payload


async def ack(server, ids, versions=None):
    arguments = {"notification_ids": ids, "context_auth": server.aqla_authority.sign("source", "synthetic-notification-wake")}
    if versions is not None:
        arguments["versions"] = versions
    return await call(server, "notifications_ack", **arguments)


def add_notices(state, count):
    state.update(lambda data: data["notifications"].update({f"notice-{index:05}": {"error": "synthetic fact", "observed_at": index}
                                                         for index in range(count)}))


async def details(server, identity, version, limit=65536):
    cursor, fragments, offset = None, [], 0
    while True:
        result = await call(server, "notifications_details", notification_id=identity, version=version, cursor=cursor, limit=limit)
        assert native_response_bytes(result) <= NATIVE_BYTE_BUDGET
        assert result["offset_bytes"] == offset and result["fragment"]
        fragment = result["fragment"].encode("utf-8")
        assert len(fragment) <= limit
        fragments.append(fragment)
        offset += len(fragment)
        cursor = result["next_cursor"]
        if result["complete"]:
            assert cursor is None and offset == result["total_bytes"]
            return b"".join(fragments)
        assert cursor


def result_payload(result):
    return result.structured_content or json.loads(next(block.text for block in result.content if block.type == "text"))


def lifecycle_fixture(data, settings, count=200, fills=100):
    """Valid closed plans with both immutable events; never sends an exchange action."""
    for index in range(count):
        identity = f"trade-{index}"
        started = 1791430000000 + index * 60000
        source = SourceEvent(chat_id="channel", message_id=index + 1, sent_at=started, text="Synthetic closed-trade evidence")
        quantity = str(fills // 2) if fills > 100 else "1"
        plan = TradePlan(plan_id=f"plan-{index}", trade_id=identity, source_refs=[source.source_id],
                         policy_revision=0, policy=Policy(), network="testnet", account=settings.account.lower(), principal=settings.account.lower(),
                         market=Market(symbol="BTC", asset_id=0, product="perp", size_decimals=2, mid="100", max_leverage=20),
                         side="long", entry_kind="market", entry_price="100", entry_limit="100.3", stop_loss="90", take_profits=["110"],
                         quantity=quantity, tp_quantities=[quantity], risk_usdc=str(int(quantity) * 10), deadline_at=started + 300000, created_at=started, profile_id="standard")
        data["events"][source.event_id] = {"event_id": source.event_id, "source_id": source.source_id,
                                             "source": source.model_dump(mode="json"), "status": "report"}
        proof = {f"fill-{number}": {"kind": "entry" if number < fills // 2 else "tp", "oid": number, "tid": number,
                                   "time": started + number, "sz": "1" if fills > 100 else str(2 / fills), "px": "100" if number < fills // 2 else "110",
                                   "fee": "0.01", "feeToken": "USDC", "coin": "BTC", "index": 0} for number in range(fills)}
        data["trades"][identity] = {"trade_id": identity, "event_id": source.event_id, "chat_id": "channel",
                                   "status": "closed", "lifecycle_version": 1, "filled_quantity": quantity, "remaining_quantity": "0",
                                   "entry_vwap": "100", "stop_price": "90", "targets": ["110"], "target_budgets": [quantity], "tp_filled": [quantity],
                                   "entry_stop_confirmed": True, "orders": [], "fill_events": proof, "plan": plan.model_dump(mode="json")}
        record_lifecycle(data, identity, "entry_fill")
        record_lifecycle(data, identity, "closed")


@pytest.mark.asyncio
async def test_ack_stale_version_preserves_replaced_protection(setup):
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    identity = "protection:hyperliquid:testnet"
    state.update(lambda data: data["notifications"].update({identity: {"error": "A", "exchange": "hyperliquid"}}))
    read = result_payload(await server.call_tool("status", {}))
    original = copy.deepcopy(read["notifications"][identity])
    state.update(lambda data: data["notifications"].update({identity: {"error": "B", "exchange": "hyperliquid"}}))
    assert digest(state.snapshot()["notifications"][identity]) != digest(original)
    token = server.aqla_authority.sign("source", "synthetic-wake")
    ack = result_payload(await server.call_tool("notifications_ack", {"notification_ids": [identity], "context_auth": token,
                                                                    "versions": {identity: read["notification_versions"][identity]}}))
    assert ack["stale"] == [identity] and ack["processed"] == []
    assert state.snapshot()["notifications"][identity]["error"] == "B"
    await close_server(server)


@pytest.mark.asyncio
async def test_snapshot_count_only_handles_large_backlog(setup):
    settings, state, fake, exchange, _ = setup
    state.update(lambda data: lifecycle_fixture(data, settings))
    data = state.snapshot()
    assert len(data["notifications"]) == 400
    for trade in data["trades"].values():
        TradePlan.model_validate(trade["plan"])
        assert set(trade["lifecycle_events"]) == {"entry_fill", "closed"}
        assert len(trade["lifecycle_events"]["closed"]["facts"]["fill_events"]) == 100
    server = create_server(settings, state=state, exchange=exchange)
    snapshot = result_payload(await server.call_tool("integration", {"action": "snapshot", "token": server.aqla_authority.key.decode()}))
    assert snapshot["notification_pending"] == 400 and "notifications" not in snapshot
    assert len(json.dumps(snapshot).encode()) < 10_000
    assert not fake.actions
    await close_server(server)


@pytest.mark.asyncio
async def test_ack_identical_protection_replacement_has_new_version(notification_server, monkeypatch):
    server = notification_server
    monkeypatch.setattr("aqla_agent.pipelines.now_ms", lambda: 123)
    venue = server.aqla_trading.engine_for("hyperliquid", "testnet").state
    venue.pause_entries("identical reason")
    first = await call(server, "notifications_read")
    identity, = first["notifications"]
    before = server.aqla_state.snapshot()["notifications"][identity]
    venue.pause_entries("identical reason")
    after = server.aqla_state.snapshot()["notifications"][identity]
    assert before["observed_at"] == after["observed_at"] == 123
    assert before["generation_id"] != after["generation_id"] and len(after["generation_id"]) == 32
    result = await ack(server, [identity], first["notification_versions"])
    assert result["stale"] == [identity] and result["processed"] == []
    second = await call(server, "notifications_read")
    assert second["notification_versions"][identity] != first["notification_versions"][identity]
    assert (await ack(server, [identity], second["notification_versions"]))["processed"] == [identity]


@pytest.mark.asyncio
async def test_legacy_ack_mutable_notice_fails_atomically(notification_server):
    server = notification_server
    server.aqla_state.update(lambda data: data["notifications"].update({"immutable": {"error": "immutable fact"}, "protection:hyperliquid": {"error": "current incident"}}))
    before = server.aqla_state.snapshot()
    with pytest.raises(ToolError, match="reread and versioned"):
        await ack(server, ["immutable", "protection:hyperliquid"])
    assert server.aqla_state.snapshot() == before
    with pytest.raises(ToolError, match="every requested ID"):
        await ack(server, ["immutable", "protection:hyperliquid"], {"immutable": digest(before["notifications"]["immutable"])})
    assert server.aqla_state.snapshot() == before


@pytest.mark.asyncio
async def test_legacy_ack_immutable_notice_is_compatible(notification_server):
    server = notification_server
    add_notices(server.aqla_state, 1)
    result = await ack(server, ["notice-00000"])
    assert result == {"processed": ["notice-00000"], "stale": [], "missing": [], "delivery_owned_by": "Hermes"}


@pytest.mark.asyncio
async def test_ack_missing_and_duplicate_ids_are_idempotent(notification_server, monkeypatch):
    server = notification_server
    add_notices(server.aqla_state, 2)
    page = await call(server, "notifications_read")
    versions = {**page["notification_versions"], "missing": "unknown"}
    calls = []
    original = server.aqla_state.update
    def update(function):
        calls.append(function)
        return original(function)
    monkeypatch.setattr(server.aqla_state, "update", update)
    result = await ack(server, ["missing", "notice-00000", "missing", "notice-00000"], versions)
    assert len(calls) == 1
    assert result["processed"] == ["notice-00000"] and result["missing"] == ["missing"]
    assert "notice-00001" in server.aqla_state.snapshot()["notifications"]  # Extra version entries never remove it.
    repeated = await ack(server, ["notice-00000", "missing", "notice-00000"], versions)
    assert len(calls) == 2 and repeated["missing"] == ["notice-00000", "missing"]


@pytest.mark.asyncio
async def test_notifications_read_cursor_survives_ack_and_restart(notification_server, setup):
    server = notification_server
    settings, state, _, exchange, _ = setup
    add_notices(state, 5)
    page = await call(server, "notifications_read", limit=2)
    assert list(page["notifications"]) == ["notice-00000", "notice-00001"]
    await ack(server, list(page["notifications"]), page["notification_versions"])
    state.close()
    restarted = State(settings)
    fresh = create_server(settings, state=restarted, exchange=exchange)
    try:
        next_page = await call(fresh, "notifications_read", cursor=page["next_cursor"], limit=2)
        assert list(next_page["notifications"]) == ["notice-00002", "notice-00003"]
        assert next_page["notification_versions"]["notice-00002"] == digest(restarted.snapshot()["notifications"]["notice-00002"])
    finally:
        await close_server(fresh)


@pytest.mark.asyncio
async def test_rejects_bad_cursor_and_scope(notification_server):
    server = notification_server
    add_notices(server.aqla_state, 3)
    page = await call(server, "notifications_read", limit=1)
    cursor = page["next_cursor"]
    for bad in ("nonsense", cursor[:-1], "!" + cursor[1:], cursor + "=", "a" * 8193):
        with pytest.raises(ToolError, match="cursor"):
            await call(server, "notifications_read", cursor=bad)
    server.aqla_state.update(lambda data: data.update(trading_account="0x" + "f" * 40))
    with pytest.raises(ToolError, match="account/network"):
        await call(server, "notifications_read", cursor=cursor)


@pytest.mark.asyncio
async def test_new_urgent_notice_visible_on_next_start_page(notification_server):
    server = notification_server
    add_notices(server.aqla_state, 3)
    page = await call(server, "notifications_read", limit=1)
    server.aqla_trading.engine_for("hyperliquid", "testnet").state.pause_entries("new protection incident")
    following = await call(server, "notifications_read", cursor=page["next_cursor"], limit=1)
    assert list(following["notifications"]) == ["notice-00001"]
    start = await call(server, "notifications_read", limit=1)
    assert next(iter(start["notifications"])).startswith("protection:")


@pytest.mark.asyncio
async def test_details_reassembles_fills_and_unicode_losslessly(notification_server, setup):
    settings, state, *_ = setup
    state.update(lambda data: lifecycle_fixture(data, settings, count=1))
    identity = "lifecycle:trade-0:closed"
    state.update(lambda data: data["notifications"][identity].update(reason="🚀 Ä \\ \"\n" * 20000))
    original = state.snapshot()["notifications"][identity]
    result = await details(notification_server, identity, digest(original), limit=1023)
    assert result == canonical_json(original)
    assert len(json.loads(result)["lifecycle"]["facts"]["fill_events"]) == 100


@pytest.mark.asyncio
async def test_details_rejects_changed_version_and_other_purpose(notification_server):
    server = notification_server
    add_notices(server.aqla_state, 3)
    page = await call(server, "notifications_read", limit=1)
    identity = next(iter(page["notifications"]))
    version = page["notification_versions"][identity]
    with pytest.raises(ToolError, match="cursor"):
        await call(server, "notifications_details", notification_id=identity, version=version, cursor=page["next_cursor"])
    fragment = await call(server, "notifications_details", notification_id=identity, version=version, limit=4)
    other = "notice-00001"
    with pytest.raises(ToolError, match="another ID/version"):
        await call(server, "notifications_details", notification_id=other, version=page["notification_versions"].get(other, digest(server.aqla_state.snapshot()["notifications"][other])), cursor=fragment["next_cursor"])
    server.aqla_state.update(lambda data: data["notifications"][identity].update(error="changed incident"))
    with pytest.raises(ToolError, match="version changed"):
        await call(server, "notifications_details", notification_id=identity, version=version, cursor=fragment["next_cursor"])


@pytest.mark.asyncio
async def test_lifecycle_details_remain_readable_after_ack(notification_server, setup):
    settings, state, *_ = setup
    state.update(lambda data: lifecycle_fixture(data, settings, count=1))
    page = await call(notification_server, "notifications_read")
    identity = "lifecycle:trade-0:closed"
    original = canonical_json(state.snapshot()["notifications"][identity])
    await ack(notification_server, [identity], page["notification_versions"])
    assert await details(notification_server, identity, page["notification_versions"][identity]) == original
    assert identity not in state.snapshot()["notifications"]


@pytest.mark.asyncio
async def test_tiny_details_limit_at_unicode_boundary_fails_without_empty_progress(notification_server):
    server = notification_server
    server.aqla_state.update(lambda data: data["notifications"].update({"unicode": {"error": "🚀"}}))
    version = digest({"error": "🚀"})
    first = await call(server, "notifications_details", notification_id="unicode", version=version, limit=10)
    assert first["fragment"] == '{"error":"'
    with pytest.raises(ToolError, match="too small"):
        await call(server, "notifications_details", notification_id="unicode", version=version, cursor=first["next_cursor"], limit=1)


@pytest.mark.asyncio
async def test_notification_reads_do_not_mutate_state(notification_server, monkeypatch):
    server = notification_server
    add_notices(server.aqla_state, 2)
    before = server.aqla_state.snapshot()
    monkeypatch.setattr(server.aqla_state, "update", lambda *_: pytest.fail("Read tool mutated state"))
    page = await call(server, "notifications_read")
    await call(server, "notifications_details", notification_id="notice-00000", version=page["notification_versions"]["notice-00000"])
    await call(server, "status")
    assert server.aqla_state.snapshot() == before
    annotations = {tool.name: tool.annotations for tool in await server.list_tools()}
    assert annotations["notifications_read"].read_only_hint and annotations["notifications_details"].read_only_hint


@pytest.mark.asyncio
async def test_legacy_without_key_first_status_page_and_clear_read_errors(setup):
    settings, state, _, exchange, _ = setup
    add_notices(state, 150)
    server = create_server(settings.model_copy(update={"integration_key_file": None}), state=state, exchange=exchange)
    try:
        status = await call(server, "status")
        assert len(status["notifications"]) == 100
        assert status["notifications_page"] == {"pending": 150, "next_cursor": None, "pagination_available": False}
        with pytest.raises(ToolError, match="key is not configured"):
            await call(server, "notifications_read")
        with pytest.raises(ToolError, match="key is not configured"):
            await call(server, "notifications_details", notification_id="notice-00000", version="unknown")
    finally:
        await close_server(server)


@pytest.mark.asyncio
async def test_status_and_telegram_view_share_bounded_page(notification_server, setup):
    settings, state, fake, *_ = setup
    state.update(lambda data: lifecycle_fixture(data, settings))
    before = state.snapshot()
    status = await call(notification_server, "status")
    page = await call(notification_server, "notifications_read")
    assert status["notifications"] == page["notifications"]
    assert status["notification_versions"] == page["notification_versions"]
    assert status["telegram_view"]["messages"] == page["telegram_view"]["messages"]
    assert 0 < len(page["notifications"]) < 100 and page["pending"] == 400
    assert native_response_bytes(page) <= NATIVE_BYTE_BUDGET
    assert native_response_bytes(status) < 2_000_000
    assert state.snapshot() == before and not fake.actions


@pytest.mark.asyncio
async def test_remaining_ids_are_page_local(notification_server):
    server = notification_server
    add_notices(server.aqla_state, 201)
    page = await call(server, "notifications_read", limit=3)
    selected = set(page["notifications"])
    delivered = set()
    for message in page["telegram_view"]["messages"]:
        delivered.update(message["notification_ids"])
        assert set(message["notification_versions"]) == set(message["notification_ids"])
        assert message["notification_versions"] == {identity: page["notification_versions"][identity] for identity in message["notification_ids"]}
    assert delivered | set(page["telegram_view"]["remaining_notification_ids"]) == selected
    assert not (set(server.aqla_state.snapshot()["notifications"]) - selected) & set(page["telegram_view"]["remaining_notification_ids"])


@pytest.mark.asyncio
async def test_oversize_notice_has_summary_and_lossless_details(notification_server):
    server = notification_server
    identity = "protection:hyperliquid:testnet"
    original = {"exchange": "hyperliquid", "network": "testnet", "account_key": "hyperliquid:testnet", "observed_at": 123,
                "error": "🚀 [UNTRUSTED]* \\ \"\n" * 50000, "generation_id": "synthetic-new-generation"}
    server.aqla_state.update(lambda data: data["notifications"].update({identity: original}))
    page = await call(server, "notifications_read")
    notice = page["notifications"][identity]
    assert notice["detail_level"] == "summary" and notice["details_available"]
    assert page["notification_versions"][identity] == digest(original)
    text = "\n".join(page["telegram_view"]["messages"][0]["texts"])
    assert "**Grund (gekürzt):**" in text and "**Entries:** gesperrt" in text
    assert "**Details:** Umfangreiche gespeicherte Belege; vollständige Daten separat abrufbar." in text
    assert digest(original) not in text and "synthetic-new-generation" not in text
    assert await details(server, identity, digest(original)) == canonical_json(original)
    assert server.aqla_state.snapshot()["notifications"][identity] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [400, 10000])
async def test_notification_pages_reach_400_and_10000_ids(notification_server, count):
    server = notification_server
    add_notices(server.aqla_state, count)
    cursor, found = None, []
    while True:
        page = await call(server, "notifications_read", cursor=cursor)
        assert native_response_bytes(page) <= NATIVE_BYTE_BUDGET
        assert page["pending"] == count and page["notifications"]
        found.extend(page["notifications"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert found == [f"notice-{index:05}" for index in range(count)]


@pytest.mark.parametrize("kind,label", [("entry_fill", "Ausführung"), ("closed", "Schließung"), ("protection", "Beobachtet")])
def test_summary_preserves_account_binding_and_berlin_date(setup, kind, label):
    settings, state, *_ = setup
    snapshot = state.snapshot()
    lifecycle_fixture(snapshot, settings, count=1)
    plan = TradePlan.model_validate(snapshot["trades"]["trade-0"]["plan"]).model_copy(update={"account_key": "aster:mainnet", "exchange": "aster", "network": "mainnet"})
    timestamp = int(datetime(2026, 10, 8, 8, tzinfo=UTC).timestamp() * 1000)
    if kind == "protection":
        identity = "protection:aster:mainnet"
        notice = {"account_key": "aster:mainnet", "exchange": "aster", "network": "mainnet", "observed_at": timestamp, "error": "saved incident"}
    else:
        identity = "lifecycle:trade-0:" + kind
        notice = {"trade_id": "trade-0", "lifecycle": {"kind": kind, "time": timestamp, "facts": {"plan": plan.model_dump(mode="json"), "status": "closed"}}}
    original = copy.deepcopy(notice)
    summary = _summary(identity, notice)
    assert summary["account_key"] == "aster:mainnet"
    text = summary_block(summary)
    assert f"**{label}:** 08.10.2026 10:00:00 · Berlin" in text
    assert "**Konto:** aster:mainnet" in text and str(timestamp) not in text
    assert "Millisekunden" not in text
    assert native_response_bytes({"notifications": {identity: summary}, "text": text}) <= NATIVE_BYTE_BUDGET
    assert notice == original
