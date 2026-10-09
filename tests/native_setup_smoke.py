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

SYNTHETIC_PASSWORD = "native-fixture-marker-never-a-user-credential"


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
    from gateway.config import GatewayConfig, PlatformConfig
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
    admitted, sent, deleted = [], [], []
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
                result = {"message_id": 1001 + len(sent), "date": 1791490000, "chat": {"id": 12345, "type": "private"}, "text": "safe setup reply"}
            elif endpoint == "deleteMessage":
                deleted.append(request_data.parameters)
                result = True
            else:
                raise AssertionError("Unexpected native transport endpoint")
            return 200, json.dumps({"ok": True, "result": result}).encode()
    def payload(text, identity):
        return {"update_id": identity, "message": {"message_id": identity, "date": 1791490000,
                "chat": {"id": 12345, "type": "private"}, "from": {"id": 12345, "is_bot": False, "first_name": "Synthetic"}, "text": text}}

    async def run():
        app = Application.builder().bot(ExtBot("999:synthetic-fixture", request=Request(), get_updates_request=Request())).build()
        adapter = TelegramAdapter(PlatformConfig(extra={"allow_from": ["12345"]}))
        adapter.set_message_handler(lambda event: admitted.append(event))
        store = SessionStore(home / "sessions", GatewayConfig())
        gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, session_store=store)
        extension.boot(gateway)
        adapter._register_handlers(app)
        assert min(app.handlers) == -100
        await app.initialize()
        try:
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
            await app.process_update(Update.de_json(payload("/setup aus", 3), app.bot))
            try:
                await extension.mcp_call("aqla", "control", {"action": "resume", **auth})
            except AqlaError:
                pass
            else:
                raise AssertionError("Revoked native grant was accepted")
            assert SYNTHETIC_PASSWORD not in captured.getvalue()
            assert extension.authority.key.decode() not in captured.getvalue()
            assert SYNTHETIC_PASSWORD not in json.dumps(read_snapshot(home / "data/state.sqlite3"))
            for path in home.rglob("*"):
                if path.is_file() and path.suffix in (".json", ".jsonl", ".yaml", ".log"):
                    assert SYNTHETIC_PASSWORD not in path.read_text(encoding="utf-8")
        finally:
            tasks = [task for task in asyncio.all_tasks() if task.get_name().startswith("aqla-")]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.shutdown()
        print(json.dumps({"native_ptb_setup": True, "debug_secret_free": True, "deletion_before_grant": True,
                          "mcp_revocation": True, "model_admissions": len(admitted)}))
    try:
        asyncio.run(run())
    finally:
        EXTENSIONS.discard(extension)
        shutdown_mcp_servers()
        undo()
        logging.getLogger().removeHandler(handler)


if __name__ == "__main__":
    main()
