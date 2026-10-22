"""Deterministic same-direction waves; live orders require signed authority."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from aqla_hyperliquid_mcp.models import TERMINAL_ORDER, AqlaError, TradePlan, digest, now_ms
from aqla_hyperliquid_mcp.rules import quantity_grid, slippage_price

from .entry import passive_entry_price
from .managed_entries import position_basis
from .observations import TemporaryObservation, read_observation

VERSION = "waverider-v1"
COST_MODEL = "fees-funding-profile-v2"
MIN_CASES, MIN_WAVES = 30, 10


@lru_cache(maxsize=1)
def software_hash():
    root = Path(__file__).parent
    files = [root / name for name in ("waverider.py", "waverider_replay.py", "managed_entries.py", "trading.py", "entry.py", "inventory_stream.py",
        "entry_watch.py", "liquidity.py", "aster.py", "lighter.py", "pipelines.py", "monthly.py", "percent_targets.py")]
    files += [root.parent / "aqla_hyperliquid_mcp" / name for name in ("models.py", "rules.py", "api.py", "streams.py")]
    return digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def profile_hash(policy, base_parameters=None):
    from .pipelines import Parameters
    profile = Parameters.model_validate(base_parameters) if base_parameters is not None else Parameters.from_policy(policy)
    values = profile.model_dump()
    values.pop("management_mode", None)  # Standard observations qualify only the matching Waverider hypothesis.
    return digest(values)


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


def qualification_valid(path, key, expected_profile=None, expected_venue=None, expected_cohort=None):
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        signature = report.pop("signature")
        body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        valid = hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).hexdigest())
        groups = list(report.get("cohorts", {}).values()) or [report]
        comparisons = all(all(Decimal(group[key]).is_finite() for key in ("net_pnl", "baseline_net_pnl", "max_drawdown", "baseline_max_drawdown"))
            and Decimal(group["net_pnl"]) >= Decimal(group["baseline_net_pnl"])
            and Decimal(group["max_drawdown"]) <= Decimal(group["baseline_max_drawdown"]) for group in groups)
        return (valid and comparisons and report["software_hash"] == software_hash()
            and (expected_profile is None or report["profile_hash"] == expected_profile)
            and (expected_venue is None or expected_venue in report["allowed_venues"])
            and (expected_cohort is None or expected_cohort in report.get("cohorts", {}))
            and report["version"] == VERSION and report["critical_errors"] == 0
            and report.get("cost_model") == COST_MODEL and report.get("data_errors") == 0
            and report.get("cases_requested") == report["cases"]
            and report["cases"] >= MIN_CASES and report["waves"] >= MIN_WAVES
            and report["data_kind"] == "executable_depth" and bool(report["holdout_hash"]))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return False


def operator_authorized(path, key, plan):
    """A separately signed owner waiver never becomes a historical qualification."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        signature = report.pop("signature")
        body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        issued = report["issued_at"]
        account = f"{plan.exchange}:{plan.network}:{plan.account.lower()}"
        cohort = f"{plan.exchange}:{plan.network}:{plan.market.quote_token}"
        return (hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).hexdigest())
            and report["kind"] == "waverider_operator_live_v1" and report["version"] == VERSION
            and report["historical_quality_waived"] is True and isinstance(report["operator"], str) and bool(report["operator"].strip())
            and len(report["consent_sha256"]) == 64 and all(c in "0123456789abcdef" for c in report["consent_sha256"])
            and type(issued) is int and 0 <= issued <= now_ms() and plan.created_at >= issued
            and report["software_hash"] == software_hash()
            and report["profile_hash"] == profile_hash(plan.policy, plan.base_parameters)
            and isinstance(report["allowed_accounts"], list) and isinstance(report["allowed_cohorts"], list)
            and account in report["allowed_accounts"] and cohort in report["allowed_cohorts"])
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
    elif venue == "aster":
        fee = await exchange.read("commissionRate", {"symbol": market.native_symbol}, private=True)
        if fee.get("symbol") != market.native_symbol:
            raise AqlaError("Waverider fees belong to another market")
        rate = Decimal(fee["takerCommissionRate"])
        fee_observed_at = now_ms()
        raw = await exchange.read("klines", {"symbol": market.native_symbol, "interval": "1m", "limit": 60})
        candles = [{"start": row[0], "end": row[6], "high": row[2], "low": row[3], "close": row[4]} for row in raw]
    else:
        reader = getattr(exchange, "waverider_facts", None)
        if reader is None:
            raise AqlaError("Waverider account fees and closed candles are not verified on this venue")
        return await reader(market)
    if not rate.is_finite() or not 0 <= rate <= Decimal(".1"):
        raise AqlaError("Waverider fee observation is invalid")
    evidence = {"exchange": venue, "network": exchange.settings.network, "account": exchange.account,
                "market": market.symbol, "currency": market.quote_token, "liquidity": "taker", "rate": str(rate),
                "source": "native_account_snapshot", "observed_at": fee_observed_at,
                "valid_from": fee_observed_at, "valid_until": fee_observed_at + 60000, "evidence_hash": digest(fee)}
    return {"fee_rate": str(rate), "fee_evidence": evidence, "atr": str(atr(candles, stamp)), "observed_at": stamp,
            "version": VERSION, "market": market.symbol}


def capture(path, setup_id, stamp, record):
    # Separate append-only diagnostics avoid copying market ticks into the financial state.
    path = Path(path)
    if path.exists() and path.stat().st_size >= 512 * 1024 * 1024:
        return False
    with sqlite3.connect(path, timeout=2) as db:
        path.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS setups (setup TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS original_fills (setup TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(setup,identity))")
        db.execute("CREATE TABLE IF NOT EXISTS observations (setup TEXT NOT NULL, stamp INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(setup,stamp))")
        metadata = {key: record[key] for key in ("plan", "family", "software_hash") if key in record}
        metadata["initial_fills"] = [row for row in record.get("original_fills", []) if row["time"] <= stamp]
        db.execute("INSERT OR IGNORE INTO setups VALUES (?,?)", (setup_id, json.dumps(metadata, default=str, separators=(",", ":"))))
        for row in record.get("original_fills", []):
            db.execute("INSERT OR IGNORE INTO original_fills VALUES (?,?,?)", (setup_id, digest(row), json.dumps(row, default=str, separators=(",", ":"))))
        value = {key: value for key, value in record.items() if key not in ("plan", "original_fills", "family", "software_hash")}
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?)", (setup_id, stamp, json.dumps(value, default=str, separators=(",", ":"))))
    return True


def queue_evidence(engine, trade_id, quote):
    """Diagnostics only, also for Standard; never enables or dispatches a wave."""
    tasks = getattr(engine, "_waverider_capture_tasks", {})
    task = tasks.get(trade_id)
    if task and not task.done():
        return
    tasks[trade_id] = asyncio.create_task(collect_evidence(engine, trade_id, quote), name="aqla-wave-evidence")
    def discard(task):
        if tasks.get(trade_id) is task:
            tasks.pop(trade_id, None)
    tasks[trade_id].add_done_callback(discard)
    engine._waverider_capture_tasks = tasks


async def collect_evidence(engine, trade_id, quote):
    try:
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
            engine._waverider_facts_cache = cache
        quote.fresh(as_of=quote.observed_at)
        from .managed_entries import original_fills
        await asyncio.to_thread(capture, Path(engine.exchange.settings.state_path).parent / "waverider-replay.sqlite3", plan.trade_id, quote.observed_at,
            {"version": VERSION, "software_hash": software_hash(), "family": plan.setup_id or plan.trade_id,
             "plan": plan.model_dump(mode="json"), "quote": quote.model_dump(mode="json"), "facts": previous,
                 "original_fills": original_fills(trade), "status": trade["status"],
                 "evidence_error": "market_facts_unavailable" if previous.get("unavailable") else None})
    except Exception:
        return


def ended(plan, trade, quote=None):
    endpoint = len(trade["targets"]) if plan.policy.waverider_until_tp == "last" else min(plan.policy.waverider_until_tp, len(trade["targets"]))
    state = trade.get("waverider", {})
    target = Decimal(trade["targets"][endpoint - 1])
    return bool(state.get("finished") or state.get("endpoint_reached_at")
        or Decimal(trade.get("tp_filled", ["0"] * len(trade["targets"]))[endpoint - 1]) > 0
        or plan.policy.waverider_lifetime == "signal_deadline" and now_ms() >= plan.deadline_at
        or quote is not None and (quote.bid >= target if plan.side == "long" else quote.ask <= target))


async def tick(engine, trade_id, quote):
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    persisted = dict(trade.get("waverider", {}))
    marker = (persisted.get("checkpoint"), persisted.get("endpoint_reached_at"), persisted.get("finished"), persisted.get("operation_id"))
    memory = getattr(engine, "_waverider_observations", {})
    state = dict(memory[trade_id][1]) if trade_id in memory and memory[trade_id][0] == marker else dict(persisted)
    if state.get("finished"):
        return
    if ended(plan, trade, quote):
        state.update(finished=True, phase="finished", reason="Configured TP or original signal deadline reached")
        engine._patch(trade_id, waverider=state)
        return
    settings = engine.exchange.settings
    root_path = Path(settings.state_path).parent
    authority = live_authority(engine, plan)
    permitted = authority is not None
    state.update(shadow=not permitted, live_authority=authority)
    try:
        if permitted and state.get("phase") in ("pending_reduce", "pending_reenter"):
            state = await resume_cycle(engine, trade_id, state)
            trade = engine.state.snapshot()["trades"][trade_id]
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
        own_quantity, basis, realized, fees = position_basis(trade)
        inventory = pools(trade, plan.policy.waverider_flex_percent)
        rebuying = state.get("phase") in ("waiting_rebound", "pending_reenter")
        quantity = quantity_grid(max(Decimal(0), inventory["rights"] - inventory["flex"]) if rebuying and permitted
                                else min(own_quantity, inventory["rights"]) if not permitted else inventory["flex"], plan.market)
        if inventory["flex"]:
            basis = inventory["flex_basis"]
        if not permitted:
            basis = Decimal(state.get("paper_basis", str(basis)))
        state.update(core_quantity=str(inventory["core"]), flexible_quantity=str(inventory["flex"]),
                     remaining_risk_budget=str(max(Decimal(0), plan.risk_usdc - inventory["spent"])))
        if quantity < plan.market.min_quantity or quantity <= 0 or quantity * quote.mid < plan.market.min_notional:
            raise AqlaError("Flexible quantity is below the exchange minimum; ordinary protection remains active")
        buy = (plan.side == "long") if rebuying else (plan.side == "short")
        limit = slippage_price(quote.mid, plan.policy.max_slippage_percent, plan.market, buy=buy)
        capacity, price = quote.executable(buy=buy, limit=limit, quantity=quantity)
        if capacity < quantity:
            raise AqlaError("Waverider depth cannot cover its flexible quantity within the price bound")
        cost = price * (Decimal(facts["fee_rate"]) * 2 + plan.policy.max_slippage_percent / 50)
        if state.get("phase") in ("warming_up", "paused"):
            state["phase"] = state.pop("resume_phase", "waiting_rise")
        state, action = advance(state, side=plan.side, price=price, basis=basis,
            volatility=Decimal(facts["atr"]), cycle_cost=cost, tick=plan.market.price_tick or Decimal(1).scaleb(-6 + plan.market.size_decimals), stamp=quote.observed_at)
        if now_ms() - state.get("last_captured_at", 0) >= 1000:
            from .managed_entries import original_fills
            await asyncio.to_thread(capture, root_path / "waverider-replay.sqlite3", plan.trade_id, quote.observed_at,
                {"version": VERSION, "software_hash": software_hash(), "family": plan.setup_id or plan.trade_id,
                 "plan": plan.model_dump(mode="json"), "original_fills": original_fills(trade), "quote": quote.model_dump(mode="json"),
                 "facts": facts, "state": state, "quantity": str(quantity), "basis": str(basis), "action": action})
            state["last_captured_at"] = now_ms()
        if action:
            if permitted:
                await execute(engine, trade_id, state, action, quantity, quote, cost)
                return
            state.setdefault("paper_actions", []).append({"action": action, "at": quote.observed_at, "price": str(price), "quantity": str(quantity)})
            state["paper_actions"] = state["paper_actions"][-20:]
            if action == "reduce":
                state.update(phase="waiting_rebound", sale_price=str(price), extreme=str(price * (1 if plan.side == "long" else -1)))
            else:
                state.update(phase="waiting_rise", cycle=state.get("cycle", 0) + 1, paper_basis=str(price), extreme=str(price * (1 if plan.side == "long" else -1)))
            for field in ("confirmation_since", "confirmation_count", "wave_atr"):
                state.pop(field, None)
        state.update(reason=None, net_realized=str(realized - fees))
    except (AqlaError, KeyError, ValueError, TypeError, OSError, sqlite3.Error) as error:
        if state.get("phase") != "paused":
            state["resume_phase"] = state.get("phase", "waiting_rise")
        state.update(phase="paused", reason=str(error) if isinstance(error, AqlaError) else "Waverider market evidence is unavailable")
    if state.get("phase") != persisted.get("phase") or state.get("operation_id") != persisted.get("operation_id") or now_ms() - persisted.get("checkpoint", 0) >= 10000:
        state["checkpoint"] = now_ms()
        engine._patch(trade_id, waverider=state)
        persisted = state
    marker = (persisted.get("checkpoint"), persisted.get("endpoint_reached_at"), persisted.get("finished"), persisted.get("operation_id"))
    memory[trade_id] = (marker, state)
    engine._waverider_observations = memory


def reset_phase(state, phase):
    result = {key: value for key, value in state.items() if key not in ("confirmation_since", "confirmation_count", "extreme", "wave_atr", "last_observed_at")}
    return {**result, "phase": phase}


async def resume_cycle(engine, trade_id, state):
    trade = engine.state.snapshot()["trades"][trade_id]
    identity = state["operation_id"]
    operation = engine.state.snapshot()["operations"].get(identity)
    if operation and operation.get("outcome") == "not_sent":
        result = {**reset_phase(state, "waiting_retrace" if state["phase"] == "pending_reduce" else "waiting_rebound"), "cycle": state.get("cycle", 0) + 1}
        engine._patch(trade_id, waverider=result)
        return result
    cloid = engine.exchange.cloid(identity)
    if state["phase"] == "pending_reenter":
        rows = [row for row in trade["entry_orders"] if row.get("cycle") == state.get("cycle", 0) and row["kind"] == "waverider" and not row.get("not_sent")]
        if rows:
            cloid = rows[-1]["cloid"]
    raw = await engine.order_status(cloid)
    if raw.get("order", {}).get("status") not in TERMINAL_ORDER:
        return state
    await engine.capture_fills(trade_id)
    current = engine.state.snapshot()["trades"][trade_id]
    oid = raw["order"]["order"]["oid"]
    fills = [row for row in current["fill_events"].values() if row["oid"] == oid]
    quantity = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
    if state["phase"] == "pending_reduce":
        if quantity:
            result = reset_phase(state, "waiting_rebound")
            result.update(sale_price=str(sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in fills) / quantity), sold_quantity=str(quantity))
        else:
            result = {**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1}
    else:
        result = {**reset_phase(state, "waiting_rise" if quantity else "waiting_rebound"), "cycle": state.get("cycle", 0) + 1}
    engine._patch(trade_id, waverider=result)
    return result


async def resume_exit(engine, trade_id):
    """Restore source targets only after the optional exit's exact outcome is known."""
    trade = engine.state.snapshot()["trades"][trade_id]
    identity = trade.get("suspended_wave_targets")
    if not identity:
        return True
    state = dict(trade["waverider"])
    operation = engine.state.snapshot()["operations"].get(identity)
    if operation is None or operation.get("outcome") == "not_sent":
        def abort(data):
            current = data["trades"][trade_id]
            for row in current["exit_orders"]:
                if row.get("operation_id") == identity:
                    row.update(not_sent=True, retired=True)
            current.update(suspended_wave_targets=None, managed_target_fingerprint=None,
                           waverider={**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1})
        engine.state.update(abort)
        return True
    cloid = engine.exchange.cloid(identity)
    status = await engine.order_status(cloid)
    if status.get("status") == "unknownOid":
        return False
    if status.get("order", {}).get("status") == "open":
        plan = TradePlan.model_validate(trade["plan"])
        await engine.exchange.mutate("cancel-wave-exit:" + identity,
            {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": cloid}]})
        status = await engine.order_status(cloid)
    if status.get("order", {}).get("status") not in TERMINAL_ORDER:
        return False
    await engine.capture_fills(trade_id)
    current = engine.state.snapshot()["trades"][trade_id]
    oid = status["order"]["order"]["oid"]
    fills = [row for row in current["fill_events"].values() if row["oid"] == oid]
    amount = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
    if amount:
        sale = sum(Decimal(row["sz"]) * Decimal(row["px"]) for row in fills) / amount
        if state.get("action") == "retire":
            state = {**reset_phase(state, "waiting_rise"), "sold_quantity": "0", "cycle": state.get("cycle", 0) + 1}
        else:
            state = {**reset_phase(state, "waiting_rebound"), "sale_price": str(sale), "sold_quantity": str(amount)}
    else:
        state = {**reset_phase(state, "waiting_rise"), "cycle": state.get("cycle", 0) + 1}
    engine._patch(trade_id, suspended_wave_targets=None, managed_target_fingerprint=None, waverider=state)
    return True


async def execute(engine, trade_id, state, action, quantity, quote, cycle_cost):
    """Scoped internal management; source close/reduce keeps its original semantics."""
    from .managed_entries import dispatch_next, patch_row
    await engine.capture_fills(trade_id)
    trade = engine.state.snapshot()["trades"][trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    if action != "retire":
        if not live_permission(engine, plan):
            raise AqlaError("Waverider live management is not authorized")
        engine.validate_configuration(engine.state.snapshot(), plan)
    signed = engine.position_size(await engine.positions(plan.market), plan.market)
    engine.owned_remaining(trade_id, signed)
    quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
    if any(row["phase"] in ("prepared", "sending", "unknown") for row in engine.state.snapshot()["operations"].values()):
        raise AqlaError("Unresolved operations block optional Waverider management")
    from .managed_entries import protected
    stops = [(row, await engine.order_status(row["cloid"])) for row in trade["orders"] if row["kind"] == "sl" and not row.get("retired")]
    if not protected(engine, trade, stops):
        raise AqlaError("Optional Waverider management requires a freshly confirmed covering stop")
    if action != "retire" and ended(plan, trade, quote):
        engine._patch(trade_id, waverider={**state, "finished": True, "phase": "finished"})
        return
    cycle = state.get("cycle", 0)
    identity = f"waverider:{trade_id}:{cycle}:{action}"
    if identity in engine.state.snapshot()["operations"]:
        return
    own, average, _, _ = position_basis(trade)
    inventory = pools(trade, plan.policy.waverider_flex_percent)
    spent = inventory["spent"]
    if any(row.get("fee") is None or row.get("feeToken") != plan.market.quote_token for row in trade.get("fill_events", {}).values()):
        raise AqlaError("Waverider needs complete actual fee evidence before live management")
    if action in ("reduce", "retire"):
        corrective = action == "retire"
        buy = plan.side == "short"
        maker = plan.take_profit_execution == "maker_only" and not corrective
        limit = passive_entry_price(plan.market, "long" if buy else "short", quote) if maker else slippage_price(quote.bid if plan.side == "long" else quote.ask, plan.policy.max_slippage_percent, plan.market, buy=buy)
        if quantity > inventory["flex"] or not corrective and (limit - inventory["flex_basis"]) * (1 if plan.side == "long" else -1) <= cycle_cost:
            state.update(phase="waiting_retrace", reason="Worst allowed exit has no net profit")
            engine._patch(trade_id, waverider=state)
            return
        from .managed_entries import target_allocations
        retained, _ = target_allocations(plan, own - quantity, trade.get("target_budgets"), trade["tp_filled"], trade.get("completed_target_indices", []))
        if not corrective and (not retained or any(size > 0 and (size < plan.market.min_quantity or size * Decimal(trade["targets"][index]) < plan.market.min_notional) for index, size in enumerate(retained))):
            raise AqlaError("Wave reduction would leave core targets below native minimums")
        order = engine.order(plan, min(quantity, own), limit, reduce=True, cloid=engine.exchange.cloid(identity))
        order["t"] = {"limit": {"tif": "Alo" if maker else "Ioc"}}
        def guard(data):
            current = data["trades"][trade_id]
            if not corrective:
                if not live_permission(engine, plan):
                    raise AqlaError("Waverider authorization changed before transport")
                engine.validate_configuration(data, plan)
            if current["status"] != "open" or not corrective and (current.get("entries_disabled") or current["waverider"].get("finished")):
                raise AqlaError("Waverider reduction no longer owns its setup")
            if not corrective and ended(plan, current, quote):
                raise AqlaError("Waverider endpoint was reached before transport")
            quote.fresh()
        def stage(data):
            current = data["trades"][trade_id]
            current.setdefault("exit_orders", []).append({"kind": "waverider_retirement" if corrective else "waverider", "reason": "retired_source_target" if corrective else "waverider", "cloid": order["c"],
                "quantity": order["s"], "price": order["p"], "cycle": cycle, "operation_id": identity})
            current.update(suspended_wave_targets=identity, waverider={**state, "phase": "preparing_reduce", "operation_id": identity, "action": action})
        engine.state.update(stage)
        await engine._cancel_owned_orders(trade_id, operation_id="wave-cancel-targets:" + identity)
        for target in [row for row in trade["orders"] if row["kind"] == "tp" and not row.get("retired")]:
            raw = await engine.order_status(target["cloid"])
            if raw.get("order", {}).get("status") not in TERMINAL_ORDER:
                raise AqlaError("Source target cancellation is unconfirmed; optional exit remains unsent")
        await engine.capture_fills(trade_id)
        latest = engine.state.snapshot()["trades"][trade_id]
        available = pools(latest, plan.policy.waverider_flex_percent)
        if corrective:
            quantity = min(quantity, max(Decimal(0), available["flex"] - available["rights"]))
            if quantity <= 0:
                await resume_exit(engine, trade_id)
                return
            from aqla_hyperliquid_mcp.api import wire_number
            order["s"] = wire_number(quantity)
            engine.state.update(lambda data: next(row for row in data["trades"][trade_id]["exit_orders"] if row["cloid"] == order["c"]).update(quantity=order["s"]))
        if quantity > available["flex"] or not corrective and latest.get("entries_disabled"):
            await resume_exit(engine, trade_id)
            return
        def retire(data):
            for target in data["trades"][trade_id]["orders"]:
                if target["kind"] == "tp":
                    target["retired"] = True
        engine.state.update(retire)
        quote = await read_observation(lambda: engine.exchange.price_snapshot(plan.market), "price")
        if not corrective and (ended(plan, latest, quote) or (limit - available["flex_basis"]) * (1 if plan.side == "long" else -1) <= cycle_cost):
            await resume_exit(engine, trade_id)
            return
        result = await engine.exchange.mutate(identity, {"type": "order", "orders": [order], "grouping": "na"}, prepare_guard=guard)
        engine._patch(trade_id, waverider={**state, "phase": "pending_reduce", "operation_id": identity, "action": action})
        if result["phase"] in ("acknowledged", "reconciled", "rejected"):
            if await resume_exit(engine, trade_id):
                from .managed_entries import refresh
                await refresh(engine, trade_id, dispatch=False)
        return
    sold = min(Decimal(state.get("sold_quantity") or 0), max(Decimal(0), inventory["rights"] - inventory["flex"]))
    if sold <= 0:
        raise AqlaError("Waverider has no proved sold quantity to rebuild")
    buy = plan.side == "long"
    maker = plan.entry_execution == "maker_only"
    limit = passive_entry_price(plan.market, plan.side, quote) if maker else slippage_price(quote.ask if buy else quote.bid, plan.policy.max_slippage_percent, plan.market, buy=buy)
    if (Decimal(state["sale_price"]) - limit) * (1 if buy else -1) <= cycle_cost:
        raise AqlaError("Waverider reentry has no price advantage after costs")
    stop = Decimal(trade["stop_price"])
    direction = Decimal(1) if plan.side == "long" else Decimal(-1)
    open_risk = (inventory["core"] * max(Decimal(0), (inventory["core_basis"] - stop) * direction)
        + inventory["flex"] * max(Decimal(0), (inventory["flex_basis"] - stop) * direction)
        + own * average * plan.policy.cost_reserve_bps / 10000)
    from .managed_entries import pending_quantity
    reserved_quantity = pending_quantity(trade)
    pending_risk = sum((Decimal(row["quantity"]) * (abs(Decimal(row["limit"]) - stop)
        + Decimal(row["limit"]) * plan.policy.cost_reserve_bps / 10000) for row in trade["entry_orders"]
        if not row.get("not_sent") and trade.get("order_observations", {}).get(row["cloid"], {}).get("status") not in ("filled", "canceled", "rejected")), Decimal(0))
    available = plan.risk_usdc - spent - open_risk - pending_risk
    maximum = quantity_grid(max(Decimal(0), available) / (abs(limit - stop) + limit * plan.policy.cost_reserve_bps / 10000), plan.market)
    pending_notional = sum((Decimal(row["quantity"]) * Decimal(row["limit"]) for row in trade["entry_orders"] if row.get("phase") == "pending" and not row.get("not_sent")), Decimal(0))
    notional_room = max(Decimal(0), plan.policy.max_notional_usdc - own * max(average, quote.ask) - pending_notional) / max(limit, quote.ask)
    quantity = quantity_grid(min(sold, maximum, plan.quantity - own - reserved_quantity, notional_room), plan.market)
    if quantity <= 0 or quantity * limit < plan.market.min_notional:
        raise AqlaError("Original lifetime risk budget cannot fund a Waverider reentry")
    row = {"kind": "waverider", "index": 0, "quantity": str(quantity), "price": str(limit), "limit": str(limit),
           "entry_kind": "limit" if maker else "market", "timing": "standard", "operation_id": identity, "cloid": engine.exchange.cloid(identity), "phase": "pending", "cycle": cycle}
    def stage(data):
        current = data["trades"][trade_id]
        if current.get("entries_disabled") or current["waverider"].get("finished"):
            raise AqlaError("Waverider reentry was terminated")
        if any(item["operation_id"] == identity for item in current["entry_orders"]):
            raise AqlaError("Waverider reentry identity already exists")
        current["entry_orders"].append(row)
        current["waverider"] = {**state, "phase": "pending_reenter", "operation_id": identity}
    engine.state.update(stage)
    await dispatch_next(engine, trade_id)
    patch_row(engine, trade_id, row["cloid"], cycle=cycle)
