from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from aqla_agent.install import DEFAULT_MODEL, prepare
from aqla_agent.runtime_controls import RuntimeControls
from aqla_hyperliquid_mcp.models import AqlaError


@pytest.fixture
def runtime(tmp_path):
    root = Path(__file__).resolve().parents[1]
    target = prepare(root, tmp_path / "runtime", "12345", DEFAULT_MODEL, "https://opencode.ai/zen/go/v1")
    calls = []
    async def mcp(*args):
        calls.append(args)
        return {"reloaded": True}
    extension = SimpleNamespace(config_path=target / "config.yaml", master_path=target / "hermes-home/skills/aqla-master/SKILL.md",
        setup=SimpleNamespace(claims=lambda: {"identity": "approved"}), authority=SimpleNamespace(key=b"synthetic-native-token"), server="aqla", mcp_call=mcp)
    return RuntimeControls(extension), calls


@pytest.mark.asyncio
async def test_technical_change_preserves_credentials_and_reloads_exact_allowed_settings(runtime):
    control, calls = runtime
    before = yaml.safe_load(control.config.read_text())
    result = await control.change("replay_enabled", False, control.read()["revision"])
    after = yaml.safe_load(control.config.read_text())
    assert result["values"]["replay_enabled"] is False
    assert {key: value for key, value in after.items() if key != "waverider_replay"} == {key: value for key, value in before.items() if key != "waverider_replay"}
    assert len(calls) == 1 and set(calls[0][2]["technical"]) == {"express", "waverider_replay"}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("reasoning", "max"), ("model", "unqualified-model"), ("caveman", False), ("source_processing", "hybrid"), ("source_processing", "shadow")])
async def test_unqualified_runtime_never_changes_active_config(runtime, field, value):
    control, calls = runtime
    original = control.config.read_bytes(), control.hermes.read_bytes()
    with pytest.raises(AqlaError):
        await control.change(field, value, control.read()["revision"])
    assert (control.config.read_bytes(), control.hermes.read_bytes()) == original and not calls


@pytest.mark.asyncio
async def test_expired_setup_or_stale_config_blocks_technical_writes(runtime):
    control, calls = runtime
    with pytest.raises(AqlaError):
        await control.change("replay_enabled", False, "stale")
    control.extension.setup.claims = lambda: None
    with pytest.raises(AqlaError):
        await control.change("replay_enabled", False, control.read()["revision"])
    assert not calls


@pytest.mark.asyncio
async def test_saved_technical_value_is_explicitly_pending_when_reload_reply_is_lost(runtime):
    control, _ = runtime
    async def unavailable(*_):
        raise AqlaError("synthetic unavailable")
    control.extension.mcp_call = unavailable
    value = await control.change("replay_enabled", False, control.read()["revision"])
    assert value["values"]["replay_enabled"] is False and value["apply_status"] == "pending"


def test_configuration_migration_dry_run_preserves_every_nonretired_setting(runtime):
    from aqla_agent.migration import configuration_changes, migrate
    control, _ = runtime
    original = yaml.safe_load(control.config.read_text())
    original["google_sheets"] = {"enabled": True, "credentials_file": "secrets/old-google.secret", "spreadsheet_id": "old-only"}
    control.config.write_text(yaml.safe_dump(original), encoding="utf-8")
    before = control.config.read_bytes(), control.hermes.read_bytes()
    changes = configuration_changes(control.config.parent)
    assert changes["after"][0] == {key: value for key, value in original.items() if key != "google_sheets"}
    assert not migrate(control.config.parent)["financial_database_modified"]
    assert (control.config.read_bytes(), control.hermes.read_bytes()) == before
