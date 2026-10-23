import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from aqla_agent.native_setup import SETUP_PROMPT, NativeSetup, PrivateTransportLogs
from aqla_agent.setup import SetupBinding, SetupGate, SetupVerifier
from aqla_hyperliquid_mcp.models import AqlaError

MARKER = "synthetic-only-password-not-a-user-secret"


class Bot:
    id = 999

    def __init__(self):
        self.deleted, self.sent = [], []
        self.ack = True
        self.delete_hook = None

    async def send_message(self, **args):
        self.sent.append(args)
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def delete_message(self, **args):
        self.deleted.append(args)
        if self.delete_hook:
            await self.delete_hook()
        return self.ack


def update(text, *, owner="12345", forwarded=False, reply=None, bot=False, chat_type="private", edited=False, via_bot=False):
    message = SimpleNamespace(text=text, message_id=77,
        from_user=SimpleNamespace(id=owner, is_bot=bot), chat=SimpleNamespace(id="12345", type=chat_type),
        forward_origin=True if forwarded else None, via_bot=True if via_bot else None,
        reply_to_message=SimpleNamespace(message_id=reply) if reply else None)
    return SimpleNamespace(effective_message=message, edited_message=message if edited else None)


def fixture_controller():
    now = [1000.0]
    current = [SetupBinding("12345", "12345", "synthetic-gateway", "synthetic-session")]
    gate = SetupGate(True, clock=lambda: now[0])
    calls, checks = [], []
    class Verifier:
        async def verify(self, text):
            checks.append(text)
            return text == MARKER
    async def request(action, **args):
        calls.append({"action": action, **args})
        if action == "setup_revoke":
            gate.revoke(args["setup_binding"]["owner_id"])
            return {"revoked": True}
        binding = SetupBinding(**args["setup_binding"])
        if action == "setup_bind":
            return {"generation": gate.bind(binding), "locked_seconds": gate.status(binding)["locked_seconds"]}
        if action == "setup_failed":
            return gate.failed(binding)
        if action == "setup_grant":
            gate.register(binding, args["lease_id"], None, args["generation"])
            return {"registered": True}
        return gate.status(binding, args.get("lease_id"))
    native = NativeSetup("12345", Verifier(), request, lambda: current[0], clock=lambda: now[0])
    return native, Bot(), gate, now, current, calls, checks


@pytest.mark.asyncio
async def test_delete_ack_precedes_local_verifier_and_exact_lease_never_renews():
    native, bot, gate, now, _, calls, checks = fixture_controller()
    assert await native.intercept(update("/setup"), bot)
    async def observed_delete():
        assert checks == [] and not native.claims()
    bot.delete_hook = observed_delete
    assert await native.intercept(update(MARKER), bot)
    claims = native.claims()
    binding = SetupBinding(**claims["binding"])
    gate.require(claims["identity"], binding)
    assert gate.status(binding, claims["identity"])["remaining_seconds"] == 300
    assert MARKER not in json.dumps(calls + bot.sent + bot.deleted)
    now[0] = 1299
    assert await native.intercept(update("/setup status"), bot)
    assert gate.status(binding, claims["identity"])["remaining_seconds"] == 1
    now[0] = 1300
    assert native.claims() is None
    with pytest.raises(AqlaError):
        gate.require(claims["identity"], binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_ack", [False, None])
async def test_missing_deletion_ack_does_not_verify_or_grant(delete_ack):
    native, bot, _, _, _, calls, checks = fixture_controller()
    await native.intercept(update("/setup"), bot)
    bot.ack = delete_ack
    await native.intercept(update(MARKER), bot)
    assert checks == [] and native.claims() is None
    assert not any(row["action"] == "setup_grant" for row in calls)


@pytest.mark.asyncio
async def test_five_failures_lock_for_fifteen_minutes_and_delayed_answers_are_deleted():
    native, bot, _, now, _, calls, _ = fixture_controller()
    await native.intercept(update("/setup"), bot)
    prompt = native.challenge.prompt_id
    for _ in range(5):
        await native.intercept(update("wrong synthetic password", reply=prompt), bot)
    assert native.locked_until == 1900 and native.claims() is None
    await native.intercept(update("/setup"), bot)
    assert native.challenge is None
    await native.intercept(update(MARKER, reply=prompt), bot)
    assert len(bot.deleted) == 6
    now[0] = 1899
    await native.intercept(update("/setup"), bot)
    assert native.challenge is None
    now[0] = 1900
    await native.intercept(update("/setup"), bot)
    assert native.challenge is not None
    assert not any(row["action"] == "setup_grant" for row in calls)


@pytest.mark.asyncio
async def test_callback_is_not_a_password_candidate_and_preserves_the_menu():
    native, bot, _, _, _, calls, checks = fixture_controller()
    await native.intercept(update("/setup"), bot)
    current = update("Menu button", bot=True)
    current.callback_query = SimpleNamespace(data="aqla-menu:synthetic-cancel")
    assert not await native.intercept(current, bot)
    assert native.challenge is not None and native.failures == 0
    assert not bot.deleted and not checks
    assert not any(row["action"] in ("setup_failed", "setup_grant") for row in calls)


@pytest.mark.asyncio
async def test_challenge_timeout_or_session_change_after_await_never_grants():
    for reason in ("expiry", "session", "stop"):
        native, bot, _, now, current, calls, checks = fixture_controller()
        await native.intercept(update("/setup"), bot)
        async def delayed(checks=checks, reason=reason, now=now, current=current, native=native, bot=bot):
            checks.append("worker-ran")
            if reason == "expiry":
                now[0] = 1120
            elif reason == "session":
                current[0] = SetupBinding("12345", "12345", "synthetic-gateway", "next-session")
            else:
                await native.intercept(update("/stop"), bot)
            return True
        native.verifier.verify = lambda _: delayed()
        await native.intercept(update(MARKER), bot)
        assert native.claims() is None and len(bot.deleted) == 1
        assert not any(row["action"] == "setup_grant" for row in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"owner": "999"}, {"forwarded": True}, {"bot": True}, {"chat_type": "group"}])
async def test_only_actual_private_owner_can_enter_setup(kwargs):
    native, bot, _, _, _, calls, _ = fixture_controller()
    assert await native.intercept(update("/setup", **kwargs), bot) is False
    assert calls == [] and bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/setup aus", "/stop", "/new"])
async def test_explicit_revocations_remove_server_lease(command):
    native, bot, gate, _, _, _, _ = fixture_controller()
    await native.intercept(update("/setup"), bot)
    await native.intercept(update(MARKER), bot)
    claims = native.claims()
    await native.intercept(update(command), bot)
    with pytest.raises(AqlaError):
        gate.require(claims["identity"], SetupBinding(**claims["binding"]))


def test_ptb_debug_payload_filter_omits_update_repr_and_api_result():
    import logging
    redactor = PrivateTransportLogs()
    for name, message, args in (("telegram.ext.Application", "Processing update %s", (MARKER,)),
                                ("telegram.Bot", "Call to Bot API endpoint `%s` finished with return value `%s`", ("getUpdates", {"text": MARKER}))):
        record = logging.LogRecord(name, logging.DEBUG, "synthetic", 1, message, args, None)
        assert redactor.filter(record)
        assert MARKER not in record.getMessage()


@pytest.mark.asyncio
async def test_different_clock_origins_and_rpc_delay_keep_exact_mcp_deadline():
    native, bot, gate, now, _, _, _ = fixture_controller()
    gate.clock = lambda: now[0] - 25
    gate._last_time = 975
    original_request = native.request
    async def delayed_grant(action, **args):
        if action == "setup_grant":
            assert "expires_at" not in args  # A caller clock cannot set the MCP deadline.
            now[0] += 2
            result = await original_request(action, **args)
            now[0] += 2
            return result
        return await original_request(action, **args)
    native.request = delayed_grant
    await native.intercept(update("/setup"), bot)
    await native.intercept(update(MARKER), bot)
    claims = native.claims()
    binding = SetupBinding(**claims["binding"])
    assert gate.status(binding, claims["identity"])["remaining_seconds"] == 298
    assert native.lease[2] == 1300
    now[0] = 1300
    assert native.claims() is None
    # MCP grants exactly 300 seconds from its own registration at server time 977.
    now[0] = 1302
    with pytest.raises(AqlaError):
        gate.require(claims["identity"], binding)


@pytest.mark.asyncio
async def test_old_challenge_answer_never_cancels_new_valid_challenge():
    native, bot, _, _, _, _, _ = fixture_controller()
    await native.intercept(update("/setup"), bot)
    old_prompt = native.challenge.prompt_id
    await native.intercept(update("/setup"), bot)
    current = native.challenge
    await native.intercept(update(MARKER, reply=old_prompt), bot)
    assert native.claims() is None
    assert native.challenge == current
    assert len(bot.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["/private-synthetic-marker", "/setup arbitrary-synthetic-password"])
async def test_unknown_slash_password_is_deleted_verified_locally_and_never_sent_to_mcp(password):
    native, bot, _, _, _, calls, checks = fixture_controller()
    async def verify(text):
        checks.append(text)
        assert bot.deleted and native.claims() is None
        return text == password
    native.verifier.verify = verify
    await native.intercept(update("/setup"), bot)
    assert await native.intercept(update(password), bot)
    assert checks == [password] and native.claims() is not None
    assert password not in json.dumps(calls + bot.sent + bot.deleted)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"forwarded": True}, {"via_bot": True}, {"edited": True}])
async def test_untrusted_owner_dm_candidates_are_deleted_without_verification_or_grant(kwargs):
    native, bot, _, _, _, calls, checks = fixture_controller()
    await native.intercept(update("/setup"), bot)
    assert await native.intercept(update(MARKER, **kwargs), bot)
    assert len(bot.deleted) == 1 and checks == [] and native.claims() is None
    assert not any(row["action"] == "setup_grant" for row in calls)
    assert MARKER not in json.dumps(calls + bot.sent + bot.deleted)


@pytest.mark.asyncio
async def test_restart_recognizes_exact_own_bot_prompt_and_deletes_late_password():
    native, bot, _, _, _, calls, checks = fixture_controller()
    assert native.challenge is None and native.old_prompts == set()
    candidate = update("/late-synthetic-password")
    candidate.effective_message.reply_to_message = SimpleNamespace(message_id=2000, text=SETUP_PROMPT,
        from_user=SimpleNamespace(id=bot.id, is_bot=True), chat=SimpleNamespace(id="12345", type="private"))
    assert await native.intercept(candidate, bot)
    assert len(bot.deleted) == 1 and checks == [] and calls == [] and native.claims() is None
    assert "/late-synthetic-password" not in json.dumps(bot.sent + bot.deleted)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("bot_id", 998), ("bot", False), ("chat_id", "other"),
                                        ("chat_type", "group"), ("text", "unrelated bot message")])
async def test_restart_prompt_recognition_requires_exact_bot_private_chat_and_prompt(field, value):
    native, bot, _, _, _, calls, checks = fixture_controller()
    prompt = SimpleNamespace(message_id=2000, text=SETUP_PROMPT,
        from_user=SimpleNamespace(id=bot.id, is_bot=True), chat=SimpleNamespace(id="12345", type="private"))
    target, attribute = {"bot_id": (prompt.from_user, "id"), "bot": (prompt.from_user, "is_bot"),
                         "chat_id": (prompt.chat, "id"), "chat_type": (prompt.chat, "type"), "text": (prompt, "text")}[field]
    setattr(target, attribute, value)
    candidate = update("normal owner message")
    candidate.effective_message.reply_to_message = prompt
    assert await native.intercept(candidate, bot) is False
    assert bot.deleted == [] and bot.sent == [] and calls == [] and checks == []


@pytest.mark.asyncio
async def test_hash_worker_is_bounded_off_loop_and_cancellation_does_not_release_running_slot(monkeypatch):
    verifier = SetupVerifier.from_record({"version": 1, "algorithm": "scrypt", "n": 131072, "r": 8, "p": 1,
                                         "salt": "00" * 16, "digest": "00" * 32})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    thread_ids = []
    def blocked(self, password):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        finished.set()
        return True
    monkeypatch.setattr(SetupVerifier, "matches", blocked)
    first = asyncio.create_task(verifier.verify(MARKER))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert await verifier.verify(MARKER) is False
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await verifier.verify(MARKER) is False
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != threading.get_ident()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
