import asyncio
from types import SimpleNamespace

import pytest
import yaml

from aqla_agent.events import HermesExtension
from aqla_agent.telegram import ReaderRetry, SourceAccessError


class Context:
    def __init__(self, configuration):
        self.configuration = configuration
        self.tasks, self.cleanups = [], []

    def get_config(self, key, default=None):
        return self.configuration.get(key, default)

    def call_mcp(self, server, tool, arguments, **_):
        assert tool == "integration" and arguments["action"] == "source_notice"
        return {"ok": True, "result": {"recorded": True}}

    def on_unload(self, callback):
        self.cleanups.append(callback)

    def spawn_task(self, coro, **_):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task




def message(owner="12345", injection=False, plugin_id="aqla-events", text="normal user text"):
    return SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_type="dm", chat_id="12345", user_id=owner, message_id="1"),
                           metadata={"hermes_plugin_injection": injection, "hermes_plugin_id": plugin_id}, message_id="1", text=text)


@pytest.fixture
def extension(setup, tmp_path):
    settings, *_ = setup
    configuration = tmp_path / "config.yaml"
    configuration.write_text(yaml.safe_dump(settings.model_dump(mode="json")))
    ctx = Context({"owner_id": "12345", "config_path": str(configuration)})
    return HermesExtension(ctx), ctx


@pytest.mark.asyncio
async def test_slow_shadow_evaluation_does_not_delay_legacy_wakeup(extension):
    ext, ctx = extension
    evaluating, awakened = asyncio.Event(), asyncio.Event()

    async def slow_tick():
        evaluating.set()
        await asyncio.Event().wait()

    async def snapshot(_):
        return {"pending": ["source"], "notification_pending": 0, "source_queue": {"ready": ["source"]}}

    def inject(*args, **kwargs):
        awakened.set()
        return True

    ext.source_worker = SimpleNamespace(config=SimpleNamespace(mode="shadow"), tick=slow_tick)
    ext.call, ctx.inject_message = snapshot, inject
    shadow = asyncio.create_task(ext.shadow_worker())
    await asyncio.wait_for(evaluating.wait(), 1)
    legacy = asyncio.create_task(ext.worker())
    try:
        await asyncio.wait_for(awakened.wait(), 1)
        assert not shadow.done()
    finally:
        shadow.cancel()
        legacy.cancel()
        await asyncio.gather(shadow, legacy, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_wakeup_and_source_tools_do_not_bind_or_call_decision_worker(extension):
    ext, ctx = extension
    awakened = asyncio.Event()
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Legacy Hermes must not invoke the Decision worker")

    async def snapshot(_):
        return {"pending": ["source"], "notification_pending": 0, "source_queue": {"ready": ["source"]}}

    def inject(*args, **kwargs):
        awakened.set()
        return True

    ext.source_worker = SimpleNamespace(config=SimpleNamespace(mode="legacy"), bind=forbidden, tick=forbidden, internal=forbidden)
    ext.call, ctx.inject_message = snapshot, inject
    worker = asyncio.create_task(ext.worker())
    try:
        await asyncio.wait_for(awakened.wait(), 1)
        ext.active_context = ext.origin(message(injection=True))
        routed = ext.before_tool("mcp__aqla__interpret_source", {"event_id": "source"})
        assert ext.authority.verify(routed["args"]["context_auth"])["kind"] == "source"
        assert routed.get("action") != "block" and not calls
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)








@pytest.mark.asyncio
async def test_reader_worker_auth_backoff_avoids_three_second_reconnect_loop(extension, tmp_path, monkeypatch):
    ext, _ = extension
    clock, starts = [0.0], []
    ext.settings.telegram_session_file = tmp_path / "missing-session.secret"
    ext.reader_retry = ReaderRetry(clock=lambda: clock[0], wall_clock=lambda: 1000, jitter=lambda: 0.5)
    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            starts.append(clock[0])
            raise SourceAccessError("reader_session_unauthorized")
        async def close(self):
            pass
    async def snapshot(*args, **kwargs):
        return {"sources": ["channel"]}
    async def sleep(seconds):
        clock[0] += seconds
        if clock[0] >= 100:
            raise asyncio.CancelledError
    monkeypatch.setattr("aqla_agent.events.TelegramReader", Reader)
    monkeypatch.setattr("aqla_agent.events.asyncio", SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
    ext.call = snapshot
    with pytest.raises(asyncio.CancelledError):
        await ext.reader_worker()
    assert starts == [0.0, 30.0, 90.0]
    assert ext.reader_error["attempt"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", ["secret_metadata", "selection"])
async def test_reader_retry_repair_resets_deadline_without_credential_output(extension, tmp_path, monkeypatch, repair):
    ext, _ = extension
    clock, starts = [0.0], []
    ext.settings.telegram_session_file = tmp_path / "session.secret"
    ext.reader_retry = ReaderRetry(clock=lambda: clock[0], wall_clock=lambda: 1000, jitter=lambda: 0.5)
    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            starts.append(clock[0])
            if len(starts) == 1:
                raise SourceAccessError("reader_session_unauthorized")
        async def sync_sources(self):
            pass
        def status_error(self):
            return None
        async def close(self):
            pass
    async def snapshot(*args, **kwargs):
        return {"sources": ["a" if clock[0] < 6 else "b"]}
    async def sleep(seconds):
        clock[0] += seconds
        if repair == "secret_metadata" and clock[0] == 3:
            ext.settings.telegram_session_file.write_text("synthetic repaired session", encoding="utf-8")
        if clock[0] >= 9:
            raise asyncio.CancelledError
    monkeypatch.setattr("aqla_agent.events.TelegramReader", Reader)
    monkeypatch.setattr("aqla_agent.events.asyncio", SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
    ext.call = snapshot
    with pytest.raises(asyncio.CancelledError):
        await ext.reader_worker()
    assert starts == [0.0, 3.0 if repair == "secret_metadata" else 6.0]
    assert ext.reader_error is None and ext.reader_retry.status is None and ext.reader_retry.attempts == 0












@pytest.mark.asyncio
async def test_native_handler_context_is_task_local_for_concurrent_commands(extension):
    extension, ctx = extension
    began = asyncio.Event()
    finish = asyncio.Event()
    seen = []

    async def original(event):
        seen.append(extension.active_context[0])
        if not event.metadata["hermes_plugin_injection"]:
            began.set()
            await finish.wait()
            assert extension.active_context[0] == "operator"
        return "native Hermes response"

    adapter = SimpleNamespace(_message_handler=original)
    adapter.set_message_handler = lambda handler: setattr(adapter, "_message_handler", handler)
    gateway = SimpleNamespace(_delivery_adapter_for=lambda source: adapter,
                              session_store=SimpleNamespace(_generate_session_key=lambda source: "private-route"))

    async def inert_worker():
        await asyncio.Event().wait()

    extension.worker = inert_worker
    extension.attach(message(), gateway)
    owner = asyncio.create_task(adapter._message_handler(message()))
    await began.wait()
    source = asyncio.create_task(adapter._message_handler(message(injection=True)))
    await asyncio.sleep(0)
    assert extension.active_context is None
    finish.set()
    await asyncio.gather(owner, source)
    assert seen == ["operator", "source"]
    assert extension.active_context is None
    for task in ctx.tasks:
        task.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    for cleanup in ctx.cleanups:
        cleanup()
    assert adapter._message_handler is original


@pytest.mark.asyncio
async def test_finalizer_revokes_claim_already_copied_into_worker_thread(extension):
    import threading

    extension, _ = extension
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        release.wait(2)
        assert extension.before_tool("mcp_aqla_control", {})["action"] == "block"
    with extension.claim_scope(("operator", "1")):
        task = asyncio.create_task(asyncio.to_thread(worker))
        await asyncio.to_thread(entered.wait, 2)
        extension.ended()
        release.set()
        await task
    assert extension.active_context is None


@pytest.mark.asyncio
async def test_rebinding_replaces_handler_without_duplicate_workers(extension):
    extension, ctx = extension
    async def handler(event):
        assert extension.active_context == ("operator", "1")
    async def worker():
        await asyncio.Event().wait()
    extension.worker = extension.reader_worker = worker
    def adapter():
        value = SimpleNamespace(_message_handler=handler)
        value.set_message_handler = lambda callback: setattr(value, "_message_handler", callback)
        return value
    first, second = adapter(), adapter()
    gateway = SimpleNamespace(_delivery_adapter_for=lambda _: first,
                              session_store=SimpleNamespace(_generate_session_key=lambda _: "owner"))
    extension.attach(message(), gateway)
    extension.attach(message(), gateway)
    assert len(ctx.tasks) == 3
    extension.attach(message(), gateway, adapter=second)
    assert first._message_handler is handler
    assert second._message_handler is not handler
    assert len(ctx.tasks) == 3
    await second._message_handler(message())
    for task in ctx.tasks:
        task.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    for cleanup in ctx.cleanups:
        cleanup()
    assert second._message_handler is handler


@pytest.mark.asyncio
async def test_master_change_evicts_native_cached_agent_before_next_turn(extension, tmp_path):
    extension, ctx = extension
    extension.master_path = tmp_path / "SKILL.md"
    extension.master_path.write_text("original", encoding="utf-8")
    extension.master_hash = extension.skill_hash()
    evicted = []
    async def handler(event):
        return "native reply"
    async def worker():
        await asyncio.Event().wait()
    extension.worker = worker
    adapter = SimpleNamespace(_message_handler=handler)
    adapter.set_message_handler = lambda value: setattr(adapter, "_message_handler", value)
    gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, _evict_cached_agent=evicted.append,
                              session_store=SimpleNamespace(_generate_session_key=lambda _: "owner"))
    extension.attach(message(), gateway)
    extension.master_path.write_text("updated", encoding="utf-8")
    await adapter._message_handler(message())
    assert evicted == ["owner"]
    await adapter._message_handler(message())
    assert evicted == ["owner"]
    for task in ctx.tasks:
        task.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    for cleanup in ctx.cleanups:
        cleanup()
    assert adapter._message_handler is handler


def test_owner_slash_input_passes_to_native_hermes(extension, monkeypatch):
    extension, _ = extension
    attached = []
    monkeypatch.setattr(extension, "attach", lambda event, gateway: attached.append(event))
    event = message(text="/help")
    assert extension.inbound(event, None) is None
    assert attached == [event]
    assert extension.inbound(message(owner="intruder", text="/help"), None)["action"] == "skip"


def test_master_edits_reject_batches_supporting_files_and_oversize(extension):
    extension, _ = extension
    extension.active_context = ("operator", "owner-message")
    base = {"name": "aqla-master", "action": "edit", "content": "small"}
    for extra in ({"operations": [{"action": "delete"}]}, {"file_path": "other.md"}, {"content": "x" * 8193}):
        assert extension.before_tool("skill_manage", {**base, **extra})["action"] == "block"
    assert extension.before_tool("skill_manage", base) is None


def test_master_proposals_allow_native_background_but_not_untrusted_source(extension, monkeypatch):
    extension, _ = extension
    args = {"operations": [{"name": "aqla-master", "action": "patch", "content": "small"}]}
    extension.active_context = ("source", "source-message")
    assert extension.before_tool("skill_manage", args)["action"] == "block"
    extension.ended()
    assert extension.before_tool("skill_manage", args)["action"] == "block"
    monkeypatch.setattr("aqla_agent.skill_review.native_background_review", lambda: True)
    assert extension.before_tool("skill_manage", args) is None
    args["operations"][0]["name"] = "another-skill"
    assert extension.before_tool("skill_manage", args)["action"] == "block"








@pytest.mark.asyncio
async def test_forwarded_native_owner_message_is_ingested_before_agent(extension):
    extension, ctx = extension
    recorded = []
    async def call(action, source):
        recorded.append((action, source))
    async def handler(event):
        assert recorded[0][0] == "native_forward"
        assert extension.active_context[0] == "source"
    extension.call = call
    async def worker():
        await asyncio.Event().wait()
    extension.worker = worker
    adapter = SimpleNamespace(_message_handler=handler)
    adapter.set_message_handler = lambda value: setattr(adapter, "_message_handler", value)
    gateway = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, session_store=SimpleNamespace(_generate_session_key=lambda _: "owner"))
    event = message()
    event.raw_message = {"forward_origin": True}
    extension.attach(event, gateway)
    await adapter._message_handler(event)
    assert recorded[0][1].chat_id == extension.owner
    for task in ctx.tasks:
        task.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)


def test_forwarded_slash_text_is_source_and_cannot_control_native_gateway(extension):
    extension, _ = extension
    source = message(text="/help")
    source.raw_message = {"forward_origin": True}
    extension.stamp(source)
    assert source.allow_gateway_control is False
    assert source.metadata["aqla_origin"] == "source"
    assert extension.forward_event(source).text == "/help"
    owner = message(text="/help")
    owner.allow_gateway_control = True
    extension.stamp(owner)
    assert owner.allow_gateway_control is True


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_start", [False, True])
@pytest.mark.parametrize("notification_only", [False, True])
async def test_reader_and_wake_continue_with_notification_backlog(extension, tmp_path, monkeypatch, blocked_start, notification_only):
    extension, ctx = extension
    extension.settings.telegram_session_file = tmp_path / "reader.secret"
    extension.settings.telegram_session_file.write_text("")
    started, woke = asyncio.Event(), asyncio.Event()
    wakes = []
    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            started.set()
            if blocked_start:
                await asyncio.Event().wait()
            raise SourceAccessError("reader_session_unauthorized")
        async def close(self):
            pass
    monkeypatch.setattr("aqla_agent.events.TelegramReader", Reader)
    async def snapshot(*args, **kwargs):
        await started.wait()
        return {"pending": [] if notification_only else ["persisted-forward"], "notification_pending": 400 if notification_only else 0}
    def inject(text, **kwargs):
        wakes.append(text)
        woke.set()
        return True
    extension.call, ctx.inject_message = snapshot, inject
    tasks = [asyncio.create_task(extension.reader_worker()), asyncio.create_task(extension.worker())]
    try:
        if notification_only:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(.05)
            assert not wakes  # Native outbox delivers the notice; no model wake is required.
        else:
            await asyncio.wait_for(woke.wait(), 1)
            assert wakes
            if not blocked_start:
                assert "reader_session_unauthorized" in wakes[0]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_reader_session_reports_status_and_preserves_pending_wake(extension, tmp_path):
    extension, ctx = extension
    extension.settings.telegram_session_file = tmp_path / "absent-session.secret"
    extension.settings.telegram_api_id = 123
    extension.settings.telegram_api_hash_file = tmp_path / "api-hash.secret"
    extension.settings.telegram_api_hash_file.write_text("test-only-api-hash")
    woke = asyncio.Event()
    wakes = []
    async def snapshot(*args, **kwargs):
        return {"pending": ["persisted-forward"], "notification_pending": 0}
    def inject(text, **kwargs):
        wakes.append(text)
        woke.set()
        return True
    extension.call, ctx.inject_message = snapshot, inject
    tasks = [asyncio.create_task(extension.reader_worker()), asyncio.create_task(extension.worker())]
    try:
        await asyncio.wait_for(woke.wait(), 1)
        assert extension.reader_error["code"] == "reader_session_missing" and extension.reader_error["sources"] == []
        assert extension.reader_error["attempt"] == 1 and isinstance(extension.reader_error["next_retry_at"], int)
        assert set(extension.reader_error) == {"code", "sources", "attempt", "next_retry_at"}
        assert "reader_session_missing" in wakes[0]
        assert "test-only-api-hash" not in wakes[0]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ready", "blocked", "recovery"])
async def test_wake_observed_source_work_independent_of_zero_notifications(extension, monkeypatch, mode):
    ext, ctx = extension
    queue = {"ready_total": 0, "blocked_total": 0, "recovery_total": 0}
    queue[mode + "_total"] = 12 if mode == "ready" else 1
    if mode == "blocked":
        queue.update(blocked=["blocked-revision"], blocked_signature="signature")
    elif mode == "recovery":
        queue.update(recovery_operations=["entry:owned-trade"])
    pending = [f"ready-{i}" for i in range(12)] if mode == "ready" else []
    calls, wakes = [], []
    async def snapshot(*args, **kwargs):
        calls.append(1)
        if len(calls) > 2:
            raise asyncio.CancelledError
        return {"pending": pending, "notification_pending": 0, "source_queue": queue}
    async def instant_sleep(*args):
        pass
    monkeypatch.setattr("aqla_agent.events.asyncio.sleep", instant_sleep)
    monkeypatch.setattr("aqla_agent.events.time.monotonic", lambda: 100)
    ext.call = snapshot
    ctx.inject_message = lambda text, **kwargs: wakes.append(text) or True
    with pytest.raises(asyncio.CancelledError):
        await ext.worker()
    if mode == "blocked":
        assert not wakes  # The versioned conflict notice belongs to native delivery.
        return
    assert len(wakes) == 1 and '"notification_pending": 0' in wakes[0]
    assert f'"{mode}_total": {queue[mode + "_total"]}' in wakes[0]
    assert "reader_recovered" not in wakes[0]
    if mode == "ready":
        assert "ready-7" in wakes[0] and "ready-8" not in wakes[0]
    elif mode == "recovery":
        assert "entry:owned-trade" in wakes[0]
