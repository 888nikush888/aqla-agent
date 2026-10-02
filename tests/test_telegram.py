from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aqla_agent.telegram import TelegramReader
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
    async def bootstrap(cutoffs):
        configuration["context_cutoffs"].update(cutoffs)
    async def deliver(source):
        nonlocal fail
        if source.message_id == 2 and fail:
            fail = False
            raise RuntimeError("Initial history interrupted")
        received.append((source.message_id, source.historical))
        configuration["cursors"][source.chat_id] = source.message_id
    class Client(ReaderClient):
        async def iter_messages(self, entity, **kwargs):
            for identity in ([3, 2, 1] if "min_id" not in kwargs else [2, 3, 4]):
                if identity > kwargs.get("min_id", 0):
                    yield message(identity)
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
