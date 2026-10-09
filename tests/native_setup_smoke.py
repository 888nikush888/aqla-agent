"""Actual pinned PTB registration, DEBUG transport admission and MCP lease checks, offline."""
import argparse
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

SYNTHETIC_PASSWORD = "/native-fixture-marker-never-a-user-credential"
WRONG_SYNTHETIC_PASSWORD = "/native-wrong-fixture-marker-never-a-user-credential"


async def grant_fixture(extension):
    """Explicit synthetic fixture goes through local hash, DeleteACK and real MCP registration."""
    class Bot:
        async def send_message(self, **_):
            return SimpleNamespace(message_id=1001)
        async def delete_message(self, **_):
            return True
    def update(text):
        return SimpleNamespace(effective_message=SimpleNamespace(text=text, message_id=1002,
            from_user=SimpleNamespace(id="12345", is_bot=False), chat=SimpleNamespace(id="12345", type="private")))
    await extension.setup.intercept(update("/setup"), Bot())
    await extension.setup.intercept(update(SYNTHETIC_PASSWORD), Bot())
    assert extension.setup.claims() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--mcp-python", required=True)
    args = parser.parse_args()
    home = Path(args.home).resolve()
    os.environ["HERMES_HOME"] = str(home / "hermes-home")
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(args.hermes_source).resolve())]
    from aqla_agent.events import EXTENSIONS
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.install import prepare
    from aqla_agent.native_setup import SETUP_PROMPT, NativeSetup
    from aqla_agent.setup import write_password_hash
    from aqla_hyperliquid_mcp.models import AqlaError
    from aqla_hyperliquid_mcp.storage import read_snapshot

    prepare(Path(__file__).resolve().parents[1], home, "12345")
    verifier = home / "secrets/synthetic-setup.json"
    write_password_hash(verifier, SYNTHETIC_PASSWORD)
    own_path = home / "config.yaml"
    own_config = yaml.safe_load(own_path.read_text())
    own_config["setup_password_hash_file"] = str(verifier)
    own_config["telegram_session_file"] = None
    own_path.write_text(yaml.safe_dump(own_config))
    host_path = home / "hermes-home/config.yaml"
    host_config = yaml.safe_load(host_path.read_text())
    for value in host_config["mcp_servers"].values():
        value["command"] = str(Path(args.mcp_python).absolute())
    host_path.write_text(yaml.safe_dump(host_config))
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session import SessionStore
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Update
    from telegram.ext import Application, ExtBot
    from telegram.request import BaseRequest
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    undo = install_strict_profile()
    from hermes_cli.plugins import discover_plugins
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    discover_plugins(force=True)
    names = discover_mcp_tools()
    assert "mcp__aqla__setup_status" in names
    assert not any(name.endswith(("sync_presentation_internal", "presentation_selection_internal", "integration")) for name in names)
    extension = next(iter(EXTENSIONS))
    admitted, sent, deleted, menus = [], [], [], []
    delete_ack, tool_arguments = [True], []
    native_mcp_call = extension.mcp_call
    async def record_mcp_call(server, tool, arguments):
        tool_arguments.append(arguments)
        return await native_mcp_call(server, tool, arguments)
    extension.mcp_call = record_mcp_call
    class Request(BaseRequest):
        read_timeout = 1
        async def initialize(self):
            pass
        async def shutdown(self):
            pass
        async def do_request(self, url, method, request_data=None, **kwargs):
            endpoint = url.rsplit("/", 1)[1]
            if endpoint == "getMe":
                result = {"id": 999, "is_bot": True, "first_name": "Synthetic", "username": "synthetic_bot"}
            elif endpoint == "getUpdates":
                result = [payload(SYNTHETIC_PASSWORD, 2)]
            elif endpoint == "sendMessage":
                sent.append(request_data.parameters)
                result = {"message_id": 1001 + len(sent), "date": 1791490000, "chat": {"id": 12345, "type": "private"}, "text": request_data.parameters["text"]}
            elif endpoint == "deleteMessage":
                deleted.append(request_data.parameters)
                result = delete_ack[0]
            elif endpoint == "setMyCommands":
                menus.append(request_data.parameters)
                result = True
            elif endpoint == "deleteMyCommands":
                result = True
            elif endpoint == "answerCallbackQuery":
                result = True
            else:
                raise AssertionError("Unexpected native transport endpoint")
            return 200, json.dumps({"ok": True, "result": result}).encode()
    def payload(text, identity):
        message = {"message_id": identity, "date": 1791490000,
                   "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345, "is_bot": False, "first_name": "Synthetic"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return {"update_id": identity, "message": message}

    async def run():
        app = Application.builder().bot(ExtBot("999:synthetic-fixture", request=Request(), get_updates_request=Request())).build()
        factory = GatewayRunner.__new__(GatewayRunner)
        factory.config = GatewayConfig()
        adapter = factory._create_adapter(Platform.TELEGRAM, PlatformConfig(extra={"allow_from": ["12345"]}))
        assert type(adapter) is not TelegramAdapter
        assert type(adapter).__module__ == "hermes_plugins.platforms__telegram.adapter"
        assert adapter._aqla_runtime_bound
        adapter.set_message_handler(lambda event: admitted.append(event))
        store = SessionStore(home / "sessions", GatewayConfig())
        gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, session_store=store)
        adapter._register_handlers(app)
        assert min(app.handlers) == -100
        assert len(app.handlers[-100]) == 1
        await app.initialize()
        adapter._bot = app.bot
        try:
            from hermes_cli.plugins import get_plugin_commands

            from aqla_agent.native_commands import COMMANDS
            registered = get_plugin_commands()
            assert {name.replace("_", "-") for name in COMMANDS} <= set(registered)
            assert "nichts geändert" in registered["setup"]["handler"]("synthetic-private-registry-fallback")
            # Menu setup and early ingress can run before finish_wiring.
            await adapter._register_command_menu()
            owner_menu = next(row for row in menus if row.get("scope", {}).get("type") == "chat")
            owner_commands = owner_menu["commands"]
            assert set(COMMANDS) <= {row["command"] for row in owner_commands}
            assert len(owner_commands) <= 60
            assert all(row["description"].startswith("aqla:" if row["command"] in COMMANDS else "Hermes:") for row in owner_commands)
            assert {row.get("scope", {}).get("type") for row in menus} >= {"default", "all_private_chats", "all_group_chats", "chat"}
            await app.process_update(Update.de_json(payload("/setup", 10), app.bot))
            assert extension.setup.challenge is None and "noch nicht bereit" in sent[-1]["text"] and admitted == []
            not_ready_late = payload(SYNTHETIC_PASSWORD, 11)
            not_ready_late["message"]["reply_to_message"] = {"message_id": 777, "date": 1791490000,
                "chat": {"id": 12345, "type": "private"}, "from": {"id": 999, "is_bot": True, "first_name": "Synthetic"}, "text": SETUP_PROMPT}
            await app.process_update(Update.de_json(not_ready_late, app.bot))
            assert len(deleted) == 1 and admitted == []
            deleted.clear()
            extension.boot(gateway)
            adapter.gateway_runner = gateway
            # The shared native renderer keeps numeric pages, skills and native
            # ACL gates. Inject registry-only fixtures to prove dynamic reading
            # and that system rows never appear in its Hermes section.
            from gateway.session import SessionSource
            from hermes_cli.commands import COMMAND_REGISTRY, CommandDef
            fixture_native = CommandDef("native_probe", "Native registry fixture", "Info")
            fixture_system = CommandDef("aqla_status", "Duplicate system fixture", "Plugins")
            COMMAND_REGISTRY.extend([fixture_native, fixture_system])
            old_gateway, extension.gateway = extension.gateway, factory
            try:
                source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345")
                help_event = SimpleNamespace(source=source, get_command_args=lambda: "")
                help_text = await factory._handle_help_command(help_event)
                hermes_text, system_text = help_text.split("**aqla Agent-System**")
                assert "**Hermes Agent**" in hermes_text and "/native_probe" in hermes_text
                assert "/aqla_status" not in hermes_text and "/aqla_status" in system_text
                for page in ("1", "2", "999", "invalid"):
                    event = SimpleNamespace(source=source, get_command_args=lambda page=page: page)
                    wrapped = await factory._handle_commands_command(event)
                    assert "**Hermes Agent**" in wrapped and "**aqla Agent-System**" in wrapped
                    assert "/aqla_status" not in wrapped.split("**aqla Agent-System**")[0]
                factory.config.platforms[Platform.TELEGRAM] = PlatformConfig(extra={"allow_admin_from": ["999"], "user_allowed_commands": ["help"]})
                gated = await factory._handle_help_command(help_event)
                assert "/aqla_status" not in gated and "/native_probe" not in gated
            finally:
                extension.gateway = old_gateway
                factory.config.platforms.clear()
                COMMAND_REGISTRY.remove(fixture_native)
                COMMAND_REGISTRY.remove(fixture_system)
            await app.process_update(Update.de_json(payload("/setup@other_bot", 12), app.bot))
            assert extension.setup.challenge is None and admitted == []
            # Authentic private candidates must also fail closed on either a
            # wrong password or a missing positive native Telegram DeleteACK.
            # Both pass through the real registry adapter and PTB dispatcher.
            for identity, candidate, ack in ((30, WRONG_SYNTHETIC_PASSWORD, True), (32, SYNTHETIC_PASSWORD, False)):
                await app.process_update(Update.de_json(payload("/setup", identity), app.bot))
                assert extension.setup.challenge is not None
                delete_ack[0] = ack
                await app.process_update(Update.de_json(payload(candidate, identity + 1), app.bot))
                assert extension.setup.claims() is None and admitted == []
                assert deleted[-1]["message_id"] == identity + 1
                assert "Setup nicht freigegeben" in sent[-1]["text"]
                ordinary = extension.authority.sign("operator", "synthetic-rejected-setup-check")
                try:
                    await extension.mcp_call("aqla", "control", {"action": "resume", "context_auth": ordinary})
                except AqlaError:
                    pass
                else:
                    raise AssertionError("Rejected native candidate acquired MCP Setup authority")
                entry = store.get_or_create_session(SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345"))
                for marker in (SYNTHETIC_PASSWORD, WRONG_SYNTHETIC_PASSWORD):
                    assert marker not in captured.getvalue()
                    assert marker not in json.dumps(tool_arguments)
                    assert marker not in json.dumps(store.load_transcript(entry.session_id))
                    assert marker not in json.dumps(read_snapshot(home / "data/state.sqlite3"))
            delete_ack[0] = True
            deleted.clear()
            await app.process_update(Update.de_json(payload("/setup", 1), app.bot))
            updates = await app.bot.get_updates()
            await app.process_update(updates[0])
            assert extension.setup.claims() and len(deleted) == 1 and admitted == [], {
                "configured": extension.setup.verifier is not None, "deleted": len(deleted), "admitted": len(admitted),
                "challenge": extension.setup.challenge is not None,
                "errors": [line for line in captured.getvalue().splitlines() if "Private Setup admission unavailable" in line]}
            with extension.claim_scope(("operator", "native-owner")):
                auth = extension.before_tool("mcp__aqla__control", {"action": "resume"})["args"]
            assert (await extension.mcp_call("aqla", "control", {"action": "resume", **auth}))["paused"] is False
            await app.process_update(Update.de_json(payload("/aqla_parameter backup leverage 5", 20), app.bot))
            state = read_snapshot(home / "data/state.sqlite3")
            assert state["profiles"]["backup"]["parameters"]["leverage"] == 5
            await app.process_update(Update.de_json(payload("/aqla_maker backup tp", 21), app.bot))
            keyboard = sent[-1]["reply_markup"]["inline_keyboard"]
            token = next(item["callback_data"] for row in keyboard for item in row if item["text"] == "maker_only")
            callback = {"update_id": 22, "callback_query": {"id": "synthetic-callback", "chat_instance": "synthetic-chat",
                "from": {"id": 12345, "is_bot": False, "first_name": "Synthetic"}, "data": token,
                "message": {"message_id": 1001 + len(sent), "date": 1791490000, "chat": {"id": 12345, "type": "private"},
                            "from": {"id": 999, "is_bot": True, "first_name": "Synthetic bot"}, "text": "Untrusted callback label"}}}
            await app.process_update(Update.de_json(callback, app.bot))
            state = read_snapshot(home / "data/state.sqlite3")
            assert state["profiles"]["backup"]["parameters"]["take_profit_execution"] == "maker_only"
            assert state["profiles"]["backup"]["parameters"].get("entry_execution", "standard") == "standard"
            revision = state["configuration_revision"]
            callback["update_id"] = 23
            await app.process_update(Update.de_json(callback, app.bot))
            assert read_snapshot(home / "data/state.sqlite3")["configuration_revision"] == revision
            await app.process_update(Update.de_json(payload("/aqla_pause", 24), app.bot))
            assert read_snapshot(home / "data/state.sqlite3")["paused"] is True
            assert admitted == []
            await app.process_update(Update.de_json(payload("/aqla_status", 25), app.bot))
            assert any("Setup:" in row["text"] for row in sent) and admitted == []
            from aqla_agent.messages import literal
            long_text = "**Status**\n" + "\n".join(literal("😀[untrusted]_*`\\" * 50) for _ in range(20))
            sent_before = len(sent)
            await extension.commands.reply(app.bot, long_text, markdown=True)
            chunks = sent[sent_before:]
            assert len(chunks) > 1 and all(len(row["text"].encode("utf-16-le")) // 2 <= 4096 for row in chunks)
            await app.process_update(Update.de_json(payload("/setup aus", 3), app.bot))
            try:
                await extension.mcp_call("aqla", "control", {"action": "resume", **auth})
            except AqlaError:
                pass
            else:
                raise AssertionError("Revoked native grant was accepted")
            await app.process_update(Update.de_json(payload("/setup", 4), app.bot))
            old_prompt = extension.setup.challenge.prompt_id
            forwarded = payload(SYNTHETIC_PASSWORD, 5)
            forwarded["message"]["forward_origin"] = {"type": "user", "date": 1791490000,
                "sender_user": {"id": 6789, "is_bot": False, "first_name": "Synthetic source"}}
            await app.process_update(Update.de_json(forwarded, app.bot))
            assert extension.setup.claims() is None and len(deleted) == 2 and admitted == []
            edited = payload(SYNTHETIC_PASSWORD, 6)
            edited["edited_message"] = edited.pop("message")
            edited["edited_message"]["edit_date"] = 1791490001
            await app.process_update(Update.de_json(edited, app.bot))
            assert extension.setup.claims() is None and len(deleted) == 3 and admitted == []
            # Recreate ingress state as after process restart. The reply metadata
            # is authenticated by the actual PTB Update parser and current bot.
            extension.revoke_setup()
            extension.setup = NativeSetup(extension.owner, extension.setup.verifier,
                                          extension.setup_request, extension.setup_binding)
            assert extension.setup.challenge is None and extension.setup.old_prompts == set()
            late = payload(SYNTHETIC_PASSWORD, 7)
            late["message"]["reply_to_message"] = {"message_id": old_prompt, "date": 1791490000,
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": app.bot.id, "is_bot": True, "first_name": "Synthetic"}, "text": SETUP_PROMPT}
            await app.process_update(Update.de_json(late, app.bot))
            assert extension.setup.claims() is None and len(deleted) == 4 and admitted == []
            # Factory, handler rebuild, plugin replacement and runtime undo all
            # retain one earliest handler and remove the previous authority.
            replacement = factory._create_adapter(Platform.TELEGRAM, PlatformConfig(extra={"allow_from": ["12345"]}))
            replacement.set_message_handler(lambda event: admitted.append(event))
            extension.boot(gateway, adapter=replacement)
            assert extension.binding[0] is replacement and extension.setup.claims() is None
            rebuild = Application.builder().bot(app.bot).build()
            replacement._register_handlers(rebuild)
            replacement._register_handlers(rebuild)
            assert len(rebuild.handlers[-100]) == 1
            assert install_strict_profile() is undo
            discover_plugins(force=True)
            assert extension not in EXTENSIONS and extension.binding is None
            fresh = next(iter(EXTENSIONS))
            fresh.boot(gateway, adapter=replacement)
            assert fresh.binding[0] is replacement and fresh.setup.claims() is None
            assert "module=hermes_plugins.platforms__telegram.adapter hooks=true setup_handler=True system_handler=True catalog_count=11" in captured.getvalue()
            assert all(marker not in captured.getvalue() for marker in (SYNTHETIC_PASSWORD, WRONG_SYNTHETIC_PASSWORD))
            assert extension.authority.key.decode() not in captured.getvalue()
            assert all(marker not in json.dumps(read_snapshot(home / "data/state.sqlite3")) for marker in (SYNTHETIC_PASSWORD, WRONG_SYNTHETIC_PASSWORD))
            for path in home.rglob("*"):
                if path.is_file() and path.suffix in (".json", ".jsonl", ".yaml", ".log"):
                    assert all(marker not in path.read_text(encoding="utf-8") for marker in (SYNTHETIC_PASSWORD, WRONG_SYNTHETIC_PASSWORD))
        finally:
            tasks = [task for task in asyncio.all_tasks() if task.get_name().startswith("aqla-")]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.shutdown()
        print(json.dumps({"native_ptb_setup": True, "debug_secret_free": True, "deletion_before_grant": True,
                          "mcp_revocation": True, "slash_password": True, "forwarded_candidate_deleted": True,
                          "edited_candidate_deleted": True, "restart_late_reply_deleted": True,
                          "actual_factory": True, "native_registry": True, "owner_menu_complete": True,
                          "native_direct_guided_controls": True, "callback_once": True, "factory_reload_rebuild": True,
                          "early_not_ready_failclosed": True, "native_catalog_pages_acl": True, "native_long_status_chunks": True,
                          "authentic_wrong_password_rejected": True, "authentic_delete_ack_false_rejected": True,
                          "rejected_passwords_absent_from_tools_history_logs": True,
                          "candidate_deletions": len(deleted), "model_admissions": len(admitted)}))
    try:
        asyncio.run(run())
    finally:
        EXTENSIONS.discard(extension)
        shutdown_mcp_servers()
        undo()
        logging.getLogger().removeHandler(handler)


if __name__ == "__main__":
    main()
