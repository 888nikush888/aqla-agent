"""Durable owner management requests using the same native position and protection gates."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, Positive, TradePlan, digest, now_ms


class ManagementRequest(Model):
    trade_id: str = Field(min_length=1, max_length=128)
    action: Literal["close", "reduce", "move_stop", "break_even", "replace_targets", "cancel_entry", "pause_waverider", "resume_waverider"]
    percent: Positive | None = Field(default=None, le=100)
    stop: Positive | None = None
    targets: list[Positive] | None = Field(default=None, min_length=1, max_length=20)
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def complete_action(self):
        if self.action == "reduce" and self.percent is None or self.action == "move_stop" and self.stop is None or self.action == "replace_targets" and not self.targets:
            raise ValueError("Die gewählte Aktion benötigt einen vollständigen Wert")
        if self.action != "reduce" and self.percent is not None or self.action != "move_stop" and self.stop is not None or self.action != "replace_targets" and self.targets is not None:
            raise ValueError("Diese Werte gehören nicht zur gewählten Aktion")
        return self

    @property
    def update_action(self):
        return self.action

    @property
    def source_refs(self):
        return []  # This is an owner instruction, never a fabricated source message.


def fingerprint(trade):
    return digest({key: trade.get(key) for key in ("trade_id", "plan", "management_revision", "status",
                   "filled_quantity", "remaining_quantity", "stop_price", "targets", "fill_events")})


def save(engine, identity, **values):
    engine.state.root.update(lambda data: data["operator_requests"][identity].update(**values))


async def dispatch(engine, identity):
    root = engine.state.root
    saved = root.read("operator_requests")["operator_requests"][identity]
    if saved.get("status") in ("done", "rejected", "superseded"):
        return {"request_id": identity, "status": saved["status"], "duplicate": True}
    request = ManagementRequest.model_validate(saved["request"])
    trade = engine.state.read("trades", entries={"trades": [request.trade_id]})["trades"][request.trade_id]
    plan = TradePlan.model_validate(trade["plan"])
    engine.assert_plan_binding(engine.state.snapshot(), plan)
    operation = root.read("operations", entries={"operations": [identity]})["operations"].get(identity)
    if operation:
        if operation["phase"] in ("prepared", "sending", "unknown"):
            operation = await engine.exchange.reconcile(identity)
        status = "pending" if operation["phase"] in ("prepared", "sending", "unknown") else "rejected" if operation["phase"] == "rejected" else "done"
        save(engine, identity, status=status)
        return {"request_id": identity, "status": status}
    if trade["status"] in ("closed", "cancelled", "rejected", "expired"):
        save(engine, identity, status="superseded", reason="Position ended")
        return {"request_id": identity, "status": "superseded"}
    if trade["status"] in ("conflicted", "cleanup"):
        raise AqlaError("Eigentum oder Abschluss ungeklärt; zuerst vorhandene Operationen abgleichen")
    action = request.action
    staged = identity in trade.get("close_intents", {}) or identity in trade.get("target_updates", {}) or (trade.get("pending_stop_update") or {}).get("operation_id") == identity
    if not staged and request.action not in ("close", "reduce") and fingerprint(trade) != request.expected_fingerprint:
        save(engine, identity, status="superseded", reason="A later position or management change superseded this unstarted request")
        return {"request_id": identity, "status": "superseded"}
    if action not in ("pause_waverider", "resume_waverider") and action not in plan.policy.allowed_updates:
        raise AqlaError("Diese Verwaltung ist im gespeicherten Trade-Profil nicht erlaubt")
    if action in ("move_stop", "break_even"):
        stop = request.stop if action == "move_stop" else Decimal(trade["entry_vwap"])
        if stop is None:
            raise AqlaError("Ein bestätigter Stop-Preis ist erforderlich")
        # queue_stop_update persists the identity before replacement and recovers on every refresh.
        result = await engine.queue_stop_update(request.trade_id, identity, request, stop, operation_id=identity)
        save(engine, identity, status="pending" if result.get("pending_stop_update") else "rejected" if result.get("phase") == "rejected" else "done")
    elif action in ("close", "reduce"):
        if action == "reduce" and request.percent is None:
            raise AqlaError("Teilreduktion benötigt einen ausdrücklichen Prozentwert")
        result = await engine.close_position(request.trade_id, operation_id=identity,
            **({"quantity": Decimal(saved["original_quantity"]) * request.percent / 100} if action == "reduce" else {}))
        save(engine, identity, status="pending" if result.get("phase") in ("prepared", "sending", "unknown") else "rejected" if result.get("phase") == "rejected" else "done")
    elif action == "replace_targets":
        if not request.targets:
            raise AqlaError("Vollständige TP-Preise erforderlich")
        result = await engine.replace_targets(request.trade_id, request.targets, identity, source_tp={"unit": "price", "origin": "operator"})
        save(engine, identity, status="pending" if result.get("phase") in ("prepared", "sending", "unknown") else "rejected" if result.get("phase") == "rejected" else "done")
    elif action == "cancel_entry":
        await engine.cancel_entry(request.trade_id)
        await engine.refresh_trade(request.trade_id)
        save(engine, identity, status="done")
    else:
        from .waverider import ended, live_permission, resume_exit
        wave_id = trade.get("waverider", {}).get("operation_id")
        wave_operation = root.read("operations", entries={"operations": [wave_id]})["operations"].get(wave_id) if wave_id else None
        if wave_operation and wave_operation["phase"] in ("prepared", "sending", "unknown"):
            reconciled = await engine.exchange.reconcile(wave_id)
            if reconciled["phase"] in ("prepared", "sending", "unknown"):
                raise AqlaError("Der ursprüngliche Waverider-Auftrag ist noch ungeklärt")
        quote = await engine.exchange.price_snapshot(plan.market)
        quote.fresh()
        if plan.policy.management_mode != "waverider" or ended(plan, trade, quote):
            save(engine, identity, status="superseded", reason="Waverider endpoint already reached")
            return {"request_id": identity, "status": "superseded"}
        if action == "resume_waverider" and not live_permission(engine, plan):
            save(engine, identity, status="rejected", reason="Waverider permission changed")
            return {"request_id": identity, "status": "rejected"}
        if trade.get("suspended_wave_targets"):
            await resume_exit(engine, request.trade_id)
            trade = engine.state.read("trades", entries={"trades": [request.trade_id]})["trades"][request.trade_id]
            if trade.get("suspended_wave_targets"):
                raise AqlaError("Zuerst den bereits begonnenen Waverider-Auftrag abgleichen")
        engine._patch(request.trade_id, waverider_paused=action == "pause_waverider")
        save(engine, identity, status="done")
    return {"request_id": identity, "status": root.read("operator_requests")["operator_requests"][identity]["status"]}


async def submit(router, request, request_id, commit_check):
    identity = "operator:" + request_id
    original = router.state.read("trades", "operator_requests").get("trades", {}).get(request.trade_id)
    if original is None:
        raise AqlaError("Wähle einen vorhandenen eigenen Trade")
    engine = router.engine_for_plan(original["plan"])
    if engine is None:
        raise AqlaError("Das ursprüngliche Ausführungskonto ist nicht verfügbar")
    async with engine.lock.work(1):
        def accept(data):
            commit_check()
            rows = data.setdefault("operator_requests", {})
            payload = request.model_dump(mode="json")
            if identity in rows:
                if rows[identity]["request"] != payload:
                    raise AqlaError("Auftragsidentität gehört zu anderen Werten")
                return
            if any(row.get("status") == "pending" and row["request"]["trade_id"] == request.trade_id for row in rows.values()):
                raise AqlaError("Ein angenommener Verwaltungsauftrag für diesen Trade ist noch ungeklärt; keine zweite Order")
            if any(row["phase"] in ("prepared", "sending", "unknown") and row.get("account_key", row.get("exchange", "hyperliquid") + ":" + row.get("request", {}).get("network", "")) == engine.state.account_key
                   for row in data["operations"].values()):
                raise AqlaError("Ungeklärte Börsenoperation zuerst mit ihrer vorhandenen Identität abgleichen")
            trade = data["trades"][request.trade_id]
            if fingerprint(trade) != request.expected_fingerprint:
                raise AqlaError("Trade hat sich geändert; aktuelle Werte erneut prüfen")
            plan = TradePlan.model_validate(trade["plan"])
            engine.assert_plan_binding(data, plan)
            if trade["status"] in ("conflicted", "cleanup"):
                raise AqlaError("Trade hat ein ungeklärtes Eigentum oder einen ungeklärten Abschluss")
            if request.action in ("pause_waverider", "resume_waverider"):
                from .waverider import ended, live_permission
                if plan.policy.management_mode != "waverider" or ended(plan, trade):
                    raise AqlaError("Dieser Trade hat keinen noch aktiven Waverider")
                if request.action == "resume_waverider" and not live_permission(engine, plan):
                    raise AqlaError("Die bestehende Waverider-Livefreigabe fehlt")
            elif request.action not in plan.policy.allowed_updates:
                raise AqlaError("Diese Verwaltung ist im gespeicherten Trade-Profil nicht erlaubt")
            if request.action == "reduce":
                from aqla_hyperliquid_mcp.rules import quantity_grid
                if quantity_grid(Decimal(trade["remaining_quantity"]) * request.percent / 100, plan.market) <= 0:
                    raise AqlaError("Diese Teilreduktion rundet auf null")
            if request.action in ("move_stop", "break_even"):
                proposed = request.stop if request.action == "move_stop" else trade.get("entry_vwap")
                if proposed is None:
                    raise AqlaError("Break-even benötigt einen bestätigten Entry")
                engine.validate_stop_budget(trade, plan, Decimal(proposed))
            if request.action == "replace_targets":
                engine.validate_targets(trade, plan, request.targets)
            rows[identity] = {"request": payload, "status": "pending", "accepted_at": now_ms(),
                              "original_quantity": trade["remaining_quantity"], "account_key": engine.state.account_key}
        router.state.update_if_changed(accept)
        try:
            return await dispatch(engine, identity)
        except AqlaError:
            # The accepted durable intent may already have produced an operation.
            return {"request_id": identity, "status": "pending", "reason": "Abgleich des angenommenen Auftrags ausstehend"}


async def recover(engine):
    for identity, row in engine.state.root.read("operator_requests").get("operator_requests", {}).items():
        if row.get("status") != "pending" or row.get("account_key") != engine.state.account_key:
            continue
        async with engine.lock.work(1):
            await dispatch(engine, identity)
