import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_native_commands import controls  # noqa: F401
from test_native_setup import update

from aqla_agent.pipelines import Parameters
from aqla_agent.telegram_menu import GROUPS, HOME, PREFIX, TelegramMenu, input_value, setting_value


@pytest.fixture
def panel(controls, tmp_path):  # noqa: F811 -- shared native fixture
    commands, bot, data, mutations, now, binding, hook = controls
    commands.extension.config_path = tmp_path / "config.yaml"
    menu = TelegramMenu(commands)
    commands.menu = menu
    bot.edits = []
    async def edit(**kwargs):
        bot.edits.append(kwargs)
        return True
    bot.edit_message_text = edit
    return menu, bot, data, mutations, now, binding


def callback(menu, token, *, owner="12345", chat_type="private", message_id=None):
    answers = []
    async def answer(**kwargs):
        answers.append(kwargs)
    message = SimpleNamespace(message_id=message_id or menu.message_id, chat=SimpleNamespace(id="12345", type=chat_type))
    return SimpleNamespace(callback_query=SimpleNamespace(data=token, from_user=SimpleNamespace(id=owner, is_bot=False), message=message, answer=answer), answers=answers)


def choose(bot, label):
    source = (bot.edits or bot.sent)[-1]
    return next(button["callback_data"] for row in source["reply_markup"]["inline_keyboard"] for button in row if button["text"] == label)


def test_every_profile_parameter_is_reachable_once():
    fields = [field for values in GROUPS.values() for field in values]
    assert len(fields) == len(set(fields))
    assert set(fields) == set(Parameters.model_fields)
    assert len(HOME) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("field", Parameters.model_fields)
async def test_every_profile_setting_round_trips_through_its_input(panel, field):
    menu, bot, data, changes, *_ = panel
    before = Parameters.model_validate(data["profiles"]["standard"]["parameters"])
    current = getattr(before, field)
    text = json.dumps(current, default=str) if current is None or isinstance(current, (list, bool)) else str(current)
    await menu.field(bot, "profile", "standard", field)
    await menu.text_input(bot, text)
    assert Parameters.model_validate(data["profiles"]["standard"]["parameters"]) == before
    assert len(changes) == 1


@pytest.mark.asyncio
async def test_menu_navigates_one_message_and_rejects_old_buttons(panel):
    menu, bot, data, changes, *_ = panel
    assert await menu.intercept(update("/aqla"), bot)
    token = choose(bot, "⚙️ Profile & Handelsregeln")
    assert await menu.intercept(callback(menu, token), bot)
    assert len(bot.sent) == 1 and len(bot.edits) == 1
    before = len(bot.edits)
    await menu.intercept(callback(menu, token), bot)
    assert len(bot.edits) == before and not changes
    assert all(len(button["callback_data"].encode()) <= 64 for row in bot.edits[-1]["reply_markup"]["inline_keyboard"] for button in row)
    assert PREFIX in json.dumps(bot.edits)


@pytest.mark.asyncio
async def test_explicit_open_is_visible_at_chat_bottom_and_retires_only_the_previous_panel(panel):
    menu, bot, _, changes, *_ = panel
    await menu.intercept(update("/aqla"), bot)
    previous = menu.message_id
    old_token = choose(bot, "⚙️ Profile & Handelsregeln")
    await menu.intercept(update("/aqla"), bot)
    assert len(bot.sent) == 2 and not bot.edits
    assert bot.deleted == [{"chat_id": "12345", "message_id": previous}]
    assert menu.message_id != previous
    await menu.intercept(callback(menu, old_token, message_id=previous), bot)
    assert not bot.edits and not changes
    await menu.intercept(callback(menu, choose(bot, "⚙️ Profile & Handelsregeln")), bot)
    assert len(bot.sent) == 2 and len(bot.edits) == 1


@pytest.mark.asyncio
async def test_new_panel_ack_precedes_deletion_and_failed_cleanup_keeps_one_active_receipt(panel):
    menu, bot, _, changes, *_ = panel
    await menu.intercept(update("/aqla"), bot)
    previous = menu.message_id
    async def failed_delete(**kwargs):
        assert menu.message_id != previous and len(bot.sent) == 2
        raise TimeoutError("synthetic deletion unavailable")
    bot.delete_message = failed_delete
    await menu.intercept(update("/aqla"), bot)
    assert json.loads(menu.receipt_file.read_text())["message_id"] == menu.message_id
    assert len(bot.sent) == 2 and not menu.fresh and not changes


@pytest.mark.asyncio
async def test_failed_reopen_does_not_delete_the_existing_panel(panel):
    menu, bot, _, changes, *_ = panel
    await menu.home(bot)
    previous = menu.message_id
    async def unavailable(**kwargs):
        raise TimeoutError("synthetic send unavailable")
    bot.send_message = unavailable
    menu.fresh = True
    with pytest.raises(TimeoutError):
        await menu.home(bot)
    assert menu.message_id == previous and not bot.deleted and not changes


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,chat_type", [("stranger", "private"), ("12345", "group")])
async def test_menu_authenticates_actual_callback_actor(panel, owner, chat_type):
    menu, bot, _, changes, *_ = panel
    await menu.home(bot)
    token = choose(bot, "⚙️ Profile & Handelsregeln")
    await menu.intercept(callback(menu, token, owner=owner, chat_type=chat_type), bot)
    assert not bot.edits and not changes


@pytest.mark.asyncio
async def test_expired_menu_explains_reopen_without_extending_authority_or_writing(panel):
    menu, bot, _, changes, now, *_ = panel
    await menu.home(bot)
    current = callback(menu, choose(bot, "⚙️ Profile & Handelsregeln"))
    now[0] = menu.deadline + 1
    await menu.intercept(current, bot)
    assert current.answers == [{"text": "Menü abgelaufen oder geändert. Mit /aqla neu öffnen."}]
    assert not bot.edits and not changes and not menu.live()


@pytest.mark.asyncio
async def test_profile_value_commits_immediately_preserving_other_fields(panel):
    menu, bot, data, changes, *_ = panel
    before = copy.deepcopy(data["profiles"]["standard"]["parameters"])
    await menu.field(bot, "profile", "standard", "risk_percent")
    await menu.text_input(bot, "0,75")
    assert len(changes) == 1 and changes[0]["profile"]["parameters"]["risk_percent"] == "0.75"
    after = data["profiles"]["standard"]["parameters"]
    assert {key: value for key, value in after.items() if key != "risk_percent"} == {key: value for key, value in before.items() if key != "risk_percent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", Parameters.model_fields)
async def test_all_profile_values_are_readable_without_setup_but_writes_stay_protected(panel, field):
    from aqla_hyperliquid_mcp.models import AqlaError
    menu, bot, _, changes, *_ = panel
    menu.extension.setup.lease = None
    await menu.field(bot, "profile", "standard", field)
    assert "Aktuell:" in (bot.edits or bot.sent)[-1]["text"]
    with pytest.raises(AqlaError, match="Setup"):
        await menu.commit_field(bot, "1")
    assert not changes


def test_settings_are_readable_without_python_representations():
    assert setting_value([Decimal("20"), Decimal("0.25")]) == "20; 0,25"
    assert setting_value(None) == setting_value(False) == "aus"
    assert setting_value(True) == "an" and setting_value([]) == "keine"


@pytest.mark.asyncio
async def test_dashboard_rounds_only_monetary_display_and_keeps_missing_values_unknown(panel):
    menu, bot, _, changes, *_ = panel
    amount = "12.34567800000000000000000001"
    async def dashboard(*args, **kwargs):
        return {"net_pnl": {"value": amount, "known": 3, "total": 4}, "paused": False,
                "resolved_period": "all", "fees": {"value": "0.123456"}, "funding": {"value": None},
                "open_trades": 1, "source_queue": {}}
    menu.rpc = dashboard
    await menu.dashboard(bot)
    text = (bot.edits or bot.sent)[-1]["text"]
    assert "12,345678 USDC" in text and "Funding: nicht bestätigt" in text
    assert amount == "12.34567800000000000000000001" and not changes


@pytest.mark.asyncio
async def test_monthly_menu_formats_currency_and_percentages_without_rewriting_accounting(panel):
    menu, bot, _, changes, *_ = panel
    summary = {"trades": 6, "complete": 3, "excluded": 3, "net_by_currency": {"USDC": "3.456789"},
               "win_rate_percent": "66.66666666666666666666666667", "roe_sum_label": "Summe der Trade-Renditen",
               "roe_sum_percent": "21.123456789123456789"}
    before = copy.deepcopy(summary)
    async def monthly(*args, **kwargs):
        return {"summary": summary}
    menu.rpc = monthly
    await menu.run(bot, "month", "2026-10")
    text = (bot.edits or bot.sent)[-1]["text"]
    assert "3,456789 USDC" in text and "66,67 %" in text and "21,12 %" in text
    assert summary == before and not changes


@pytest.mark.asyncio
async def test_evidence_distinguishes_replaced_orders_and_operation_phases(panel):
    menu, bot, _, changes, *_ = panel
    async def evidence(_server, tool, args):
        assert tool == "journal_read" and args["trade_id"] == "own-trade"
        return {"rows": [{"Art": "sl", "Order-ID": "original", "Status Börse": "canceled", "Ersetzt": True},
                         {"Art": "sl", "Order-ID": "current", "Status Börse": "open", "Ersetzt": False},
                         {"Art": "order", "Phase": "unknown"}], "next_offset": None}
    menu.extension.mcp_call = evidence
    await menu.run(bot, "evidence_table", "own-trade", "Orders")
    text = (bot.edits or bot.sent)[-1]["text"]
    assert "Status: canceled · ersetzt" in text and "Status: open" in text
    await menu.run(bot, "evidence_table", "own-trade", "Vorgänge")
    assert "Phase: unknown" in (bot.edits or bot.sent)[-1]["text"]
    assert not changes


@pytest.mark.asyncio
async def test_unexpected_text_input_error_stays_native_and_does_not_leak_details(panel):
    menu, bot, _, changes, *_ = panel
    await menu.run(bot, "prompt", "filter_symbol", "")
    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic-private-diagnostic")
    menu.rpc = unavailable
    assert await menu.intercept(update("ETH"), bot)
    text = (bot.edits or bot.sent)[-1]["text"]
    assert "vorübergehend" in text and "synthetic-private" not in text
    assert menu.pending is None and not changes


@pytest.mark.asyncio
async def test_stale_revision_expired_setup_and_invalid_combination_do_not_write(panel):
    menu, bot, data, changes, now, _ = panel
    await menu.field(bot, "profile", "standard", "sizing_mode")
    with pytest.raises(ValueError):
        await menu.commit_field(bot, "fixed_notional")
    assert not changes
    await menu.field(bot, "profile", "standard", "risk_percent")
    now[0] = 1300.0
    with pytest.raises(Exception, match="abgelaufen|Setup"):
        await menu.commit_field(bot, "1")
    assert not changes


def test_native_inputs_preserve_decimal_comma_and_typed_tp_arrays():
    schema = Parameters.model_json_schema()["properties"]
    assert input_value("50; 25; 25", schema["take_profit_weights"]) == ["50", "25", "25"]
    assert input_value("0,5", schema["risk_percent"]) == "0.5"
    assert input_value("1", schema["waverider_until_tp"]) == 1
    assert input_value("last", schema["waverider_until_tp"]) == "last"


@pytest.mark.asyncio
async def test_unconfigured_profile_requires_explicit_template_acceptance(panel):
    menu, bot, data, changes, *_ = panel
    data["profiles"]["standard"]["parameters"] = None
    await menu.profile(bot, "standard")
    assert "noch nicht eingerichtet" in (bot.edits or bot.sent)[-1]["text"] and not changes
    token = choose(bot, "Dieses Ausgangsprofil übernehmen")
    await menu.intercept(callback(menu, token), bot)
    assert len(changes) == 1 and data["profiles"]["standard"]["parameters"]["risk_percent"] == "0.5"


@pytest.mark.asyncio
async def test_first_account_limits_require_both_explicit_values(panel):
    menu, bot, data, changes, *_ = panel
    data["execution_accounts"]["hyperliquid:testnet"]["limits"] = None
    await menu.run(bot, "limit_fields", "hyperliquid:testnet")
    with pytest.raises(ValueError, match="Beide Werte"):
        await menu.text_input(bot, "3")
    assert not changes
    await menu.text_input(bot, "3; 2")
    assert len(changes) == 1
    assert data["execution_accounts"]["hyperliquid:testnet"]["limits"] == {"max_open_trades": 3, "max_total_risk_percent": "2"}


@pytest.mark.asyncio
async def test_menu_cosmetic_name_keeps_existing_no_setup_permission(panel):
    menu, bot, data, changes, *_ = panel
    menu.extension.setup.lease = None
    await menu.run(bot, "prompt", "rename_source", "")
    await menu.text_input(bot, '-101 "Neuer Name"')
    assert data["source_names"]["-101"] == "Neuer Name" and len(changes) == 1


@pytest.mark.asyncio
async def test_menu_cancel_revokes_an_unanswered_password_challenge(panel):
    from aqla_agent.native_setup import Challenge
    menu, bot, _, changes, *_ = panel
    menu.extension.setup.challenge = Challenge(menu.extension.setup_binding(), 1300.0, 999)
    await menu.run(bot, "cancel")
    assert menu.extension.setup.challenge is None and menu.extension.setup.lease is None
    assert 999 in menu.extension.setup.old_prompts and not changes


@pytest.mark.asyncio
async def test_removed_source_opened_from_old_selection_is_read_only_history(panel):
    menu, bot, _, changes, *_ = panel
    async def historical(*args, **kwargs):
        return {"sources": {}, "inactive_sources": {"-103": {"active": False, "tier": "C"}}, "history": []}
    menu.rpc = historical
    await menu.run(bot, "source_details", "-103")
    source = (bot.edits or bot.sent)[-1]
    labels = [button["text"] for row in source["reply_markup"]["inline_keyboard"] for button in row]
    assert "nicht mehr aktiv" in source["text"] and "Rang ändern" not in labels and not changes


@pytest.mark.asyncio
async def test_current_source_menu_resolves_alias_rank_and_excludes_retired_rows(panel):
    menu, bot, data, changes, *_ = panel
    data["sources"] = ["@current"]
    data["source_aliases"] = {"@current": "-101", "@retired": "-102"}
    await menu.run(bot, "sources")
    text = (bot.edits or bot.sent)[-1]["text"]
    assert "Kanalname · D" in text and "Zweiter Kanal" not in text
    await menu.run(bot, "source_list")
    assert "Kanalname" in str((bot.edits or bot.sent)[-1]["reply_markup"]) and not changes


@pytest.mark.asyncio
async def test_rank_picker_excludes_inactive_rows_even_from_an_old_status_response(panel):
    menu, bot, data, changes, *_ = panel
    data["sources"] = ["@current"]
    data["source_aliases"] = {"@current": "-101", "@retired": "-102"}
    await menu.commands.start(bot, "aqla_rang", "")
    assert menu.commands.targets(menu.commands.dialog) == [("Kanalname · -101", "-101")]
    assert not changes


@pytest.mark.asyncio
async def test_natural_current_source_request_opens_a_visible_native_reply_without_setup(panel):
    menu, bot, _, changes, *_ = panel
    menu.extension.setup.lease = None
    await menu.home(bot)
    old = menu.message_id
    assert await menu.commands.intercept(update("Zeig mir alle aktuellen Quellkanäle"), bot)
    assert len(bot.sent) == 2 and menu.message_id != old
    assert "Aktuell ausgewählte Kanäle" in bot.sent[-1]["text"]
    assert bot.deleted == [{"chat_id": "12345", "message_id": old}] and not changes


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", ["Abbrechen", "⌂ Hauptmenü"])
async def test_setup_started_from_menu_can_be_cancelled_through_actual_ingress(panel, navigation):
    menu, bot, _, changes, *_ = panel
    await menu.run(bot, "help")
    start = callback(menu, choose(bot, "Setup"))
    assert await menu.intercept(start, bot)
    assert menu.extension.setup.challenge is not None
    current = callback(menu, choose(bot, navigation))
    current.effective_message = current.callback_query.message
    current.effective_message.from_user = SimpleNamespace(id="999", is_bot=True)
    assert not await menu.extension.setup.intercept(current, bot)
    assert await menu.intercept(current, bot)
    assert menu.extension.setup.challenge is None and menu.extension.setup.claims() is None
    assert menu.extension.setup.failures == 0 and not changes
