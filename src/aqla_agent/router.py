"""One interpretation, immutable target selection and independent venue execution."""
from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal
from typing import Literal, NotRequired, TypedDict

from aqla_hyperliquid_mcp.models import AqlaError, Market, SymbolUnavailable, TradePlan, digest, now_ms

from .accounting import inventory_view
from .observations import TemporaryObservation, diagnostic, retry_due, retry_marker
from .pipelines import ConfigurationChanged, VenueState, validate_ranking_binding
from .trading import OPEN, PendingStopUpdate, StopUpdatePhase, Trading
from .venues import VenueAdapter, adapter_settings, execution_settings

RecoveryCategory = Literal["complete", "incomplete", "unavailable", "timeout", "cancelled"]


class RecoveryDiagnostic(TypedDict):
    last_attempt_at: int
    last_success_at: NotRequired[int]
    duration_ms: int
    category: RecoveryCategory
    error_count: int


def recovery_diagnostics(data) -> dict[str, RecoveryDiagnostic]:
    """Bounded safe metadata; no source text, exception or provider payload."""
    result = {}
    for venue in ("hyperliquid", "aster", "lighter"):
        for network in ("mainnet", "testnet"):
            key = venue + ":" + network
            row = data.get("account_recovery", {}).get(key, {})
            if row.get("category") not in ("complete", "incomplete", "unavailable", "timeout", "cancelled"):
                continue
            numbers = ("last_attempt_at", "duration_ms", "error_count")
            if any(type(row.get(name)) is not int or row[name] < 0 for name in numbers):
                continue
            fact = {name: row[name] for name in numbers}
            fact["category"] = row["category"]
            if type(row.get("last_success_at")) is int and row["last_success_at"] >= 0:
                fact["last_success_at"] = row["last_success_at"]
            result[key] = fact
    return result


class Router:
    def __init__(self, settings, state, hyperliquid, *, adapters=None, recovery_concurrency: int = 2, recovery_timeout_seconds: float = 40):
        if type(recovery_concurrency) is not int or not 1 <= recovery_concurrency <= 6:
            raise AqlaError("Recovery concurrency must be 1–6 independent accounts")
        if type(recovery_timeout_seconds) not in (int, float) or not math.isfinite(recovery_timeout_seconds) or not 0 < recovery_timeout_seconds <= 120:
            raise AqlaError("Account recovery timeout must be positive and at most 120 seconds")
        self.settings, self.state, self.exchange = settings, state, hyperliquid
        self.lock = asyncio.Lock()
        self._recovery_semaphore = asyncio.Semaphore(recovery_concurrency)
        self._recovery_timeout = recovery_timeout_seconds
        self._recovery_tasks: set[asyncio.Task] = set()
        self._recovery_owners: set[asyncio.Task] = set()
        self._closing = False
        self.source = Trading(hyperliquid, state)
        self.engines = {}
        self.errors = {}
        adapters = adapters or {}
        for venue in ("hyperliquid", "aster", "lighter"):
            selected = settings if venue == "hyperliquid" else adapter_settings(settings, venue)
            adapter = hyperliquid if venue == "hyperliquid" else adapters.get(venue)
            if adapter is not None and selected is None:
                selected = adapter.settings
            if selected is None:
                self.errors[venue] = "Dedicated account and API credentials are not configured"
                continue
            view = VenueState(state, venue, selected)
            try:
                if adapter is None:
                    from .aster import Aster
                    from .lighter import Lighter

                    adapter = (Aster if venue == "aster" else Lighter)(selected, view)
                adapter.state = view
                adapter.fail_closed_protection = True
                view.assert_binding(selected)
                binding = {"network": selected.network, "account": selected.account.lower()}
                if venue not in state.snapshot()["venue_accounts"]:
                    state.update(lambda data, name=venue, account=binding: data["venue_accounts"].__setitem__(name, account))
                self.engines[venue] = Trading(adapter, view)
                self.register_account(view, selected)
            except Exception as error:
                self.errors[venue] = str(error) if isinstance(error, AqlaError) else "Exchange initialization unavailable"
        for key in settings.execution_accounts:
            venue, network = key.split(":", 1) if ":" in key else ("", "")
            if venue not in ("hyperliquid", "aster", "lighter") or network not in ("mainnet", "testnet"):
                raise AqlaError("Execution account keys must be exchange:network")
            selected = execution_settings(settings, key)
            if selected is None:
                self.errors[key] = "Dedicated execution account/API credentials are not configured"
                continue
            existing = self.engine_for(venue, network)
            if existing:
                if selected.account.lower() != existing.exchange.settings.account.lower():
                    raise AqlaError("An existing exchange/network account cannot be rebound")
                continue
            try:
                from aqla_hyperliquid_mcp.api import Hyperliquid

                from .aster import Aster
                from .lighter import Lighter
                view = VenueState(state, venue, selected, legacy=False)
                adapter = adapters.get(key) or {"hyperliquid": Hyperliquid, "aster": Aster, "lighter": Lighter}[venue](selected, view)
                adapter.state = view
                adapter.fail_closed_protection = True
                view.assert_binding(selected)
                self.register_account(view, selected)
                self.engines[key] = Trading(adapter, view)
            except Exception as error:
                self.errors[key] = str(error) if isinstance(error, AqlaError) else "Execution account initialization unavailable"

    def register_account(self, view, selected):
        snapshot = self.state.snapshot()
        expected = {"exchange": view.exchange, "network": selected.network, "account": selected.account.lower()}
        if snapshot["execution_accounts"].get(view.account_key) == expected and view.account_key in snapshot["account_limits"] and view.account_key in snapshot["account_enabled"]:
            return
        def register(data):
            binding = {"exchange": view.exchange, "network": selected.network, "account": selected.account.lower()}
            existing = data["execution_accounts"].get(view.account_key)
            if existing and existing != binding:
                raise AqlaError("Execution account binding changed")
            data["execution_accounts"][view.account_key] = binding
            legacy = view.legacy_context(data)
            data["account_limits"].setdefault(view.account_key, data["venue_limits"].get(view.exchange) if legacy else data["venue_limits"].get("hyperliquid"))
            data["account_enabled"].setdefault(view.account_key, data["venue_enabled"].get(view.exchange, False) if legacy else False)
        self.state.update(register)

    def engine_for(self, venue, network):
        return next((engine for engine in self.engines.values() if engine.state.exchange == venue and engine.exchange.settings.network == network), None)

    def engine_for_plan(self, plan):
        return self.engine_for(plan.get("exchange", "hyperliquid"), plan["network"])

    def accounts_ready(self, data=None):
        data = self.state.snapshot() if data is None else data
        result = {}
        for venue in ("hyperliquid", "aster", "lighter"):
            for network in ("mainnet", "testnet"):
                key = venue + ":" + network
                engine = self.engine_for(venue, network)
                legacy = engine and engine.state.legacy_context(data)
                result[key] = {"exchange": venue, "network": network, "configured": bool(engine),
                               "enabled": data["venue_enabled"].get(venue, False) if legacy else data["account_enabled"].get(key, False),
                               "limits": data["venue_limits"].get(venue) if legacy else data["account_limits"].get(key),
                               "reason": None if engine else self.errors.get(key, self.errors.get(venue, "Account is not configured"))}
        return result

    def __getattr__(self, name):
        return getattr(self.engines["hyperliquid"], name)

    def ingest(self, source, **kwargs):
        result = self.source.ingest(source, **kwargs)
        if result["status"] == "already_processed":
            return result
        from .ranking import source_key, source_rank
        ranking = self.state.read("source_aliases", "source_ranks")
        if source_key(ranking, source.chat_id) not in ranking.get("source_ranks", {}):
            self.state.update(lambda data: source_rank(data, source.chat_id))
        return result

    def context(self, event_id, *, decision=False):
        result = self.source.context(event_id, compact_receipt=decision)
        data = self.state.snapshot()
        result.update(profiles=data["profiles"], pipelines=data["pipelines"], configuration_revision=data["configuration_revision"],
                      venue_limits=data["venue_limits"], execution_targets=data["events"][event_id].get("targets", {}))
        from .ranking import snapshot_rank
        result.update(source_rank=snapshot_rank(data, data["events"][event_id]["source"]["chat_id"]), execution_accounts=self.accounts_ready())
        if decision:
            selected = self.selected_targets(data["events"][event_id], data)
            pipeline_ids = {target["pipeline_id"] for target in selected.values()}
            result["pipelines"] = {key: row for key, row in data["pipelines"].items() if key in pipeline_ids}
            profile_ids = {target["profile_id"] for target in selected.values()} | {"backup"}
            result["profiles"] = {key: row for key, row in data["profiles"].items() if key in profile_ids}
        return result

    def record_interpretation(self, event_id, interpretation):
        return self.source.record_interpretation(event_id, interpretation)

    def ready(self):
        data = self.state.snapshot()
        return {name: {"configured": name in self.engines, "enabled": data["venue_enabled"].get(name, False),
                       "network": self.engines[name].exchange.settings.network if name in self.engines else "mainnet",
                       "account": self.engines[name].exchange.account if name in self.engines else None,
                       "limits": data["venue_limits"].get(name), "reason": self.errors.get(name)} for name in ("hyperliquid", "aster", "lighter")}

    async def inventory_status(self):
        engine = self.engines.get("hyperliquid")
        if engine is None or not engine.exchange.settings.shared_account:
            return {}
        try:
            account = await engine.exchange.account_inventory()
            data = engine.state.snapshot()
            return {"hyperliquid": {"available": True, **inventory_view(data, account, engine.state.limits().max_open_trades)}}
        except Exception as error:
            return {"hyperliquid": {"available": False, "reason": str(error) if isinstance(error, AqlaError) else "Complete account inventory is unavailable"}}

    def selected_targets(self, row, data):
        source = row["source"]["chat_id"]
        aliases = {value: key for key, value in data.get("source_aliases", {}).items()}
        result = {}
        matching = []
        for pipeline in sorted(data["pipelines"].values(), key=lambda item: item["id"]):
            if not pipeline["enabled"]:
                continue
            selectors = pipeline["sources"]
            if not ("*" in selectors or source in selectors or aliases.get(source) in selectors or row.get("native_forward") and "owner_forward" in selectors):
                continue
            matching.append(pipeline)
        # Explicit routing replaces the migration catch-all for this source, avoiding surprise duplicate entries.
        if any(pipeline["id"] != "legacy" for pipeline in matching):
            matching = [pipeline for pipeline in matching if pipeline["id"] != "legacy"]
        for pipeline in matching:
            for target in pipeline["targets"]:
                key = pipeline["id"] + ":" + target["exchange"]
                result[key] = {**target, "pipeline_id": pipeline["id"], "status": "pending"}
        return result

    @staticmethod
    def target_started(data, target):
        saved = data["plans"].get(target.get("plan_id"))
        if target.get("plan_id") and saved is None:
            return True
        trade_id = target.get("trade_id") or (saved["plan"]["trade_id"] if saved else None)
        return bool(trade_id and (trade_id in data["trades"] or "entry:" + trade_id in data["operations"]
                                  or "leverage:" + trade_id in data["operations"]))

    async def preview(self, event_id, interpretation):
        from .ranking import snapshot_rank
        self.record_interpretation(event_id, interpretation)
        if interpretation.kind != "new_signal":
            return self.state.snapshot()["events"][event_id]
        def select(data):
            row = data["events"][event_id]
            if "targets" not in row:
                row["targets"] = self.selected_targets(row, data)
            current = self.selected_targets(row, data)
            for identity, target in list(row["targets"].items()):
                if target["status"] == "rejected" and target.get("preview_invalidated") and not self.target_started(data, target):
                    if identity in current:
                        row["targets"][identity] = current[identity]
            if set(row["targets"]) == {"legacy:hyperliquid"}:
                legacy = row["targets"]["legacy:hyperliquid"]
                if legacy["status"] == "rejected" and not legacy.get("preview_invalidated") and not self.target_started(data, legacy):
                    if "legacy:hyperliquid" in current:
                        row["targets"]["legacy:hyperliquid"] = current["legacy:hyperliquid"]
            rank = snapshot_rank(data, row["source"]["chat_id"]) if data["ranking_policy"]["enabled"] else None
            return row["targets"], data, rank
        targets, preview_basis, rank = self.state.update(select)
        ranked = preview_basis["ranking_policy"]["enabled"]
        if not targets:
            self.state.update(lambda data: data["events"][event_id].update(status="unrouted", last_error="No enabled pipeline matches this source"))
            return {"event_id": event_id, "targets": [], "error": "No enabled pipeline matches this source"}
        outputs = []
        order = sorted(targets, key=lambda identity: (targets[identity]["exchange"] == "hyperliquid", identity)) if ranked else list(targets)
        reserved = set()
        for identity in order:
            target = targets[identity]
            if target["status"] != "pending":
                outputs.append({**target, **({"plan": self.state.snapshot()["plans"][target["plan_id"]]["plan"]} if target.get("plan_id") else {})})
                continue
            marker = target.get("observation_retry")
            if marker:
                invalid = (marker["exhausted"] or now_ms() >= marker["deadline_at"]
                           or marker["configuration_revision"] != preview_basis["configuration_revision"]
                           or marker["ranking_revision"] != preview_basis["ranking_revision"])
                if invalid:
                    row = {**target, "status": "rejected", "error": "Original preview retry expired or its configuration changed",
                           "observation_retry": {**marker, "exhausted": True}}
                    self.state.update(lambda data, key=identity, value=row: data["events"][event_id]["targets"].__setitem__(key, value))
                    outputs.append(row)
                    continue
                if not retry_due(marker):
                    outputs.append(target)
                    continue
            def claim_preview(data, key=identity):
                current = data["events"][event_id]["targets"][key]
                if current["status"] != "pending" or current.get("preview_inflight_until", 0) > now_ms():
                    return False
                if current.get("observation_retry") and not retry_due(current["observation_retry"]):
                    return False
                current["preview_inflight_until"] = now_ms() + 300000
                return True
            if not self.state.update(claim_preview):
                outputs.append(self.state.snapshot()["events"][event_id]["targets"][identity])
                continue
            network = rank["network"] if rank else self.engines[target["exchange"]].exchange.settings.network if target["exchange"] in self.engines else "mainnet"
            requested = target["exchange"]
            engine = self.engine_for(requested, network)
            try:
                if engine is None:
                    raise AqlaError(self.errors.get(requested + ":" + network, self.errors.get(requested, "Required account/network is unavailable")))
                from aqla_hyperliquid_mcp.rules import latest_sources
                with engine.state.scope(target, preview_snapshot=preview_basis):
                    basis = engine.state.preview_snapshot()
                    deadline = min(item["source"]["sent_at"] for item in latest_sources(basis["events"], interpretation.source_refs).values()) + basis["policy"]["max_signal_age_seconds"] * 1000
                if ranked:
                    target = {**target, "requested_exchange": requested, "network": network, "rank_snapshot": rank,
                              "setup_id": digest([rank["source_id"], sorted(interpretation.source_refs)])[:32], "leg_id": identity}
                    try:
                        await engine.exchange.market(interpretation.symbol)
                    except SymbolUnavailable as missing:
                        if requested != "hyperliquid":
                            raise
                        pipeline_target = next(row for row in preview_basis["pipelines"][target["pipeline_id"]]["targets"] if row["exchange"] == requested)
                        replacements = pipeline_target.get("fallback_exchanges")
                        replacements = ["aster", "lighter"] if replacements is None else replacements
                        reasons = []
                        replacement = None
                        readiness = self.accounts_ready(preview_basis)
                        for venue in replacements:
                            candidate = self.engine_for(venue, network)
                            key = venue + ":" + network
                            if candidate is None or not readiness[key]["enabled"] or not readiness[key]["limits"]:
                                reasons.append(venue + ": account/permissions/limits unavailable")
                                continue
                            if (key, interpretation.symbol.upper()) in reserved:
                                reasons.append(venue + ": explicit pipeline target already reserves this market")
                                continue
                            try:
                                await candidate.exchange.market(interpretation.symbol)
                            except SymbolUnavailable:
                                reasons.append(venue + ": market unavailable")
                                continue
                            replacement = candidate
                            target.update(exchange=venue, fallback_reason=str(missing), skipped_replacements=reasons)
                            break
                        if replacement is None:
                            raise AqlaError("No replacement market/account in " + network + ": " + "; ".join(reasons)) from missing
                        engine = replacement
                async with engine.lock:
                    with engine.state.scope(target, preview_snapshot=preview_basis):
                        basis = engine.state.preview_snapshot()
                        deadline = min(item["source"]["sent_at"] for item in latest_sources(basis["events"], interpretation.source_refs).values()) + basis["policy"]["max_signal_age_seconds"] * 1000
                        plan = await engine.preview(event_id, interpretation)
                row = {**target, "status": "planned", "plan_id": plan["plan_id"]}
                if marker:
                    row["observation_retry"] = {**marker, "completed": True, "exhausted": True}
                outputs.append({**row, "plan": plan})
                reserved.add((engine.state.account_key, interpretation.symbol.upper()))
            except Exception as error:
                row = {**target, "status": "rejected", "error": str(error) if isinstance(error, AqlaError) else "Exchange preview unavailable"}
                row["entry_diagnostic"] = diagnostic(error, "preview")
                if isinstance(error, TemporaryObservation) and engine is not None:
                    retry = retry_marker(marker, error, deadline, "preview")
                    retry.update(configuration_revision=preview_basis["configuration_revision"], ranking_revision=preview_basis["ranking_revision"])
                    row.update(status="rejected" if retry["exhausted"] else "pending", observation_retry=retry)
                if isinstance(error, ConfigurationChanged):
                    row["preview_invalidated"] = True
                outputs.append(row)
            self.state.update(lambda data, key=identity, value=row: data["events"][event_id]["targets"].__setitem__(key, value))
        self.finish_event(event_id)
        # Keep the old one-target interface usable for native Hermes and existing clients.
        if len(outputs) == 1 and outputs[0].get("plan"):
            return {**outputs[0]["plan"], "targets": outputs}
        if len(outputs) == 1 and outputs[0]["pipeline_id"] == "legacy" and outputs[0]["status"] == "rejected" and outputs[0].get("error"):
            raise AqlaError(outputs[0]["error"])
        outputs.sort(key=lambda row: list(targets).index(row.get("leg_id", row["pipeline_id"] + ":" + row.get("requested_exchange", row["exchange"]))))
        return {"event_id": event_id, "targets": outputs}

    def finish_event(self, event_id):
        def finish(data):
            row = data["events"][event_id]
            targets = row.get("targets", {})
            if targets and all(value["status"] not in ("pending", "planned") for value in targets.values()):
                row["status"] = "executed" if any(value.get("trade_id") for value in targets.values()) else "rejected"
                errors = [value for value in targets.values() if value.get("error")]
                if errors:
                    chat = data["events"][event_id]["source"]["chat_id"]
                    data["notifications"].setdefault("targets:" + event_id, {"event_id": event_id, "targets": errors,
                        "source_id": chat, "source_name": data.get("source_names", {}).get(chat)})
        self.state.update(finish)

    async def execute(self, plan_id):
        saved = self.state.snapshot()["plans"].get(plan_id)
        if saved is None:
            raise AqlaError("Unknown immutable plan")
        plan = saved["plan"]
        venue = plan.get("exchange", "hyperliquid")
        engine = self.engine_for_plan(plan)
        if engine is None:
            raise AqlaError("The plan's exchange account is unavailable; no fallback exchange")
        target = {"pipeline_id": plan.get("pipeline_id") or "legacy", "profile_id": plan.get("profile_id") or "standard",
                  **({"rank_snapshot": plan["rank_snapshot"], "requested_exchange": plan.get("requested_exchange"), "setup_id": plan["setup_id"]} if plan.get("rank_snapshot") else {})}
        try:
            with engine.state.scope(target):
                result = await engine.execute(plan_id)
            outcome = {"status": result["status"], "trade_id": result["trade_id"]}
        except Exception as error:
            result = {"exchange": venue, "plan_id": plan_id, "error": str(error) if isinstance(error, AqlaError) else "Exchange execution unavailable"}
            trade = self.state.snapshot()["trades"].get(plan["trade_id"])
            outcome = {"status": trade["status"] if trade else "rejected", "error": result["error"]}
            outcome["entry_diagnostic"] = diagnostic(error, "execute")
            if trade:
                outcome["trade_id"] = trade["trade_id"]
            elif isinstance(error, ConfigurationChanged):
                outcome["preview_invalidated"] = True
        def record(data):
            for row in data["events"][saved["event_id"]].get("targets", {}).values():
                if row.get("plan_id") == plan_id:
                    row.update(outcome)
                    if trade := data["trades"].get(plan["trade_id"]):
                        if trade.get("observation_retry"):
                            row["observation_retry"] = trade["observation_retry"]
        self.state.update(record)
        self.finish_event(saved["event_id"])
        return result

    async def execute_many(self, plan_ids):
        if not plan_ids or len(plan_ids) > 100 or len(set(plan_ids)) != len(plan_ids):
            raise AqlaError("Supply unique saved plan IDs, at most 100")
        return {"targets": await asyncio.gather(*(self.execute(identity) for identity in plan_ids))}

    async def update_trade(self, event_id, interpretation):
        self.record_interpretation(event_id, interpretation)
        candidates = {}
        for name, engine in self.engines.items():
            rows = engine.update_candidates(event_id, interpretation)
            previous = engine.state.snapshot()["bindings"].get("update:" + event_id)
            if previous:
                rows = [engine.state.snapshot()["trades"][previous["trade_id"]]]
            if rows:
                candidates[name] = rows
        if interpretation.bound_trade_id:
            candidates = {name: [row for row in rows if row["trade_id"] == interpretation.bound_trade_id]
                          for name, rows in candidates.items() if any(row["trade_id"] == interpretation.bound_trade_id for row in rows)}
        anchor = self.state.snapshot()["events"][event_id]["source"]
        if anchor.get("reply_to_id"):
            parent = digest([anchor["chat_id"], anchor["reply_to_id"]])[:32]
            parents = {binding["trade_id"] for engine in self.engines.values() if (binding := engine.state.snapshot()["bindings"].get(parent))}
            families = {tuple(sorted(row["plan"]["source_refs"])) for row in self.state.snapshot()["trades"].values() if row["trade_id"] in parents or parent in row["plan"]["source_refs"]}
            if len(families) != 1:
                raise AqlaError("The replied source does not identify one executed setup family")
            selected = next(iter(families))
            candidates = {name: [row for row in rows if tuple(sorted(row["plan"]["source_refs"])) == selected]
                          for name, rows in candidates.items() if any(tuple(sorted(row["plan"]["source_refs"])) == selected for row in rows)}
        families = {tuple(sorted(row["plan"]["source_refs"])) for rows in candidates.values() for row in rows}
        if len(families) != 1 or any(len(rows) != 1 for rows in candidates.values()):
            raise AqlaError("Update matches multiple independent setups; specify its original source/reply")
        results = []
        for name in candidates:
            engine = self.engines[name]
            try:
                result = await engine.update_trade(event_id, interpretation)
                results.append({"exchange": name, "result": self.public_operation(result)})
            except Exception as error:
                results.append({"exchange": name, "error": str(error) if isinstance(error, AqlaError) else "Trade update unavailable"})
        def finish_update(data):
            event = data["events"][event_id]
            event["update_targets"] = results
            stop_statuses = self.stop_event_statuses(data)
            for identity, status in stop_statuses.items():
                if identity in data["events"] and data["events"][identity].get("update_targets"):
                    data["events"][identity]["status"] = status
            stop_status = stop_statuses.get(event_id)
            uncertain = any(row.get("result", {}).get("result", {}).get("phase") in ("prepared", "sending", "unknown") for row in results)
            event["status"] = stop_status or ("needs_reconciliation" if uncertain else "rejected" if all(row.get("error") for row in results) else "updated")
        self.state.update(finish_update)
        return results[0]["result"] if len(results) == 1 and "result" in results[0] else {"targets": results}

    @staticmethod
    def stop_event_statuses(data) -> dict[str, Literal["needs_reconciliation", "updated", "superseded", "rejected"]]:
        phases: dict[str, list[StopUpdatePhase]] = {}
        for trade in data["trades"].values():
            for operation, saved in trade.get("target_updates", {}).items():
                if not operation.startswith("update:") or "completed" not in saved and "superseded" not in saved:
                    continue
                phase = ("superseded" if saved.get("superseded") else "closed_without_application" if saved.get("closed_without_application")
                         else "rejected" if saved.get("rejected")
                         else "confirmed" if saved.get("completed") else "unknown")
                phases.setdefault(operation.removeprefix("update:"), []).append(phase)
            requests = dict(trade.get("stop_update_history", {}))
            if pending := trade.get("pending_stop_update"):
                requests[pending["operation_id"]] = pending
            for saved in requests.values():
                request = PendingStopUpdate.model_validate(saved)
                phases.setdefault(request.event_id, []).append(request.phase)
        result = {}
        for event_id, values in phases.items():
            result[event_id] = ("needs_reconciliation" if any(phase in ("waiting_fill", "replacing", "unknown") for phase in values)
                                else "updated" if "confirmed" in values else "superseded" if all(phase == "superseded" for phase in values) else "rejected")
        return result

    async def recover(self, *, diagnostics_sink=None):
        if diagnostics_sink is not None:
            diagnostics_sink.clear()
        if self._closing:
            raise AqlaError("Account recovery is closed")
        owner = asyncio.current_task()
        self._recovery_owners.add(owner)
        try:
            return await self._recover(diagnostics_sink=diagnostics_sink)
        finally:
            self._recovery_owners.discard(owner)

    async def _recover(self, *, diagnostics_sink=None):
        from .ranking import evaluate
        self.state.update_if_changed(evaluate)
        reports = []
        diagnostics: dict[str, RecoveryDiagnostic] = {}
        missing_accounts = {row["plan"].get("account_key") or row["plan"].get("exchange", "hyperliquid") + ":" + row["plan"]["network"]
                            for row in self.state.snapshot()["trades"].values()
                            if row["status"] in OPEN and self.engine_for_plan(row["plan"]) is None}
        for key in sorted(missing_accounts):
            reports.append({"exchange": key.split(":", 1)[0], "account_key": key,
                            "error": "The original account adapter is unavailable; existing exposure needs attention"})
            diagnostics[key] = {"last_attempt_at": now_ms(), "duration_ms": 0, "category": "unavailable", "error_count": 1}
        async def account(venue, engine):
            async with self._recovery_semaphore:
                started, attempted = time.monotonic(), now_ms()
                category: RecoveryCategory = "unavailable"
                rows = []
                try:
                    rows = await asyncio.wait_for(self._recover_account(engine), timeout=self._recovery_timeout)
                    view = engine.state.snapshot()
                    unresolved = any(row["phase"] in ("prepared", "sending", "unknown") for row in view["operations"].values())
                    unsettled = any(row["status"] in OPEN and (row["status"] not in ("open", "opening", "waiting_entry")
                        or row.get("quantity_evidence_pending")) for row in view["trades"].values())
                    category = "incomplete" if not engine.recovery_checked or unresolved or unsettled or any(row.get("error") or row.get("market_conflict") or row.get("replacement_unconfirmed") for row in rows) else "complete"
                except TimeoutError:
                    engine.recovery_checked = False
                    category = "timeout"
                    rows = [{"error": "Account recovery timed out; protection reconciliation is unconfirmed"}]
                except asyncio.CancelledError:
                    engine.recovery_checked = False
                    category = "cancelled"
                    raise
                except Exception:
                    engine.recovery_checked = False
                    rows = [{"error": "Account recovery is unavailable; protection reconciliation is unconfirmed"}]
                finally:
                    old = self.state.read("account_recovery").get("account_recovery", {}).get(engine.state.account_key, {})
                    fact: RecoveryDiagnostic = {"last_attempt_at": attempted, "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "category": category, "error_count": sum(bool(row.get("error") or row.get("market_conflict") or row.get("replacement_unconfirmed")) for row in rows)}
                    if "last_success_at" in old:
                        fact["last_success_at"] = old["last_success_at"]
                    if category == "complete":
                        fact["last_success_at"] = now_ms()
                    diagnostics[engine.state.account_key] = fact
                return [{"exchange": venue, "account_key": engine.state.account_key, **row} for row in rows]
        tasks = [asyncio.create_task(account(venue, engine)) for venue, engine in self.engines.items()]
        self._recovery_tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks)
            reports.extend(row for rows in results for row in rows)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if diagnostics:
                self.state.update(lambda data: data.setdefault("account_recovery", {}).update(diagnostics))
            raise
        finally:
            self._recovery_tasks.difference_update(tasks)
        def expire(data):
            data.setdefault("account_recovery", {}).update(diagnostics)
            stop_statuses = self.stop_event_statuses(data)
            for event_id, event in data["events"].items():
                if event.get("update_targets") and (status := stop_statuses.get(event_id)):
                    event["status"] = status
            for event in data["events"].values():
                for target in event.get("targets", {}).values():
                    if target["status"] == "pending" and (marker := target.get("observation_retry")) and (marker["deadline_at"] <= now_ms() or marker["configuration_revision"] != data["configuration_revision"] or marker["ranking_revision"] != data["ranking_revision"]):
                        target.update(status="expired", error="Original preview retry expired or its configuration changed", observation_retry={**marker, "exhausted": True})
                    if target.get("trade_id") in data["trades"]:
                        target["status"] = data["trades"][target["trade_id"]]["status"]
                    if target["status"] == "planned" and data["plans"][target["plan_id"]]["plan"]["deadline_at"] <= now_ms():
                        target.update(status="expired", error="Original signal deadline expired")
                if event.get("targets") and all(target["status"] not in ("planned", "pending") for target in event["targets"].values()):
                    event["status"] = "executed" if any(target.get("trade_id") for target in event["targets"].values()) else "rejected"
        self.state.update(expire)
        if diagnostics_sink is not None:
            # Call-scoped completion evidence: readers must not infer a positive
            # current pass from an older durable account_recovery snapshot.
            diagnostics_sink.clear()
            diagnostics_sink.update(diagnostics)
        return reports

    async def _recover_account(self, engine):
        reports = []
        async with engine.lock:
            for row in engine.state.snapshot()["trades"].values():
                market = Market.model_validate(row["plan"]["market"])
                engine.exchange.market_cache[market.symbol] = market
            for row in engine.state.snapshot()["trades"].values():
                if row["status"] not in OPEN or Decimal(row["filled_quantity"]) > 0:
                    continue
                try:
                    validate_ranking_binding(self.state.snapshot(), TradePlan.model_validate(row["plan"]))
                except AqlaError:
                    try:
                        await engine.cancel_entry(row["trade_id"])
                        if engine.state.snapshot()["trades"][row["trade_id"]]["status"] in OPEN:
                            await engine.refresh_trade(row["trade_id"])
                    except Exception:
                        reports.append({"trade_id": row["trade_id"], "error": "Rank-changed entry cancellation/fill reconciliation is unconfirmed"})
            reports.extend(await engine.recover())
        return reports

    async def activate(self, venue):
        engine = self.engines.get(venue)
        if engine is None:
            raise AqlaError("Configure dedicated exchange credentials before activation")
        await engine.exchange.verify_signer()
        engine.state.assert_binding(engine.exchange.settings)
        if engine.state.limits() is None:
            raise AqlaError("Explicit exchange limits are required")

    async def close_trade(self, trade_id, request_id):
        row = self.state.snapshot()["trades"].get(trade_id)
        if row is None:
            raise AqlaError("Specify one exact trade ID; do not guess between exchanges")
        engine = self.engine_for_plan(row["plan"])
        if engine is None:
            raise AqlaError("This trade's original exchange account is unavailable")
        async with engine.lock:
            return self.public_operation(await engine.close_position(trade_id, Decimal(100), "operator-close:" + request_id))

    def conflict_engine(self, trade_id):
        row = self.state.snapshot()["trades"].get(trade_id)
        if row is None or row["status"] != "conflicted":
            raise AqlaError("Select one current conflicted trade ID")
        engine = self.engine_for_plan(row["plan"])
        if engine is None:
            raise AqlaError("This trade's original exchange account is unavailable")
        return engine

    async def conflict_review(self, trade_id):
        from .conflict_review import observe_conflict
        engine = self.conflict_engine(trade_id)
        async with engine.lock:
            review, _ = await observe_conflict(engine, trade_id)
            def record(data):
                data.setdefault("source_status", {}).setdefault("ownership_reviews", {})[trade_id] = {
                    "fingerprint": review["fingerprint"], "expires_at": now_ms() + 90000}
            self.state.update_metadata(("source_status",), record)
            return review

    async def resolve_conflict(self, trade_id, fingerprint, commit_check):
        from .conflict_review import observe_conflict
        engine = self.conflict_engine(trade_id)
        async with engine.lock:
            saved = self.state.snapshot().get("source_status", {}).get("ownership_reviews", {}).get(trade_id, {})
            if not fingerprint or saved.get("fingerprint") != fingerprint or saved.get("expires_at", 0) <= now_ms():
                raise AqlaError("Read a current ownership review before resolution")
            review, evidence = await observe_conflict(engine, trade_id)
            if review["fingerprint"] != fingerprint:
                raise AqlaError("Ownership evidence changed; read a fresh review")
            if not review["resolvable"]:
                return {"resolved": False, "review": review}
            commit_check()
            def commit(data):
                commit_check()
                current = data["trades"][trade_id]
                latest = data.get("source_status", {}).get("ownership_reviews", {}).get(trade_id, {})
                if latest.get("fingerprint") != fingerprint or latest.get("expires_at", 0) <= now_ms():
                    raise AqlaError("Ownership review expired; read a fresh review")
                if (current["status"] != "conflicted" or digest(current) != evidence["trade_hash"]
                        or digest(engine.state.snapshot()["operations"]) != evidence["operations_hash"]):
                    raise AqlaError("Trade changed during ownership review")
                engine.assert_plan_binding(engine.state.snapshot(), TradePlan.model_validate(current["plan"]))
                current.update(status="closed", remaining_quantity="0", reserved_risk="0", cleanup_complete=True,
                    fill_events=evidence["fills"], closed_at=evidence["closed_at"],
                    manual_closure={"confirmed_at": now_ms(), "review_fingerprint": fingerprint, "pnl_complete": False,
                        "order_statuses": review["order_statuses"], "basis": "proved_original_account_closure"})
                from .monthly import accounting_facts
                current.update(accounting_facts(current))
                data["notifications"].setdefault("ownership-resolved:" + fingerprint, {"trade_id": trade_id, "change": {"status": "closed"}, "observed_at": now_ms()})
                return {"resolved": True, "trade_id": trade_id, "pnl_complete": False}
            return self.state.update(commit)

    async def reconcile(self, operation_id):
        row = self.state.snapshot()["operations"].get(operation_id)
        if row is None:
            raise AqlaError("Unknown operation")
        engine = self.engine_for(row.get("exchange", "hyperliquid"), row["request"]["network"])
        if engine is None:
            raise AqlaError("Original operation account is unavailable")
        return await engine.exchange.reconcile(operation_id)

    async def close(self):
        self._closing = True
        tasks = [task for task in self._recovery_tasks | self._recovery_owners if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for engine in self.engines.values():
            await engine.exchange.close()

    @staticmethod
    def public_operation(row):
        return VenueAdapter.public_operation(row)
