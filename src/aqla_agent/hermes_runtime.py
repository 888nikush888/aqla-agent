"""Launch native Hermes for the isolated VPS. No custom bot or agent loop."""
import os
import sys
import threading
from pathlib import Path
from types import MethodType

_ACTIVE_UNDO = None


def install_strict_profile():
    """Keep Aqla authority boundaries while using native SQLite session storage."""
    # Process-wide install/undo identity prevents duplicate native patches.
    # skipcq: PYL-W0603
    global _ACTIVE_UNDO
    if _ACTIVE_UNDO is not None:
        return _ACTIVE_UNDO
    originals = []
    adapters = []
    applications = []
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
    import logging

    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.run_startup import GatewayStartupMixin
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_runner import TurnRunner

    from .events import (
        gateway_extension,
        gateway_ready,
        ingress_identity,
        intercept_native_setup,
        rebind_native_adapter,
        record_clarify_outcome,
        run_native_followup,
        run_native_turn,
        stamp_native_event,
    )
    from .native_setup import PrivateTransportLogs
    from .skill_review import install_skill_review
    # PTB emits getUpdates bodies and Update.__repr__ before handler dispatch.
    # These narrow transport filters run before formatting, including DEBUG.
    filters = []
    for name in ("telegram.Bot", "telegram.ext.ExtBot", "telegram.ext.Application", "telegram.ext.Updater"):
        logger = logging.getLogger(name)
        private_filter = PrivateTransportLogs()
        logger.addFilter(private_filter)
        filters.append((logger, private_filter))
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    native_turn = GatewayTurnMixin._run_agent
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    native_followup = GatewayTurnMixin._run_agent_queued_followup
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    reconnect = GatewayAdapterLifecycleMixin._install_reconnected_adapter

    async def run_agent(self, message, context_prompt, history, source, session_id, **kwargs):
        from .tool_outputs import capture_scope
        from .tool_runtime import GUIDANCE, owned_profile
        extension = gateway_extension(self)
        incoming = extension.turn_event.get() if extension else None
        if incoming and (getattr(incoming, "metadata", None) or {}).get("hermes_plugin_injection") is True:
            from .events import WAKE
            context_prompt = (context_prompt or "") + "\n\n" + WAKE
            kwargs["scheduled_heartbeat"] = True
        if owned_profile():
            context_prompt = (context_prompt or "") + "\n\n" + GUIDANCE
            with capture_scope(session_id, "context-history"):
                return await run_native_turn(self, source, lambda: native_turn(self, message, context_prompt, history, source, session_id, **kwargs))
        return await run_native_turn(self, source, lambda: native_turn(self, message, context_prompt, history, source, session_id, **kwargs))

    async def queued_followup(self, turn_ctx, adapter, pending, pending_event, response, result, stream_task):
        return await run_native_followup(self, pending_event,
                                         lambda: native_followup(self, turn_ctx, adapter, pending, pending_event, response, result, stream_task))

    async def install_reconnected_adapter(self, platform, adapter):
        rebind_native_adapter(self, platform, adapter)
        return await reconnect(self, platform, adapter)

    replace(GatewayTurnMixin, "_run_agent", run_agent)
    import toolsets

    from .efficiency import SOURCE_TOOLS
    replace(toolsets, "TOOLSETS", {**toolsets.TOOLSETS, "aqla-source": {
        "description": "Aqla source decisions and evidence, without operator/configuration tools",
        "tools": list(SOURCE_TOOLS), "includes": []}})
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    resolve_toolsets = GatewayTurnMixin._resolve_turn_toolsets

    def turn_toolsets(self, user_config, source, platform_key):
        enabled, disabled = resolve_toolsets(self, user_config, source, platform_key)
        extension = gateway_extension(self)
        origin = extension.active_context if extension else None
        if origin and origin[0] == "source" and extension.efficiency.get("source_toolsets", True):
            enabled = ["aqla-source"] if "mcp-aqla" in enabled else []
        return enabled, disabled

    replace(GatewayTurnMixin, "_resolve_turn_toolsets", turn_toolsets)
    replace(GatewayTurnMixin, "_run_agent_queued_followup", queued_followup)
    replace(GatewayAdapterLifecycleMixin, "_install_reconnected_adapter", install_reconnected_adapter)
    from .usage_metrics import install as install_usage_metrics
    install_usage_metrics(replace)
    from .provider_cooldown import install as install_provider_cooldown
    install_provider_cooldown(replace)
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
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

    # Every lifecycle path uses this factory before connect, including registry plugins.
    # Bind to the product itself so import aliases cannot leave ingress unprotected.
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    create_adapter = GatewayAdapterLifecycleMixin._create_adapter

    def bind_telegram(adapter):
        from .hermes_contract import check_telegram_contract
        check_telegram_contract(adapter)
        if getattr(adapter, "_aqla_runtime_bound", False):
            return
        saved = []

        def instance_replace(name, function):
            previous = getattr(adapter, name)
            function.__aqla_contract_original__ = getattr(previous, "__func__", previous)
            saved.append((name, name in adapter.__dict__, adapter.__dict__.get(name)))
            setattr(adapter, name, MethodType(function, adapter))

        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        register_handlers = adapter._register_handlers
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        build_event = adapter._build_message_event
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        text_key = adapter._text_batch_key
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        photo_key = adapter._photo_batch_key
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        media_group = adapter._queue_media_group_event

        def setup_handlers(self, app):
            from telegram import Update
            from telegram.ext import ApplicationHandlerStop, TypeHandler

            async def private_setup(update, context):
                try:
                    handled = await intercept_native_setup(self, update, context)
                except Exception as error:
                    logging.getLogger(__name__).warning("Private native admission unavailable (%s)", type(error).__name__)
                    handled = True
                if handled:
                    # Pinned native integration interface; covered by native contract and integration checks.
                    # skipcq: PYL-W0212
                    self._accept_update()
                    raise ApplicationHandlerStop

            # Handler ownership is explicit; re-registration cannot duplicate admission.
            table = getattr(app, "handlers", {})
            if not any(getattr(getattr(handler, "callback", None), "_aqla_admission", False) for handler in table.get(-100, [])):
                handler = TypeHandler(Update, private_setup)
                # Pinned native integration interface; covered by native contract and integration checks.
                # skipcq: PYL-W0212
                private_setup._aqla_admission = True
                app.add_handler(handler, group=-100)
                applications.append((app, handler))
            register_handlers(app)
            from .native_commands import COMMANDS
            present = any(getattr(getattr(handler, "callback", None), "_aqla_admission", False) for handler in app.handlers.get(-100, []))
            if not present or not getattr(self, "_aqla_runtime_bound", False):
                raise RuntimeError("Private native Telegram admission is unavailable")
            logging.getLogger(__name__).info(
                "aqla native Telegram ready module=%s hooks=true setup_handler=%s system_handler=%s catalog_count=%d",
                type(self).__module__, present, present, len(COMMANDS))

        def build_message_event(_self, *args, **kwargs):
            return stamp_native_event(build_event(*args, **kwargs))

        def text_batch_key(_self, event):
            key = text_key(event)
            kind, identity = ingress_identity(event)
            return f"{key}:aqla-source:{identity}" if kind == "source" else key

        def photo_batch_key(_self, event, message):
            key = photo_key(event, message)
            kind, identity = ingress_identity(event)
            return f"{key}:aqla-source:{identity}" if kind == "source" else key

        async def queue_media_group(_self, group_id, event):
            kind, identity = ingress_identity(event)
            if kind == "source":
                group_id = f"{group_id}:aqla-source:{identity}"
            await media_group(group_id, event)

        from .events import EXTENSIONS
        from .native_commands import install_menu
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        adapter._aqla_owners = tuple(extension.owner for extension in EXTENSIONS)
        instance_replace("_register_handlers", setup_handlers)
        instance_replace("_build_message_event", build_message_event)
        instance_replace("_text_batch_key", text_batch_key)
        instance_replace("_photo_batch_key", photo_batch_key)
        instance_replace("_queue_media_group_event", queue_media_group)
        install_menu(adapter, instance_replace)
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        adapter._aqla_runtime_bound = True
        adapters.append((adapter, saved))

    def actual_adapter(self, platform, config):
        adapter = create_adapter(self, platform, config)
        if adapter is not None and getattr(platform, "value", platform) == "telegram":
            bind_telegram(adapter)
        return adapter

    replace(GatewayAdapterLifecycleMixin, "_create_adapter", actual_adapter)
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    can_merge = BasePlatformAdapter._can_merge_text_debounce_events

    def can_merge_text(self, existing, event):
        return ingress_identity(existing) == ingress_identity(event) and can_merge(self, existing, event)

    replace(BasePlatformAdapter, "_can_merge_text_debounce_events", can_merge_text)
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    replace(GatewayRunner, "_SECURITY_METADATA_KEYS", (*GatewayRunner._SECURITY_METADATA_KEYS, "aqla_origin", "aqla_source_message_id"))

    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    original_wiring = GatewayStartupMixin._start_finish_wiring

    async def finish_wiring(self, connected_count):
        await original_wiring(self, connected_count)
        gateway_ready(self)

    async def trading_services_only(self, connected_count):
        # The pinned native ABI supplies this count; configured services govern startup.
        # skipcq: PTC-W0043
        del connected_count
        # Preserve the native event-loop watchdog; unused room/kanban/heartbeat
        # products must not create their own storage or strategy work.
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
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
    from .native_commands import install_catalog
    install_catalog(replace)
    from .tool_runtime import install as install_tool_runtime
    install_tool_runtime(replace)

    def undo():
        # Undo only the active process-wide installation.
        # skipcq: PYL-W0603
        global _ACTIVE_UNDO
        if _ACTIVE_UNDO is not undo:
            return
        _ACTIVE_UNDO = None
        for app, handler in applications:
            app.remove_handler(handler, group=-100)
        for adapter, saved in reversed(adapters):
            for name, existed, value in reversed(saved):
                if existed:
                    setattr(adapter, name, value)
                else:
                    adapter.__dict__.pop(name, None)
            adapter.__dict__.pop("_aqla_runtime_bound", None)
            adapter.__dict__.pop("_aqla_owners", None)
        for logger, private_filter in filters:
            logger.removeFilter(private_filter)
        for target, name, original in reversed(originals):
            setattr(target, name, original)

    _ACTIVE_UNDO = undo
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
