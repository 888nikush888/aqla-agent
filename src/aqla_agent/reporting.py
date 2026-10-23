"""Internal evidence reconciliation and publication jobs; no exchange mutation path."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Literal, TypedDict

from aqla_hyperliquid_mcp.models import AqlaError, Market, TradePlan, digest, now_ms

from .evidence import FundingCoverage, market_fills, order_observations, verified_coverage_matches
from .funding import bounded_history
from .monthly import CLOSED, accounting_facts, entry_quantity_evidence, fill_identity, report_row
from .read_budget import BudgetCode, ReadBudget, ReadBudgetExceeded, read_limits

RefreshReason = Literal["complete", "missing_lifecycle", "history_unavailable", "evidence_conflict", "unavailable"]
AttemptReason = RefreshReason | BudgetCode | Literal["timeout"]


class RefreshResult(TypedDict):
    complete: bool
    reason: RefreshReason


class ReportingAttempt(TypedDict):
    trade_id: str
    complete: bool
    reason: AttemptReason
    requests: int
    elapsed_seconds: float


class ReportingTick(TypedDict):
    attempts: list[ReportingAttempt]
    requests: int
    elapsed_seconds: float
    stop_reason: Literal["idle", "complete", "max_jobs", "tick_request_limit", "tick_time_limit"]


class EvidenceConflict(AqlaError):
    """New positive evidence contradicts a previously verified attribution."""


class Reporting:
    def __init__(self, state, router, *, max_jobs=8, max_requests=32, time_budget_seconds=10.0,
                 job_requests=None, job_time_seconds=None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], int] | None = None, wait_for: Callable[..., Awaitable[RefreshResult]] | None = None):
        job_requests = min(8, max_requests) if job_requests is None and type(max_requests) is int else job_requests
        job_time_seconds = min(8.0, time_budget_seconds) if job_time_seconds is None and type(time_budget_seconds) in (int, float) else job_time_seconds
        if any(type(value) is not int or not 1 <= value <= 4096 for value in (max_jobs, max_requests, job_requests)):
            raise ValueError("Reporting job/request budgets must be integers in 1..4096")
        if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 40 for value in (time_budget_seconds, job_time_seconds)):
            raise ValueError("Reporting time budgets must be finite seconds in (0,40]")
        if job_requests > max_requests or job_time_seconds > time_budget_seconds:
            raise ValueError("A reporting job budget cannot exceed its tick budget")
        self.state, self.router = state, router
        self.max_jobs, self.max_requests, self.job_requests = max_jobs, max_requests, job_requests
        self.time_budget_seconds, self.job_time_seconds = time_budget_seconds, job_time_seconds
        self.clock, self.wall_clock, self.wait_for = clock, wall_clock or now_ms, wait_for
        self._tick_lock = asyncio.Lock()

    async def hydrate(self, trade_id, engine):
        with order_observations(engine.exchange):
            return await self._hydrate(trade_id, engine)

    async def _hydrate(self, trade_id, engine):
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
        from .managed_entries import all_order_rows
        orders = all_order_rows(trade)
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
            return await market_fills(adapter, plan.market, low, high)
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

    async def load_reporting_trade(self, trade_id):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        if snapshot.get("retired_execution_configuration") and trade["plan"].get("exchange", "hyperliquid") != "hyperliquid":
            complete = report_row(snapshot, trade)["complete"]
            return None, None, {"complete": complete, "reason": "complete" if complete else "unavailable"}
        engine = self.router.engine_for_plan(trade["plan"]) if hasattr(self.router, "engine_for_plan") else self.router.engines.get(trade["plan"].get("exchange", "hyperliquid"))
        if engine and (not trade.get("entry_accounting") or not trade.get("closed_at") or trade.get("entry_fill_quantity_confirmed") is None):
            try:
                await self.hydrate(trade_id, engine)
                trade = self.state.snapshot()["trades"][trade_id]
            except ReadBudgetExceeded:
                raise
            except Exception:
                self._incomplete(trade_id, "Historische Entry-/Exit-Belege nicht vollst\u00e4ndig verf\u00fcgbar")
                return None, None, {"complete": False, "reason": "history_unavailable"}
        return trade, engine, None

    @staticmethod
    def select_observed_fills(trade, market, observed, manual, end):
        selected: dict[str, dict] = {}
        for row in observed:
            if row.get("coin") != market.symbol or not trade["plan"]["created_at"] <= row["time"] <= end:
                continue
            identity = fill_identity(row)
            if manual and identity in selected and selected[identity] != row:
                raise EvidenceConflict("A historical manual fill identity has conflicting receipts")
            selected[identity] = row
        return selected

    @staticmethod
    def validate_funding_position(fills, side, market, fact, start, end):
        timestamp = fact["time"]
        if fact["market"] != market.symbol or fact["currency"] != market.quote_token or not start <= timestamp <= end:
            raise EvidenceConflict("Funding-Markt, Zeitraum oder Währung stimmt nicht überein")
        if any(row["time"] == timestamp for row in fills.values()):
            raise EvidenceConflict("Reihenfolge von Funding und Fills zum selben Zeitpunkt nicht eindeutig")
        size = sum((Decimal(row["sz"]) * (1 if row["kind"] == "entry" else -1)
                    for row in fills.values() if row["time"] < timestamp), Decimal(0))
        if size <= 0:
            raise EvidenceConflict("Funding-Zahlung ohne belegte eigene Position")
        signed = size * (1 if side == "long" else -1)
        if fact["signed_size"] is not None and Decimal(fact["signed_size"]) != signed:
            raise EvidenceConflict("Funding-Positionsmenge stimmt nicht mit eigenen Fills überein")

    @staticmethod
    def own_funding_events(trade, market, payments, start, end):
        fills = trade["fill_events"]
        assigned = {}
        for fact in payments:
            Reporting.validate_funding_position(fills, trade["plan"]["side"], market, fact, start, end)
            identity = fact["identity"]
            if identity in assigned and assigned[identity] != fact:
                raise EvidenceConflict("Funding-Identität ist widersprüchlich")
            old = trade.get("funding_events", {}).get(identity)
            if old and old != fact:
                raise EvidenceConflict("Ein unveränderlicher Funding-Beleg wurde geändert")
            assigned[identity] = fact
        if set(trade.get("funding_events", {})) - set(assigned):
            raise AqlaError("Bereits gespeicherte Funding-Belege fehlen im erneuten vollständigen Abgleich")
        return assigned

    @staticmethod
    def confirmed_fee_evidence(trade, fills, observed):
        fee_evidence = {identity: {"fee": row.get("fee"), "feeToken": row.get("feeToken")}
                        for identity, row in observed.items()}
        for identity, fact in fee_evidence.items():
            old = trade.get("report_fee_evidence", {}).get(identity, fills[identity])
            for key in ("fee", "feeToken"):
                if old.get(key) is not None:
                    previous_value, observed_value = old[key], fact[key]
                    same = (Decimal(previous_value) == Decimal(observed_value)
                            if key == "fee" and observed_value is not None and previous_value is not None
                            else previous_value == observed_value)
                    if fact[key] is not None and not same:
                        raise EvidenceConflict("Ein bereits belegter eigener Gebührenwert wurde geändert")
                    fact[key] = old[key]
        return fee_evidence

    async def refresh_manual_coverage(self, trade_id, trade, adapter, market, start, end, coverage, observed):
        fills = trade["fill_events"]
        from .manual_accounting import attribute_manual_closure
        payments = await adapter.funding_payments(market, start, end)
        try:
            attribution, assigned, fees = attribute_manual_closure(trade, observed, payments)
        except (InvalidOperation, KeyError, ValueError, TypeError) as error:
            raise EvidenceConflict(str(error) if isinstance(error, AqlaError) else "Manual PnL attribution is unconfirmed") from None
        coverage.update({"complete": True, "fill_fingerprint": digest(fills), "payments_fingerprint": digest(assigned)})
        def save_manual(data):
            current = data["trades"][trade_id]
            from .manual_accounting import closure_fingerprint
            if (digest(current["fill_events"]) != attribution["fill_fingerprint"]
                    or closure_fingerprint(current) != attribution["closure_fingerprint"]
                    or digest(current["plan"]) != attribution["plan_fingerprint"]
                    or digest(current.get("entry_accounting", {})) != attribution["entry_fingerprint"]):
                raise EvidenceConflict("Trade evidence changed during manual accounting")
            current.update(manual_accounting=attribution, funding_events=assigned, funding_coverage=coverage,
                report_fee_evidence=fees, reporting_checked_at=now_ms())
            current["manual_closure"]["pnl_complete"] = True
        self.state.update(save_manual)
        return {"complete": True, "reason": "complete"}

    async def refresh_coverage(self, trade_id, trade, start, end, coverage, engine):
        if engine is None:
            raise AqlaError("Börsenkonto für Funding-Abgleich nicht verfügbar")
        engine.assert_plan_binding(engine.state.snapshot(), TradePlan.model_validate(trade["plan"]))
        manual = isinstance(trade.get("manual_closure"), dict) and trade["manual_closure"].get("basis") in (
            "proved_original_account_closure", "proved_account_flat_after_last_owned_fill")
        if (trade.get("external_conflict") or trade.get("manual_closure")) and not manual:
            raise EvidenceConflict("Funding bei manuellem/gemischtem Positionsverlauf nicht eindeutig zuordenbar")
        adapter = engine.exchange
        market = Market.model_validate(trade["plan"]["market"])
        # Restore immutable native market identities for historical reads.
        if hasattr(adapter, "market_cache"):
            adapter.market_cache.setdefault(market.symbol, market)
        fills = trade["fill_events"]
        async def fetch(low, high):
            return await market_fills(adapter, market, low, high)
        observed = await bounded_history(fetch, trade["plan"]["created_at"], end, limit=2000)
        observed = self.select_observed_fills(trade, market, observed, manual, end)
        if manual:
            return await self.refresh_manual_coverage(trade_id, trade, adapter, market, start, end, coverage, observed)
        if set(observed) - set(fills):
            raise EvidenceConflict("Zusätzliche manuelle Fills im eigenen Positionsverlauf vorhanden")
        if set(fills) - set(observed):
            raise AqlaError("Historische eigene Fills nicht vollständig aus der Börse abrufbar")
        payments = await adapter.funding_payments(market, start, end)
        assigned = self.own_funding_events(trade, market, payments, start, end)
        coverage.update({"complete": True, "fill_fingerprint": digest(fills), "payments_fingerprint": digest(assigned)})
        fee_evidence = self.confirmed_fee_evidence(trade, fills, observed)
        def save(data):
            current = data["trades"][trade_id]
            if digest(current["fill_events"]) != digest(fills):
                raise AqlaError("Trade-Belege wurden während des Funding-Abgleichs geändert")
            current.update(funding_events=assigned, funding_coverage=coverage, report_fee_evidence=fee_evidence,
                           reporting_checked_at=now_ms())
        self.state.update(save)
        return {"complete": True, "reason": "complete"}

    async def refresh(self, trade_id) -> RefreshResult:
        trade, engine, early = await self.load_reporting_trade(trade_id)
        if early is not None:
            return early
        metrics = accounting_facts(trade)
        if any(trade.get(key) != value for key, value in metrics.items()):
            self.state.update(lambda data: data["trades"][trade_id].update(metrics))
            trade = self.state.snapshot()["trades"][trade_id]
        entry = trade.get("entry_accounting", {})
        start, end = entry.get("entry_at"), trade.get("closed_at")
        if not start or not end:
            self.state.update(lambda data: data["trades"][trade_id].update(reporting_checked_at=now_ms()))
            return {"complete": False, "reason": "missing_lifecycle"}
        coverage: FundingCoverage = {"start": start, "end": end, "complete": False}
        try:
            return await self.refresh_coverage(trade_id, trade, start, end, coverage, engine)
        except ReadBudgetExceeded:
            raise
        except Exception as error:
            failure_reason: RefreshReason = "evidence_conflict" if isinstance(error, EvidenceConflict) else "unavailable"
            coverage["reason"] = str(error) if isinstance(error, AqlaError) else "Börsenbelege für Funding/Gebühren vorübergehend nicht vollständig verfügbar"
            if not isinstance(error, EvidenceConflict) and verified_coverage_matches(trade, start=start, end=end):
                self.state.update(lambda data: data["trades"][trade_id].update(reporting_checked_at=now_ms(), reporting_last_error=coverage["reason"]))
                return {"complete": False, "reason": "unavailable"}
        def incomplete(data):
            current = data["trades"][trade_id]
            current.update(funding_coverage=coverage, reporting_checked_at=now_ms())
            if isinstance(current.get("manual_closure"), dict):
                current["manual_closure"]["pnl_complete"] = False
        self.state.update(incomplete)
        return {"complete": False, "reason": failure_reason}

    def _incomplete(self, trade_id: str, reason: str) -> None:
        def save(data):
            row = data["trades"][trade_id]
            if verified_coverage_matches(row):
                row["reporting_last_error"] = reason
            else:
                row["funding_coverage"] = {**row.get("funding_coverage", {}), "complete": False, "reason": reason}
            row["reporting_checked_at"] = self.wall_clock()
        self.state.update(save)

    def reporting_candidates(self, snapshot):
        candidates = [trade for trade in snapshot["trades"].values()
                      if trade["status"] in CLOSED and (trade.get("fill_events") or Decimal(trade.get("filled_quantity", "0")) > 0)
                      and self.wall_clock() >= trade.get("reporting_next_retry_at", trade.get("reporting_checked_at", 0)
                          + (3_600_000 if verified_coverage_matches(trade) else 60_000))]
        candidates.sort(key=lambda trade: (trade.get("reporting_attempt_sequence", 0), trade.get("reporting_checked_at", 0), trade["trade_id"]))
        return candidates


    async def tick(self) -> ReportingTick:
        # Jobs remain serial and independent of the execution lock. Durable
        # attempt ordering survives cancellation/restarts at the same millisecond.
        async with self._tick_lock:
            started = self.clock()
            tick = ReadBudget("tick", self.max_requests, started + self.time_budget_seconds, self.clock)
            snapshot = self.state.snapshot()
            candidates = self.reporting_candidates(snapshot)
            result: ReportingTick = {"attempts": [], "requests": 0, "elapsed_seconds": 0.0, "stop_reason": "idle" if not candidates else "complete"}
            for trade in candidates:
                if len(result["attempts"]) == self.max_jobs:
                    result["stop_reason"] = "max_jobs"
                    break
                try:
                    tick.check()
                except ReadBudgetExceeded as exhausted:
                    result["stop_reason"] = "tick_request_limit" if exhausted.code == "tick_request_limit" else "tick_time_limit"
                    break
                trade_id = trade["trade_id"]
                def reserve(data, trade_id=trade_id):
                    sequence = max((row.get("reporting_attempt_sequence", 0) for row in data["trades"].values()), default=0) + 1
                    data["trades"][trade_id].update(reporting_attempt_sequence=sequence, reporting_attempted_at=self.wall_clock(),
                                                   reporting_next_retry_at=self.wall_clock()+60_000)
                self.state.update(reserve)
                job_started = self.clock()
                duration = min(self.job_time_seconds, max(0.0, tick.deadline - job_started))
                job = ReadBudget("job", min(self.job_requests, self.max_requests - tick.requests), job_started + duration, self.clock)
                reason: AttemptReason
                with read_limits(tick, job):
                    try:
                        refreshed = await (self.wait_for or asyncio.wait_for)(self.refresh(trade_id), timeout=duration)
                        complete, reason = refreshed["complete"], refreshed["reason"]
                    except (ReadBudgetExceeded, TimeoutError) as exhausted:
                        reason = exhausted.code if isinstance(exhausted, ReadBudgetExceeded) else "timeout"
                        message = ("Lesebudget beim Funding-Abgleich erreicht; gespeicherte Belege erhalten" if "request" in reason
                                   else "Zeitlimit beim erneuten Funding-Abgleich; gespeicherte Belege erhalten")
                        self._incomplete(trade_id, message)
                        complete = False
                    except Exception:
                        self._incomplete(trade_id, "Börsenbelege für Funding/Gebühren vorübergehend nicht vollständig verfügbar")
                        complete, reason = False, "unavailable"
                attempt: ReportingAttempt = {"trade_id": trade_id, "complete": complete, "reason": reason,
                    "requests": job.requests, "elapsed_seconds": max(0.0, self.clock() - job_started)}
                def schedule(data, *, trade_id, attempt):
                    row = data["trades"][trade_id]
                    # A failed read is not a completed hourly evidence check.
                    transient = not attempt["complete"] and attempt["reason"] not in ("missing_lifecycle", "evidence_conflict")
                    if transient:
                        count = min(row.get("reporting_failure_count", 0)+1, 4)
                        row["reporting_failure_count"] = count
                        delay = min(60_000 * 2**(count-1), 300_000)
                    else:
                        row.pop("reporting_failure_count", None)
                        delay = 3_600_000
                    if attempt["complete"]:
                        row.pop("reporting_last_error", None)
                    row.update(reporting_checked_at=self.wall_clock(), reporting_next_retry_at=self.wall_clock()+delay,
                               reporting_last_attempt=attempt)
                self.state.update(partial(schedule, trade_id=trade_id, attempt=attempt))
                result["attempts"].append(attempt)
            result.update({"requests": tick.requests, "elapsed_seconds": max(0.0, self.clock() - started)})
            return result
