"""Launch native Hermes for the isolated VPS. No custom bot or agent loop."""
import os
import sqlite3
import sys
import threading
from pathlib import Path


def install_strict_profile():
    """Use file-based session storage in the isolated native Hermes process.

    Applied only inside the new VPS process, before native gateway startup. The
    SQLite guard fails closed if a future host feature attempts another database.
    Native transport, polling, delivery and agent execution are not replaced.
    """
    originals = []

    def replace(target, name, value):
        originals.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def no_database(*_, **__):
        raise sqlite3.OperationalError("SQLite is disabled in the aqla VPS profile")

    replace(sqlite3, "connect", no_database)
    import gateway.run as native_gateway

    # This factory runs outside GatewayStartupMixin, after adapters connect.
    # Its cron and housekeeping products attempt their own SQLite stores.
    # Native async session/transport watchdogs remain enabled below.
    replace(native_gateway, "_start_gateway_start_cron_and_housekeeping",
            lambda runner: (threading.Event(), None, None, None))
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.run_startup import GatewayStartupMixin
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session import SessionStore
    from gateway.session_persistence import SessionPersistenceMixin
    from hermes_state_registry import __name__ as registry_name
    from plugins.platforms.telegram.adapter import TelegramAdapter

    from .conversation import Conversations
    from .events import (
        gateway_ready,
        ingress_identity,
        rebind_native_adapter,
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

    # This is the host's explicitly supported fallback, not a new storage engine.
    replace(SessionPersistenceMixin, "_open_session_db_for_active_scope", lambda *args, **kwargs: None)
    replace(sys.modules[registry_name], "acquire", lambda *args, **kwargs: None)
    replace(GatewayRunner, "_open_session_db_for_active_scope", lambda *args, **kwargs: None)
    original_init = SessionStore.__init__

    def init_store(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._db = None
        self._aqla_conversations = Conversations(self.sessions_dir / "transcripts")

    replace(SessionStore, "__init__", init_store)
    replace(SessionStore, "_append_transcript_message", lambda self, session_id, message: self._aqla_conversations.append(session_id, message))
    replace(SessionStore, "load_transcript", lambda self, session_id: self._aqla_conversations.load(session_id))
    replace(SessionStore, "rewrite_transcript", lambda self, session_id, messages, **kwargs: self._aqla_conversations.rewrite(session_id, messages))
    replace(SessionStore, "has_input_owner", lambda self, session_id, owner: self._aqla_conversations.has_owner(session_id, owner))
    replace(SessionStore, "has_platform_message_id", lambda self, session_id, message_id: self._aqla_conversations.has_message(session_id, message_id))
    replace(SessionStore, "transcript_tail_role", lambda self, session_id: (self._aqla_conversations.load(session_id) or [{}])[-1].get("role"))

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
    install_strict_profile()
    sys.argv = ["hermes", "gateway", "run"]
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == "__main__":
    main()
