"""One interpretation, immutable target selection and independent venue execution."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from aqla_hyperliquid_mcp.models import AqlaError, Market, SymbolUnavailable, digest, now_ms

from .accounting import inventory_view
from .pipelines import VenueState
from .trading import OPEN, Trading
from .venues import VenueAdapter, adapter_settings, execution_settings


class Router:
    def __init__(self, settings, state, hyperliquid, *, adapters=None):
        self.settings, self.state, self.exchange = settings, state, hyperliquid
        self.lock = asyncio.Lock()
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

    def accounts_ready(self):
        data = self.state.snapshot()
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
        from .ranking import source_rank
        self.state.update(lambda data: source_rank(data, source.chat_id))
        return result

    def context(self, event_id):
        result = self.source.context(event_id)
        data = self.state.snapshot()
        result.update(profiles=data["profiles"], pipelines=data["pipelines"], configuration_revision=data["configuration_revision"],
                      venue_limits=data["venue_limits"], execution_targets=data["events"][event_id].get("targets", {}))
        from .ranking import snapshot_rank
        result.update(source_rank=snapshot_rank(data, data["events"][event_id]["source"]["chat_id"]), execution_accounts=self.accounts_ready())
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

    async def preview(self, event_id, interpretation):
        self.record_interpretation(event_id, interpretation)
        if interpretation.kind != "new_signal":
            return self.state.snapshot()["events"][event_id]
        def select(data):
            row = data["events"][event_id]
            if "targets" not in row:
                row["targets"] = self.selected_targets(row, data)
            if set(row["targets"]) == {"legacy:hyperliquid"} and row["targets"]["legacy:hyperliquid"]["status"] == "rejected":
                row["targets"]["legacy:hyperliquid"]["status"] = "pending"
            return row["targets"]
        targets = self.state.update(select)
        ranked = self.state.snapshot()["ranking_policy"]["enabled"]
        rank = None
        if ranked:
            from .ranking import snapshot_rank
            rank = self.state.update(lambda data: snapshot_rank(data, data["events"][event_id]["source"]["chat_id"]))
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
            network = rank["network"] if rank else self.engines[target["exchange"]].exchange.settings.network if target["exchange"] in self.engines else "mainnet"
            requested = target["exchange"]
            engine = self.engine_for(requested, network)
            try:
                if engine is None:
                    raise AqlaError(self.errors.get(requested + ":" + network, self.errors.get(requested, "Required account/network is unavailable")))
                if ranked:
                    target = {**target, "requested_exchange": requested, "network": network, "rank_snapshot": rank,
                              "setup_id": digest([rank["source_id"], sorted(interpretation.source_refs)])[:32], "leg_id": identity}
                    try:
                        await engine.exchange.market(interpretation.symbol)
                    except SymbolUnavailable as missing:
                        if requested != "hyperliquid":
                            raise
                        pipeline_target = next(row for row in self.state.snapshot()["pipelines"][target["pipeline_id"]]["targets"] if row["exchange"] == requested)
                        replacements = pipeline_target.get("fallback_exchanges")
                        replacements = ["aster", "lighter"] if replacements is None else replacements
                        reasons = []
                        replacement = None
                        readiness = self.accounts_ready()
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
                    with engine.state.scope(target):
                        plan = await engine.preview(event_id, interpretation)
                row = {**target, "status": "planned", "plan_id": plan["plan_id"]}
                outputs.append({**row, "plan": plan})
                reserved.add((engine.state.account_key, interpretation.symbol.upper()))
            except Exception as error:
                row = {**target, "status": "rejected", "error": str(error) if isinstance(error, AqlaError) else "Exchange preview unavailable"}
                outputs.append(row)
            self.state.update(lambda data, key=identity, value=row: data["events"][event_id]["targets"].__setitem__(key, value))
        self.finish_event(event_id)
        # Keep the old one-target interface usable for native Hermes and existing clients.
        if len(outputs) == 1 and outputs[0].get("plan"):
            return {**outputs[0]["plan"], "targets": outputs}
        if len(outputs) == 1 and outputs[0]["pipeline_id"] == "legacy" and outputs[0].get("error"):
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
            if trade:
                outcome["trade_id"] = trade["trade_id"]
        def record(data):
            for row in data["events"][saved["event_id"]].get("targets", {}).values():
                if row.get("plan_id") == plan_id:
                    row.update(outcome)
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
        self.state.update(lambda data: data["events"][event_id].update(status="updated", update_targets=results))
        return results[0]["result"] if len(results) == 1 and "result" in results[0] else {"targets": results}

    async def recover(self):
        from .ranking import evaluate
        self.state.update(evaluate)
        reports = []
        for venue in self.errors:
            if any((row["plan"].get("account_key") == venue if ":" in venue else row["plan"].get("exchange", "hyperliquid") == venue)
                   and row["status"] in OPEN for row in self.state.snapshot()["trades"].values()):
                reports.append({"exchange": venue, "error": "The original account adapter is unavailable; existing exposure needs attention"})
        for venue, engine in self.engines.items():
            for row in engine.state.snapshot()["trades"].values():
                market = Market.model_validate(row["plan"]["market"])
                engine.exchange.market_cache[market.symbol] = market
            async with engine.lock:
                for row in engine.state.snapshot()["trades"].values():
                    saved_rank = row["plan"].get("rank_snapshot")
                    if not saved_rank or row["status"] not in OPEN or Decimal(row["filled_quantity"]) > 0:
                        continue
                    from .ranking import snapshot_rank
                    current_rank = snapshot_rank(self.state.snapshot(), saved_rank["source_id"])
                    if current_rank["source_revision"] != saved_rank["source_revision"] or current_rank["policy_revision"] != saved_rank["policy_revision"]:
                        try:
                            await engine.cancel_entry(row["trade_id"])
                            if engine.state.snapshot()["trades"][row["trade_id"]]["status"] in OPEN:
                                await engine.refresh_trade(row["trade_id"])
                        except Exception:
                            reports.append({"trade_id": row["trade_id"], "error": "Rank-changed entry cancellation/fill reconciliation is unconfirmed"})
                rows = await engine.recover()
            reports.extend({"exchange": venue, **row} for row in rows)
        def expire(data):
            for event in data["events"].values():
                for target in event.get("targets", {}).values():
                    if target.get("trade_id") in data["trades"]:
                        target["status"] = data["trades"][target["trade_id"]]["status"]
                    if target["status"] == "planned" and data["plans"][target["plan_id"]]["plan"]["deadline_at"] <= now_ms():
                        target.update(status="expired", error="Original signal deadline expired")
                if event.get("targets") and all(target["status"] not in ("planned", "pending") for target in event["targets"].values()):
                    event["status"] = "executed" if any(target.get("trade_id") for target in event["targets"].values()) else "rejected"
        self.state.update(expire)
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

    async def reconcile(self, operation_id):
        row = self.state.snapshot()["operations"].get(operation_id)
        if row is None:
            raise AqlaError("Unknown operation")
        engine = self.engine_for(row.get("exchange", "hyperliquid"), row["request"]["network"])
        if engine is None:
            raise AqlaError("Original operation account is unavailable")
        return await engine.exchange.reconcile(operation_id)

    async def close(self):
        for engine in self.engines.values():
            await engine.exchange.close()

    @staticmethod
    def public_operation(row):
        return VenueAdapter.public_operation(row)
