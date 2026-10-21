"""Compare executable first-leg depth without creating any exchange exposure."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, Interpretation, TradePlan, now_ms

from .entry import execution_limit
from .managed_entries import leg_view
from .observations import read_observation


def score(plan, quote):
    first = plan.entry_legs[0] if plan.entry_legs else None
    view = leg_view(plan, {"index": first.index, "quantity": str(first.quantity)}) if first else plan
    if view.entry_execution == "maker_only" or view.entry_timing == "deferred":
        return Decimal(0), Decimal(0)
    quantity, price = quote.executable(buy=plan.side == "long", limit=execution_limit(view), quantity=view.quantity)
    coverage = quantity / view.quantity
    advantage = ((view.entry_limit - price) if plan.side == "long" else (price - view.entry_limit)) / view.entry_limit if price else Decimal(0)
    return coverage, advantage


async def select(router, current, target, requested, network, replacements, preview_on, reserved, basis):
    readiness = router.accounts_ready(basis)
    original_venue = target["exchange"]
    venues = list(dict.fromkeys([requested, *replacements]))
    if original_venue != requested:
        venues = [venue for venue in venues if venue != requested]
    observed_ids = {current["plan_id"]}
    async def candidate(venue):
        engine = router.engine_for(venue, network)
        key = venue + ":" + network
        if engine is None or not readiness.get(key, {}).get("enabled") or not readiness.get(key, {}).get("limits"):
            return None
        if venue != original_venue and (key, current["source_symbol"].upper()) in reserved:
            return None
        route = {**target, "exchange": venue}
        if venue != requested:
            route["fallback_reason"] = target.get("fallback_reason") or "Executable source-bound order book depth is better on the replacement venue"
        try:
            raw = current if venue == original_venue else await preview_on(engine, route)
            observed_ids.add(raw["plan_id"])
            router.state.update(lambda data: data["plans"][raw["plan_id"]].update(liquidity_candidate=True, liquidity_selected=False))
            plan = TradePlan.model_validate(raw)
            reader = getattr(engine.exchange, "depth_snapshot", engine.exchange.price_snapshot)
            quote = await read_observation(lambda: reader(plan.market), "price")
            quality = score(plan, quote)
            return engine, route, raw, quality
        except (AqlaError, TimeoutError, OSError, KeyError, ValueError):
            return None
    observed = await asyncio.gather(*(candidate(venue) for venue in venues), return_exceptions=True)
    candidates = [row for row in observed if row is not None and not isinstance(row, BaseException)]
    winner = max(candidates, key=lambda row: (row[3][0], row[3][1], -venues.index(row[1]["exchange"])), default=None)
    if winner is None or winner[3][0] <= 0:
        router.state.update(lambda data: data["plans"][current["plan_id"]].update(liquidity_candidate=True, liquidity_selected=True))
        return router.engine_for(original_venue, network), target, current
    engine, route, raw, quality = winner
    if route["exchange"] != requested:
        route["fallback_reason"] = target.get("fallback_reason") or "Executable source-bound order book depth is better on the replacement venue"
    def bind(data):
        for identity in observed_ids:
            saved = data["plans"][identity]
            saved.update(liquidity_candidate=True, liquidity_selected=identity == raw["plan_id"])
        data["plans"][raw["plan_id"]]["liquidity_observation"] = {"coverage": str(quality[0]), "price_advantage": str(quality[1]),
            "candidates": [{"exchange": row[1]["exchange"], "coverage": str(row[3][0]), "price_advantage": str(row[3][1])} for row in candidates]}
    router.state.update(bind)
    return engine, route, raw


async def resume_switch(router, trade_id):
    from .managed_entries import entry_final
    snapshot = router.state.snapshot()
    trade = snapshot["trades"][trade_id]
    switch = trade["venue_switch"]
    original = router.engine_for_plan(trade["plan"])
    candidate = snapshot["plans"][switch["plan_id"]]
    if switch["phase"] == "canceling":
        async with original.lock:
            await original.cancel_entry(trade_id)
            await original.capture_fills(trade_id)
            current = router.state.snapshot()["trades"][trade_id]
            plan = TradePlan.model_validate(current["plan"])
            signed = original.position_size(await original.positions(plan.market), plan.market)
            if Decimal(current["filled_quantity"]) or signed:
                original._patch(trade_id, venue_switch={**switch, "phase": "abandoned", "reason": "Fill occurred before cancellation was confirmed"})
                await original.refresh_trade(trade_id)
                return
            if not entry_final(current):
                raise AqlaError("Previous entry quantities are not fully reconciled")
            if any(row["phase"] in ("prepared", "sending", "unknown") for row in original.state.snapshot()["operations"].values()):
                raise AqlaError("Previous venue operations are not fully reconciled")
            if current["status"] != "cancelled" or not current.get("cleanup_complete"):
                original.begin_cleanup(trade_id, "cancelled")
                await original.finish_cleanup(trade_id)
        def release(data):
            parent = data["trades"][trade_id]
            if Decimal(parent["filled_quantity"]) or not parent.get("cleanup_complete") or parent["status"] != "cancelled":
                raise AqlaError("Previous venue has not proved zero-fill release")
            saved = data["plans"][switch["plan_id"]]
            saved.update(rerouted_from_trade_id=trade_id, liquidity_candidate=True, liquidity_selected=True,
                         managed_entry_retry=parent.get("managed_entry_retry"))
            parent.update(rerouted_to_plan_id=switch["plan_id"], venue_switch={**switch, "phase": "released"})
            for row in data["events"][candidate["event_id"]].get("targets", {}).values():
                if row.get("trade_id") == trade_id:
                    row.update(switch["route"], plan_id=switch["plan_id"], status="planned")
                    row.pop("trade_id", None)
        router.state.update(release)
    await router.execute(switch["plan_id"])
    router.state.update(lambda data: data["trades"][trade_id]["venue_switch"].update(phase="done"))


async def reroute_waiting(router):
    """One durable zero-fill handoff; never move exposure or restart a deadline."""
    for identity, trade in list(router.state.read("trades")["trades"].items()):
        if trade.get("venue_switch", {}).get("phase") in ("canceling", "released"):
            await resume_switch(router, identity)
            continue
        if (trade.get("lifecycle_version") != 2 or trade.get("venue_switch_count", 0)
                or trade.get("venue_switch") or trade.get("entries_disabled") or trade.get("entry_tp1_reached_at")
                or trade["status"] not in ("opening", "open") or Decimal(trade["filled_quantity"])):
            continue
        old = TradePlan.model_validate(trade["plan"])
        if old.deadline_at <= now_ms() or old.requested_exchange != "hyperliquid" or not old.rank_snapshot:
            continue
        previous = trade.get("managed_entry_retry")
        if previous and previous.get("exhausted"):
            continue
        engine = router.engine_for_plan(trade["plan"])
        if not engine or not engine.recovery_checked:
            continue
        if any(row["phase"] in ("prepared", "sending", "unknown") for row in engine.state.snapshot()["operations"].values()):
            continue
        reader = getattr(engine.exchange, "depth_snapshot", engine.exchange.price_snapshot)
        try:
            quality = score(old, await read_observation(lambda read=reader, market=old.market: read(market), "price"))
        except (AqlaError, OSError, TimeoutError, KeyError, ValueError):
            continue
        basis = router.state.snapshot()
        pipeline = basis["pipelines"][old.pipeline_id]
        configured = next(row for row in pipeline["targets"] if row["exchange"] == old.requested_exchange)
        replacements = configured.get("fallback_exchanges")
        replacements = ["aster", "lighter"] if replacements is None else replacements
        readiness = router.accounts_ready(basis)
        event_id = basis["plans"][old.plan_id]["event_id"]
        signal = Interpretation.model_validate(basis["events"][event_id]["interpretation"])
        choices = []
        for venue in dict.fromkeys([old.requested_exchange, *replacements]):
            if venue == old.exchange:
                continue
            candidate = router.engine_for(venue, old.network)
            key = venue + ":" + old.network
            if candidate is None or not candidate.recovery_checked or not readiness.get(key, {}).get("enabled") or not readiness.get(key, {}).get("limits"):
                continue
            route = {"pipeline_id": old.pipeline_id, "profile_id": old.profile_id, "exchange": venue, "network": old.network,
                "requested_exchange": old.requested_exchange, "rank_snapshot": old.rank_snapshot, "setup_id": old.setup_id,
                "leg_id": old.routing_leg_id, "fallback_reason": "Unfilled source-bound entry moved after confirmed cancellation and zero-fill reconciliation"}
            try:
                async with candidate.lock:
                    with candidate.state.scope(route):
                        raw = await candidate.preview(event_id, signal, _quantity_cap=old.quantity)
                router.state.update(lambda data, plan_id=raw["plan_id"]: data["plans"][plan_id].update(liquidity_candidate=True, liquidity_selected=False))
                plan = TradePlan.model_validate(raw)
                read = getattr(candidate.exchange, "depth_snapshot", candidate.exchange.price_snapshot)
                value = score(plan, await read_observation(lambda read=read, market=plan.market: read(market), "price"))
                if value[0] > 0 and value > quality and plan.deadline_at == old.deadline_at:
                    choices.append((value, route, raw))
            except (AqlaError, OSError, TimeoutError, KeyError, ValueError):
                continue
        if not choices:
            continue
        _, route, raw = max(choices, key=lambda row: row[0])
        def claim(data, identity=identity, plan_id=raw["plan_id"], route=route):
            current = data["trades"][identity]
            if current.get("venue_switch") or Decimal(current["filled_quantity"]) or current.get("entries_disabled"):
                raise AqlaError("Setup changed before zero-fill venue handoff")
            current.update(venue_switch_count=1, venue_switch={"phase": "canceling", "plan_id": plan_id, "route": route})
        router.state.update(claim)
        await resume_switch(router, identity)
