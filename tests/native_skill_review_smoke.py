"""Native staged skill review, Telegram approval commands and unchanged trading state."""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    destination = Path(args.destination).resolve()
    os.environ["HERMES_HOME"] = str(destination / "hermes-home")
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_SKIP_UPDATE_CHECK"] = "1"
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(Path(args.hermes_source).resolve()))
    from aqla_agent.install import prepare

    prepare(project, destination, "12345", "qa-model", "https://example.invalid/v1")
    from aqla_agent.events import EXTENSIONS, HermesExtension
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.skill_review import REVIEW_FOCUS

    undo = install_strict_profile()
    from agent import background_review as review
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from tools import skill_manager_tool as manager
    from tools import write_approval as approval
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    values = {"owner_id": "12345", "config_path": str(destination / "config.yaml")}
    extension = HermesExtension(SimpleNamespace(get_config=lambda key, default=None: values.get(key, default)))
    EXTENSIONS.add(extension)
    master = destination / "hermes-home" / "skills" / "aqla-master" / "SKILL.md"
    original = master.read_bytes()
    operation = {"action": "patch", "name": "aqla-master", "old_string": "Du bist aqla Agent.",
                 "new_string": "Du bist aqla Agent. Prüfe Replies besonders sorgfältig."}
    runner = GatewayRunner(GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id="12345")

    async def command(text, origin="operator"):
        event = SimpleNamespace(get_command_args=lambda: text, source=source)
        with extension.claim_scope((origin, "owner-confirmation")):
            return await GatewaySlashCommandsMixin._handle_skills_command(runner, event)

    def propose(op):
        assert extension.before_tool("skill_manage", {"operations": [op]}) is None
        return json.loads(manager.skill_manage(action="", name="", operations=[op]))

    try:
        # Config failure or turning the native toggle off never removes the owner's gate.
        config_path = destination / "hermes-home" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["skills"]["write_approval"] = False
        config_path.write_text(yaml.safe_dump(config))
        assert approval.write_approval_enabled(approval.SKILLS) is True
        token = set_current_write_origin("background_review")
        try:
            staged = propose(operation)
            assert staged["staged"] is True and master.read_bytes() == original
            assert propose(operation)["pending_id"] == staged["pending_id"]
            assert len(approval.list_pending(approval.SKILLS)) == 1
            with_change = {**operation, "new_string": "Du bist aqla Agent. Andere Verbesserung."}
            try:
                propose(with_change)
            except ValueError as error:
                assert "awaits approval" in str(error)
            else:
                raise AssertionError("A second pending proposal was admitted")
            assert extension.before_tool("skill_manage", {"operations": [{**operation, "name": "another"}]})["action"] == "block"
            _, prompt = review.spawn_background_review_thread(SimpleNamespace(), [], review_skills=True)
            assert REVIEW_FOCUS in prompt
            messages = [
                {"role": "assistant", "tool_calls": [{"id": "proposal", "function": {"name": "skill_manage", "arguments": json.dumps({"operations": [operation]})}}]},
                {"role": "tool", "tool_call_id": "proposal", "content": json.dumps(staged)},
            ]
            actions = review.summarize_background_review_actions(messages, [])
            delivered = []
            review._publish_review_summary(SimpleNamespace(_safe_print=lambda _: None, background_review_callback=delivered.append), actions)
            assert len(delivered) == 1 and "Soll ich" in delivered[0]
            assert f"/skills approve {staged['pending_id']}" in delivered[0]
            assert "Memory updated" not in delivered[0]
        finally:
            reset_current_write_origin(token)
        pending = approval.get_pending(approval.SKILLS, staged["pending_id"])
        assert json.loads(manager.apply_skill_pending(pending["payload"]))["success"] is False
        with extension.claim_scope(("operator", "approval")):
            for forbidden in (
                {"action": "edit", "name": "another", "content": "small"},
                {"action": "edit", "name": "aqla-master", "content": "x" * 8193},
                {"action": "delete", "name": "aqla-master"},
            ):
                payload = {**forbidden, "aqla_base_sha256": pending["payload"]["aqla_base_sha256"]}
                assert json.loads(manager.apply_skill_pending(payload))["success"] is False
        assert master.read_bytes() == original
        assert "Approved 0" in asyncio.run(command("approve " + staged["pending_id"], "source"))
        assert master.read_bytes() == original
        assert "Prüfe Replies" in asyncio.run(command("diff " + staged["pending_id"]))
        assert "Rejected" in asyncio.run(command("reject " + staged["pending_id"]))
        assert master.read_bytes() == original and not approval.list_pending(approval.SKILLS)
        # A later master edit invalidates the exact proposal the owner originally inspected.
        with extension.claim_scope(("operator", "request")):
            stale = propose(operation)
        master.write_bytes(original + b"\nExternal master edit.\n")
        result = asyncio.run(command("approve " + stale["pending_id"]))
        assert "Approved 0" in result and "changed since" in result
        asyncio.run(command("reject " + stale["pending_id"]))
        master.write_bytes(original)
        with extension.claim_scope(("operator", "request")):
            accepted = propose(operation)
        assert "Approved 1" in asyncio.run(command("approve " + accepted["pending_id"]))
        assert "Prüfe Replies besonders sorgfältig." in master.read_text(encoding="utf-8")
        assert not approval.list_pending(approval.SKILLS)
        assert hashlib.sha256(master.read_bytes()).digest() != hashlib.sha256(original).digest()
        # Native Hermes may open its session DB; no trading state is created by
        # drafting, approving or rejecting a skill.
        assert not (destination / "data" / "state.sqlite3").exists()
        print(json.dumps({"background_proposal": True, "native_telegram_approval": True, "rejection": True,
                          "stale_proposal_blocked": True, "source_approval_blocked": True, "always_ask": True,
                          "native_approval_question": True, "exchange_actions": 0, "model_calls": 0}))
    finally:
        EXTENSIONS.discard(extension)
        undo()


if __name__ == "__main__":
    main()
