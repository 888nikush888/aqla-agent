"""Durable reduction-then-stop instructions; Python owns order and fixed quantity basis."""

import re
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, Interpretation, TradePlan, digest, now_ms, require_first
from aqla_hyperliquid_mcp.rules import management_steps, price_grid, quantity_grid

from .interpretation_contract import validate_reduction_quantity
from .semantic_contract import current_structure_text
from .source_candidates import build_packet


def finish(engine, trade_id, event_id, phase, reason=None):
    def commit(data):
        sequence = data["trades"][trade_id]["management_sequences"][event_id]
        sequence.update(completed=True, phase=phase, reason=reason)
        data["events"][event_id]["status"] = "updated" if phase == "confirmed" else phase
        if phase == "rejected":
            data["notifications"].setdefault("management-sequence:" + event_id,
                {"trade_id": trade_id, "event_id": event_id, "severity": "urgent", "error": reason, "observed_at": now_ms()})
    engine.state.update(commit)


def validate_sequence_market(engine, packet, text):
    symbols = {market.symbol.split(":")[-1].upper().removesuffix("USDT").removesuffix("USDC") for market in engine.exchange.market_cache.values()}
    symbols.update(row["symbol"].split(":")[-1].upper() for row in packet["trades"] if row.get("symbol"))
    named = {symbol for symbol in symbols - {"LONG", "SHORT", "BUY", "SELL"} if re.search(r"(?<!\w)" + re.escape(symbol) + r"(?:USDT|USDC)?(?:\.P)?(?!\w)", text, re.I)}
    if len(named) > 1:
        raise AqlaError("Management sequence must identify one current market; several named markets require review")


def validate_sequence_stop_estimate(engine, signal, trade, plan):
    if signal.management_steps[1].action == "move_stop":
        estimate = Decimal(trade["remaining_quantity"]) * (1 - signal.management_steps[0].reduce_percent / 100)
        proposed = price_grid(signal.management_steps[1].stop_loss, plan.market, up=plan.side == "long")
        engine.validate_stop_budget(trade, plan, proposed, size=estimate)


async def accept_sequence(engine, event_id, signal, identity):
    candidates = engine.update_candidates(event_id, signal)
    if len(candidates) != 1:
        raise AqlaError("Management sequence requires one unambiguous own position")
    trade = candidates[0]
    if trade["status"] not in ("open", "protecting"):
        raise AqlaError("Management sequence needs a confirmed existing own position")
    plan = TradePlan.model_validate(trade["plan"])
    if any(step.action not in plan.policy.allowed_updates for step in signal.management_steps):
        raise AqlaError("Every management step must be permitted by the original user rules")
    validate_sequence_stop_estimate(engine, signal, trade, plan)
    if not await engine.resume_close_intents(trade["trade_id"]):
        raise AqlaError("The original close intention must be reconciled before another instruction")
    await engine.management_stop_preflight(trade["trade_id"], identity)
    await engine.cancel_entry(trade["trade_id"])
    await engine.capture_fills(trade["trade_id"])
    size = engine.owned_remaining(trade["trade_id"], engine.position_size(await engine.positions(plan.market), plan.market))
    amount = quantity_grid(size * signal.management_steps[0].reduce_percent / 100, plan.market)
    if amount <= 0 or amount >= size:
        raise AqlaError("Partial management sequence must leave a real own remainder")
    if signal.management_steps[1].action == "move_stop":
        current = engine.state.read("trades", entries={"trades": [trade["trade_id"]]})["trades"][trade["trade_id"]]
        engine.validate_stop_budget(current, plan, signal.management_steps[1].stop_loss, size=size - amount)
    revisions = engine.source_revisions(engine.state.snapshot(), signal.source_refs)
    sequence = {"event_id": event_id, "signal": signal.model_dump(mode="json"), "source_revisions": revisions,
                "base_quantity": str(size), "requested_reduction": str(amount), "operations": [], "amounts": {}, "phase": "reducing", "completed": False, "created_at": now_ms()}
    def accept(data):
        if engine.source_revisions(data, signal.source_refs) != revisions:
            raise AqlaError("Source changed before compound management acceptance")
        row = data["trades"][trade["trade_id"]]
        row.setdefault("management_sequences", {})[event_id] = sequence
        data["bindings"][identity] = {"trade_id": trade["trade_id"], "kind": "sequence", "payload_hash": digest(signal.model_dump(mode="json"))}
        data["events"][event_id]["status"] = "needs_reconciliation"
    engine.state.update(accept)
    return trade


async def start(engine, event_id, signal):
    packet = build_packet(engine.context(event_id))
    text = current_structure_text(require_first(row["text"] for row in packet["messages"] if row["event_id"] == event_id))
    validate_sequence_market(engine, packet, text)
    for child in management_steps(signal):
        validate_reduction_quantity(packet, child)
    engine.record_interpretation(event_id, signal)
    snapshot = engine.state.snapshot()
    identity = "update:" + event_id
    prior = snapshot["bindings"].get(identity)
    if prior:
        if prior.get("kind") != "sequence" or prior.get("payload_hash") != digest(signal.model_dump(mode="json")):
            raise AqlaError("This source is already bound to another management instruction")
        trade = snapshot["trades"][prior["trade_id"]]
    else:
        trade = await accept_sequence(engine, event_id, signal, identity)
    await resume(engine, trade["trade_id"])
    current = engine.state.read("trades", entries={"trades": [trade["trade_id"]]})["trades"][trade["trade_id"]]
    sequence = current["management_sequences"][event_id]
    return {"trade_id": trade["trade_id"], "result": {"phase": sequence["phase"], "pending_sequence": not sequence["completed"],
            "quantity_basis": sequence["base_quantity"], "reason": sequence.get("reason")}}


def sequence_reduction_filled(engine, current, sequence):
    filled = sum((Decimal(fill["sz"]) for fill in current.get("fill_events", {}).values()
        if fill["kind"] == "close" and any(current.get("order_observations", {}).get(row["cloid"], {}).get("oid") == fill["oid"]
            for row in current.get("exit_orders", []) if row.get("operation_id") in sequence["operations"]
            or any(row["cloid"] == engine.exchange.cloid(identity) for identity in sequence["operations"]))), Decimal(0))
    return filled


async def resume_sequence_reduction(engine, trade_id, event_id, sequence, needed):
    operations = engine.state.read("operations")["operations"]
    reserved = next((identity for identity in sequence["operations"] if identity not in operations), None)
    if reserved is not None:
        await engine.close_position(trade_id, operation_id=reserved, quantity=Decimal(sequence["amounts"][reserved]))
        return True
    if now_ms() < sequence.get("next_attempt_at", 0):
        return True
    if len(sequence["operations"]) >= 3:
        finish(engine, trade_id, event_id, "rejected", "Combined management needs review: requested reduction remains incomplete; original protection retained")
        return False
    identity = f"update:{event_id}:step:0:part:{len(sequence['operations'])}"
    def reserve(data, event_id=event_id, identity=identity, needed=needed):
        saved = data["trades"][trade_id]["management_sequences"][event_id]
        saved["operations"].append(identity)
        saved["amounts"][identity] = str(needed)
        saved["next_attempt_at"] = now_ms() + 30000
    engine.state.update(reserve)
    await engine.close_position(trade_id, operation_id=identity, quantity=needed)
    return True


async def resume_sequence_stop(engine, trade_id, event_id, sequence, current):
    source = Interpretation.model_validate(sequence["signal"])
    stop_signal = management_steps(source)[1]
    stop_identity = f"update:{event_id}:step:1"
    pending = current.get("pending_stop_update")
    if pending and pending["operation_id"] == stop_identity:
        await engine.resume_pending_stop(trade_id)
        pending = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"]
        if pending["phase"] == "confirmed":
            finish(engine, trade_id, event_id, "confirmed")
        elif pending["phase"] not in ("waiting_fill", "replacing", "unknown"):
            finish(engine, trade_id, event_id, "rejected", "Combined management needs review: remainder stop was not confirmed")
    else:
        price = Decimal(current["entry_vwap"]) if stop_signal.update_action == "break_even" else stop_signal.stop_loss
        await engine.queue_stop_update(trade_id, event_id, stop_signal, price, operation_id=stop_identity)
        pending = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"]
        if pending["phase"] == "confirmed":
            finish(engine, trade_id, event_id, "confirmed")


async def resume(engine, trade_id):
    if getattr(engine, "_sequence_running", False):
        return
    # Reentrancy marker for the owning engine's persisted management sequence.
    # skipcq: PYL-W0212
    engine._sequence_running = True
    try:
        trade = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        for event_id, sequence in trade.get("management_sequences", {}).items():
            if sequence["completed"]:
                continue
            if any(op["phase"] in ("prepared", "sending", "unknown") for op in engine.state.read("operations")["operations"].values()):
                return
            if engine.source_revisions(engine.state.snapshot(), sequence["source_revisions"]) != sequence["source_revisions"]:
                finish(engine, trade_id, event_id, "superseded", "Source changed; no unsubmitted later step was executed")
                continue
            await engine.refresh_trade(trade_id)
            current = engine.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            if current["status"] in ("closed", "cleanup") or Decimal(current["remaining_quantity"]) == 0:
                finish(engine, trade_id, event_id, "confirmed", "Own position is fully closed; no remainder needs a stop")
                continue
            filled = sequence_reduction_filled(engine, current, sequence)
            needed = max(Decimal(0), Decimal(sequence["requested_reduction"]) - filled)
            if needed:
                if await resume_sequence_reduction(engine, trade_id, event_id, sequence, needed):
                    return
                continue
            await resume_sequence_stop(engine, trade_id, event_id, sequence, current)
    finally:
        # Reentrancy marker for the owning engine's persisted management sequence.
        # skipcq: PYL-W0212
        engine._sequence_running = False
