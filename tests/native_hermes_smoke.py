"""Run with the isolated Hermes Python interpreter and the exact pinned checkout."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


async def verify_native_ingress(home, runner):
    from datetime import UTC, datetime

    from gateway.config import PlatformConfig
    from gateway.platforms.event import MessageType
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Message

    from aqla_agent.events import EXTENSIONS, HermesExtension
    from aqla_hyperliquid_mcp.models import Settings

    secret = home / "integration.secret"
    secret.write_text("native-test-only-integration-" * 3, encoding="utf-8")
    settings = Settings(network="testnet", account="0x" + "0" * 40, integration_key_file=secret)
    configuration = home / "aqla.yaml"
    configuration.write_text(yaml.safe_dump(settings.model_dump(mode="json")), encoding="utf-8")
    values = {"owner_id": "12345", "config_path": str(configuration)}
    extension = HermesExtension(SimpleNamespace(get_config=lambda key, default=None: values.get(key, default)))
    EXTENSIONS.add(extension)
    adapter = TelegramAdapter(PlatformConfig(extra={"allow_from": ["12345"]}))
    adapter._should_drop_delayed_delivery = lambda: False
    adapter._accept_update = lambda: None
    adapter._text_batch_delay_seconds = adapter._text_batch_split_delay_seconds = 0
    adapter._media_batch_delay_seconds = 0
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0
    captured = []
    async def dispatch(event):
        captured.append(event)
    adapter.handle_message = dispatch

    def event(identity, *, forwarded=False, photo=False, text=None):
        body = text or ("forwarded source " if forwarded else "owner request ") + str(identity)
        payload = {"message_id": identity, "date": int(datetime.now(UTC).timestamp()),
                   "chat": {"id": 12345, "type": "private", "first_name": "Owner"},
                   "from": {"id": 12345, "is_bot": False, "first_name": "Owner"},
                   "caption" if photo else "text": body}
        if forwarded:
            payload["forward_origin"] = {"type": "user", "date": payload["date"],
                                         "sender_user": {"id": 99, "is_bot": False, "first_name": "Source"}}
        raw = Message.de_json(payload, None)
        return adapter._build_message_event(raw, MessageType.PHOTO if photo else MessageType.TEXT)

    try:
        # Actual native text enqueue/flush: both mixed-origin arrival orders,
        # plus two forwards, retain separate identities and original bodies.
        for pair in ((event(1), event(2, forwarded=True)), (event(3, forwarded=True), event(4)),
                     (event(5, forwarded=True), event(6, forwarded=True))):
            captured.clear()
            for item in pair:
                adapter._enqueue_text_event(item)
            await asyncio.gather(*tuple(adapter._pending_text_batch_tasks.values()))
            assert {item.message_id for item in captured} == {item.message_id for item in pair}
            assert len(captured) == 2
            for item in captured:
                if item.metadata["aqla_origin"] == "source":
                    receipt = extension.forward_event(item)
                    assert receipt.message_id == int(item.message_id) and receipt.text == item.text
                    assert item.allow_gateway_control is False
                else:
                    assert item.allow_gateway_control is True

        # Native photo enqueue/flush and forwarded album dispatch cannot merge
        # a source into owner media or collapse two source receipts.
        for pair in ((event(7, photo=True), event(8, forwarded=True, photo=True)),
                     (event(81, forwarded=True, photo=True), event(82, photo=True)),
                     (event(9, forwarded=True, photo=True), event(10, forwarded=True, photo=True))):
            captured.clear()
            for item in pair:
                adapter._enqueue_photo_event(adapter._photo_batch_key(item, item.raw_message), item)
            await asyncio.gather(*tuple(adapter._pending_photo_batch_tasks.values()))
            assert len(captured) == 2
            assert {item.message_id for item in captured} == {item.message_id for item in pair}
            for item in captured:
                if item.metadata["aqla_origin"] == "source":
                    assert item.allow_gateway_control is False
                    assert extension.forward_event(item).message_id == int(item.message_id)
        captured.clear()
        await adapter._queue_media_group_event("same-forwarded-album", event(11, forwarded=True, photo=True))
        await adapter._queue_media_group_event("same-forwarded-album", event(12, forwarded=True, photo=True))
        await asyncio.gather(*tuple(adapter._media_group_tasks.values()))
        assert [item.message_id for item in captured] == ["11", "12"]
        held = []
        adapter._should_drop_delayed_delivery = lambda: True
        adapter._hold_inbound_event = lambda item, **kwargs: held.append(item)
        await adapter._queue_media_group_event("held-forwarded-album", event(121, forwarded=True, photo=True))
        assert [item.message_id for item in held] == ["121"]
        assert [item.message_id for item in captured] == ["11", "12"]
        adapter._should_drop_delayed_delivery = lambda: False

        owner, forwarded = event(13, text="/help"), event(14, forwarded=True, text="/help")
        assert owner.is_command() and owner.get_command() == "help"
        assert not forwarded.is_command() and forwarded.get_command() is None
        assert extension.forward_event(forwarded).text == "/help"
        assert not adapter._can_merge_text_debounce_events(owner, forwarded)
        assert not adapter._can_merge_text_debounce_events(event(15, forwarded=True), event(16, forwarded=True))
        assert adapter._can_merge_text_debounce_events(event(17), event(18))

        # Actual native busy FIFO/photo merge boundary, not a hand-written queue.
        runner._delivery_adapter_for = lambda source: adapter
        queue_key = "native-ingress-test"
        for queued in ((event(19, photo=True), event(20, forwarded=True, photo=True), event(21, forwarded=True, photo=True)),
                       (event(22, forwarded=True, photo=True), event(23, photo=True), event(24, forwarded=True, photo=True)),
                       (event(25), event(26, forwarded=True), event(27, forwarded=True)),
                       (event(28, forwarded=True), event(29), event(30, forwarded=True))):
            for item in queued:
                runner._queue_or_replace_pending_event(queue_key, item)
            drained = [adapter._pending_messages.pop(queue_key)]
            while runner._overflow_queue(queue_key):
                drained.append(runner._promote_queued_event(queue_key, adapter, None))
            assert [item.message_id for item in drained] == [item.message_id for item in queued]
            assert [item.text for item in drained] == [item.text for item in queued]
            assert [item.metadata["aqla_origin"] for item in drained] == [item.metadata["aqla_origin"] for item in queued]
            for item in drained:
                if item.metadata["aqla_origin"] == "source":
                    assert item.allow_gateway_control is False
                    receipt = extension.forward_event(item)
                    assert receipt.message_id == int(item.message_id) and receipt.text == item.text
    finally:
        EXTENSIONS.discard(extension)
        for task in (*adapter._pending_text_batch_tasks.values(), *adapter._pending_photo_batch_tasks.values(), *adapter._media_group_tasks.values()):
            task.cancel()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_SKIP_UPDATE_CHECK"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(args.hermes_source).resolve()))

    from plugins.platforms.telegram.adapter import TelegramAdapter

    from aqla_agent.hermes_runtime import install_strict_profile
    command_methods = {name: getattr(TelegramAdapter, name) for name in ("_register_command_menu", "_handle_command", "_handle_inline_query", "_ensure_forum_commands")}
    undo = install_strict_profile()
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore
    assert all(getattr(TelegramAdapter, name) is method for name, method in command_methods.items())

    configuration = GatewayConfig()
    runner = GatewayRunner(configuration)
    assert runner._session_db is None
    store = SessionStore(home / "sessions", configuration)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345")
    entry = store.get_or_create_session(source)
    message = {"role": "user", "content": "Hallo", "message_id": "1", "display_metadata": {"gateway_input_owner": "native-input"}}
    store.append_to_transcript(entry.session_id, message)
    assert store.load_transcript(entry.session_id)[-1] == message
    assert store.has_input_owner(entry.session_id, "native-input")
    assert store.transcript_tail_role(entry.session_id) == "user"
    restored = SessionStore(home / "sessions", configuration)
    assert restored.load_transcript(entry.session_id)[-1] == message

    class Bot:
        def __init__(self):
            self.registered = []

        async def set_my_commands(self, commands, **kwargs):
            self.registered.append((commands, kwargs["scope"]))

    adapter = object.__new__(TelegramAdapter)
    adapter._bot = Bot()
    adapter.platform = Platform.TELEGRAM
    adapter._forum_command_registered = set()
    asyncio.run(adapter._register_command_menu())
    assert len(adapter._bot.registered) == 3
    assert all(commands for commands, _ in adapter._bot.registered)
    asyncio.run(verify_native_ingress(home, runner))
    assert not list(home.rglob("*.db"))
    assert not list(home.rglob("*-wal"))
    undo()
    print(json.dumps({"native_session": True, "restart_history": True, "database_files": 0, "native_commands_preserved": True, "command_scopes_registered": 3, "native_ingress_batches_isolated": True}))


if __name__ == "__main__":
    main()
