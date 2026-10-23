import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_native_setup import fixture_controller, update

from aqla_agent.native_commands import CALLBACK_PREFIX, COMMANDS, NativeCommands, register_commands
from aqla_agent.pipelines import Parameters
from aqla_hyperliquid_mcp.auth import Authority


@pytest.fixture
def controls():
    native, bot, gate, now, current, calls, checks = fixture_controller()
    native.lease = "a" * 64, current[0], 1300.0
    generation = gate.bind(current[0])
    gate.register(current[0], "a" * 64, None, generation)
    parameters = Parameters().model_dump(mode="json")
    data = {"configuration_revision": 7, "paused": False, "sources": ["-101", "-102"], "source_names_revision": 0,
            "profiles": {"standard": {"id": "standard", "name": "Standard", "parameters": parameters, "revision": 4},
                         **{f"p{i}": {"id": f"p{i}", "name": f"Profil {i}", "parameters": parameters, "revision": 1} for i in range(10)}},
            "source_names": {"-101": "Kanalname", "-102": "Zweiter Kanal"},
            "source_ranks": {"-101": {"tier": "D", "revision": 3}, "-102": {"tier": "C", "revision": 1}},
            "execution_accounts": {"hyperliquid:testnet": {"configured": True, "enabled": True, "limits": {"max_open_trades": 3, "max_total_risk_percent": "2"}},
                                   "aster:mainnet": {"configured": True, "enabled": False, "limits": {"max_open_trades": 2, "max_total_risk_percent": "1"}}},
            "journal": {"enabled": True, "pending": 2, "last_synced_at": 123},
            "trades": [], "telegram_view": {"status": ["**Status** · online"]}, "setup": {"remaining_seconds": 300}}
    authority = Authority("synthetic-integration-fixture-only" * 2)
    mutations = []
    status_hook = [None]

    async def mcp(server, tool, args):
        if tool == "status":
            authority.verify(args["context_auth"], operator=True)
            if status_hook[0]:
                hook, status_hook[0] = status_hook[0], None
                await hook()
            return copy.deepcopy(data)
        if tool == "ranking_status":
            return {"sources": {identity: {**row, "name": data["source_names"][identity], "proof_total": 4, "evidence": [{"net_r": "1"}], "exclusions": []} for identity, row in data["source_ranks"].items()}}
        assert tool == "control"
        claims = authority.verify(args["context_auth"], operator=True)
        if args["action"] not in ("pause", "set_source_name"):
            setup = claims.get("setup") or {}
            from aqla_agent.setup import SetupBinding
            gate.require(setup["identity"], SetupBinding(**setup["binding"]))
        mutations.append(args)
        previous_paused = data["paused"]
        if args["action"] in ("pause", "resume"):
            data["paused"] = args["action"] == "pause"
        elif args["action"] == "set_profile":
            assert args["expected_revision"] == data["configuration_revision"]
            data["profiles"][args["profile"]["id"]] = args["profile"]
            data["configuration_revision"] += 1
        elif args["action"] == "set_account_limits":
            assert args["expected_revision"] == data["configuration_revision"]
            data["execution_accounts"][args["account_key"]]["limits"] = args["limits"]
            data["configuration_revision"] += 1
        elif args["action"] == "set_source_rank":
            row = data["source_ranks"][args["source_id"]]
            assert row["revision"] == args["expected_revision"]
            row["tier"], row["revision"] = args["tier"], row["revision"] + 1
        elif args["action"] in ("add_sources", "remove_sources"):
            from aqla_agent.sources import change_sources
            change_sources(data, args["action"], args["sources"], args["expected_revision"])
        elif args["action"] == "set_source_name":
            from aqla_agent.sources import set_source_name
            set_source_name(data, args["source_id"], args["source_name"], args["expected_revision"])
        return {**copy.deepcopy(data), "previous_paused": previous_paused}

    async def send(_chat, text):
        bot.sent.append({"text": text, "native_send": True})
        return SimpleNamespace(success=True)

    extension = SimpleNamespace(owner="12345", setup=native, setup_binding=lambda: current[0], authority=authority,
        server="aqla", mcp_call=mcp, binding=[SimpleNamespace(send=send)],
        settings=SimpleNamespace(google_sheets=SimpleNamespace(spreadsheet_id="synthetic-configured-book-id")))
    command = NativeCommands(extension)
    return command, bot, data, mutations, now, current, status_hook


def callback(token, *, actor="12345", chat="12345", chat_type="private", bot=False):
    async def answer():
        pass
    query = SimpleNamespace(data=token, from_user=SimpleNamespace(id=actor, is_bot=bot),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat, type=chat_type), text="Untrusted button label"), answer=answer)
    return SimpleNamespace(callback_query=query, effective_message=query.message)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["ALLE AKTUELLEN KANÄLE", "ALLEAKTUELLENKANÄLE", "zeig mir alle kanäle", "zeigmirallekanäle", "wie ist Status?", "wieistStatus", "alle profile", "was kannst du?"])
async def test_routine_german_queries_are_native_authenticated_and_no_model(controls, text):
    commands, bot, _, mutations, *_ = controls
    commands.extension.setup.lease = None
    assert await commands.intercept(update(text), bot)
    assert bot.sent and mutations == []
    assert not await commands.intercept(update("Warum wurde das Signal gestern abgelehnt?"), bot)
    before = len(bot.sent)
    assert await commands.intercept(update(text, owner="99999"), bot)
    assert len(bot.sent) == before


@pytest.mark.asyncio
async def test_native_add_remove_and_name_preserve_existing_channels(controls):
    commands, bot, data, mutations, *_ = controls
    assert await commands.intercept(update('/aqla_hinzufuegen -103 @newchannel'), bot)
    assert set(data["sources"]) == {"-101", "-102", "-103", "@newchannel"}
    assert await commands.intercept(update('/aqla_kanalname -103 "Neuer Kanal"'), bot)
    assert data["source_names"]["-103"] == "Neuer Kanal"
    assert await commands.intercept(update('/aqla_entfernen "Neuer Kanal"'), bot)
    assert set(data["sources"]) == {"-101", "-102", "@newchannel"}
    assert [row["action"] for row in mutations] == ["add_sources", "set_source_name", "remove_sources"]
    assert all(row["action"] != "set_sources" for row in mutations)
    commands.extension.setup.lease = None
    before = copy.deepcopy(data)
    assert await commands.intercept(update('/aqla_hinzufuegen -104'), bot)
    assert data == before


@pytest.mark.asyncio
async def test_native_cosmetic_name_works_without_setup_and_subscription_changes_stay_protected(controls):
    commands, bot, data, mutations, *_ = controls
    commands.extension.setup.lease = None
    commands.extension.setup.verifier = None
    before_sources = data["sources"].copy()
    assert await commands.intercept(update('/aqla_kanalname -101 "New display name"'), bot)
    assert data["source_names"]["-101"] == "New display name" and data["source_names_revision"] == 1
    assert data["sources"] == before_sources
    assert [row["action"] for row in mutations] == ["set_source_name"]
    assert "setup" not in commands.extension.authority.verify(mutations[0]["context_auth"], operator=True)
    for text in ('/aqla_hinzufuegen -103', '/aqla_entfernen -101'):
        assert await commands.intercept(update(text), bot)
    assert data["sources"] == before_sources and len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"owner": "999"}, {"forwarded": True}, {"edited": True}, {"via_bot": True}, {"chat_type": "group"}, {"bot": True}])
async def test_native_cosmetic_name_still_requires_actual_private_owner(controls, kwargs):
    commands, bot, data, mutations, *_ = controls
    commands.extension.setup.lease = None
    before = copy.deepcopy(data)
    assert await commands.intercept(update('/aqla_kanalname -101 "Untrusted name"', **kwargs), bot)
    assert data == before and not mutations and not bot.sent


@pytest.mark.asyncio
async def test_native_cosmetic_name_uses_actual_mcp_when_setup_is_unconfigured(controls, setup):
    from aqla_hyperliquid_mcp.server import close_server, create_server

    commands, bot, *_ = controls
    settings, state, fake, exchange, _ = setup
    settings.telegram_owner_id = commands.extension.owner
    commands.extension.setup.lease = commands.extension.setup.verifier = None
    server = create_server(settings, state=state, exchange=exchange)
    commands.extension.authority = server.aqla_authority
    calls = []

    async def actual_mcp(_server, tool, arguments):
        calls.append((tool, arguments))
        result = await server.call_tool(tool, arguments)
        return result.structured_content or json.loads(result.content[0].text)

    commands.extension.mcp_call = actual_mcp
    try:
        assert not server._aqla_setup_gate.configured
        before = state.snapshot()
        assert await commands.intercept(update('/aqla_kanalname -101 "New display name"'), bot)
        after = state.snapshot()
        assert after["source_names"]["-101"] == "New display name"
        assert after["source_names_revision"] == before["source_names_revision"] + 1
        assert after["sources"] == before["sources"] and after["configuration_revision"] == before["configuration_revision"]
        control_args = next(arguments for tool, arguments in calls if tool == "control")
        assert "setup" not in server.aqla_authority.verify(control_args["context_auth"], operator=True)
        assert not after["trades"] and not after["plans"] and not after["operations"] and not fake.actions
    finally:
        await close_server(server)


def button(bot, label):
    keyboard = bot.sent[-1]["reply_markup"]["inline_keyboard"]
    return next(item["callback_data"] for row in keyboard for item in row if item["text"] == label)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,key,value", [
    ('/aqla_parameter standard leverage 5', "leverage", 5),
    ('/aqla_parameter Standard risk_percent 0,75', "risk_percent", "0.75"),
    ('/aqla_parameter standard allowed_updates ["move_stop", "close"]', "allowed_updates", ["move_stop", "close"]),
    ('/aqla_parameter standard fallback_take_profit_percentages [1, 2]', "fallback_take_profit_percentages", ["1", "2"]),
    ('/aqla_parameter standard fallback_stop_loss_unit roe_percent', "fallback_stop_loss_unit", "roe_percent"),
    ('/aqla_parameter standard fallback_take_profit_unit roe_percent', "fallback_take_profit_unit", "roe_percent"),
    ('/aqla_parameter standard fallback_take_profit_percentages ["1,5", "2,5"]', "fallback_take_profit_percentages", ["1.5", "2.5"]),
    ('/aqla_parameter standard allow_stop_widening true', "allow_stop_widening", True),
    ('/aqla_parameter standard margin_mode cross', "margin_mode", "cross"),
    ('/aqla_parameter standard break_even_after_tp null', "break_even_after_tp", None),
])
async def test_direct_parameter_complete_schema_preserves_other_values(controls, text, key, value):
    commands, bot, data, mutations, *_ = controls
    before = copy.deepcopy(data["profiles"]["standard"]["parameters"])
    assert await commands.intercept(update(text), bot)
    assert len(mutations) == 1
    after = data["profiles"]["standard"]["parameters"]
    assert after[key] == value
    assert {name: val for name, val in after.items() if name != key} == {name: val for name, val in before.items() if name != key}
    assert "Gespeichert:" in bot.sent[-1]["text"] and "→" in bot.sent[-1]["text"]
    assert commands.dialog is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,label", [("fallback_stop_loss_percent", "% Marginverlust"),
                                         ("fallback_take_profit_percentages", "% ROE (JSON-Liste)")])
async def test_guided_backup_units_and_original_margin_labels(controls, field, label):
    commands, bot, data, mutations, *_ = controls
    await commands.intercept(update("/aqla_parameter standard " + field.replace("percentages", "unit").replace("percent", "unit")), bot)
    token = button(bot, "% ROE (ursprüngliche Margin)")
    await commands.intercept(callback(token), bot)
    assert len(mutations) == 1
    await commands.intercept(update("/aqla_parameter standard " + field), bot)
    assert "Einheit: " + label in bot.sent[-1]["text"]
    assert "ursprüngliche Positionsmargin vor Gebühren und Funding" in bot.sent[-1]["text"]
    assert data["profiles"]["standard"]["parameters"][field.replace("percentages", "unit").replace("percent", "unit")] == "roe_percent"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["entry", "tp", "beide"])
async def test_direct_independent_maker(controls, target):
    commands, bot, data, mutations, *_ = controls
    await commands.intercept(update(f"/aqla_maker standard {target} maker_only"), bot)
    parameters = Parameters.model_validate(data["profiles"]["standard"]["parameters"])
    assert parameters.entry_execution == ("maker_only" if target in ("entry", "beide") else "standard")
    assert parameters.take_profit_execution == ("maker_only" if target in ("tp", "beide") else "standard")
    assert len(mutations) == 1


@pytest.mark.asyncio
async def test_rank_limits_pause_resume_and_read_commands(controls):
    commands, bot, data, mutations, *_ = controls
    for text in ('/aqla_rang "Kanalname" C', '/aqla_limits hyperliquid:testnet "offene Trades" 5',
                 '/aqla_limits aster:mainnet Gesamtrisiko 1,5', '/aqla_pause', '/aqla_fortsetzen'):
        assert await commands.intercept(update(text), bot)
    assert data["source_ranks"]["-101"]["tier"] == "C"
    assert data["execution_accounts"]["hyperliquid:testnet"]["limits"]["max_open_trades"] == 5
    assert data["execution_accounts"]["aster:mainnet"]["limits"]["max_total_risk_percent"] == "1.5"
    assert len(mutations) == 5 and not data["paused"]
    commands.extension.setup.lease = None
    for command in ("aqla_status", "aqla_kanaele", "aqla_profile", "aqla_journal"):
        assert await commands.intercept(update("/" + command), bot)
    rendered = "\n".join(row["text"] for row in bot.sent)
    assert "lokal in SQLite" in rendered and "Nachweise" in rendered and "leverage:" in rendered
    assert "Setup:" in rendered and "letzter Sync:" in rendered
    assert len(mutations) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["aqla_rang", "aqla_parameter", "aqla_maker", "aqla_limits"])
async def test_guided_target_field_value_once(controls, command):
    commands, bot, _, mutations, *_ = controls
    await commands.intercept(update("/" + command), bot)
    deadline = commands.dialog.deadline
    target = "Kanalname · -101" if command == "aqla_rang" else "hyperliquid:testnet" if command == "aqla_limits" else "Standard · standard"
    await commands.intercept(callback(button(bot, target)), bot)
    if command != "aqla_rang":
        label = "entry" if command == "aqla_maker" else "Offene Trades" if command == "aqla_limits" else "sizing_mode"
        await commands.intercept(callback(button(bot, label)), bot)
    value = "C" if command == "aqla_rang" else "maker_only" if command == "aqla_maker" else "risk_percent"
    if command == "aqla_limits":
        assert "Aktuell: 3" in bot.sent[-1]["text"] and "Einheit: Trades" in bot.sent[-1]["text"]
        await commands.intercept(update("5"), bot)
    else:
        token = button(bot, value)
        assert token.startswith(CALLBACK_PREFIX) and token != CALLBACK_PREFIX + value and len(token) == 29
        await commands.intercept(callback(token), bot)
        await commands.intercept(callback(token), bot)
    assert len(mutations) == 1 and commands.dialog is None and deadline == 1300


@pytest.mark.asyncio
async def test_pagination_max_eight_options_and_new_dialog_replaces_tokens(controls):
    commands, bot, _, mutations, *_ = controls
    await commands.intercept(update("/aqla_parameter"), bot)
    old = button(bot, "Standard · standard")
    options = bot.sent[-1]["reply_markup"]["inline_keyboard"]
    assert len(options) == 10  # eight choices + next + cancel
    await commands.intercept(callback(button(bot, "Weiter →")), bot)
    assert commands.dialog.deadline == 1300
    await commands.intercept(update("/aqla_maker"), bot)
    await commands.intercept(callback(old), bot)
    assert commands.dialog.target is None and not mutations


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"actor": "999"}, {"chat": "999"}, {"chat_type": "group"}, {"bot": True}])
async def test_callbacks_use_actual_actor_and_private_chat(controls, kwargs):
    commands, bot, _, mutations, *_ = controls
    await commands.intercept(update('/aqla_maker standard entry'), bot)
    token = button(bot, "maker_only")
    await commands.intercept(callback(token, **kwargs), bot)
    assert not mutations and token in commands.dialog.tokens
    await commands.intercept(callback(token), bot)
    assert len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ['/aqla_parameter standard leverage 101', '/aqla_parameter standard leverage 1.5',
    '/aqla_maker standard entry taker', '/aqla_parameter standard allowed_updates ["arbitrary"]',
    '/aqla_parameter standard take_profit_allocation custom', '/aqla_limits hyperliquid:testnet Gesamtrisiko 101'])
async def test_invalid_values_never_mutate(controls, text):
    commands, bot, data, mutations, *_ = controls
    before = copy.deepcopy(data)
    assert await commands.intercept(update(text), bot)
    assert not mutations and data == before


@pytest.mark.asyncio
async def test_ambiguous_exact_names_guide_and_stale_revision_redisplays(controls):
    commands, bot, data, mutations, *_ = controls
    data["profiles"]["p0"]["name"] = "Standard"
    await commands.intercept(update('/aqla_parameter Standard leverage 5'), bot)
    assert not mutations and commands.dialog.target is None and "mehrdeutig" in bot.sent[-1]["text"]
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    data["configuration_revision"] += 1
    data["profiles"]["standard"]["parameters"]["leverage"] = 4
    await commands.intercept(update('5'), bot)
    assert not mutations and "Aktuell: 4" in bot.sent[-1]["text"] and commands.dialog.snapshot["configuration_revision"] == 8
    await commands.intercept(update('5'), bot)
    assert len(mutations) == 1 and data["profiles"]["standard"]["parameters"]["leverage"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "session"])
async def test_expire_or_session_change_while_status_await_prevents_commit(controls, change):
    commands, bot, _, mutations, now, current, hook = controls
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    async def alter():
        if change == "expiry":
            now[0] = 1300
        else:
            from aqla_agent.setup import SetupBinding
            current[0] = SetupBinding("12345", "12345", "synthetic-gateway", "new-session")
    hook[0] = alter
    assert await commands.intercept(update('5'), bot)
    assert not mutations and commands.dialog is None


@pytest.mark.asyncio
async def test_expiration_cancel_setup_reset_and_typed_native_commands(controls):
    commands, bot, _, mutations, now, *_ = controls
    for command in ("/setup", "/new", "/reset", "/stop", "/restart", "/help", "/commands 2"):
        await commands.intercept(update('/aqla_parameter standard leverage'), bot)
        assert await commands.intercept(update(command), bot) is False
        assert commands.dialog is None
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    assert await commands.intercept(update('/cancel'), bot)
    assert commands.dialog is None
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    now[0] = 1300
    assert await commands.intercept(update('5'), bot)
    assert not mutations and commands.dialog is None
    assert await commands.intercept(update('/aqla_pause'), bot)
    assert len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"owner": "999"}, {"forwarded": True}, {"edited": True}, {"via_bot": True}, {"chat_type": "group"}])
async def test_unauthorized_commands_values_never_mutate(controls, kwargs):
    commands, bot, _, mutations, *_ = controls
    for text in ('/aqla_pause', '/aqla_parameter standard leverage 5', '/aqla_fortsetzen', '/aqla_status'):
        assert await commands.intercept(update(text, **kwargs), bot)
    assert not mutations and not bot.sent


def test_every_command_registered_with_failclosed_fallback():
    registered = {}
    ctx = SimpleNamespace(register_command=lambda name, fn, **meta: registered.update({name: (fn, meta)}))
    register_commands(ctx)
    assert set(registered) == {name.replace('_', '-') for name in COMMANDS}
    for fn, meta in registered.values():
        assert "nichts geändert" in fn("synthetic-private-value")
        assert not meta['args_hint'].startswith('<')
        assert 'synthetic-private-value' not in json.dumps(meta)


@pytest.mark.asyncio
async def test_pause_does_not_wait_for_status_and_pending_dialog_is_cancelled(controls):
    commands, bot, data, mutations, _, _, hook = controls
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    async def unavailable_status():
        raise AssertionError("Pause must not read slow status before stopping entries")
    hook[0] = unavailable_status
    await commands.intercept(update('/aqla_pause'), bot)
    assert data['paused'] and len(mutations) == 1 and commands.dialog is None
    assert hook[0] is unavailable_status


@pytest.mark.asyncio
async def test_pause_bypasses_busy_dialog_serial_lock_and_invalidates_pending_value(controls):
    commands, bot, data, mutations, _, _, hook = controls
    await commands.intercept(update('/aqla_parameter standard leverage'), bot)
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_status():
        entered.set()
        await release.wait()
    hook[0] = slow_status
    pending = asyncio.create_task(commands.intercept(update('5'), bot))
    await entered.wait()
    await asyncio.wait_for(commands.intercept(update('/aqla_pause'), bot), 1)
    assert data['paused'] and commands.dialog is None and len(mutations) == 1
    release.set()
    await pending
    assert len(mutations) == 1 and data['profiles']['standard']['parameters']['leverage'] == 2


@pytest.mark.asyncio
async def test_wrong_bot_suffix_never_starts_or_mutates_and_current_bot_works(controls):
    commands, bot, _, mutations, *_ = controls
    bot.username = "synthetic_bot"
    assert await commands.intercept(update('/aqla_parameter@otherBot standard leverage 5'), bot)
    assert commands.dialog is None and not mutations and not bot.sent
    assert await commands.intercept(update('/aqla_parameter@synthetic_bot standard leverage 5'), bot)
    assert len(mutations) == 1


@pytest.mark.asyncio
async def test_native_formatter_send_owns_markup_and_plain_chunks_use_utf16(controls):
    commands, bot, *_ = controls
    text = '**Status**\n' + ('😀' * 2400) + '\n' + r'Literal \_*[]'
    await commands.reply(bot, text, markdown=True)
    assert bot.sent[-1] == {'text': text, 'native_send': True}
    bot.sent.clear()
    await commands.reply(bot, text)
    assert ''.join(row['text'] for row in bot.sent) == text
    assert all(len(row['text'].encode('utf-16-le')) // 2 <= 3500 and row['parse_mode'] is None for row in bot.sent)


@pytest.mark.asyncio
async def test_legacy_status_names_hermes_deepseek_tools_and_labels_old_jobs_historical(controls):
    commands, bot, data, mutations, *_ = controls
    data["source_processing"] = {"mode": "legacy", "historical_jobs": {"applying": 2, "done": 10},
        "decision": {"model": "inception/mercury-decide:free", "validated": True, "permissions": {"channel": ["new_signal"]}}}
    assert await commands.intercept(update("/aqla_status"), bot)
    text = "\n".join(row["text"] for row in bot.sent)
    assert "Hermes + DeepSeek + Tools" in text and "Historische Quellenjobs:" in text
    assert "Mercury" not in text and "mercury" not in text and "kalibriert" not in text and "Decision-Freigabe" not in text
    assert not mutations
