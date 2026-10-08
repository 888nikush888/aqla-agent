"""Launch native Hermes for the isolated VPS. No custom bot or agent loop."""
import os
import sys
import threading
from pathlib import Path


def install_strict_profile():
    """Keep Aqla authority boundaries while using native SQLite session storage."""
    originals = []
    from .hermes_contract import check_native_contract
    check_native_contract()

    def replace(target, name, value):
        previous = getattr(target, name)
        originals.append((target, name, previous))
        if callable(value):
            value.__aqla_contract_original__ = previous
            value.__wrapped__ = previous
        setattr(target, name, value)

    import gateway.run as native_gateway

    # This factory runs outside GatewayStartupMixin, after adapters connect.
    # Cron/housekeeping products are outside the three requested operator tools.
    # Native async session/transport watchdogs remain enabled below.
    replace(native_gateway, "_start_gateway_start_cron_and_housekeeping",
            lambda runner: (threading.Event(), None, None, None))
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.run_startup import GatewayStartupMixin
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_runner import TurnRunner
    from plugins.platforms.telegram.adapter import TelegramAdapter

    from .events import (
        gateway_ready,
        ingress_identity,
        rebind_native_adapter,
        record_clarify_outcome,
        run_native_followup,
        run_native_turn,
        stamp_native_event,
    )
    from .skill_review import install_skill_review

    native_turn = GatewayTurnMixin._run_agent
    native_followup = GatewayTurnMixin._run_agent_queued_followup
    reconnect = GatewayAdapterLifecycleMixin._install_reconnected_adapter

    async def run_agent(self, message, context_prompt, history, source, session_id, **kwargs):
        return await run_native_turn(self, source, lambda: native_turn(self, message, context_prompt, history, source, session_id, **kwargs))

    async def queued_followup(self, turn_ctx, adapter, pending, pending_event, response, result, stream_task):
        return await run_native_followup(self, pending_event,
                                         lambda: native_followup(self, turn_ctx, adapter, pending, pending_event, response, result, stream_task))

    async def install_reconnected_adapter(self, platform, adapter):
        rebind_native_adapter(self, platform, adapter)
        return await reconnect(self, platform, adapter)

    replace(GatewayTurnMixin, "_run_agent", run_agent)
    replace(GatewayTurnMixin, "_run_agent_queued_followup", queued_followup)
    replace(GatewayAdapterLifecycleMixin, "_install_reconnected_adapter", install_reconnected_adapter)
    original_clarify = TurnRunner._clarify_callback_sync

    def clarify_callback(self, questions):
        try:
            result = original_clarify(self, questions)
        except BaseException:
            record_clarify_outcome({"outcome": "undelivered", "answers": {}})
            raise
        record_clarify_outcome(result)
        return result

    replace(TurnRunner, "_clarify_callback_sync", clarify_callback)

    # Classify native Telegram events before any text, photo or busy-queue merge.
    # The transport and normal owner command handlers remain entirely native.
    build_event = TelegramAdapter._build_message_event
    text_key = TelegramAdapter._text_batch_key
    photo_key = TelegramAdapter._photo_batch_key
    media_group = TelegramAdapter._queue_media_group_event
    can_merge = BasePlatformAdapter._can_merge_text_debounce_events

    def build_message_event(self, *args, **kwargs):
        return stamp_native_event(build_event(self, *args, **kwargs))

    def text_batch_key(self, event):
        key = text_key(self, event)
        kind, identity = ingress_identity(event)
        return f"{key}:aqla-source:{identity}" if kind == "source" else key

    def photo_batch_key(self, event, message):
        key = photo_key(self, event, message)
        kind, identity = ingress_identity(event)
        return f"{key}:aqla-source:{identity}" if kind == "source" else key

    async def queue_media_group(self, group_id, event):
        kind, identity = ingress_identity(event)
        if kind == "source":
            # Keep native hold, acknowledgement and flush behavior for each item.
            group_id = f"{group_id}:aqla-source:{identity}"
        await media_group(self, group_id, event)

    def can_merge_text(self, existing, event):
        return ingress_identity(existing) == ingress_identity(event) and can_merge(self, existing, event)

    replace(TelegramAdapter, "_build_message_event", build_message_event)
    replace(TelegramAdapter, "_text_batch_key", text_batch_key)
    replace(TelegramAdapter, "_photo_batch_key", photo_batch_key)
    replace(TelegramAdapter, "_queue_media_group_event", queue_media_group)
    replace(BasePlatformAdapter, "_can_merge_text_debounce_events", can_merge_text)
    replace(GatewayRunner, "_SECURITY_METADATA_KEYS", (*GatewayRunner._SECURITY_METADATA_KEYS, "aqla_origin", "aqla_source_message_id"))

    original_wiring = GatewayStartupMixin._start_finish_wiring

    async def finish_wiring(self, connected_count):
        await original_wiring(self, connected_count)
        gateway_ready(self)

    async def trading_services_only(self, connected_count):
        # Preserve the native event-loop watchdog; unused room/kanban/heartbeat
        # products must not create their own storage or strategy work.
        self._start_loop_heartbeat_task()
        await self.hooks.emit("gateway:startup", {"platforms": [platform.value for platform in self.adapters]})

    replace(GatewayStartupMixin, "_start_finish_wiring", finish_wiring)
    replace(GatewayStartupMixin, "_start_post_connect_services", trading_services_only)
    replace(GatewayStartupMixin, "_PRE_RECONNECT_WATCHERS", ("_session_housekeeping_watcher", "_session_stall_watcher"))
    replace(GatewayStartupMixin, "_POST_RECONNECT_WATCHERS", ("_loop_wakeup_watcher",))
    from hermes_cli.plugins import PluginContext

    # The isolated profile grants only Aqla's own two MCP servers to its plugin.
    # Journal snapshots are internal data, not model context. Keep the host's
    # native 2 MB flood limit; bounded journal batches stay below it after wrapping.
    replace(PluginContext, "_MCP_RESULT_CHAR_CAP", 2_000_000)
    install_skill_review(replace)

    def undo():
        for target, name, original in reversed(originals):
            setattr(target, name, original)

    return undo


def main():
    root = Path(os.environ["HERMES_HOME"]).resolve().parent
    source = root / "hermes-source"
    if not source.is_dir():
        raise SystemExit("Pinned Hermes source is missing; run aqla-install-hermes on the VPS")
    sys.path.insert(0, str(source))
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    from .session_history import migrate_session_history
    migrate_session_history(Path(os.environ["HERMES_HOME"]))
    install_strict_profile()
    sys.argv = ["hermes", "gateway", "run"]
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == "__main__":
    main()
