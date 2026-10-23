"""Deterministic same-direction waves; live orders require signed authority."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache, partial
from pathlib import Path

from aqla_hyperliquid_mcp.models import (
    STOP_UPDATE_ACTIVE,
    TERMINAL_ORDER,
    AqlaError,
    ReplaySettings,
    TradePlan,
    digest,
    now_ms,
    require_first,
)
from aqla_hyperliquid_mcp.precision import price_step
from aqla_hyperliquid_mcp.rules import quantity_grid, slippage_price

from .accounting import entry_quantity_evidence, position_basis
from .entry import passive_entry_price
from .observations import TemporaryObservation, read_observation

VERSION = "waverider-v1"
COST_MODEL = "fees-funding-profile-v2"
MIN_CASES, MIN_WAVES = 30, 10


@lru_cache(maxsize=1)
def software_hash():
    root = Path(__file__).parent
    files = [root / name for name in ("waverider.py", "waverider_replay.py", "managed_entries.py", "trading.py", "entry.py", "inventory_stream.py",
        "entry_watch.py", "pipelines.py", "monthly.py", "percent_targets.py",
        "account_work.py", "accounting.py", "manual_accounting.py", "source_text.py", "management_sequences.py", "operator_management.py", "interpretation_contract.py", "source_candidates.py", "semantic_contract.py", "conflict_review.py", "target_recovery.py")]
    files += [root.parent / "aqla_hyperliquid_mcp" / name for name in ("models.py", "rules.py", "precision.py", "api.py", "streams.py", "state.py", "storage.py")]
    return digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def profile_hash(policy, base_parameters=None):
    from .pipelines import Parameters
    profile = Parameters.model_validate(base_parameters) if base_parameters is not None else Parameters.from_policy(policy)
    values = profile.model_dump()
    values.pop("management_mode", None)  # Standard observations qualify only the matching Waverider hypothesis.
    return digest(values)


def exit_pool_sizes(row, core, flex, rights):
    size = Decimal(row["sz"])
    if size > core + flex or row["kind"] in ("waverider", "waverider_retirement") and size > flex:
        raise AqlaError("Waverider reduction exceeds its proved flexible inventory")
    if row["kind"] in ("waverider", "waverider_retirement"):
        core_exit, flex_exit = Decimal(0), size
    elif row.get("exit_pool") == "core":
        if size > core or core <= 0:
            raise AqlaError("Source core target exceeds its reserved core inventory")
        rights *= 1 - size / core
        core_exit, flex_exit = size, Decimal(0)
    else:
        ratio = size / (core + flex)
        core_exit = core * ratio
        flex_exit = size - core_exit
        rights *= 1 - ratio
    return core_exit, flex_exit, rights


def pools(trade, percent):
    """Core and flex cost bases stay distinct, including partially filled cycles."""
    core, flex, core_cost, flex_cost, rights, losses, fees = (Decimal(0) for _ in range(7))
    direction = Decimal(1) if trade["plan"]["side"] == "long" else Decimal(-1)
    for row in sorted(trade.get("fill_events", {}).values(), key=lambda row: (row["time"], int(str(row.get("tid", 0))) if str(row.get("tid", 0)).isdigit() else str(row.get("tid", "")))):
        size, price = Decimal(row["sz"]), Decimal(row["px"])
        fees += max(Decimal(0), Decimal(row["fee"])) if row.get("fee") is not None else Decimal(0)
        if row["kind"] == "entry":
            added = size if row.get("entry_role") == "waverider" else size * percent / 100
            if row.get("entry_role") != "waverider":
                rights += added
            core_cost += (size - added) * price
            flex_cost += added * price
            core, flex = core + size - added, flex + added
        else:
            core_exit, flex_exit, rights = exit_pool_sizes(row, core, flex, rights)
            core_average, flex_average = core_cost / core if core else Decimal(0), flex_cost / flex if flex else Decimal(0)
            losses += max(Decimal(0), -(price - core_average) * core_exit * direction)
            losses += max(Decimal(0), -(price - flex_average) * flex_exit * direction)
            core_cost, flex_cost = core_cost - core_exit * core_average, flex_cost - flex_exit * flex_average
            # Preserve the exact native quantity instead of rounding both remaining shares.
            remaining = core + flex - size
            core -= core_exit
            flex = remaining - core
    funding = sum((max(Decimal(0), -Decimal(row["amount"])) for row in trade.get("funding_events", {}).values()), Decimal(0))
    return {"core": core, "flex": flex, "core_basis": core_cost / core if core else Decimal(0), "flex_basis": flex_cost / flex if flex else Decimal(0),
            "rights": max(Decimal(0), rights), "spent": losses + fees + funding}


def atr(candles, stamp):
    """Wilder ATR over closed, contiguous minutes; no current-candle lookahead."""
    closed = sorted([row for row in candles if row["end"] < stamp], key=lambda row: row["start"])
    if len(closed) < 15 or stamp - closed[-1]["end"] > 120000:
        raise AqlaError("Waverider requires at least 15 fresh closed one-minute candles")
    if any(right["start"] - left["start"] != 60000 for left, right in zip(closed, closed[1:], strict=False)):
        raise AqlaError("Waverider candle history has a gap or duplicate")
    ranges = []
    for previous, row in zip(closed, closed[1:], strict=False):
        high, low, close = Decimal(row["high"]), Decimal(row["low"]), Decimal(previous["close"])
        if not low <= Decimal(row["close"]) <= high or low <= 0:
            raise AqlaError("Waverider candle price structure is invalid")
        ranges.append(max(high - low, abs(high - close), abs(low - close)))
    value = sum(ranges[:14]) / 14
    for item in ranges[14:]:
        value = (value * 13 + item) / 14
    if value <= 0:
        raise AqlaError("Waverider requires observed nonzero volatility")
    return value


# Public pure-state API: callers pass the native price increment as tick=.
# skipcq: PYL-W0621
def advance(state, *, side, price, basis, volatility, cycle_cost, tick, stamp):
    """Pure hysteresis state machine. An instruction is not an execution receipt."""
    result = dict(state)
    sign = Decimal(1) if side == "long" else Decimal(-1)
    value, origin = price * sign, basis * sign
    phase = result.get("phase", "waiting_rise")
    if phase not in ("waiting_rise", "waiting_retrace", "waiting_rebound"):
        return result, None
    if stamp <= result.get("last_observed_at", -1):
        return result, None
    result["last_observed_at"] = stamp
    if "wave_atr" not in result:
        result["wave_atr"] = str(volatility)
    reversal = max(Decimal(result["wave_atr"]) / 2, tick * 2)
    movement = max(Decimal(result["wave_atr"]), cycle_cost * 2)
    extreme = Decimal(result.get("extreme", str(value)))
    if phase == "waiting_rise":
        extreme = max(extreme, value)
        if extreme - origin >= movement:
            phase = "waiting_retrace"
    if phase == "waiting_retrace":
        extreme = max(extreme, value)
        crossed = extreme - value >= reversal and value - origin > cycle_cost
        action = "reduce"
    elif phase == "waiting_rebound":
        extreme = min(extreme, value)
        sale = Decimal(result["sale_price"]) * sign
        crossed = value - extreme >= reversal and sale - value > cycle_cost
        action = "reenter"
    else:
        crossed, action = False, None
    result.update(phase=phase, extreme=str(extreme))
    if crossed:
        result.setdefault("confirmation_since", stamp)
        result["confirmation_count"] = result.get("confirmation_count", 0) + 1
        if result["confirmation_count"] >= 3 and stamp - result["confirmation_since"] >= 2000:
            result.update(phase="pending_" + action, decision_at=stamp)
            return result, action
    else:
        result.pop("confirmation_since", None)
        result.pop("confirmation_count", None)
    return result, None


def comparable_performance(group):
    return (all(Decimal(group[key]).is_finite() for key in ("net_pnl", "baseline_net_pnl", "max_drawdown", "baseline_max_drawdown"))
        and Decimal(group["net_pnl"]) >= Decimal(group["baseline_net_pnl"])
        and Decimal(group["max_drawdown"]) <= Decimal(group["baseline_max_drawdown"]))


def qualification_scope(report, expected_profile, expected_venue, expected_cohort):
    return (report["software_hash"] == software_hash()
        and (expected_profile is None or report["profile_hash"] == expected_profile)
        and (expected_venue is None or expected_venue in report["allowed_venues"])
        and (expected_cohort is None or expected_cohort in report.get("cohorts", {})))


def qualification_evidence(report):
    return (report["version"] == VERSION and report["critical_errors"] == 0
        and report.get("cost_model") == COST_MODEL and report.get("data_errors") == 0
        and report.get("cases_requested") == report["cases"] >= MIN_CASES and report["waves"] >= MIN_WAVES
        and report["data_kind"] == "executable_depth" and bool(report["holdout_hash"]))


def operator_consent(report):
    return (report["kind"] == "waverider_operator_live_v1" and report["version"] == VERSION
        and report["historical_quality_waived"] is True and isinstance(report["operator"], str) and bool(report["operator"].strip())
        and len(report["consent_sha256"]) == 64 and all(c in "0123456789abcdef" for c in report["consent_sha256"]))


def operator_scope(report, plan, issued):
    account = f"{plan.exchange}:{plan.network}:{plan.account.lower()}"
    cohort = f"{plan.exchange}:{plan.network}:{plan.market.quote_token}"
    return (type(issued) is int and 0 <= issued <= now_ms() and plan.created_at >= issued
        and report["software_hash"] == software_hash()
        and report["profile_hash"] == profile_hash(plan.policy, plan.base_parameters)
        and isinstance(report["allowed_accounts"], list) and isinstance(report["allowed_cohorts"], list)
        and account in report["allowed_accounts"] and cohort in report["allowed_cohorts"])


def qualification_valid(path, key, expected_profile=None, expected_venue=None, expected_cohort=None):
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        signature = report.pop("signature")
        body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        valid = hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).hexdigest())
        groups = list(report.get("cohorts", {}).values()) or [report]
        comparisons = all(comparable_performance(group) for group in groups)
        return (valid and comparisons
            and qualification_scope(report, expected_profile, expected_venue, expected_cohort)
            and qualification_evidence(report))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return False


def operator_authorized(path, key, plan):
    """A separately signed owner waiver never becomes a historical qualification."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        signature = report.pop("signature")
        body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        issued = report["issued_at"]
        return (hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).hexdigest())
            and operator_consent(report) and operator_scope(report, plan, issued))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return False


def live_authority(engine, plan):
    if plan.policy.management_mode != "waverider":
        return None
    settings = engine.exchange.settings
    key_path = settings.integration_key_file
    if not key_path or not key_path.is_file():
        return None
    identity = profile_hash(plan.policy, plan.base_parameters)
    directory = Path(settings.state_path).parent
    key = key_path.read_bytes().strip()
    cohort = f"{plan.exchange}:{plan.network}:{plan.market.quote_token}"
    if any(qualification_valid(path, key, identity, plan.exchange, cohort) for path in (
            directory / f"waverider-qualification-{identity}.json", directory / "waverider-qualification.json")):
        return "qualification"
    if operator_authorized(directory / f"waverider-operator-live-{identity}.json", key, plan):
        return "operator"
    return None


def live_permission(engine, plan):
    return live_authority(engine, plan) is not None


async def market_facts(exchange, market):
    venue = getattr(exchange.settings, "venue", "hyperliquid")
    stamp = now_ms()
    if venue == "hyperliquid":
        if market.dex:
            raise AqlaError("Waverider needs verified deployment-specific fees for this market")
        fee = await exchange.info("userFees", user=exchange.account)
        discount = Decimal(fee["activeReferralDiscount"])
        if not discount.is_finite() or not 0 <= discount < 1:
            raise AqlaError("Waverider account referral discount is invalid")
        rate = Decimal(fee["userCrossRate"]) * (1 - discount)
        fee_observed_at = now_ms()
        raw = await exchange.info("candleSnapshot", req={"coin": market.symbol, "interval": "1m", "startTime": stamp - 3600000, "endTime": stamp})
        candles = [{"start": row["t"], "end": row["T"], "high": row["h"], "low": row["l"], "close": row["c"]} for row in raw]
    else:
        raise AqlaError("Waverider supports Hyperliquid only")
    if not rate.is_finite() or not 0 <= rate <= Decimal(".1"):
        raise AqlaError("Waverider fee observation is invalid")
    evidence = {"exchange": venue, "network": exchange.settings.network, "account": exchange.account,
                "market": market.symbol, "currency": market.quote_token, "liquidity": "taker", "rate": str(rate),
                "source": "native_account_snapshot", "observed_at": fee_observed_at,
                "valid_from": fee_observed_at, "valid_until": fee_observed_at + 60000, "evidence_hash": digest(fee)}
    return {"fee_rate": str(rate), "fee_evidence": evidence, "atr": str(atr(candles, stamp)), "observed_at": stamp,
            "version": VERSION, "market": market.symbol}


def expire_recordings(db, stamp, retention_days):
    cutoff = stamp - retention_days * 86400000
    expired = db.execute("SELECT setups.setup, setups.payload, MAX(observations.stamp) FROM setups JOIN observations ON setups.setup=observations.setup GROUP BY setups.setup HAVING MAX(observations.stamp)<? LIMIT 100", (cutoff,)).fetchall()
    removed = False
    for identity, raw, _ in expired:
        plan = json.loads(raw)["plan"]
        last = json.loads(db.execute("SELECT payload FROM observations WHERE setup=? ORDER BY stamp DESC LIMIT 1", (identity,)).fetchone()[0])
        if last.get("state", {}).get("finished") or plan["policy"].get("waverider_lifetime", "signal_deadline") == "signal_deadline" and plan["deadline_at"] < cutoff:
            db.execute("DELETE FROM observations WHERE setup=?", (identity,))
            db.execute("DELETE FROM original_fills WHERE setup=?", (identity,))
            db.execute("DELETE FROM setups WHERE setup=?", (identity,))
            removed = True
    if removed:
        db.commit()
        db.execute("VACUUM")


def capture(path, setup_id, stamp, record, *, max_bytes=512 * 1024 * 1024, retention_days=None):
    # Separate append-only diagnostics avoid copying market ticks into the financial state.
    path = Path(path)
    with sqlite3.connect(path, timeout=2) as db:
        path.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS setups (setup TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS original_fills (setup TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(setup,identity))")
        db.execute("CREATE TABLE IF NOT EXISTS observations (setup TEXT NOT NULL, stamp INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(setup,stamp))")
        if retention_days is not None:
            expire_recordings(db, stamp, retention_days)
        if path.stat().st_size >= max_bytes:
            return False
        metadata = {key: record[key] for key in ("plan", "family", "software_hash") if key in record}
        metadata["initial_fills"] = [row for row in record.get("original_fills", []) if row["time"] <= stamp]
        db.execute("INSERT OR IGNORE INTO setups VALUES (?,?)", (setup_id, json.dumps(metadata, default=str, separators=(",", ":"))))
        for row in record.get("original_fills", []):
            db.execute("INSERT OR IGNORE INTO original_fills VALUES (?,?,?)", (setup_id, digest(row), json.dumps(row, default=str, separators=(",", ":"))))
        value = {key: value for key, value in record.items() if key not in ("plan", "original_fills", "family", "software_hash")}
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)", (setup_id, stamp, json.dumps(value, default=str, separators=(",", ":"))))
        if db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] > max_bytes:
            db.rollback()
            return False
    return True


def replay_config(engine):
    value = getattr(engine.exchange.settings, "waverider_replay", None)
    return ReplaySettings.model_validate(value) if value is not None else ReplaySettings()


def recorder_health(engine):
    config = replay_config(engine)
    path = Path(engine.exchange.settings.state_path).parent / "waverider-replay.sqlite3"
    size = path.stat().st_size if path.exists() else 0
    return {**getattr(engine, "_replay_health", {}), "enabled": config.enabled, "bytes": size, "max_bytes": config.max_bytes,
            "retention_days": config.retention_days,
            "status": "disabled" if not config.enabled else "saturated" if size >= config.max_bytes else getattr(engine, "_replay_health", {}).get("status", "idle")}


def queue_evidence(engine, trade_id, quote):
    """Diagnostics only, also for Standard; never enables or dispatches a wave."""
    if not replay_config(engine).enabled:
        return
    tasks = getattr(engine, "_waverider_capture_tasks", {})
    task = tasks.get(trade_id)
    if task and not task.done():
        # Per-account wave observation/recorder cache shared with the owning engine.
        # skipcq: PYL-W0212
        engine._replay_health = {"status": "lagging", "reason": "previous_observation_still_pending"}
        return
    tasks[trade_id] = asyncio.create_task(collect_evidence(engine, trade_id, quote), name="aqla-wave-evidence")
    def discard(task):
        if tasks.get(trade_id) is task:
            tasks.pop(trade_id, None)
    tasks[trade_id].add_done_callback(discard)
    # Per-account wave observation/recorder cache shared with the owning engine.
    # skipcq: PYL-W0212
    engine._waverider_capture_tasks = tasks


async def collect_evidence(engine, trade_id, quote):
    try:
        config = replay_config(engine)
        if not config.enabled:
            return
        trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        cache = getattr(engine, "_waverider_facts_cache", {})
        previous = cache.get(plan.market.symbol, {})
        if not 0 <= now_ms() - previous.get("observed_at", 0) < 60000:
            try:
                previous = await read_observation(lambda: market_facts(engine.exchange, plan.market), "waverider_market")
            except Exception:
                previous = {"observed_at": now_ms(), "unavailable": True}
            cache[plan.market.symbol] = previous
            # Per-account wave observation/recorder cache shared with the owning engine.
            # skipcq: PYL-W0212
            engine._waverider_facts_cache = cache
        quote.fresh(as_of=quote.observed_at)
        from .managed_entries import original_fills
        captured = await asyncio.to_thread(capture, Path(engine.exchange.settings.state_path).parent / "waverider-replay.sqlite3", plan.trade_id, quote.observed_at,
            {"version": VERSION, "software_hash": software_hash(), "family": plan.setup_id or plan.trade_id,
             "plan": plan.model_dump(mode="json"), "quote": quote.model_dump(mode="json"), "facts": previous,
                 "original_fills": original_fills(trade), "status": trade["status"], "state": trade.get("waverider", {}),
                 "evidence_error": "market_facts_unavailable" if previous.get("unavailable") else None},
            max_bytes=config.max_bytes, retention_days=config.retention_days)
        # Per-account wave observation/recorder cache shared with the owning engine.
        # skipcq: PYL-W0212
        engine._replay_health = {"status": "recording" if captured else "saturated", "last_observed_at": quote.observed_at}
    except Exception:
        # Per-account wave observation/recorder cache shared with the owning engine.
        # skipcq: PYL-W0212
        engine._replay_health = {"status": "unavailable", "reason": "recording_failed"}
        return


def ended(plan, trade, quote=None):
    endpoint = len(trade["targets"]) if plan.policy.waverider_until_tp == "last" else min(plan.policy.waverider_until_tp, len(trade["targets"]))
    state = trade.get("waverider", {})
    target = Decimal(trade["targets"][endpoint - 1])
    return bool(state.get("finished") or state.get("endpoint_reached_at")
        or Decimal(trade.get("tp_filled", ["0"] * len(trade["targets"]))[endpoint - 1]) > 0
        or plan.policy.waverider_lifetime == "signal_deadline" and now_ms() >= plan.deadline_at
        or quote is not None and (quote.bid >= target if plan.side == "long" else quote.ask <= target))


def cycle_observation(plan, quote, quantity, *, reentering, fee_rate, as_of=None):
    if quantity < plan.market.min_quantity or quantity <= 0 or quantity * quote.mid < plan.market.min_notional:
        raise AqlaError("Flexible quantity is below the exchange minimum; ordinary protection remains active")
    buy = (plan.side == "long") if reentering else (plan.side == "short")
    limit = slippage_price(quote.mid, plan.policy.max_slippage_percent, plan.market, buy=buy)
    capacity, price = quote.executable(buy=buy, limit=limit, quantity=quantity, as_of=as_of)
    if capacity < quantity:
        raise AqlaError("Waverider depth cannot cover its flexible quantity within the price bound")
    cost = price * (Decimal(fee_rate) * 2 + plan.policy.max_slippage_percent / 50)
    return price, cost, price_step(price, plan.market)


def observation_marker(state):
    return tuple(state.get(key) for key in ("checkpoint", "endpoint_reached_at", "finished", "operation_id",
        "phase", "cycle", "sale_price", "sold_quantity", "resume_phase"))


async def fresh_cycle_quote(engine, plan, state, quote):
    facts = state.get("market_facts")
    if not facts or not 0 <= now_ms() - facts["observed_at"] < 60000:
        facts = await read_observation(lambda: market_facts(engine.exchange, plan.market), "waverider_market")
        state["market_facts"] = facts
    stale = False
    try:
        quote.fresh()
    except TemporaryObservation:
        stale = True
    if stale or not quote.bids or not quote.asks:
        reader = getattr(engine.exchange, "depth_snapshot", engine.exchange.price_snapshot)
        quote = await read_observation(lambda: reader(plan.market), "price")
    return quote, facts


def cycle_inventory(trade, plan, state, permitted, rebuying):
    own_quantity, basis, realized, fees = position_basis(trade)
    inventory = pools(trade, plan.policy.waverider_flex_percent)
    quantity = quantity_grid(max(Decimal(0), inventory["rights"] - inventory["flex"]) if rebuying and permitted
                            else min(own_quantity, inventory["rights"]) if not permitted else inventory["flex"], plan.market)
    if inventory["flex"]:
        basis = inventory["flex_basis"]
    if not permitted:
        basis = Decimal(state.get("paper_basis", str(basis)))
    state.update(core_quantity=str(inventory["core"]), flexible_quantity=str(inventory["flex"]),
                 remaining_risk_budget=str(max(Decimal(0), plan.risk_usdc - inventory["spent"])))
    return basis, quantity, realized, fees


def paper_cycle(state, plan, action, price, quantity, stamp):
    state.setdefault("paper_actions", []).append({"action": action, "at": stamp, "price": str(price), "quantity": str(quantity)})
    state["paper_actions"] = state["paper_actions"][-20:]
    if action == "reduce":
        state.update(phase="waiting_rebound", sale_price=str(price), extreme=str(price * (1 if plan.side == "long" else -1)))
    else:
        state.update(phase="waiting_rise", cycle=state.get("cycle", 0) + 1, paper_basis=str(price), extreme=str(price * (1 if plan.side == "long" else -1)))
    for field in ("confirmation_since", "confirmation_count", "wave_atr"):
        state.pop(field, None)


def save_cycle_observation(engine, trade_id, state, persisted, memory):
    if state.get("phase") != persisted.get("phase") or state.get("operation_id") != persisted.get("operation_id") or now_ms() - persisted.get("checkpoint", 0) >= 10000:
        state["checkpoint"] = now_ms()
        engine.patch_trade(trade_id, waverider=state)
        persisted = state
    marker = observation_marker(persisted)
    memory[trade_id] = (marker, state)
    # Per-account wave observation/recorder cache shared with the owning engine.
    # skipcq: PYL-W0212
    engine._waverider_observations = memory


def pause_cycle(state, error):
    if state.get("phase") != "paused":
        state["resume_phase"] = state.get("phase", "waiting_rise")
    state.update(phase="paused", reason=str(error) if isinstance(error, AqlaError) else "Waverider market evidence is unavailable")


def cycle_state_after_error(engine, trade_id, state, persisted, error):
    current = dict(engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("waverider", {}))
    if observation_marker(current) != observation_marker(persisted):
        # A completed durable transition takes precedence over an older observation.
        return current, current
    pause_cycle(state, error)
    return state, persisted


async def tick(engine, trade_id, quote):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    persisted = dict(trade.get("waverider", {}))
    marker = observation_marker(persisted)
    memory = getattr(engine, "_waverider_observations", {})
    state = dict(memory[trade_id][1]) if trade_id in memory and memory[trade_id][0] == marker else dict(persisted)
    if state.get("finished"):
        return
    if ended(plan, trade, quote):
        state.update(finished=True, phase="finished", reason="Configured TP or original signal deadline reached")
        engine.patch_trade(trade_id, waverider=state)
        return
    if trade.get("waverider_paused") or trade.get("pending_stop_update", {}).get("phase") in STOP_UPDATE_ACTIVE:
        return
    authority = live_authority(engine, plan)
    permitted = authority is not None
    state.update(shadow=not permitted, live_authority=authority)
    try:
        if state.get("phase") in ("warming_up", "paused"):
            state["phase"] = state.pop("resume_phase", "waiting_rise")
        if permitted and state.get("phase") in ("pending_reduce", "pending_reenter"):
            state = await resume_cycle(engine, trade_id, state)
            trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            if state.get("phase") in ("pending_reduce", "pending_reenter"):
                engine.patch_trade(trade_id, waverider=state)
                return
        quote, facts = await fresh_cycle_quote(engine, plan, state, quote)
        rebuying = state.get("phase") in ("waiting_rebound", "pending_reenter")
        basis, quantity, realized, fees = cycle_inventory(trade, plan, state, permitted, rebuying)
        price, cost, tick_size = cycle_observation(plan, quote, quantity, reentering=rebuying, fee_rate=facts["fee_rate"])
        state, action = advance(state, side=plan.side, price=price, basis=basis,
            volatility=Decimal(facts["atr"]), cycle_cost=cost, tick=tick_size, stamp=quote.observed_at)
        if now_ms() - state.get("last_captured_at", 0) >= 1000:
            queue_evidence(engine, trade_id, quote)
            state["last_captured_at"] = now_ms()
        if action:
            if permitted:
                await execute(engine, trade_id, state, action, quantity, quote, cost)
                return
            paper_cycle(state, plan, action, price, quantity, quote.observed_at)
        state.update(reason=None, net_realized=str(realized - fees))
    except (KeyError, ValueError, TypeError, OSError, sqlite3.Error) as error:
        state, persisted = cycle_state_after_error(engine, trade_id, state, persisted, error)
    save_cycle_observation(engine, trade_id, state, persisted, memory)


def reset_phase(state, phase):
    result = {key: value for key, value in state.items() if key not in ("confirmation_since", "confirmation_count", "extreme", "wave_atr", "last_observed_at", "operation_id", "action", "decision_at", "resume_phase")}
    return {**result, "phase": phase}


def pending_cycle_order(engine, trade_id, trade, state, operations):
    """Resolve this action's own order, without borrowing a previous exit."""
    reentering = state["phase"] == "pending_reenter"
    action = "reenter" if reentering else state.get("action", "reduce")
    expected = f"waverider:{trade_id}:{state.get('cycle', 0)}:{action}"
    rows = [row for row in trade["entry_orders"] if row.get("cycle") == state.get("cycle", 0)
            and row["kind"] == "waverider" and not row.get("not_sent")] if reentering else []
    if reentering and not rows:
        if expected in operations:
            raise AqlaError("Waverider reentry operation has no matching owned entry; reconciliation is required")
        return None
    identity = rows[-1]["operation_id"] if reentering else expected
    if not reentering and identity not in operations and not trade.get("suspended_wave_targets"):
        return None
    cloid = rows[-1]["cloid"] if reentering else engine.exchange.cloid(identity)
    return identity, cloid


def own_cycle_fills(trade, oid, reentering):
    return [row for row in trade["fill_events"].values() if row["oid"] == oid
            and (row["kind"] == "entry" and row.get("entry_role") == "waverider" if reentering
                 else row["kind"] in ("waverider", "waverider_retirement"))]


def cycle_owned_order(trade, identity, cloid, reentering):
    rows = trade.get("entry_orders" if reentering else "exit_orders", [])
    kinds = ("waverider",) if reentering else ("waverider", "waverider_retirement")
    matched = [row for row in rows if row.get("operation_id") == identity and row.get("cloid") == cloid
               and row.get("kind") in kinds and not row.get("not_sent")]
    return matched[0] if len(matched) == 1 else None


def cycle_request_order(trade, identity, cloid, operation, reentering):
    row = cycle_owned_order(trade, identity, cloid, reentering)
    if row is None or not operation or operation.get("operation_id") != identity:
        return None
    action = operation.get("wire_action", {})
    orders = [order for order in action.get("orders", []) if order.get("c") == cloid]
    if action.get("type") != "order" or len(orders) != 1:
        return None
    order = orders[0]
    buy = (trade["plan"]["side"] == "long") == reentering
    if (order.get("a") != trade["plan"]["market"]["asset_id"] or order.get("b") is not buy
            or order.get("r") is not (not reentering)):
        return None
    price = row["limit"] if reentering else row["price"]
    if Decimal(order["s"]) != Decimal(row["quantity"]) or Decimal(order["p"]) != Decimal(price):
        return None
    return row, order


def cycle_known_order_ids(trade, cloid, operation):
    known = {trade.get("order_observations", {}).get(cloid, {}).get("oid")}
    for row in [*trade.get("entry_orders", []), *trade.get("exit_orders", [])]:
        if row.get("cloid") == cloid:
            known.add(row.get("oid"))
    orders = operation.get("wire_action", {}).get("orders", [])
    indices = {index for index, order in enumerate(orders) if order.get("c") == cloid}
    for leg in operation.get("legs", []):
        if leg.get("index") in indices and isinstance(leg.get("status"), dict):
            for kind in ("resting", "filled"):
                if isinstance(leg["status"].get(kind), dict):
                    known.add(leg["status"][kind].get("oid"))
    known.discard(None)
    return known


def cycle_native_fields_match(trade, cloid, native, wire, reentering):
    buy = (trade["plan"]["side"] == "long") == reentering
    return (native.get("cloid") == cloid and native.get("coin") == trade["plan"]["market"]["symbol"]
        and native.get("side") == ("B" if buy else "A") and native.get("reduceOnly") is (not reentering)
        and native.get("isTrigger") is False and type(native.get("oid")) is int and native["oid"] > 0
        and Decimal(native["limitPx"]) == Decimal(wire["p"]))


def cycle_native_sizes_match(native, wire):
    original, remaining, requested = (Decimal(native["origSz"]), Decimal(native["sz"]), Decimal(wire["s"]))
    if not all(value.is_finite() for value in (original, remaining, requested)):
        return False
    if requested != original or original <= 0:
        return False
    return 0 <= remaining <= original


def cycle_native_order_matches(trade, identity, cloid, operation, status, reentering):
    try:
        matched = cycle_request_order(trade, identity, cloid, operation, reentering)
        if matched is None or status.get("status") != "order":
            return False
        _, wire = matched
        native = status.get("order", {}).get("order", {})
        if not cycle_native_fields_match(trade, cloid, native, wire, reentering) or not cycle_native_sizes_match(native, wire):
            return False
        known = cycle_known_order_ids(trade, cloid, operation)
        if known and known != {native["oid"]}:
            return False
        return not any(key != cloid and fact.get("oid") == native["oid"]
                       for key, fact in trade.get("order_observations", {}).items())
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return False


def cycle_order_quantity_gap(trade, identity, cloid, operation, status, reentering):
    if status.get("order", {}).get("status") not in TERMINAL_ORDER:
        return None
    if not cycle_native_order_matches(trade, identity, cloid, operation, status, reentering):
        return None
    try:
        confirmed = entry_quantity_evidence(status)
        fills = own_cycle_fills(trade, status["order"]["order"]["oid"], reentering)
        observed = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
        gap = Decimal(confirmed) - observed if confirmed is not None else None
        return gap if gap is not None and gap.is_finite() and gap >= 0 else None
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return None


def confirmed_cycle_order_fills(trade, identity, cloid, operation, status, reentering):
    if cycle_order_quantity_gap(trade, identity, cloid, operation, status, reentering) != 0:
        return None
    return own_cycle_fills(trade, status["order"]["order"]["oid"], reentering)


async def exit_fill_history_pending(engine, trade_id, signed):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    identity = trade.get("suspended_wave_targets")
    if trade.get("lifecycle_version") != 2 or not identity:
        return False
    direction = 1 if trade["plan"]["side"] == "long" else -1
    if signed * direction < 0:
        return False
    own, _, _, _ = position_basis(trade)
    difference = own - abs(signed)
    if difference <= 0:
        return False
    operation = engine.state.read("operations")["operations"].get(identity)
    cloid = engine.exchange.cloid(identity)
    try:
        if cycle_request_order(trade, identity, cloid, operation, reentering=False) is None:
            return False
        raw = await read_observation(lambda: engine.order_status(cloid), "exit_quantity")
        missing = cycle_order_quantity_gap(trade, identity, cloid, operation, raw, reentering=False)
        return missing is not None and missing > 0 and missing == difference
    except TemporaryObservation:
        raise
    except (AqlaError, InvalidOperation, KeyError, TypeError):
        return False


def completed_cycle(state, fills):
    quantity = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
    if state["phase"] == "pending_reduce":
        if quantity:
            result = reset_phase(state, "waiting_rebound")
            result.update(sale_price=str(sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in fills) / quantity), sold_quantity=str(quantity))
        else:
            result = {**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1}
    else:
        result = {**reset_phase(state, "waiting_rise" if quantity else "waiting_rebound"), "cycle": state.get("cycle", 0) + 1}
    return result


async def resume_cycle(engine, trade_id, state):
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    operations = engine.state.read("operations")["operations"]
    order = pending_cycle_order(engine, trade_id, trade, state, operations)
    if order is None:
        # A pre-send guard can fail before the new action is staged.
        result = reset_phase(state, "waiting_rebound" if state["phase"] == "pending_reenter" else "waiting_retrace")
        engine.patch_trade(trade_id, waverider=result)
        return result
    identity, cloid = order
    state = {**state, "operation_id": identity}
    operation = operations.get(identity)
    if operation and operation.get("outcome") == "not_sent":
        result = {**reset_phase(state, "waiting_retrace" if state["phase"] == "pending_reduce" else "waiting_rebound"), "cycle": state.get("cycle", 0) + 1}
        engine.patch_trade(trade_id, waverider=result)
        return result
    raw = await engine.order_status(cloid)
    reentering = state["phase"] == "pending_reenter"
    if not cycle_native_order_matches(trade, identity, cloid, operation, raw, reentering):
        return state
    if raw.get("order", {}).get("status") not in TERMINAL_ORDER:
        return state
    await engine.capture_fills(trade_id)
    current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    fills = confirmed_cycle_order_fills(current, identity, cloid, operation, raw, reentering)
    if fills is None:
        return state
    result = completed_cycle(state, fills)
    engine.patch_trade(trade_id, waverider=result)
    return result


def abort_cycle_exit(engine, trade_id, state, identity, not_sent):
    def abort(data):
        current = data["trades"][trade_id]
        for row in current["exit_orders"]:
            if row.get("operation_id") == identity:
                row.update(retired=True, **({"not_sent": True} if not_sent else {}))
        current.update(suspended_wave_targets=None, exit_quantity_evidence_pending=False, managed_target_fingerprint=None,
                       protection_repair_pending=identity,
                       waverider={**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1})
    engine.state.update(abort)
    return True


def definitive_cycle_exit_rejection(trade, identity, cloid, operation):
    if (operation.get("phase") != "rejected" or operation.get("outcome") != "rejected"
            or operation.get("operation_id") != identity):
        return False
    from aqla_hyperliquid_mcp.api import Hyperliquid
    try:
        action = operation.get("wire_action", {})
        if len(action.get("orders", [])) != 1 or cycle_request_order(trade, identity, cloid, operation, reentering=False) is None:
            return False
        # A global nonce/request error is not a rejection of this exact order.
        phase, outcome, legs = Hyperliquid.decode_result(action, operation.get("result"))
        return ((phase, outcome) == ("rejected", "rejected") and len(legs) == 1
            and legs[0].get("index") == 0 and legs[0].get("disposition") == "rejected")
    except (InvalidOperation, KeyError, ValueError, TypeError):
        return False


def cycle_exit_positive_evidence(trade, cloid, operation):
    if cycle_known_order_ids(trade, cloid, operation or {}):
        return True
    other_ids = {fact.get("oid") for key, fact in trade.get("order_observations", {}).items() if key != cloid}
    return any(fill.get("kind") in ("waverider", "waverider_retirement") and fill.get("oid") not in other_ids
               for fill in trade.get("fill_events", {}).values())


async def rejected_cycle_exit_absent(engine, trade_id, trade, identity, cloid, operation):
    if cycle_exit_positive_evidence(trade, cloid, operation):
        return False
    if not definitive_cycle_exit_rejection(trade, identity, cloid, operation):
        return False
    await engine.capture_fills(trade_id)
    current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    return not cycle_exit_positive_evidence(current, cloid, operation)


async def resume_exit(engine, trade_id):
    """Restore source targets only after the optional exit's exact outcome is known."""
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    identity = trade.get("suspended_wave_targets")
    if not identity:
        return True
    state = dict(trade["waverider"])
    operation = engine.state.read("operations")["operations"].get(identity)
    cloid = engine.exchange.cloid(identity)
    if operation is None or operation.get("outcome") == "not_sent":
        if cycle_exit_positive_evidence(trade, cloid, operation):
            return False
        abort_cycle_exit(engine, trade_id, state, identity, not_sent=True)
        return True
    status = await engine.order_status(cloid)
    if status.get("status") == "unknownOid":
        if await rejected_cycle_exit_absent(engine, trade_id, trade, identity, cloid, operation):
            abort_cycle_exit(engine, trade_id, state, identity, not_sent=False)
            return True
        return False
    if not cycle_native_order_matches(trade, identity, cloid, operation, status, reentering=False):
        return False
    if status.get("order", {}).get("status") == "open":
        plan = TradePlan.model_validate(trade["plan"])
        await engine.exchange.mutate("cancel-wave-exit:" + identity,
            {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": cloid}]})
        status = await engine.order_status(cloid)
    if status.get("order", {}).get("status") not in TERMINAL_ORDER:
        return False
    await engine.capture_fills(trade_id)
    current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    # Terminal order status can precede complete native fill-history visibility.
    fills = confirmed_cycle_order_fills(current, identity, cloid, operation, status, reentering=False)
    if fills is None:
        return False
    amount = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
    if amount:
        sale = sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in fills) / amount
        if state.get("action") == "retire":
            state = {**reset_phase(state, "waiting_rise"), "sold_quantity": "0", "cycle": state.get("cycle", 0) + 1}
        else:
            state = {**reset_phase(state, "waiting_rebound"), "sale_price": str(sale), "sold_quantity": str(amount)}
    else:
        state = {**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1}
    engine.patch_trade(trade_id, suspended_wave_targets=None, exit_quantity_evidence_pending=False, managed_target_fingerprint=None, protection_repair_pending=identity, waverider=state)
    return True


async def confirmed_cycle_quote(engine, trade_id, plan, trade):
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    await engine.observe_owned_remaining(trade_id, signed)
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    if any(row["phase"] in ("prepared", "sending", "unknown") for row in engine.state.read("operations")["operations"].values()):
        raise AqlaError("Unresolved operations block optional Waverider management")
    from .managed_entries import protected
    stops = [(row, await engine.order_status(row["cloid"])) for row in trade["orders"] if row["kind"] == "sl" and not row.get("retired")]
    if not protected(engine, trade, stops):
        raise AqlaError("Optional Waverider management requires a freshly confirmed covering stop")
    return quote


async def suspend_cycle_targets(engine, context, action, quantity, order):
    trade_id, trade, plan, state = context.trade_id, context.trade, context.plan, context.state
    cycle, identity = context.cycle, context.identity
    corrective = action == "retire"
    def stage(data):
        current = data["trades"][trade_id]
        current.setdefault("exit_orders", []).append({"kind": "waverider_retirement" if corrective else "waverider", "reason": "retired_source_target" if corrective else "waverider", "cloid": order["c"],
            "quantity": order["s"], "price": order["p"], "cycle": cycle, "operation_id": identity})
        current.update(suspended_wave_targets=identity, waverider={**state, "phase": "preparing_reduce", "operation_id": identity, "action": action})
    engine.state.update(stage)
    await engine.cancel_owned_orders(trade_id, operation_id="wave-cancel-targets:" + identity)
    for target in [row for row in trade["orders"] if row["kind"] == "tp" and not row.get("retired")]:
        raw = await engine.order_status(target["cloid"])
        if raw.get("order", {}).get("status") not in TERMINAL_ORDER:
            raise AqlaError("Source target cancellation is unconfirmed; optional exit remains unsent")
    await engine.capture_fills(trade_id)
    latest = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    available = pools(latest, plan.policy.waverider_flex_percent)
    if corrective:
        quantity = min(quantity, max(Decimal(0), available["flex"] - available["rights"]))
        if quantity <= 0:
            await resume_exit(engine, trade_id)
            return
        from aqla_hyperliquid_mcp.models import wire_number
        order["s"] = wire_number(quantity)
        engine.state.update(lambda data: require_first(row for row in data["trades"][trade_id]["exit_orders"] if row["cloid"] == order["c"]).update(quantity=order["s"]))
    if quantity > available["flex"] or not corrective and latest.get("entries_disabled"):
        await resume_exit(engine, trade_id)
        return
    engine.state.update(partial(retire_cycle_targets, trade_id))
    return latest, available


def retire_cycle_targets(trade_id, data):
    for target in data["trades"][trade_id]["orders"]:
        if target["kind"] == "tp":
            target["retired"] = True


def validate_cycle_reduction(engine, context, quote, corrective, data):
    trade_id, plan = context.trade_id, context.plan
    current = data["trades"][trade_id]
    if not corrective:
        if current.get("pending_stop_update", {}).get("phase") in STOP_UPDATE_ACTIVE:
            raise AqlaError("A pending stop update blocks optional Waverider management")
        if not live_permission(engine, plan):
            raise AqlaError("Waverider authorization changed before transport")
        engine.validate_configuration(data, plan)
    if current["status"] != "open" or not corrective and (current.get("entries_disabled") or current["waverider"].get("finished")):
        raise AqlaError("Waverider reduction no longer owns its setup")
    if not corrective and ended(plan, current, quote):
        raise AqlaError("Waverider endpoint was reached before transport")
    quote.fresh()


async def submit_cycle_reduction(engine, context, action, order, limit, latest, available, cycle_cost):
    trade_id, plan, state = context.trade_id, context.plan, context.state
    identity = context.identity
    corrective = action == "retire"
    try:
        quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    except BaseException:
        if await resume_exit(engine, trade_id):
            from .managed_entries import restore_targets
            await restore_targets(engine, trade_id)
        raise
    if not corrective and (ended(plan, latest, quote) or (limit - available["flex_basis"]) * (1 if plan.side == "long" else -1) <= cycle_cost):
        await resume_exit(engine, trade_id)
        return
    result = await engine.exchange.mutate(identity, {"type": "order", "orders": [order], "grouping": "na"}, prepare_guard=partial(validate_cycle_reduction, engine, context, quote, corrective))
    engine.patch_trade(trade_id, waverider={**state, "phase": "pending_reduce", "operation_id": identity, "action": action})
    if result["phase"] in ("acknowledged", "reconciled", "rejected") and await resume_exit(engine, trade_id):
        from .managed_entries import refresh
        await refresh(engine, trade_id, dispatch=False)
    return


def reentry_size(context, quote, limit, sold):
    trade, plan = context.trade, context.plan
    own, average, inventory = context.own, context.average, context.inventory
    spent = inventory["spent"]
    stop = Decimal(trade["stop_price"])
    direction = Decimal(1) if plan.side == "long" else Decimal(-1)
    open_risk = (inventory["core"] * max(Decimal(0), (inventory["core_basis"] - stop) * direction)
        + inventory["flex"] * max(Decimal(0), (inventory["flex_basis"] - stop) * direction)
        + own * average * plan.policy.cost_reserve_bps / 10000)
    from .managed_entries import pending_quantity
    reserved_quantity = pending_quantity(trade)
    pending_risk = sum((Decimal(row["quantity"]) * (abs(Decimal(row["limit"]) - stop)
        + Decimal(row["limit"]) * plan.policy.cost_reserve_bps / 10000) for row in trade["entry_orders"]
        if not row.get("not_sent") and trade.get("order_observations", {}).get(row["cloid"], {}).get("status") not in TERMINAL_ORDER), Decimal(0))
    available = plan.risk_usdc - spent - open_risk - pending_risk
    maximum = quantity_grid(max(Decimal(0), available) / (abs(limit - stop) + limit * plan.policy.cost_reserve_bps / 10000), plan.market)
    pending_notional = sum((Decimal(row["quantity"]) * Decimal(row["limit"]) for row in trade["entry_orders"] if row.get("phase") == "pending" and not row.get("not_sent")), Decimal(0))
    notional_room = max(Decimal(0), plan.policy.max_notional_usdc - own * max(average, quote.ask) - pending_notional) / max(limit, quote.ask)
    quantity = quantity_grid(min(sold, maximum, plan.quantity - own - reserved_quantity, notional_room), plan.market)
    if quantity <= 0 or quantity * limit < plan.market.min_notional:
        raise AqlaError("Original lifetime risk budget cannot fund a Waverider reentry")
    return quantity


@dataclass(frozen=True, repr=False)
class CycleExecution:
    trade_id: str
    trade: dict
    plan: TradePlan
    state: dict
    cycle: int
    identity: str
    own: Decimal
    average: Decimal
    inventory: dict[str, Decimal]


async def execute(engine, trade_id, state, action, quantity, quote, cycle_cost):
    """Scoped internal management; source close/reduce keeps its original semantics."""
    await engine.capture_fills(trade_id)
    trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    if action != "retire" and trade.get("pending_stop_update", {}).get("phase") in STOP_UPDATE_ACTIVE:
        raise AqlaError("A pending stop update blocks optional Waverider management")
    if action != "retire":
        if not live_permission(engine, plan):
            raise AqlaError("Waverider live management is not authorized")
        engine.validate_configuration(engine.state.snapshot(), plan)
    quote = await confirmed_cycle_quote(engine, trade_id, plan, trade)
    if action != "retire" and ended(plan, trade, quote):
        engine.patch_trade(trade_id, waverider={**state, "finished": True, "phase": "finished"})
        return
    cycle = state.get("cycle", 0)
    identity = f"waverider:{trade_id}:{cycle}:{action}"
    if identity in engine.state.read("operations")["operations"]:
        return
    own, average, _, _ = position_basis(trade)
    inventory = pools(trade, plan.policy.waverider_flex_percent)
    if any(row.get("fee") is None or row.get("feeToken") != plan.market.quote_token for row in trade.get("fill_events", {}).values()):
        raise AqlaError("Waverider needs complete actual fee evidence before live management")
    context = CycleExecution(trade_id, trade, plan, state, cycle, identity, own, average, inventory)
    if action in ("reduce", "retire"):
        return await reduce_cycle(engine, context, action, quantity, quote, cycle_cost)
    return await reenter_cycle(engine, context, quote, cycle_cost)


def retained_targets_fit(plan, quantity, budgets, filled, completed, targets):
    from .managed_entries import target_allocations
    retained, _ = target_allocations(plan, quantity, budgets, filled, completed)
    return bool(retained) and all(size <= 0 or size >= plan.market.min_quantity
        and size * Decimal(targets[index]) >= plan.market.min_notional for index, size in enumerate(retained))


async def reduce_cycle(engine, context, action, quantity, quote, cycle_cost):
    trade_id, trade, plan, state = context.trade_id, context.trade, context.plan, context.state
    identity, own, inventory = context.identity, context.own, context.inventory
    corrective = action == "retire"
    buy = plan.side == "short"
    maker = plan.take_profit_execution == "maker_only" and not corrective
    limit = passive_entry_price(plan.market, "long" if buy else "short", quote) if maker else slippage_price(quote.bid if plan.side == "long" else quote.ask, plan.policy.max_slippage_percent, plan.market, buy=buy)
    if quantity > inventory["flex"] or not corrective and (limit - inventory["flex_basis"]) * (1 if plan.side == "long" else -1) <= cycle_cost:
        state.update(phase="waiting_retrace", reason="Worst allowed exit has no net profit")
        engine.patch_trade(trade_id, waverider=state)
        return
    if not corrective and not retained_targets_fit(plan, own - quantity, trade.get("target_budgets"), trade["tp_filled"], trade.get("completed_target_indices", []), trade["targets"]):
        raise AqlaError("Wave reduction would leave core targets below native minimums")
    order = engine.order(plan, min(quantity, own), limit, reduce=True, cloid=engine.exchange.cloid(identity))
    order["t"] = {"limit": {"tif": "Alo" if maker else "Ioc"}}
    suspended = await suspend_cycle_targets(engine, context, action, quantity, order)
    if suspended is None:
        return
    latest, available = suspended
    return await submit_cycle_reduction(engine, context, action, order, limit, latest, available, cycle_cost)


async def reenter_cycle(engine, context, quote, cycle_cost):
    from .managed_entries import dispatch_next, patch_row
    trade_id, plan, state = context.trade_id, context.plan, context.state
    cycle, identity, inventory = context.cycle, context.identity, context.inventory
    sold = min(Decimal(state.get("sold_quantity") or 0), max(Decimal(0), inventory["rights"] - inventory["flex"]))
    if sold <= 0:
        raise AqlaError("Waverider has no proved sold quantity to rebuild")
    buy = plan.side == "long"
    maker = plan.entry_execution == "maker_only"
    limit = passive_entry_price(plan.market, plan.side, quote) if maker else slippage_price(quote.ask if buy else quote.bid, plan.policy.max_slippage_percent, plan.market, buy=buy)
    if (Decimal(state["sale_price"]) - limit) * (1 if buy else -1) <= cycle_cost:
        state.update(phase="waiting_rebound", reason="Worst allowed reentry has no price advantage after costs")
        engine.patch_trade(trade_id, waverider=state)
        return
    quantity = reentry_size(context, quote, limit, sold)
    row = {"kind": "waverider", "index": 0, "quantity": str(quantity), "price": str(limit), "limit": str(limit),
           "entry_kind": "limit" if maker else "market", "timing": "standard", "operation_id": identity, "cloid": engine.exchange.cloid(identity), "phase": "pending", "cycle": cycle}
    def stage_reentry(data):
        current = data["trades"][trade_id]
        if current.get("entries_disabled") or current["waverider"].get("finished"):
            raise AqlaError("Waverider reentry was terminated")
        if any(item["operation_id"] == identity for item in current["entry_orders"]):
            raise AqlaError("Waverider reentry identity already exists")
        current["entry_orders"].append(row)
        current["waverider"] = {**state, "phase": "pending_reenter", "operation_id": identity}
    engine.state.update(stage_reentry)
    await dispatch_next(engine, trade_id)
    patch_row(engine, trade_id, row["cloid"], cycle=cycle)
