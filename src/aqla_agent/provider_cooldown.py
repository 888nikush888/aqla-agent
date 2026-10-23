"""Carry native provider quota resets across gateway agents and process restarts."""
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, digest

LOG = logging.getLogger(__name__)
REPLAY = ContextVar("aqla_provider_cooldown_replay", default=False)


def identity(agent):
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    runtime = agent._primary_runtime or {}
    return digest({key: runtime.get(key, "") for key in ("provider", "base_url", "model", "api_key")})


def path():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "cache" / "aqla-provider-cooldown.sqlite3"


@contextmanager
def storage():
    target = path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink() or target.parent.is_symlink():
        raise OSError("Provider cooldown storage link forbidden")
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        pass
    os.chmod(target, 0o600)
    connection = sqlite3.connect(target, timeout=1)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS cooldowns (route TEXT PRIMARY KEY, until REAL NOT NULL)")
            yield connection
    finally:
        connection.close()


def remember(agent, seconds):
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return
    try:
        with storage() as connection:
            connection.execute("DELETE FROM cooldowns WHERE until <= ?", (time.time(),))
            connection.execute("INSERT INTO cooldowns VALUES (?,?) ON CONFLICT(route) DO UPDATE SET until=MAX(until,excluded.until)",
                (identity(agent), time.time() + seconds))
    except (OSError, sqlite3.Error):
        LOG.warning("aqla provider cooldown persistence unavailable; native fallback remains active")


def reset_at(agent):
    try:
        with storage() as connection:
            row = connection.execute("SELECT until FROM cooldowns WHERE route=?", (identity(agent),)).fetchone()
        return row[0] if row and isinstance(row[0], (int, float)) and math.isfinite(row[0]) and row[0] > time.time() else None
    except (OSError, sqlite3.Error):
        LOG.warning("aqla provider cooldown read unavailable; native provider recovery remains active")
        return None


def install(replace):
    from agent import agent_runtime_helpers, chat_completion_helpers, fallback_cooldown
    from agent.error_classifier import FailoverReason

    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    arm = fallback_cooldown._arm_rate_limit_cooldown
    restore = agent_runtime_helpers.restore_primary_runtime
    # Pinned native integration interface; covered by native contract and integration checks.
    # skipcq: PYL-W0212
    buffer = chat_completion_helpers._buffer_fallback_notice

    def buffer_notice(agent, notice):
        # A new agent carrying the same persisted outage is not a new provider switch.
        if not REPLAY.get():
            return buffer(agent, notice)

    # The pinned native callback keyword and public reset_at helper are distinct APIs.
    # skipcq: PYL-W0621
    def arm_shared(agent, reason, reset_at=None):
        seconds = arm(agent, reason, reset_at=reset_at)
        if not REPLAY.get():
            remember(agent, seconds)
        return seconds

    def restore_shared(agent):
        until = reset_at(agent)
        if until:
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            agent._rate_limited_until = max(getattr(agent, "_rate_limited_until", 0), time.monotonic() + until - time.time())
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            if not agent._fallback_activated:
                token = REPLAY.set(True)
                try:
                    # Pinned native integration interface; covered by native contract and integration checks.
                    # skipcq: PYL-W0212
                    activated = agent._try_activate_fallback(reason=FailoverReason.rate_limit, reset_at=until)
                finally:
                    REPLAY.reset(token)
                if not activated:
                    raise AqlaError("Primary provider quota remains exhausted and no configured fallback is available")
        return restore(agent)

    replace(fallback_cooldown, "_arm_rate_limit_cooldown", arm_shared)
    replace(agent_runtime_helpers, "restore_primary_runtime", restore_shared)
    replace(chat_completion_helpers, "_buffer_fallback_notice", buffer_notice)
