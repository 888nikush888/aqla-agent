"""Run with the isolated Hermes Python interpreter and the exact pinned checkout."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


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
    assert not list(home.rglob("*.db"))
    assert not list(home.rglob("*-wal"))
    undo()
    print(json.dumps({"native_session": True, "restart_history": True, "database_files": 0, "native_commands_preserved": True, "command_scopes_registered": 3}))


if __name__ == "__main__":
    main()
