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
    import gateway.run as native_gateway
    from gateway.run_turn import GatewayTurnMixin
    from tools import write_approval as approval_before

    from aqla_agent.events import EXTENSIONS, HermesExtension
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.skill_review import REVIEW_FOCUS
    before_factory = native_gateway._start_gateway_start_cron_and_housekeeping
    before_stage = approval_before.stage_write
    before_turn = GatewayTurnMixin._run_agent
    async def incompatible_turn(self, wrong_argument):
        pass
    GatewayTurnMixin._run_agent = incompatible_turn
    try:
        try:
            install_strict_profile()
        except RuntimeError as error:
            assert "compatibility contract" in str(error)
        else:
            raise AssertionError("Changed native signature was accepted")
        assert native_gateway._start_gateway_start_cron_and_housekeeping is before_factory
        assert approval_before.stage_write is before_stage
        assert GatewayTurnMixin._run_agent is incompatible_turn
    finally:
        GatewayTurnMixin._run_agent = before_turn
    del GatewayTurnMixin._run_agent
    try:
        try:
            install_strict_profile()
        except RuntimeError as error:
            assert "compatibility contract" in str(error)
        else:
            raise AssertionError("Missing native method was accepted")
        assert native_gateway._start_gateway_start_cron_and_housekeeping is before_factory
        assert approval_before.stage_write is before_stage
        assert "_run_agent" not in GatewayTurnMixin.__dict__
    finally:
        GatewayTurnMixin._run_agent = before_turn
    from agent import background_review as review_before
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.run_startup import GatewayStartupMixin
    from gateway.run_turn_runner import TurnRunner
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_cli import commands as commands_before
    from hermes_cli.plugins import PluginContext
    from tools import skill_manager_tool as manager_before

    native_targets = (
        (native_gateway, ("_start_gateway_start_cron_and_housekeeping",)),
        (GatewayTurnMixin, ("_run_agent", "_run_agent_queued_followup")),
        (GatewayAdapterLifecycleMixin, ("_create_adapter", "_install_reconnected_adapter")),
        (TurnRunner, ("_clarify_callback_sync",)),
        (BasePlatformAdapter, ("_can_merge_text_debounce_events",)),
        (GatewayRunner, ("_SECURITY_METADATA_KEYS",)),
        (GatewayStartupMixin, ("_start_finish_wiring", "_start_post_connect_services", "_PRE_RECONNECT_WATCHERS", "_POST_RECONNECT_WATCHERS")),
        (PluginContext, ("_MCP_RESULT_CHAR_CAP",)),
        (approval_before, ("write_approval_enabled", "stage_write")),
        (manager_before, ("apply_skill_pending",)),
        (review_before, ("spawn_background_review_thread", "_action_lines", "_publish_review_summary")),
        (GatewaySlashCommandsMixin, ("_handle_help_command", "_handle_commands_command")),
        (commands_before, ("gateway_help_lines",)),
    )
    native_callbacks = [(target, name, getattr(target, name)) for target, names in native_targets for name in names]
    undo = install_strict_profile()
    undo_twice = install_strict_profile()
    assert undo_twice is undo
    undo_twice()
    assert all(getattr(target, name) is callback for target, name, callback in native_callbacks)
    undo = install_strict_profile()
    assert undo is not undo_twice
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
    fork_starts = []
    original_fork = review.build_cache_parity_fork
    def counted_fork(*args, **kwargs):
        fork_starts.append(True)
        raise RuntimeError("synthetic stop at actual native fork boundary; no model request")
    review.build_cache_parity_fork = counted_fork
    reviewer = SimpleNamespace(client=SimpleNamespace(SUPPORTS_HERMES_TOOL_CALLS=True), provider="custom", model="synthetic",
                               _safe_print=lambda _: None, _emit_auxiliary_failure=lambda *_: None, background_review_callback=None)
    def automatic_review(*, explicit=False):
        target, _ = review.spawn_background_review_thread(reviewer, [], review_skills=True, task_cfg={}, explicit=explicit)
        target()
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
            automatic_review()
            assert len(fork_starts) == 0
            automatic_review(explicit=True)
            assert len(fork_starts) == 1
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
        automatic_review()
        assert len(fork_starts) == 2
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
        automatic_review()
        assert len(fork_starts) == 3
        assert hashlib.sha256(master.read_bytes()).digest() != hashlib.sha256(original).digest()
        # Native Hermes may open its session DB; no trading state is created by
        # drafting, approving or rejecting a skill.
        assert not (destination / "data" / "state.sqlite3").exists()
        print(json.dumps({"background_proposal": True, "native_telegram_approval": True, "rejection": True,
                          "stale_proposal_blocked": True, "source_approval_blocked": True, "always_ask": True,
                          "native_approval_question": True, "pending_blocks_native_fork": True, "explicit_review_available": True,
                          "review_resumes_after_reject_and_approve": True, "compatibility_failure_has_no_partial_patch": True,
                          "double_install_undo": True, "counted_native_fork_starts": len(fork_starts), "exchange_actions": 0, "model_calls": 0}))
    finally:
        EXTENSIONS.discard(extension)
        review.build_cache_parity_fork = original_fork
        undo()


if __name__ == "__main__":
    main()
