import json

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Settings
from aqla_hyperliquid_mcp.state import State


def test_exclusive_writer_and_restart_keeps_rules(setup):
    settings, state, *_ = setup
    with pytest.raises(AqlaError, match="already owns"):
        State(settings)
    state.update(lambda row: row.update(paused=True, policy_revision=7))
    state.close()
    with State(settings) as restored:
        assert restored.snapshot()["paused"] is True
        assert restored.snapshot()["policy_revision"] == 7


def test_corrupt_state_is_not_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken")
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=path)
    with pytest.raises(json.JSONDecodeError):
        State(settings)
    assert path.read_text() == "{broken"


def test_write_failure_rolls_back_memory(setup, monkeypatch):
    _, state, *_ = setup
    old = state.snapshot()

    def fail(_):
        raise OSError("Disk full")

    monkeypatch.setattr(state, "_write", fail)
    with pytest.raises(OSError):
        state.update(lambda row: row.update(paused=True))
    assert state.snapshot() == old
