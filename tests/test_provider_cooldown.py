import sqlite3
import sys
from types import SimpleNamespace

import pytest

from aqla_agent import provider_cooldown as quota
from aqla_hyperliquid_mcp.models import AqlaError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=1000, elapsed=10)
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.elapsed))
    monkeypatch.setattr(quota, "path", lambda: tmp_path / "cache/quota.sqlite3")
    native = SimpleNamespace()
    def arm(agent, reason, reset_at=None):
        if reason != "limited":
            return None
        seconds = reset_at - clock.wall if reset_at else 60
        agent._rate_limited_until = clock.elapsed + seconds
        return seconds
    def restore(agent):
        if agent._fallback_activated and agent._rate_limited_until > clock.elapsed:
            return False
        agent.route = "primary"
        return True
    native._arm_rate_limit_cooldown = arm
    helpers = SimpleNamespace(restore_primary_runtime=restore)
    monkeypatch.setitem(sys.modules,"agent",SimpleNamespace(fallback_cooldown=native,agent_runtime_helpers=helpers))
    monkeypatch.setitem(sys.modules,"agent.error_classifier",SimpleNamespace(FailoverReason=SimpleNamespace(rate_limit="limited")))
    quota.install(lambda target,name,fn: setattr(target,name,fn))
    def agent(key="synthetic-primary-key", model="fixture"):
        value=SimpleNamespace(_primary_runtime={"provider":"custom","model":model,"base_url":"https://synthetic.invalid","api_key":key},
            _fallback_activated=False,_rate_limited_until=0,route="primary",fallback_available=True)
        def activate(**arguments):
            native._arm_rate_limit_cooldown(value,**arguments)
            if value.fallback_available:
                value._fallback_activated=True
                value.route="fallback"
            return value.fallback_available
        value._try_activate_fallback=activate
        return value
    return clock,native,helpers,agent


def test_new_agent_and_changed_monotonic_origin_keep_provider_reset(runtime):
    clock,native,helpers,agent=runtime
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=10000)
    clock.wall,clock.elapsed=1100,2
    fresh=agent()
    helpers.restore_primary_runtime(fresh)
    assert fresh.route=="fallback" and fresh._rate_limited_until==8902
    assert quota.reset_at(fresh)==10000


def test_expired_quota_restores_primary_and_credential_or_model_change_is_independent(runtime):
    clock,native,helpers,agent=runtime
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=2000)
    for value in (agent(key="new-key"),agent(model="other-model")):
        helpers.restore_primary_runtime(value)
        assert value.route=="primary"
    clock.wall,clock.elapsed=2001,5
    value=agent()
    helpers.restore_primary_runtime(value)
    assert value.route=="primary"


def test_blocked_primary_without_configured_fallback_is_not_retried(runtime):
    _,native,helpers,agent=runtime
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=2000)
    value=agent()
    value.fallback_available=False
    with pytest.raises(AqlaError,match="quota remains exhausted"):
        helpers.restore_primary_runtime(value)


def test_storage_contains_only_opaque_route_and_reset_and_corruption_does_not_block_native(runtime):
    _,native,helpers,agent=runtime
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=2000)
    with sqlite3.connect(quota.path()) as connection:
        rows=connection.execute("SELECT route,until FROM cooldowns").fetchall()
    assert len(rows)==1 and len(rows[0][0])==64 and rows[0][1]==2000
    assert b"synthetic-primary-key" not in quota.path().read_bytes()
    quota.path().write_bytes(b"invalid sqlite storage")
    value=agent()
    helpers.restore_primary_runtime(value)
    assert value.route=="primary"


def test_shorter_observation_cannot_erase_existing_provider_quota(runtime):
    _,native,_,agent=runtime
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=10000)
    native._arm_rate_limit_cooldown(agent(),"limited",reset_at=1100)
    assert quota.reset_at(agent())==10000
