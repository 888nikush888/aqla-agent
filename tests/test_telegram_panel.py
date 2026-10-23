import copy
import json
from types import SimpleNamespace

import pytest
from test_native_commands import controls  # noqa: F401
from test_native_setup import update

from aqla_agent.pipelines import Parameters
from aqla_agent.telegram_menu import GROUPS, HOME, PREFIX, TelegramMenu, input_value


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
    async def answer():
        pass
    message = SimpleNamespace(message_id=message_id or menu.message_id, chat=SimpleNamespace(id="12345", type=chat_type))
    return SimpleNamespace(callback_query=SimpleNamespace(data=token, from_user=SimpleNamespace(id=owner, is_bot=False), message=message, answer=answer))


def choose(bot, label):
    source = (bot.edits or bot.sent)[-1]
    return next(button["callback_data"] for row in source["reply_markup"]["inline_keyboard"] for button in row if button["text"] == label)


def test_every_profile_parameter_is_reachable_once():
    fields = [field for values in GROUPS.values() for field in values]
    assert len(fields) == len(set(fields))
    assert set(fields) == set(Parameters.model_fields)
    assert len(HOME) == 8


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
@pytest.mark.parametrize("owner,chat_type", [("stranger", "private"), ("12345", "group")])
async def test_menu_authenticates_actual_callback_actor(panel, owner, chat_type):
    menu, bot, _, changes, *_ = panel
    await menu.home(bot)
    token = choose(bot, "⚙️ Profile & Handelsregeln")
    await menu.intercept(callback(menu, token, owner=owner, chat_type=chat_type), bot)
    assert not bot.edits and not changes


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
