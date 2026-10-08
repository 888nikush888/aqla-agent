"""Internal evidence reconciliation and publication jobs; no exchange mutation path."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, Market, TradePlan, digest, now_ms

from .funding import bounded_history
from .monthly import CLOSED, accounting_facts, entry_quantity_evidence, fill_identity


class EvidenceConflict(AqlaError):
    """New positive evidence contradicts a previously verified attribution."""


class Reporting:
    def __init__(self, state, router):
        self.state, self.router = state, router

    async def hydrate(self, trade_id, engine):
        """Backfill only fills belonging to stored own order identities; never adopt manual exits."""
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        engine.assert_plan_binding(engine.state.snapshot(), plan)
        adapter = engine.exchange
        if hasattr(adapter, "market_cache"):
            adapter.market_cache.setdefault(plan.market.symbol, plan.market)
        mapping = {}
        observations = {}
        confirmed_quantity = None
        orders = [{"kind": "entry", "cloid": trade["entry_cloid"]}, *trade.get("orders", []), *trade.get("exit_orders", [])]
        for order in orders:
            observed = await adapter.order_status(order["cloid"])
            if order["kind"] == "entry":
                confirmed_quantity = entry_quantity_evidence(observed)
            detail = observed.get("order", {})
            oid = detail.get("order", {}).get("oid")
            if oid is not None:
                mapping[oid] = order
                observations[order["cloid"]] = {"oid": oid, "status": detail["status"]}
        async def fetch(low, high):
            return await adapter.info("userFillsByTime", user=adapter.account, startTime=low, endTime=high)
        rows = await bounded_history(fetch, plan.created_at, trade.get("closed_at") or trade.get("cleanup_observed_at") or now_ms(), limit=2000)
        additions = {}
        for row in rows:
            if row.get("coin") != plan.market.symbol or row.get("oid") not in mapping:
                continue
            identity = fill_identity(row)
            fact = {key: row.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin")}
            fact.update(kind=mapping[row["oid"]]["kind"], index=mapping[row["oid"]].get("index"))
            additions[identity] = fact
        def save(data):
            current = data["trades"][trade_id]
            for identity, fact in additions.items():
                current.setdefault("fill_events", {}).setdefault(identity, fact)
            for identity, fact in observations.items():
                current.setdefault("order_observations", {})[identity] = fact
            if confirmed_quantity is not None:
                current["entry_fill_quantity_confirmed"] = confirmed_quantity
            current.update(accounting_facts(current))
        self.state.update(save)

    async def refresh(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        engine = self.router.engine_for_plan(trade["plan"]) if hasattr(self.router, "engine_for_plan") else self.router.engines.get(trade["plan"].get("exchange", "hyperliquid"))
        if engine and (not trade.get("entry_accounting") or not trade.get("closed_at") or trade.get("entry_fill_quantity_confirmed") is None):
            try:
                await self.hydrate(trade_id, engine)
                trade = self.state.snapshot()["trades"][trade_id]
            except Exception:
                self.state.update(lambda data: data["trades"][trade_id].update(reporting_checked_at=now_ms(),
                    funding_coverage={"complete": False, "reason": "Historische Entry-/Exit-Belege nicht vollständig verfügbar"}))
                return
        metrics = accounting_facts(trade)
        if metrics:
            self.state.update(lambda data: data["trades"][trade_id].update(metrics))
            trade = self.state.snapshot()["trades"][trade_id]
        entry = trade.get("entry_accounting", {})
        start, end = entry.get("entry_at"), trade.get("closed_at")
        if not start or not end:
            self.state.update(lambda data: data["trades"][trade_id].update(reporting_checked_at=now_ms()))
            return
        coverage = {"start": start, "end": end, "complete": False}
        try:
            if engine is None:
                raise AqlaError("Börsenkonto für Funding-Abgleich nicht verfügbar")
            engine.assert_plan_binding(engine.state.snapshot(), TradePlan.model_validate(trade["plan"]))
            if trade.get("external_conflict") or trade.get("manual_closure"):
                raise EvidenceConflict("Funding bei manuellem/gemischtem Positionsverlauf nicht eindeutig zuordenbar")
            adapter = engine.exchange
            market = Market.model_validate(trade["plan"]["market"])
            # Restore immutable native market identities for historical Aster/Lighter reads.
            if hasattr(adapter, "market_cache"):
                adapter.market_cache.setdefault(market.symbol, market)
            fills = trade["fill_events"]
            async def fetch(low, high):
                return await adapter.info("userFillsByTime", user=adapter.account, startTime=low, endTime=high)
            observed = await bounded_history(fetch, trade["plan"]["created_at"], end, limit=2000)
            observed = {fill_identity(row): row for row in observed if row.get("coin") == market.symbol
                        and trade["plan"]["created_at"] <= row["time"] <= end}
            if set(observed) - set(fills):
                raise EvidenceConflict("Zusätzliche manuelle Fills im eigenen Positionsverlauf vorhanden")
            if set(fills) - set(observed):
                raise AqlaError("Historische eigene Fills nicht vollständig aus der Börse abrufbar")
            payments = await adapter.funding_payments(market, start, end)
            assigned = {}
            for fact in payments:
                timestamp = fact["time"]
                if fact["market"] != market.symbol or fact["currency"] != market.quote_token or not start <= timestamp <= end:
                    raise EvidenceConflict("Funding-Markt, Zeitraum oder Währung stimmt nicht überein")
                if any(row["time"] == timestamp for row in fills.values()):
                    raise EvidenceConflict("Reihenfolge von Funding und Fills zum selben Zeitpunkt nicht eindeutig")
                size = sum((Decimal(row["sz"]) * (1 if row["kind"] == "entry" else -1)
                            for row in fills.values() if row["time"] < timestamp), Decimal(0))
                if size <= 0:
                    raise EvidenceConflict("Funding-Zahlung ohne belegte eigene Position")
                signed = size * (1 if trade["plan"]["side"] == "long" else -1)
                if fact["signed_size"] is not None and Decimal(fact["signed_size"]) != signed:
                    raise EvidenceConflict("Funding-Positionsmenge stimmt nicht mit eigenen Fills überein")
                identity = fact["identity"]
                if identity in assigned and assigned[identity] != fact:
                    raise EvidenceConflict("Funding-Identität ist widersprüchlich")
                old = trade.get("funding_events", {}).get(identity)
                if old and old != fact:
                    raise EvidenceConflict("Ein unveränderlicher Funding-Beleg wurde geändert")
                assigned[identity] = fact
            if set(trade.get("funding_events", {})) - set(assigned):
                raise AqlaError("Bereits gespeicherte Funding-Belege fehlen im erneuten vollständigen Abgleich")
            coverage.update(complete=True, fill_fingerprint=digest(fills), payments_fingerprint=digest(assigned))
            fee_evidence = {identity: {"fee": row.get("fee"), "feeToken": row.get("feeToken")}
                            for identity, row in observed.items()}
            for identity, fact in fee_evidence.items():
                old = trade.get("report_fee_evidence", {}).get(identity, fills[identity])
                for key in ("fee", "feeToken"):
                    if old.get(key) is not None:
                        same = Decimal(old[key]) == Decimal(fact[key]) if key == "fee" and fact[key] is not None else old[key] == fact[key]
                        if fact[key] is not None and not same:
                            raise EvidenceConflict("Ein bereits belegter eigener Gebührenwert wurde geändert")
                        fact[key] = old[key]
            def save(data):
                current = data["trades"][trade_id]
                if digest(current["fill_events"]) != digest(fills):
                    raise AqlaError("Trade-Belege wurden während des Funding-Abgleichs geändert")
                current.update(funding_events=assigned, funding_coverage=coverage, report_fee_evidence=fee_evidence,
                               reporting_checked_at=now_ms())
            self.state.update(save)
            return
        except Exception as error:
            coverage["reason"] = str(error) if isinstance(error, AqlaError) else "Börsenbelege für Funding/Gebühren vorübergehend nicht vollständig verfügbar"
            old = trade.get("funding_coverage", {})
            if (not isinstance(error, EvidenceConflict) and old.get("complete")
                    and old.get("start") == start and old.get("end") == end
                    and old.get("fill_fingerprint") == digest(trade.get("fill_events", {}))
                    and old.get("payments_fingerprint") == digest(trade.get("funding_events", {}))):
                self.state.update(lambda data: data["trades"][trade_id].update(reporting_checked_at=now_ms(), reporting_last_error=coverage["reason"]))
                return
        self.state.update(lambda data: data["trades"][trade_id].update(funding_coverage=coverage, reporting_checked_at=now_ms()))

    async def tick(self):
        # Keep slow reporting reads outside the protection/admission lock. One bounded
        # reconciliation per pass, persistent fairness even after gateway restarts.
        candidates = [trade for trade in self.state.snapshot()["trades"].values()
                      if trade["status"] in CLOSED and (trade.get("fill_events") or Decimal(trade.get("filled_quantity", "0")) > 0)]
        candidates.sort(key=lambda trade: (trade.get("reporting_checked_at", 0), trade["trade_id"]))
        if candidates and now_ms() - candidates[0].get("reporting_checked_at", 0) >= 3_600_000:
            trade_id = candidates[0]["trade_id"]
            try:
                await asyncio.wait_for(self.refresh(trade_id), timeout=40)
            except TimeoutError:
                def timeout(data):
                    row = data["trades"][trade_id]
                    old = row.get("funding_coverage", {})
                    if (old.get("complete") and old.get("fill_fingerprint") == digest(row.get("fill_events", {}))
                            and old.get("payments_fingerprint") == digest(row.get("funding_events", {}))):
                        row["reporting_last_error"] = "Zeitlimit beim erneuten Funding-Abgleich; gespeicherte Belege erhalten"
                    else:
                        row["funding_coverage"] = {"complete": False, "reason": "Zeitlimit beim vollständigen Funding-Abgleich"}
                    row["reporting_checked_at"] = now_ms()
                self.state.update(timeout)
