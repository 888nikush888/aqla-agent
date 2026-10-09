"""Lifecycle regressions invoked by native_pipeline_smoke --mode lifecycle.

Real pinned turn/followup, executor, cancellation and reconnect methods drive a
cached native AIAgent and its real tool hooks/finalizer. Transport is offline.
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import MethodType, SimpleNamespace


async def verify_lifecycle(extension, agent, store, source, original):
    from datetime import UTC, datetime

    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.run_turn import GatewayTurnMixin

    async def noop(*args, **kwargs):
        pass

    main_thread = threading.get_ident()
    claims, finalizations, ingests = [], [], []
    native_before_tool = extension.before_tool
    # Update the callback already registered in the actual plugin hook registry.
    # PluginContext exposes registration, so capture at the actual tool boundary
    # without replacing the host's pre_tool or finalizer machinery.
    def observe_tool(tool_name, args, **kwargs):
        if tool_name in ("mcp__aqla__control", "mcp__aqla__interpret_source"):
            claims.append((extension.active_context, threading.get_ident(), native_before_tool(tool_name, args, **kwargs)))
    extension.ctx.register_hook("pre_tool_call", observe_tool)
    extension.ctx.register_hook("on_session_end", lambda **kwargs: finalizations.append(extension.active_context))

    owner = MessageEvent(text="owner request", source=source, message_id="1")
    forwarded = MessageEvent(text=original.text, source=source, message_id="42",
                             raw_message={"forward_origin": True, "date": datetime.fromtimestamp(original.sent_at / 1000, UTC)})
    following_owner = MessageEvent(text="owner followup", source=source, message_id="3")
    wake = MessageEvent(text="internal wake", source=source, message_id="4", internal=True, allow_gateway_control=False,
                        metadata={"hermes_plugin_injection": True, "hermes_plugin_id": "aqla-events"})
    pending = [forwarded, following_owner, wake]
    original_call = extension.call
    async def captured_call(action, value=None, **kwargs):
        if action == "native_forward":
            ingests.append(value.event_id)
        return await original_call(action, value, **kwargs)
    extension.call = captured_call

    executor = ThreadPoolExecutor(max_workers=2)
    adapter = SimpleNamespace(_active_sessions={}, _message_handler=None)
    adapter.set_message_handler = lambda callback: setattr(adapter, "_message_handler", callback)
    runner = SimpleNamespace(_MAX_INTERRUPT_DEPTH=5, session_store=store, adapters={Platform.TELEGRAM: adapter})
    runner._delivery_adapter_for = lambda origin: runner.adapters[Platform.TELEGRAM]
    runner._intake_adapter_for = lambda origin: None
    runner._get_executor = lambda: executor
    runner._profile_scope_for_source = lambda origin: nullcontext()
    runner._run_agent = MethodType(GatewayTurnMixin._run_agent, runner)
    runner._run_in_executor_with_context = MethodType(GatewayRunner._run_in_executor_with_context, runner)
    runner._run_agent_deliver_first_response = noop
    runner._session_key_for_source = store._generate_session_key
    runner._is_goal_continuation_event = lambda event: False
    runner._prepare_profile_scoped_inbound_message_text = lambda **kwargs: asyncio.sleep(0, result=kwargs["event"].text)
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._pinned_channel_inputs = lambda key, prompt, origin, **kwargs: (prompt, origin)
    runner._persist_prompt_pins = noop
    runner._refresh_agent_cache_message_count = noop
    turns = []
    async def inner(message, context_prompt, history, source, session_id, **kwargs):
        turns.append(extension.active_context)
        if extension.active_context[0] == "source" and not kwargs.get("_interrupt_depth", 0) == 3:
            assert ingests == [original.event_id], "Forward must be ingested before actual model execution"
        result = await runner._run_in_executor_with_context(lambda: agent.run_conversation(user_message=message, conversation_history=history))
        assert extension.active_context is None, "The actual native finalizer must revoke the copied receipt"
        if pending:
            next_event = pending.pop(0)
            extension.stamp(next_event)
            turn = SimpleNamespace(source=source, session_id=session_id, session_key=store._generate_session_key(source), run_generation=1,
                                   _interrupt_depth=kwargs.get("_interrupt_depth", 0), history=history, _status_thread_metadata={}, context_prompt="")
            return await GatewayTurnMixin._run_agent_queued_followup(runner, turn, adapter, next_event.text, next_event, result, result, None)
        return result
    runner._run_agent_inner = inner
    async def handler(event):
        result = await runner._run_agent(event.text, "", [], event.source, "lifecycle", event_message_id=event.message_id)
        assert extension.active_context is None, "The handler must not restore pre-run authority after the actual turn"
        return result
    adapter.set_message_handler(handler)
    extension.boot(runner)
    from native_setup_smoke import grant_fixture
    await grant_fixture(extension)
    try:
        result = await adapter._message_handler(owner)
        assert turns == [("operator", "1"), ("source", "42"), ("operator", "3"), ("source", "4")], turns
        assert len(finalizations) == 4 and all(value is None for value in finalizations), finalizations
        assert ingests == [original.event_id], ingests
        assert [claim[0][0] for claim in claims] == ["operator", "source", "source", "operator", "source", "source"], claims
        assert all(thread_id != main_thread for _, thread_id, _ in claims)
        assert [reply["action"] for _, _, reply in claims] == ["modify", "block", "modify", "modify", "block", "modify"]
        assert extension.active_context is None
        # The cached native agent's worker cannot retain an old turn's authority.
        assert (await runner._run_in_executor_with_context(lambda: extension.before_tool("mcp__aqla__control", {})))["action"] == "block"
        await verify_commands(extension, runner, adapter)
        await verify_reconnect(extension, runner, adapter)
        print("Native lifecycle: cached agent, recursive owner/source/wake turns, worker claims, finalizer revocation, stop/new/reset and reconnect passed")
        return result
    finally:
        extension.call = original_call
        executor.shutdown(wait=True)


async def verify_commands(extension, runner, adapter):
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent

    original = extension.binding[1]
    source = extension.turn_event.get()  # Outside all turns: deliberately absent.
    assert source is None
    from gateway.config import Platform
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=extension.owner, user_id=extension.owner)
    for command in ("stop", "new", "reset"):
        started = asyncio.Event()
        thread_started, during_command, probed, after_cancel, thread_done = (threading.Event() for _ in range(5))
        contexts = []
        def blocked_worker(thread_started=thread_started, during_command=during_command, probed=probed,
                           after_cancel=after_cancel, thread_done=thread_done):
            thread_started.set()
            assert during_command.wait(2)
            assert extension.active_context == ("source", "100")
            assert extension.before_tool("mcp__aqla__control", {})["action"] == "block"
            allowed = extension.before_tool("mcp__aqla__interpret_source", {})
            assert extension.authority.verify(allowed["args"]["context_auth"])["kind"] == "source"
            probed.set()
            assert after_cancel.wait(2)
            assert extension.active_context is None
            assert extension.before_tool("mcp__aqla__interpret_source", {})["action"] == "block"
            thread_done.set()
        async def blocked_handler(event, started=started, contexts=contexts, worker=blocked_worker,
                                  thread_started=thread_started, during_command=during_command, probed=probed):
            if event.message_id == "100":
                started.set()
                try:
                    await runner._run_in_executor_with_context(worker)
                finally:
                    contexts.append(extension.active_context)
            else:
                assert extension.active_context[0] == "operator"
                assert await asyncio.to_thread(thread_started.wait, 2)
                during_command.set()
                assert await asyncio.to_thread(probed.wait, 2)
                return None
        adapter.set_message_handler(blocked_handler)
        extension.attach(MessageEvent(text="source", source=source, message_id="100"), runner)
        # This turn is a synthetic internal wake; source tools have source authority.
        event = MessageEvent(text="wake", source=source, message_id="100", internal=True,
                             metadata={"hermes_plugin_injection": True, "hermes_plugin_id": "aqla-events"})
        running = asyncio.create_task(adapter._message_handler(event))
        key = runner.session_store._generate_session_key(source)
        adapter._active_sessions[key] = asyncio.Event()
        adapter._session_tasks = {key: running}
        adapter.name = "OfflineTelegram"
        adapter._unwrap_ephemeral = lambda response: (response, 0)
        adapter._dispatch_inline_reply = MethodType(BasePlatformAdapter._dispatch_inline_reply, adapter)
        adapter._drain_pending_after_session_command = noop_command
        adapter._requeue_counts = {}
        adapter._expected_cancelled_tasks = set()
        adapter.cancel_session_processing = MethodType(BasePlatformAdapter.cancel_session_processing, adapter)
        await started.wait()
        command_event = MessageEvent(text="/" + command, source=source, message_id="101")
        await asyncio.wait_for(BasePlatformAdapter._dispatch_active_session_command(adapter, command_event, key, command), 2)
        assert running.cancelled() and running in adapter._expected_cancelled_tasks
        after_cancel.set()
        assert await asyncio.to_thread(thread_done.wait, 2)
        assert contexts == [("source", "100")], contexts
        assert extension.active_context is None
    adapter.set_message_handler(original)
    extension.attach(MessageEvent(text="owner", source=source, message_id="1"), runner)


async def noop_command(*args, **kwargs):
    pass


async def verify_reconnect(extension, runner, first):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.session import SessionSource

    seen = []
    async def handler(event):
        seen.append(extension.active_context)
    replacement = SimpleNamespace(_message_handler=handler, send_path_degraded=False)
    replacement.set_message_handler = lambda callback: setattr(replacement, "_message_handler", callback)
    runner.delivery_router = SimpleNamespace()
    runner._failed_platforms = {Platform.TELEGRAM: {}}
    runner._publish_primary_adapter = lambda platform, adapter: runner.adapters.update({platform: adapter})
    runner._update_platform_runtime_status = lambda *args, **kwargs: None
    runner._schedule_planned_restart_replay = lambda: None
    runner._redeliver_failed_obligations_for_platform = noop_command
    runner._schedule_resume_pending_sessions = lambda **kwargs: None
    workers = {task for task in asyncio.all_tasks() if task.get_name().startswith("aqla-")}
    old_handler = extension.binding[1]
    await GatewayAdapterLifecycleMixin._install_reconnected_adapter(runner, Platform.TELEGRAM, replacement)
    assert replacement._message_handler is not handler
    assert first._message_handler is old_handler
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=extension.owner, user_id=extension.owner)
    await replacement._message_handler(MessageEvent(text="owner", source=source, message_id="200"))
    # Capture just the offline forward delivery; the real MCP receipt above was
    # already verified before model execution. No external transport is active.
    forwarded = []
    async def capture(action, value=None, **kwargs):
        forwarded.append((action, value))
    prior = extension.call
    extension.call = capture
    try:
        await replacement._message_handler(MessageEvent(text="source", source=source, message_id="201", raw_message={"forward_origin": True}))
    finally:
        extension.call = prior
    assert seen == [("operator", "200"), ("source", "201")], seen
    assert [action for action, _ in forwarded] == ["native_forward"]
    assert {task for task in asyncio.all_tasks() if task.get_name().startswith("aqla-")} == workers
