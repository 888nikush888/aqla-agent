from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aqla_agent.telegram import SourceAccessError, TelegramReader
from aqla_hyperliquid_mcp.models import Settings


def message(identity, *, edited=False):
    return SimpleNamespace(chat_id=-100123, id=identity, date=datetime.now(UTC), edit_date=datetime.now(UTC) if edited else None,
                           reply_to=None, grouped_id=None, message="Updated SL" if edited else "Historical setup", fwd_from=None, photo=None, media=None)


class ReaderClient:
    def __init__(self):
        self.handlers = []
        self.message = message(1)
        self.entity = SimpleNamespace(username="SignalA")

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    async def iter_dialogs(self):
        yield SimpleNamespace(id=-100123, entity=self.entity)

    async def iter_messages(self, entity, **kwargs):
        if "min_id" not in kwargs:
            yield self.message

    async def get_messages(self, entity, ids):
        return [message(identity, edited=True) for identity in ids]

    async def disconnect(self):
        pass


@pytest.mark.asyncio
async def test_public_source_name_resolves_to_real_chat_id_without_sqlite(tmp_path):
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=tmp_path / "state.json",
                        telegram_api_id=123, telegram_api_hash_file=tmp_path / "hash.secret", telegram_session_file=tmp_path / "session.secret")
    settings.telegram_api_hash_file.write_text("test-hash")
    settings.telegram_session_file.write_text("")
    configuration = {"sources": ["https://t.me/SignalA"], "cursors": {}, "tracked_ids": {}}
    received, resolved = [], []

    async def snapshot():
        return configuration

    async def deliver(source):
        received.append(source)

    async def aliases(value):
        resolved.append(value)

    client = ReaderClient()
    reader = TelegramReader(settings, deliver, snapshot, resolve=aliases, client_factory=lambda *args: client)
    await reader.start()
    assert resolved == [{"https://t.me/SignalA": "-100123"}]
    assert received[0].chat_id == "-100123"
    assert received[0].historical is True
    assert not list(tmp_path.glob("*.session"))
    await reader.close()


@pytest.mark.asyncio
async def test_restart_fetches_edits_of_tracked_messages_below_the_new_message_cursor(tmp_path):
    settings = Settings(network="testnet", account="0x" + "0" * 40)
    configuration = {"sources": ["@signala"], "cursors": {"-100123": 50}, "tracked_ids": {"-100123": [1]}}
    received = []

    async def snapshot():
        return configuration

    async def deliver(source):
        received.append(source)

    reader = TelegramReader(settings, deliver, snapshot)
    reader.client = ReaderClient()
    await reader.sync_sources()
    assert len(received) == 1
    assert received[0].message_id == 1
    assert received[0].edited_at is not None
    assert received[0].historical is False


@pytest.mark.asyncio
async def test_failed_low_message_cannot_be_skipped_by_later_live_cursor(tmp_path):
    settings = Settings(network="testnet", account="0x" + "0" * 40)
    configuration = {"sources": ["@signala"], "cursors": {"-100123": 1}, "tracked_ids": {}}
    received = []
    fail = True

    async def snapshot():
        return configuration

    async def deliver(source):
        nonlocal fail
        if source.message_id == 2 and fail:
            fail = False
            raise RuntimeError("Transient ingest failure")
        if source.message_id not in received:
            received.append(source.message_id)
        configuration["cursors"][source.chat_id] = source.message_id

    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            for identity in (2, 3):
                if identity > kwargs.get("min_id", 0):
                    yield message(identity)

    reader = TelegramReader(settings, deliver, snapshot)
    reader.client = Client()
    reader.aliases = {"@signala": "-100123"}
    reader.selected = list(configuration["sources"])
    reader.entities = {"-100123": reader.client.entity}
    with pytest.raises(RuntimeError):
        await reader.received(SimpleNamespace(message=message(2)))
    assert configuration["cursors"]["-100123"] == 1
    await reader.received(SimpleNamespace(message=message(3)))
    assert received == [2, 3]


@pytest.mark.asyncio
async def test_initial_history_stays_historical_after_interrupted_seed(tmp_path):
    settings = Settings(network="testnet", account="0x" + "0" * 40)
    configuration = {"sources": ["@signala"], "cursors": {}, "tracked_ids": {}, "context_cutoffs": {}}
    received = []
    fail = True
    async def snapshot():
        return configuration
    async def bootstrap(cutoffs, context_ids):
        configuration["context_cutoffs"].update(cutoffs)
        configuration["context_ids"] = context_ids
        configuration["cursors"].update(cutoffs)
    async def deliver(source):
        nonlocal fail
        if source.message_id == 2 and fail:
            fail = False
            raise RuntimeError("Initial history interrupted")
        if (source.message_id, source.historical) not in received:
            received.append((source.message_id, source.historical))
        configuration["cursors"][source.chat_id] = max(source.message_id, configuration["cursors"].get(source.chat_id, 0))
    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            for identity in ([3, 2, 1] if "min_id" not in kwargs else [2, 3, 4]):
                if identity > kwargs.get("min_id", 0):
                    yield message(identity)
        async def get_messages(self, entity, ids):
            return [message(identity) for identity in ids]
    reader = TelegramReader(settings, deliver, snapshot, bootstrap=bootstrap)
    reader.client = Client()
    with pytest.raises(RuntimeError):
        await reader.sync_sources()
    restarted = TelegramReader(settings, deliver, snapshot, bootstrap=bootstrap)
    restarted.client = Client()
    await restarted.sync_sources()
    assert received == [(1, True), (2, True), (3, True), (4, False)]


@pytest.mark.asyncio
async def test_historical_edits_stay_context_in_reread_and_live_paths(tmp_path):
    settings = Settings(network="testnet", account="0x" + "0" * 40)
    configuration = {"sources": ["@signala"], "cursors": {"-100123": 10}, "tracked_ids": {"-100123": [1]}, "context_cutoffs": {"-100123": 10}}
    received = []
    async def snapshot():
        return configuration
    async def deliver(source):
        received.append(source)
    reader = TelegramReader(settings, deliver, snapshot)
    reader.client = ReaderClient()
    await reader.sync_sources()
    await reader.received(SimpleNamespace(message=message(1, edited=True)))
    assert received
    assert all(source.historical and source.edited_at is not None for source in received)


@pytest.mark.asyncio
async def test_unsubscribed_live_message_does_no_snapshot_or_history_io():
    async def unexpected(*args, **kwargs):
        pytest.fail("An unrelated live message must not cause intake I/O")
    reader = TelegramReader(Settings(network="testnet", account="0x" + "0" * 40), unexpected, unexpected)
    reader.aliases = {"@signala": "-100123"}
    await reader.received(SimpleNamespace(message=SimpleNamespace(chat_id=-999)))


@pytest.mark.asyncio
async def test_live_recovery_only_queries_affected_subscribed_source():
    configuration = {"sources": ["@signala", "@signalb"], "cursors": {"-100123": 10, "-100456": 20}, "tracked_ids": {}}
    queries = []
    async def snapshot():
        return configuration
    async def deliver(source):
        pass
    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            queries.append(entity)
            if False:
                yield
    reader = TelegramReader(Settings(network="testnet", account="0x" + "0" * 40), deliver, snapshot)
    reader.client = Client()
    reader.selected = list(configuration["sources"])
    reader.aliases = {"@signala": "-100123", "@signalb": "-100456"}
    reader.entities = {"-100123": "source-a", "-100456": "source-b"}
    await reader.received(SimpleNamespace(message=message(11)))
    assert queries == ["source-a"]
    assert reader.last_backfill == 0
    await reader.sync_sources()
    assert queries == ["source-a", "source-a", "source-b"]


@pytest.mark.asyncio
async def test_failed_first_seed_retries_only_persisted_context_ids_and_newer_posts():
    configuration = {"sources": ["@signala"], "cursors": {}, "context_cutoffs": {}, "context_ids": {}, "tracked_ids": {}}
    delivered, queries = [], []
    fail = True
    async def snapshot():
        return configuration
    async def bootstrap(cutoffs, context_ids):
        configuration["context_cutoffs"].update(cutoffs)
        configuration["context_ids"].update(context_ids)
        configuration["cursors"].update(cutoffs)
    async def deliver(source):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("First context delivery interrupted")
        delivered.append((source.message_id, source.historical))
        configuration["cursors"][source.chat_id] = max(configuration["cursors"][source.chat_id], source.message_id)
    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            queries.append(kwargs)
            if "limit" in kwargs:
                for identity in (10000, 9999):
                    yield message(identity)
            else:
                assert kwargs["min_id"] == 10000
                yield message(10001)
        async def get_messages(self, entity, ids):
            assert ids == [9999, 10000]
            return [message(identity) for identity in ids]
    settings = Settings(network="testnet", account="0x" + "0" * 40, max_context_messages=2)
    reader = TelegramReader(settings, deliver, snapshot, bootstrap=bootstrap)
    reader.client = Client()
    with pytest.raises(RuntimeError):
        await reader.sync_sources()
    assert configuration["cursors"]["-100123"] == 10000
    restarted = TelegramReader(settings, deliver, snapshot, bootstrap=bootstrap)
    restarted.client = Client()
    await restarted.sync_sources()
    assert delivered == [(9999, True), (10000, True), (10001, False)]
    assert queries == [{"limit": 2}, {"min_id": 10000, "reverse": True}]


@pytest.mark.asyncio
async def test_seed_context_mcp_persists_selection_before_any_delivery(setup):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, _, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    token = server.aqla_authority.key.decode()
    await server.call_tool("integration", {"action": "seed_context", "token": token, "cutoffs": {"channel": 10000}, "context_ids": {"channel": [9999, 10000]}})
    data = state.snapshot()
    assert data["context_ids"] == {"channel": [9999, 10000]}
    assert data["context_cutoffs"] == {"channel": 10000}
    assert data["cursors"]["channel"] == 10000
    await server.call_tool("integration", {"action": "seed_context", "token": token, "cutoffs": {"channel": 10000}, "context_ids": {"channel": [10000]}})
    assert state.snapshot()["context_ids"] == {"channel": [9999, 10000]}
    with pytest.raises(ToolError, match="cannot be replaced"):
        await server.call_tool("integration", {"action": "seed_context", "token": token, "cutoffs": {"channel": 10001}, "context_ids": {"channel": [10000, 10001]}})
    assert state.snapshot()["context_cutoffs"] == {"channel": 10000}
    snapshot = await server.call_tool("integration", {"action": "snapshot", "token": token})
    assert snapshot.is_error is not True
    await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_ids", [None, {}, {"channel": [0]}, {"channel": [3]}, {"channel": [2, 1]},
                                         {"channel": [1, 1]}, {"channel": [1, 2, 3]}])
async def test_seed_context_mcp_rejects_invalid_or_unbounded_selection(setup, context_ids):
    from mcp.server.mcpserver.exceptions import ToolError

    from aqla_hyperliquid_mcp.server import close_server, create_server
    settings, state, _, exchange, _ = setup
    settings.max_context_messages = 2
    server = create_server(settings, state=state, exchange=exchange)
    token = server.aqla_authority.key.decode()
    with pytest.raises(ToolError, match="bounded sorted context IDs"):
        await server.call_tool("integration", {"action": "seed_context", "token": token, "cutoffs": {"channel": 2}, "context_ids": context_ids})
    assert state.snapshot().get("context_cutoffs", {}) == {}
    assert state.snapshot().get("context_ids", {}) == {}
    assert state.snapshot()["cursors"] == {}
    await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, 9998, 10005])
async def test_legacy_seed_recovers_bounded_selection_with_original_cutoff(cursor):
    configuration = {"sources": ["@signala"], "cursors": {} if cursor is None else {"-100123": cursor},
                     "context_cutoffs": {"-100123": 10000}, "tracked_ids": {}}
    delivered, queries = [], []
    async def snapshot():
        return configuration
    async def bootstrap(cutoffs, context_ids):
        assert cutoffs == {"-100123": 10000}
        configuration["context_ids"] = context_ids
        configuration["cursors"].setdefault("-100123", 10000)
    async def deliver(source):
        delivered.append((source.message_id, source.historical))
    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            queries.append(kwargs)
            if "limit" in kwargs:
                yield message(10000)
                yield message(9999)
            else:
                yield message(10006)
        async def get_messages(self, entity, ids):
            assert ids == [9999, 10000]
            return [message(identity) for identity in reversed(ids)]
    reader = TelegramReader(Settings(network="testnet", account="0x" + "0" * 40, max_context_messages=2), deliver, snapshot, bootstrap=bootstrap)
    reader.client = Client()
    await reader.sync_sources()
    assert queries == [{"limit": 2, "max_id": 10001}, {"min_id": max(cursor or 0, 10000), "reverse": True}]
    assert delivered == [(9999, True), (10000, True), (10006, False)]


@pytest.mark.asyncio
async def test_unavailable_subscription_does_not_block_accessible_source_and_recovers():
    configuration = {"sources": ["@missing", "@signala"], "cursors": {}, "tracked_ids": {}}
    delivered, resolutions = [], []
    async def snapshot():
        return configuration
    async def deliver(source):
        delivered.append(source.chat_id)
    async def resolve(aliases):
        resolutions.append(aliases)
    class Client(ReaderClient):
        available = False
        async def iter_dialogs(self):
            async for dialog in super().iter_dialogs():
                yield dialog
            if self.available:
                yield SimpleNamespace(id=-100456, entity=SimpleNamespace(username="missing"))
        async def iter_messages(self, entity, **kwargs):
            if "limit" in kwargs:
                item = message(1)
                item.chat_id = -100123 if entity.username == "SignalA" else -100456
                yield item
    reader = TelegramReader(Settings(network="testnet", account="0x" + "0" * 40), deliver, snapshot, resolve=resolve)
    reader.client = Client()
    await reader.sync_sources()
    assert delivered == ["-100123"]
    assert reader.status_error() == {"code": "sources_unavailable", "sources": ["@missing"]}
    reader.client.available = True
    await reader.sync_sources(force=True)
    assert "-100456" in delivered
    assert resolutions[-1] == {"@missing": "-100456", "@signala": "-100123"}
    assert reader.status_error() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["session_missing", "session_invalid", "hash_missing"])
async def test_reader_file_errors_have_safe_precise_codes(tmp_path, failure):
    settings = Settings(network="testnet", account="0x" + "0" * 40, telegram_api_id=123,
                        telegram_api_hash_file=tmp_path / "hash.secret", telegram_session_file=tmp_path / "session.secret")
    if failure != "session_missing":
        settings.telegram_session_file.write_text("invalid-secret-session" if failure == "session_invalid" else "")
    if failure != "hash_missing":
        settings.telegram_api_hash_file.write_text("secret-hash")
    reader = TelegramReader(settings, None, None)
    with pytest.raises(SourceAccessError) as captured:
        await reader.start()
    assert captured.value.code == {"session_missing": "reader_session_missing", "session_invalid": "reader_session_invalid",
                                   "hash_missing": "reader_configuration_missing"}[failure]
    assert "secret" not in str(captured.value)
    assert reader.client is None


@pytest.mark.parametrize("exception, code", [("SessionRevokedError", "reader_session_unauthorized"),
                                            ("AuthKeyDuplicatedError", "reader_session_unauthorized"),
                                            ("ApiIdInvalidError", "reader_api_credentials_invalid"),
                                            ("ChannelPrivateError", "sources_access_denied")])
def test_reader_rpc_errors_expose_only_safe_type_codes(exception, code):
    from telethon import errors
    failure = SourceAccessError.from_error(getattr(errors, exception)(request="private-credential-value"))
    assert failure.code == code
    assert "private-credential-value" not in str(failure)
    assert SourceAccessError.from_error(RuntimeError("private-credential-value")).code == "reader_unavailable"
