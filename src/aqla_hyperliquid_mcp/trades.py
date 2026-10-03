from __future__ import annotations

import asyncio
from decimal import Decimal

from .api import Hyperliquid, wire_number
from .models import TERMINAL_ORDER, AqlaError, Interpretation, Policy, SourceEvent, TradePlan, digest, now_ms
from .rules import (
    latest_sources,
    make_plan,
    price_grid,
    quantity_grid,
    slippage_price,
    split_targets,
    verify_evidence,
)
from .state import State

OPEN = {"preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup"}
UNFINISHED = {"pending", "interpreted", "needs_reconciliation"}


class Trading:
    def __init__(self, exchange: Hyperliquid, state: State):
        self.exchange, self.state = exchange, state
        self.lock = asyncio.Lock()
        self.recovery_checked = False

    def ingest(self, source: SourceEvent, *, native_forward=False):
        snapshot = self.state.snapshot()
        trusted_forward = native_forward and self.exchange.settings.telegram_owner_id and source.chat_id == self.exchange.settings.telegram_owner_id
        if not trusted_forward and source.chat_id not in {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}:
            raise AqlaError("Source is not subscribed")
        if source.event_id in snapshot["events"]:
            return snapshot["events"][source.event_id]
        if source.event_id in snapshot.get("event_tombstones", {}):
            return {"event_id": source.event_id, "source_id": source.source_id, "status": "already_processed"}

        def insert(state):
            if source.event_id in state["events"]:
                return state["events"][source.event_id]
            if source.event_id in state.get("event_tombstones", {}):
                return {"event_id": source.event_id, "source_id": source.source_id, "status": "already_processed"}
            row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json"),
                   "status": "context" if source.historical else "pending", "interpretation": None, "native_forward": bool(trusted_forward)}
            state["events"][source.event_id] = row
            state["cursors"][source.chat_id] = max(source.message_id, state["cursors"].get(source.chat_id, 0))
            self.prune_events(state, inserted_event=source.event_id)
            return row

        return self.state.update(insert)

    def prune_events(self, state, *, inserted_event=None):
        maximum = self.exchange.settings.max_events
        if len(state["events"]) <= maximum:
            return
        protected_refs = {ref for trade in state["trades"].values() if trade["status"] in OPEN for ref in trade["plan"]["source_refs"]}
        protected_events = {event for plan in state["plans"].values() if plan["plan"]["deadline_at"] > now_ms() for event in plan["plan"].get("source_revisions", {}).values()}
        protected_events.update(key.removeprefix("Signale:") for key in state.get("journal_pending", {}) if key.startswith("Signale:"))
        # State.update enqueues journal changes after this callback, in the same write.
        if self.state.journal_enabled and inserted_event:
            protected_events.add(inserted_event)
        by_chat = {}
        for row in state["events"].values():
            by_chat.setdefault(row["source"]["chat_id"], []).append(row)
        for rows in by_chat.values():
            rows.sort(key=lambda row: (row["source"]["sent_at"], row["source"].get("edited_at") or 0))
            protected_events.update(row["event_id"] for row in rows[-self.exchange.settings.max_context_messages:])
        candidates = [row for row in state["events"].values() if row["source_id"] not in protected_refs and row["event_id"] not in protected_events
                      and row["status"] in ("report", "unrelated", "ambiguous", "incomplete", "context", "executed", "updated")]
        candidates.sort(key=lambda row: row["source"]["received_at"])
        for row in candidates[:max(0, len(state["events"]) - maximum)]:
            state.setdefault("event_tombstones", {})[row["event_id"]] = row["source_id"]
            del state["events"][row["event_id"]]

    def context(self, event_id):
        snapshot = self.state.snapshot()
        row = snapshot["events"].get(event_id)
        if not row:
            raise AqlaError("Unknown source event")
        source = row["source"]
        messages = [item for item in snapshot["events"].values() if item["source"]["chat_id"] == source["chat_id"]]
        messages.sort(key=lambda item: (item["source"]["sent_at"], item["source"].get("edited_at") or 0))
        included = messages[-self.exchange.settings.max_context_messages:]
        reply = source.get("reply_to_id")
        seen = set()
        while reply and reply not in seen:
            seen.add(reply)
            parent = next((item for item in reversed(messages) if item["source"]["message_id"] == reply), None)
            if parent is None:
                break
            if parent not in included:
                included.insert(0, parent)
            reply = parent["source"].get("reply_to_id")
        trades = [trade for trade in snapshot["trades"].values() if trade["status"] in OPEN and trade["chat_id"] == source["chat_id"]]
        return {"event": row, "messages": included, "trades": trades, "policy": snapshot["policy"],
                "policy_revision": snapshot["policy_revision"], "context_truncated": len(included) < len(messages)}

    def record_interpretation(self, event_id, interpretation: Interpretation):
        snapshot = self.state.snapshot()
        row = snapshot["events"].get(event_id)
        if row is None:
            raise AqlaError("Unknown source event")
        refs = [item for item in snapshot["events"].values() if item["source_id"] in interpretation.source_refs]
        if not refs or any(item["source"]["chat_id"] != row["source"]["chat_id"] for item in refs):
            raise AqlaError("A setup may only combine messages from the same source")
        verify_evidence(interpretation, snapshot["events"])
        previous = row.get("interpretation")
        if row["status"] in ("executed", "updated"):
            if previous != interpretation.model_dump(mode="json"):
                raise AqlaError("An executed revision is immutable; interpret the edit as a new update revision")
            return row
        if row["source"].get("historical") and interpretation.kind in ("new_signal", "trade_update"):
            raise AqlaError("Historical context cannot authorize an exchange action")
        self.state.update(lambda state: state["events"][event_id].update(
            interpretation=interpretation.model_dump(mode="json"),
            status=interpretation.kind if interpretation.kind not in ("new_signal", "trade_update") else "interpreted"))
        return self.state.snapshot()["events"][event_id]

    async def preview(self, event_id, interpretation: Interpretation):
        row = self.record_interpretation(event_id, interpretation)
        if interpretation.kind != "new_signal":
            return row
        snapshot = self.state.snapshot()
        if snapshot["paused"]:
            raise AqlaError("New entries are paused; existing exits and protection continue")
        if snapshot["policy"] is None:
            raise AqlaError("User trading rules have not been configured")
        self.state.assert_binding(self.exchange.settings)
        policy = Policy.model_validate(snapshot["policy"])
        if policy.product != "perp":
            raise AqlaError("Automatic source trading requires perpetual markets with native reduce-only protection; Spot remains available through the full MCP")
        active = [trade for trade in snapshot["trades"].values() if trade["status"] in OPEN]
        if len(active) >= policy.max_open_trades:
            raise AqlaError("Maximum open trade count reached")
        if any(ref in snapshot["bindings"] for ref in interpretation.source_refs):
            raise AqlaError("This source already owns a trade; process its edit as an update")
        market = await self.exchange.market(interpretation.symbol, policy.product)
        if any(trade["plan"]["market"]["asset_id"] == market.asset_id for trade in active):
            raise AqlaError("An active trade already owns this net-position market")
        positions = await self.positions(market)
        if self.position_size(positions, market) != 0:
            raise AqlaError("Existing exchange exposure has no free logical owner; no additional entry")
        account = await self.exchange.account_snapshot(market)
        refs = [item["source"]["sent_at"] for item in latest_sources(snapshot["events"], interpretation.source_refs).values()]
        risk = sum(Decimal(trade["reserved_risk"]) for trade in active)
        plan = make_plan(interpretation, market, account, policy, snapshot["policy_revision"], min(refs),
                         (self.exchange.settings.network, self.exchange.account.lower()), reserved_risk=risk,
                         principal=self.exchange.settings.account.lower())
        plan.source_revisions = self.source_revisions(snapshot, interpretation.source_refs)
        plan.plan_id = digest(plan.model_dump(mode="json", exclude={"plan_id"}))
        def save(state):
            self.assert_plan_binding(state, plan)
            state["plans"][plan.plan_id] = {"event_id": event_id, "plan": plan.model_dump(mode="json")}
        self.state.update(save)
        return plan.model_dump(mode="json")

    @staticmethod
    def source_revisions(snapshot, refs):
        return {ref: row["event_id"] for ref, row in latest_sources(snapshot["events"], refs).items()}

    def assert_plan_binding(self, snapshot, plan):
        self.state.assert_binding(self.exchange.settings)
        if (snapshot["network"] != plan.network or plan.network != self.exchange.settings.network
                or plan.account.lower() != self.exchange.account.lower()
                or plan.principal not in (None, self.exchange.settings.account.lower())):
            raise AqlaError("Saved plan belongs to another trading target or network")

    def validate_admission(self, snapshot, plan, account=None, *, owned=False):
        self.assert_plan_binding(snapshot, plan)
        if snapshot["paused"] or snapshot["policy_revision"] != plan.policy_revision:
            raise AqlaError("Pause or policy changed; create a new unsent plan")
        if now_ms() >= plan.deadline_at:
            raise AqlaError("Entry plan deadline expired")
        if self.source_revisions(snapshot, plan.source_refs) != plan.source_revisions:
            raise AqlaError("Source changed after preview; the old unsent plan cannot execute")
        subscribed = {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}
        for row in latest_sources(snapshot["events"], plan.source_refs).values():
            source = row["source"]
            forwarded = row.get("native_forward") and source["chat_id"] == self.exchange.settings.telegram_owner_id
            if source["chat_id"] not in subscribed and not forwarded:
                raise AqlaError("Source is no longer subscribed; no unsent exposure may execute")
        active = [trade for key, trade in snapshot["trades"].items() if trade["status"] in OPEN and (not owned or key != plan.trade_id)]
        if len(active) >= plan.policy.max_open_trades or any(trade["plan"]["market"]["asset_id"] == plan.market.asset_id for trade in active):
            raise AqlaError("Trade admission changed after preview")
        for ref in plan.source_refs:
            binding = snapshot["bindings"].get(ref)
            if binding and (not owned or binding["trade_id"] != plan.trade_id):
                raise AqlaError("Source has already been consumed")
        if any(row["phase"] in ("prepared", "sending", "unknown") for row in snapshot["operations"].values()):
            raise AqlaError("Unresolved exchange operations must be reconciled before new exposure")
        if account is not None:
            reserved = sum(Decimal(trade["reserved_risk"]) for trade in active)
            if now_ms() - account.observed_at > 10000:
                raise AqlaError("Account observation is stale")
            if (plan.risk_usdc + reserved > account.equity * plan.policy.max_total_risk_percent / 100
                    or plan.risk_usdc > account.equity * plan.policy.risk_percent / 100
                    or plan.quantity * plan.entry_limit / plan.policy.leverage > account.available_margin):
                raise AqlaError("Current equity/margin no longer permits this exact plan")

    async def positions(self, market):
        if market.product == "spot":
            return await self.exchange.info("spotClearinghouseState", user=self.exchange.account)
        return await self.exchange.info("clearinghouseState", user=self.exchange.account, **({"dex": market.dex} if market.dex else {}))

    @staticmethod
    def position_size(raw, market):
        if market.product == "spot":
            row = next((item for item in raw["balances"] if item["coin"] == market.base_token), None)
            return Decimal(row["total"]) if row else Decimal(0)
        row = next((item["position"] for item in raw["assetPositions"] if item["position"]["coin"] == market.symbol), None)
        return Decimal(row["szi"]) if row else Decimal(0)

    @staticmethod
    def order(plan, quantity, price, *, reduce=False, trigger=None, cloid=None):
        order = {"a": plan.market.asset_id, "b": (plan.side == "long") != reduce, "p": wire_number(price),
                 "s": wire_number(quantity), "r": reduce if plan.market.product == "perp" else False,
                 "t": {"limit": {"tif": "Ioc" if not reduce and plan.entry_kind == "market" else "Gtc"}}}
        if trigger:
            order["t"] = {"trigger": {"isMarket": True, "triggerPx": wire_number(trigger[1]), "tpsl": trigger[0]}}
        if cloid:
            order["c"] = cloid
        return order

    async def execute(self, plan_id):
        async with self.lock:
            if not self.recovery_checked:
                await self.recover()
                if not self.recovery_checked:
                    raise AqlaError("Startup reconciliation is incomplete; new exposure remains blocked")
            snapshot = self.state.snapshot()
            saved = snapshot["plans"].get(plan_id)
            if not saved:
                raise AqlaError("Unknown immutable plan")
            plan = TradePlan.model_validate(saved["plan"])
            self.assert_plan_binding(snapshot, plan)
            if plan.trade_id in snapshot["trades"]:
                return snapshot["trades"][plan.trade_id]
            self.validate_admission(snapshot, plan)
            if self.position_size(await self.positions(plan.market), plan.market) != 0:
                raise AqlaError("Exchange position changed after preview")
            await self.exchange.verify_signer()
            current_market = await self.exchange.market(plan.market.symbol, plan.market.product)
            if (current_market.asset_id, current_market.size_decimals) != (plan.market.asset_id, plan.market.size_decimals):
                raise AqlaError("Exchange market metadata changed after preview")
            account = await self.exchange.account_snapshot(plan.market)
            trade = {"trade_id": plan.trade_id, "chat_id": snapshot["events"][saved["event_id"]]["source"]["chat_id"],
                     "plan": plan.model_dump(mode="json"), "event_id": saved["event_id"], "status": "preparing",
                     "reserved_risk": str(plan.risk_usdc), "entry_operation_id": "entry:" + plan.trade_id,
                     "entry_cloid": self.exchange.cloid("entry:" + plan.trade_id), "orders": [],
                     "filled_quantity": "0", "remaining_quantity": "0", "entry_vwap": None,
                     "stop_price": str(plan.stop_loss), "tp_filled": ["0"] * len(plan.take_profits), "management_revision": 0,
                     "fill_events": {}, "exit_orders": [], "updates": {}, "targets": [str(price) for price in plan.take_profits]}

            def admit(state):
                self.validate_admission(state, plan, account)
                state["trades"][plan.trade_id] = trade
                for ref in plan.source_refs:
                    state["bindings"][ref] = {"trade_id": plan.trade_id, "kind": "entry"}
                state["events"][saved["event_id"]]["status"] = "executed"

            self.state.update(admit)
            return await self._entry_steps(plan, trade)

    async def _entry_steps(self, plan, trade):
        if plan.market.product == "perp":
            leverage = await self.exchange.mutate("leverage:" + plan.trade_id,
                                                 {"type": "updateLeverage", "asset": plan.market.asset_id,
                                                  "isCross": plan.policy.margin_mode == "cross", "leverage": plan.policy.leverage}, deadline=plan.deadline_at,
                                                  prepare_guard=lambda data: self.validate_admission(data, plan, owned=True))
            if leverage["phase"] != "acknowledged":
                self._patch(plan.trade_id, status="unknown" if leverage["phase"] == "unknown" else "rejected")
                return self.state.snapshot()["trades"][plan.trade_id]
        account = await self.exchange.account_snapshot(plan.market)
        self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        entry = self.order(plan, plan.quantity, plan.entry_limit, cloid=trade["entry_cloid"])
        initial_stop = self.order(plan, plan.quantity, plan.stop_loss * (Decimal("0.95") if plan.side == "long" else Decimal("1.05")),
                                  reduce=True, trigger=("sl", plan.stop_loss), cloid=self.exchange.cloid(trade["entry_operation_id"], 1))
        initial_stop["p"] = wire_number(price_grid(Decimal(initial_stop["p"]), plan.market, up=plan.side == "short"))
        action = {"type": "order", "orders": [entry, initial_stop], "grouping": "normalTpsl"}
        self._patch(plan.trade_id, status="opening", orders=[{"kind": "sl", "cloid": initial_stop["c"], "quantity": str(plan.quantity), "price": str(plan.stop_loss)}])
        result = await self.exchange.mutate(trade["entry_operation_id"], action, deadline=plan.deadline_at,
                                            prepare_guard=lambda data: self.validate_admission(data, plan, account, owned=True))
        if result["phase"] == "unknown":
            self._patch(plan.trade_id, status="unknown")
        await self.refresh_trade(plan.trade_id)
        return self.state.snapshot()["trades"][plan.trade_id]

    async def resume_unsent_entry(self, trade_id):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        if trade["entry_operation_id"] in snapshot["operations"]:
            return await self.refresh_trade(trade_id)
        self.assert_plan_binding(snapshot, plan)
        try:
            self.validate_admission(snapshot, plan, owned=True)
            if self.position_size(await self.positions(plan.market), plan.market) != 0:
                raise AqlaError("Unsent entry cannot resume against an existing foreign position")
            account = await self.exchange.account_snapshot(plan.market)
            self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        except AqlaError as error:
            if any(row["phase"] in ("prepared", "sending", "unknown") for row in self.state.snapshot()["operations"].values()):
                raise
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason=str(error))
            return self.state.snapshot()["trades"][trade_id]
        return await self._entry_steps(plan, trade)

    def _patch(self, trade_id, **values):
        old = self.state.snapshot()["trades"][trade_id]
        changed = {key: value for key, value in values.items() if old.get(key) != value}
        if not changed:
            return

        def patch(state):
            row = state["trades"][trade_id]
            row.update(changed)
            important = {key: value for key, value in changed.items() if key in ("status", "filled_quantity", "remaining_quantity", "stop_price")}
            if important and row["status"] not in ("preparing", "opening"):
                identity = digest([trade_id, important])[:32]
                state["notifications"].setdefault(identity, {"trade_id": trade_id, "change": important, "observed_at": now_ms()})

        self.state.update(patch)

    async def _fills(self, plan):
        result = {}
        pages = 0

        async def window(start, end):
            nonlocal pages
            pages += 1
            if pages > 64:
                raise AqlaError("Fill history requires more than the bounded page budget")
            rows = await self.exchange.info("userFillsByTime", user=self.exchange.account, startTime=start, endTime=end)
            if len(rows) >= 2000:
                if start >= end:
                    raise AqlaError("Fill history is saturated within one millisecond; absence is not proved")
                middle = (start + end) // 2
                await window(start, middle)
                await window(middle + 1, end)
            else:
                for row in rows:
                    result[digest(row)] = row

        await window(plan.created_at, now_ms())
        return list(result.values())

    async def capture_fills(self, trade_id, entry_status=None):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        entry_status = entry_status or await self.exchange.order_status(trade["entry_cloid"])
        observed = {trade["entry_cloid"]: entry_status}
        mapping = {}
        entry_oid = entry_status.get("order", {}).get("order", {}).get("oid")
        if entry_oid is not None:
            mapping[entry_oid] = {"kind": "entry"}
        for order in [*trade["orders"], *trade.get("exit_orders", [])]:
            status = await self.exchange.order_status(order["cloid"])
            observed[order["cloid"]] = status
            oid = status.get("order", {}).get("order", {}).get("oid")
            if oid is not None:
                mapping[oid] = order
        owned = dict(trade.get("fill_events", {}))
        for row in await self._fills(plan):
            if row.get("oid") not in mapping or row.get("coin") != plan.market.symbol:
                continue
            identity = digest([row["oid"], row.get("tid"), row.get("time"), row.get("sz"), row.get("px")])
            fact = {key: row.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin")}
            fact.update(kind=mapping[row["oid"]]["kind"], index=mapping[row["oid"]].get("index"))
            if identity in owned and owned[identity] != fact:
                raise AqlaError("An immutable owned fill changed; reconcile before managing quantities")
            owned[identity] = fact
        entry_fills = [row for row in owned.values() if row["kind"] == "entry"]
        total = sum((Decimal(row["sz"]) for row in entry_fills), Decimal(0))
        cost = sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in entry_fills), Decimal(0))
        target_fills = [sum((Decimal(row["sz"]) for row in owned.values() if row["kind"] == "tp" and row["index"] == index), Decimal(0)) for index in range(len(plan.take_profits))]
        self._patch(trade_id, fill_events=owned, filled_quantity=str(total), tp_filled=[str(size) for size in target_fills],
                    entry_vwap=str(cost / total) if total else trade["entry_vwap"])
        self.observe_orders(trade_id, observed)
        return total, cost

    def observe_orders(self, trade_id, observations):
        previous = self.state.snapshot()["trades"][trade_id].get("order_observations", {})
        proposed = dict(previous)
        for identity, raw in observations.items():
            detail = raw.get("order", {})
            fact = {"status": detail.get("status") or raw.get("status", "unknown"), "oid": detail.get("order", {}).get("oid")}
            old = previous.get(identity, {})
            if {key: old.get(key) for key in fact} != fact:
                proposed[identity] = {**fact, "changed_at": now_ms()}
        if proposed != previous:
            self._patch(trade_id, order_observations=proposed)

    def owned_remaining(self, trade_id, signed):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        fills = trade.get("fill_events", {}).values()
        entered = sum((Decimal(row["sz"]) for row in fills if row["kind"] == "entry"), Decimal(0))
        exited = sum((Decimal(row["sz"]) for row in fills if row["kind"] != "entry"), Decimal(0))
        owned = max(Decimal(0), entered - exited)
        observed = abs(signed)
        if owned == 0 and entered:
            return Decimal(0)
        if observed > owned or (signed < 0 and plan.side == "long") or (signed > 0 and plan.side == "short" and plan.market.product == "perp"):
            raise AqlaError("Exchange exposure differs from remaining owned fills; no automatic management")
        if observed < owned:
            raise AqlaError("Position and owned fills are inconsistent; preserve protection until fresh reconciliation")
        return observed

    async def refresh_trade(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        if trade["status"] == "cleanup":
            return await self.finish_cleanup(trade_id)
        plan = TradePlan.model_validate(trade["plan"])
        entry_status = await self.exchange.order_status(trade["entry_cloid"])
        total, cost = await self.capture_fills(trade_id, entry_status)
        stage = entry_status.get("order", {}).get("status")
        if total and stage == "open":
            # Freeze the actual fill budget before installing partial target legs.
            await self.cancel_entry(trade_id)
            entry_status = await self.exchange.order_status(trade["entry_cloid"])
            stage = entry_status.get("order", {}).get("status")
            total, cost = await self.capture_fills(trade_id, entry_status)
        signed = self.position_size(await self.positions(plan.market), plan.market)
        remaining = self.owned_remaining(trade_id, signed) if total else abs(signed)
        if total and remaining:
            self._patch(trade_id, filled_quantity=str(total), remaining_quantity=str(remaining), entry_vwap=str(cost / total),
                        status="open" if trade["status"] == "open" else "protecting")
            await self.ensure_protection(trade_id)
        elif stage in TERMINAL_ORDER and not remaining:
            self.begin_cleanup(trade_id, "closed" if total else "rejected")
            await self.finish_cleanup(trade_id)
        elif remaining and not total:
            self._patch(trade_id, status="unknown")
            raise AqlaError("Position exists but its entry fills are not proved; new entries remain blocked")
        elif now_ms() >= plan.deadline_at and stage == "open":
            await self.cancel_entry(trade_id)
            return await self.refresh_trade(trade_id)
        return self.state.snapshot()["trades"][trade_id]

    async def ensure_protection(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        remaining = Decimal(trade["remaining_quantity"])
        if remaining <= 0:
            return
        status_rows = [(order, await self.exchange.order_status(order["cloid"])) for order in trade["orders"] if order["kind"] == "sl"]
        covered = any(row.get("order", {}).get("status") == "open" and self._stop_matches(row, plan, remaining, Decimal(trade["stop_price"])) for _, row in status_rows)
        if not covered:
            await self.cancel_entry(trade_id)
            await self.replace_stop(trade_id, Decimal(trade["stop_price"]))
            refreshed = self.state.snapshot()["trades"][trade_id]
            if refreshed["status"] in ("protecting", "closing"):
                return
        trade = self.state.snapshot()["trades"][trade_id]
        if trade["status"] == "closing":
            return
        if not any(order["kind"] == "tp" for order in trade["orders"]):
            sizes = split_targets(Decimal(trade["filled_quantity"]), len(plan.take_profits), plan.policy, plan.market)
            operation = "targets:" + trade_id + ":0"
            orders = [self.order(plan, size, price, reduce=True, cloid=self.exchange.cloid(operation, i))
                      for i, (size, price) in enumerate(zip(sizes, plan.take_profits, strict=True))]
            def stage_initial(state):
                row = state["trades"][trade_id]
                row["orders"].extend({"kind": "tp", "cloid": order["c"], "quantity": order["s"], "price": order["p"], "index": i} for i, order in enumerate(orders))
                row["target_budgets"] = [str(size) for size in sizes]
                row["initial_targets"] = {"operation_id": operation, "action": {"type": "order", "orders": orders, "grouping": "na"}}
            self.state.update(stage_initial)
        staged = self.state.snapshot()["trades"][trade_id].get("initial_targets")
        if staged and staged["operation_id"] not in self.state.snapshot()["operations"]:
            result = await self.exchange.mutate(staged["operation_id"], staged["action"])
            if result["phase"] not in ("acknowledged", "reconciled"):
                self._patch(trade_id, status="protecting")
                return
        active_targets = [order for order in self.state.snapshot()["trades"][trade_id]["orders"] if order["kind"] == "tp" and not order.get("retired")]
        if active_targets:
            observations = [await self.exchange.order_status(order["cloid"]) for order in active_targets]
            if any(row.get("order", {}).get("status") not in {"open", "filled", "triggered"} for row in observations):
                self._patch(trade_id, status="protecting")
                return
        self._patch(trade_id, status="open")
        stage = plan.policy.break_even_after_tp
        if stage is not None:
            budgets = split_targets(Decimal(trade["filled_quantity"]), len(plan.take_profits), plan.policy, plan.market)
            filled = [Decimal(value) for value in self.state.snapshot()["trades"][trade_id]["tp_filled"]]
            if stage <= len(budgets) and all(filled[i] >= budgets[i] for i in range(stage)):
                entry = Decimal(trade["entry_vwap"])
                tighter = entry > Decimal(trade["stop_price"]) if plan.side == "long" else entry < Decimal(trade["stop_price"])
                if tighter:
                    await self.replace_stop(trade_id, entry, "break-even:" + trade_id + ":" + str(stage))

    @staticmethod
    def _stop_matches(status, plan, size, stop):
        order = status.get("order", {}).get("order", {})
        return (order.get("coin") == plan.market.symbol and order.get("isTrigger") is True
                and order.get("reduceOnly") is (plan.market.product == "perp")
                and Decimal(order.get("sz", "0")) >= size and Decimal(order.get("triggerPx", "0")) == stop
                and order.get("side") == ("A" if plan.side == "long" else "B"))

    async def cancel_entry(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        if trade["entry_operation_id"] not in self.state.snapshot()["operations"]:
            return {"not_sent": True}
        status = await self.exchange.order_status(trade["entry_cloid"])
        if status.get("order", {}).get("status") in TERMINAL_ORDER:
            return status
        result = await self.exchange.mutate("cancel-entry:" + trade_id,
                                            {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": trade["entry_cloid"]}]})
        status = await self.exchange.order_status(trade["entry_cloid"])
        if status.get("order", {}).get("status") not in TERMINAL_ORDER:
            raise AqlaError("Entry remainder termination is not proved; preserve existing protection")
        return result

    async def replace_stop(self, trade_id, proposed: Decimal, operation_id=None):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        if trade["status"] not in OPEN:
            return {"already_closed": True}
        remaining = Decimal(trade["remaining_quantity"])
        if not remaining:
            return {"already_closed": True}
        stop = price_grid(proposed, plan.market, up=plan.side == "long")
        old_stop = Decimal(trade["stop_price"])
        widening = stop < old_stop if plan.side == "long" else stop > old_stop
        if widening and not plan.policy.allow_stop_widening:
            raise AqlaError("Stop widening is not permitted")
        if widening and remaining * abs(Decimal(trade["entry_vwap"]) - stop) > Decimal(trade["reserved_risk"]):
            raise AqlaError("Stop widening exceeds the original loss budget")
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        signed = self.position_size(await self.positions(plan.market), plan.market)
        remaining = self.owned_remaining(trade_id, signed)
        if not remaining:
            return {"already_closed": True}
        market = await self.exchange.market(plan.market.symbol, plan.market.product)
        if (plan.side == "long" and stop >= market.mid) or (plan.side == "short" and stop <= market.mid):
            return await self.close_position(trade_id, Decimal(100), operation_id="stop-crossed:" + (operation_id or trade_id))
        operation_id = operation_id or "stop:" + trade_id + ":" + str(trade["management_revision"])
        cloid = self.exchange.cloid(operation_id)
        limit = price_grid(stop * (Decimal("0.95") if plan.side == "long" else Decimal("1.05")), plan.market, up=plan.side == "short")
        existing = self.state.snapshot()["operations"].get(operation_id)
        replacement = self.order(plan, remaining, limit, reduce=True, trigger=("sl", stop), cloid=cloid)
        if not any(order["cloid"] == cloid for order in trade["orders"]):
            self.state.update(lambda state: state["trades"][trade_id]["orders"].append(
                {"kind": "sl", "cloid": cloid, "quantity": str(remaining), "price": str(stop)}))
        result = existing or await self.exchange.mutate(operation_id, {"type": "order", "orders": [replacement], "grouping": "na"})
        confirmed = await self.exchange.order_status(cloid)
        if confirmed.get("order", {}).get("status") == "open" and self._stop_matches(confirmed, plan, remaining, stop):
            self._patch(trade_id, stop_price=str(stop), management_revision=trade["management_revision"] + 1)
            old = [order for order in trade["orders"] if order["kind"] == "sl" and order["cloid"] != cloid]
            for order in old:
                await self.exchange.mutate("cancel:" + operation_id + ":" + order["cloid"],
                                           {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": order["cloid"]}]})
            self._patch(trade_id, status="open")
            return result
        if result["phase"] == "rejected":
            return await self.close_position(trade_id, Decimal(100), operation_id="emergency:" + operation_id)
        self._patch(trade_id, status="protecting")
        return {"operation": result, "old_stop_retained": True, "replacement_unconfirmed": True}

    async def dispatch_close_intent(self, trade_id, intent):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(snapshot, plan)
        previous = snapshot["operations"].get(intent["operation_id"])
        if previous:
            return previous
        if trade["status"] not in OPEN or trade["status"] == "cleanup":
            return {"already_closed": True}
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        remaining = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        if not remaining:
            return {"already_closed": True}
        return await self.exchange.mutate(intent["operation_id"], intent["action"],
                                          prepare_guard=lambda data: self.assert_plan_binding(data, plan))

    async def close_position(self, trade_id, percent=Decimal(100), operation_id=None):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(snapshot, plan)
        if trade["status"] not in OPEN or trade["status"] == "cleanup":
            return {"already_closed": True}
        operation_id = operation_id or "close:" + trade_id + ":" + str(trade["management_revision"])
        existing = snapshot["operations"].get(operation_id)
        if existing:
            return existing
        staged = trade.get("close_intents", {}).get(operation_id)
        if staged:
            return await self.dispatch_close_intent(trade_id, staged)
        if any(key not in snapshot["operations"] for key in trade.get("close_intents", {})):
            raise AqlaError("Finish the saved close intention before another reduction")
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        size = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        if not size:
            return {"already_closed": True}
        amount = quantity_grid(size * percent / 100, plan.market)
        if amount <= 0:
            raise AqlaError("Reduction rounds to zero")
        market = await self.exchange.market(plan.market.symbol, plan.market.product)
        limit = slippage_price(market.mid, plan.policy.max_slippage_percent, plan.market, buy=plan.side == "short")
        order = self.order(plan, amount, limit, reduce=True, cloid=self.exchange.cloid(operation_id))
        order["t"] = {"limit": {"tif": "Ioc"}}
        intent = {"operation_id": operation_id, "action": {"type": "order", "orders": [order], "grouping": "na"}, "created_at": now_ms()}
        def stage(data):
            self.assert_plan_binding(data, plan)
            row = data["trades"][trade_id]
            row.setdefault("close_intents", {})[operation_id] = intent
            row.setdefault("exit_orders", []).append({"kind": "close", "cloid": order["c"], "quantity": order["s"], "price": order["p"]})
            row.update(status="closing", management_revision=row["management_revision"] + 1)
        self.state.update(stage)
        return await self.dispatch_close_intent(trade_id, intent)

    async def _cancel_owned_orders(self, trade_id, include_sl=False, operation_id=None):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        orders = [order for order in trade["orders"] if include_sl or order["kind"] != "sl"]
        if not orders:
            return
        cancels = [{"asset": plan.market.asset_id, "cloid": order["cloid"]} for order in orders]
        return await self.exchange.mutate(operation_id or "cleanup:" + trade_id + ":" + str(trade["management_revision"]), {"type": "cancelByCloid", "cancels": cancels})

    def begin_cleanup(self, trade_id, terminal_status):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
        identities = []
        for order in trade["orders"]:
            for operation in snapshot["operations"].values():
                for index, leg in enumerate(operation["wire_action"].get("orders", [])):
                    rejected = operation["phase"] == "rejected" or any(row["index"] == index and row["disposition"] == "rejected" for row in operation.get("legs", []))
                    if leg.get("c") == order["cloid"] and not rejected:
                        identities.append(order["cloid"])
        self._patch(trade_id, status="cleanup", remaining_quantity="0", cleanup_target=terminal_status,
                    cleanup_cloids=sorted(set(identities)), cleanup_complete=False)

    async def finish_cleanup(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
        if trade.get("cleanup_cloids"):
            await self._cancel_owned_orders(trade_id, include_sl=True)
            observations = [await self.exchange.order_status(identity) for identity in trade["cleanup_cloids"]]
            self.observe_orders(trade_id, dict(zip(trade["cleanup_cloids"], observations, strict=True)))
            if any(row.get("order", {}).get("status") not in TERMINAL_ORDER for row in observations):
                raise AqlaError("Owned exit cancellation is unconfirmed; market ownership remains reserved")
        self._patch(trade_id, status=trade["cleanup_target"], cleanup_complete=True, reserved_risk="0")
        return self.state.snapshot()["trades"][trade_id]

    def update_candidates(self, event_id, signal: Interpretation):
        context = self.context(event_id)
        snapshot = self.state.snapshot()
        reply = context["event"]["source"].get("reply_to_id")
        parent_ref = digest([context["event"]["source"]["chat_id"], reply])[:32] if reply else None
        binding = snapshot["bindings"].get(parent_ref, {})
        trades = context["trades"]
        if binding:
            trades = [trade for trade in trades if trade["trade_id"] == binding["trade_id"]]
        if signal.symbol:
            def same_symbol(trade):
                named = signal.symbol
                if trade["plan"]["market"]["product"] == "perp" and ":" not in named:
                    named = named.upper()
                    for suffix in ("/USDT", "/USDC", "USDT", "USDC"):
                        if named.endswith(suffix):
                            named = named[:-len(suffix)]
                            break
                return trade["plan"]["market"]["symbol"] == named
            trades = [trade for trade in trades if same_symbol(trade)]
        if signal.side:
            trades = [trade for trade in trades if trade["plan"]["side"] == signal.side]
        return trades

    async def update_trade(self, event_id, signal: Interpretation):
        async with self.lock:
            if signal.kind != "trade_update" or not signal.update_action:
                raise AqlaError("An update needs a definite action")
            snapshot = self.state.snapshot()
            previous = snapshot["bindings"].get("update:" + event_id)
            if previous:
                if previous["kind"] != signal.update_action or previous["payload_hash"] != digest(signal.model_dump(mode="json")) or signal.bound_trade_id not in (None, previous["trade_id"]):
                    raise AqlaError("Update source is already bound to another trade or action")
                trade = snapshot["trades"][previous["trade_id"]]
                self.assert_plan_binding(snapshot, TradePlan.model_validate(trade["plan"]))
                if snapshot["events"][event_id]["status"] == "updated":
                    return {"duplicate": True, "trade": trade}
                if trade["status"] not in OPEN:
                    operation = snapshot["operations"].get("update:" + event_id)
                    uncertain = operation and operation["phase"] in ("prepared", "sending", "unknown")
                    self.state.update(lambda data: data["events"][event_id].update(status="needs_reconciliation" if uncertain else "updated"))
                    return {"trade_id": trade["trade_id"], "result": operation or {"already_closed": True}}
                candidates = [trade]
            else:
                candidates = self.update_candidates(event_id, signal)
            self.record_interpretation(event_id, signal)
            if len(candidates) != 1 or signal.bound_trade_id not in (None, candidates[0]["trade_id"]):
                raise AqlaError("Trade update cannot be bound unambiguously")
            trade = candidates[0]
            if trade["status"] == "cleanup":
                raise AqlaError("Exit cleanup must finish before further trade updates")
            plan = TradePlan.model_validate(trade["plan"])
            self.assert_plan_binding(self.state.snapshot(), plan)
            if signal.update_action not in plan.policy.allowed_updates:
                raise AqlaError("Update action is not allowed by this trade's user rules")
            previous = self.state.snapshot()["bindings"].get("update:" + event_id)
            binding = {"trade_id": trade["trade_id"], "kind": signal.update_action, "payload_hash": digest(signal.model_dump(mode="json"))}
            if previous and previous != binding:
                raise AqlaError("Update source is already bound to another trade or action")
            if previous and self.state.snapshot()["events"][event_id]["status"] == "updated":
                return {"duplicate": True, "trade": self.state.snapshot()["trades"][trade["trade_id"]]}
            # Validate requested values before consuming the update source or canceling orders.
            if signal.update_action == "reduce" and signal.reduce_percent is None:
                raise AqlaError("Reduction update needs an explicit percentage")
            if signal.update_action == "move_stop":
                if signal.stop_loss is None:
                    raise AqlaError("Stop update needs a source-backed price")
                old = Decimal(trade["stop_price"])
                if not plan.policy.allow_stop_widening and (signal.stop_loss < old if plan.side == "long" else signal.stop_loss > old):
                    raise AqlaError("Stop widening is not permitted")
            if signal.update_action == "replace_targets":
                self.validate_targets(trade, plan, signal.take_profits)
            if not previous:
                self.state.update(lambda state: state["bindings"].__setitem__("update:" + event_id, binding))
            operation = "update:" + event_id
            if signal.update_action == "cancel_entry":
                result = await self.cancel_entry(trade["trade_id"])
            elif signal.update_action in ("close", "reduce"):
                result = await self.close_position(trade["trade_id"], signal.reduce_percent or Decimal(100), operation)
            elif signal.update_action in ("move_stop", "break_even"):
                price = Decimal(trade["entry_vwap"]) if signal.update_action == "break_even" and trade["entry_vwap"] else signal.stop_loss
                if not price:
                    raise AqlaError("Stop update needs a price or proved entry VWAP")
                result = await self.replace_stop(trade["trade_id"], price, operation)
            else:
                result = await self.replace_targets(trade["trade_id"], signal.take_profits, operation)
            uncertain = result.get("phase") in ("prepared", "sending", "unknown") or result.get("replacement_unconfirmed")
            self.state.update(lambda state: state["events"][event_id].update(status="needs_reconciliation" if uncertain else "updated"))
            return {"trade_id": trade["trade_id"], "result": result}

    @staticmethod
    def validate_targets(trade, plan, prices):
        if not prices or len(prices) != len(plan.take_profits):
            raise AqlaError("Replace targets with the same target count")
        target_prices = [price_grid(price, plan.market, up=plan.side == "short") for price in prices]
        entry = Decimal(trade["entry_vwap"] or plan.entry_price)
        if target_prices != sorted(set(target_prices), reverse=plan.side == "short") or any(price <= entry if plan.side == "long" else price >= entry for price in target_prices):
            raise AqlaError("Targets must be strictly ordered on the profitable side of the actual entry")
        return target_prices

    async def replace_targets(self, trade_id, prices, operation_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        target_prices = self.validate_targets(trade, plan, prices)
        prior = self.state.snapshot()["operations"].get(operation_id)
        if prior:
            return prior
        staged = trade.get("target_updates", {}).get(operation_id)
        if staged is None:
            staged = {"prices": [str(price) for price in target_prices],
                      "original_orders": [order for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")],
                      "action": None}
            self.state.update(lambda state: state["trades"][trade_id].setdefault("target_updates", {}).__setitem__(operation_id, staged))
        elif staged["prices"] != [str(price) for price in target_prices]:
            raise AqlaError("Target update ID already identifies different prices")
        if staged["action"] is not None:
            return await self.exchange.mutate(operation_id, staged["action"]) if staged["action"]["orders"] else {"no_unfilled_targets": True}
        old_orders = staged["original_orders"]
        cancels = [{"asset": plan.market.asset_id, "cloid": order["cloid"]} for order in old_orders]
        if cancels:
            await self.exchange.mutate("cancel-targets:" + operation_id, {"type": "cancelByCloid", "cancels": cancels})
        await self.capture_fills(trade_id)
        trade = self.state.snapshot()["trades"][trade_id]
        # Cancellation confirmation and refreshed fill budgets are required before replacement.
        unfilled = []
        for order in old_orders:
            status = await self.exchange.order_status(order["cloid"])
            if status.get("order", {}).get("status") not in TERMINAL_ORDER:
                raise AqlaError("Old target cancellation is not confirmed")
            if status["order"]["status"] != "filled":
                unfilled.append(order)
        current = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        budgets = [Decimal(value) for value in trade.get("target_budgets", plan.tp_quantities)]
        consumed = [Decimal(value) for value in trade["tp_filled"]]
        outstanding = [max(Decimal(0), budget - spent) for budget, spent in zip(budgets, consumed, strict=True)]
        total_outstanding = sum(outstanding)
        if total_outstanding > current:
            outstanding = [quantity_grid(size * current / total_outstanding, plan.market) for size in outstanding]
            live_indices = [i for i, size in enumerate(outstanding) if size > 0]
            if live_indices:
                outstanding[live_indices[-1]] += current - sum(outstanding)
        quantity_left = current
        new = []
        for index in sorted({order["index"] for order in unfilled}):
            size = min(outstanding[index], quantity_left)
            if size <= 0:
                continue
            price = target_prices[index]
            new.append((index, self.order(plan, size, price, reduce=True, cloid=self.exchange.cloid(operation_id, index))))
            quantity_left -= size

        def record(data):
            row = data["trades"][trade_id]
            for old in row["orders"]:
                if old["kind"] == "tp":
                    old["retired"] = True
            row["orders"].extend({"kind": "tp", "cloid": order["c"], "quantity": order["s"], "price": order["p"], "index": index} for index, order in new)
            row["targets"] = [str(price) for price in target_prices]
            row["target_budgets"] = [str(spent + future) for spent, future in zip(consumed, outstanding, strict=True)]
            row["target_updates"][operation_id]["action"] = {"type": "order", "orders": [order for _, order in new], "grouping": "na"}

        self.state.update(record)
        result = await self.exchange.mutate(operation_id, {"type": "order", "orders": [order for _, order in new], "grouping": "na"}) if new else {"no_unfilled_targets": True}
        self._patch(trade_id, management_revision=trade["management_revision"] + 1)
        return result

    async def recover(self):
        snapshot = self.state.snapshot()
        reports = []
        for operation_id, row in snapshot["operations"].items():
            if row["phase"] in ("prepared", "sending", "unknown"):
                try:
                    reports.append(await self.exchange.reconcile(operation_id))
                except Exception:
                    reports.append({"operation_id": operation_id, "error": "Exact operation reconciliation is unavailable"})
        for trade_id, trade in self.state.snapshot()["trades"].items():
            try:
                self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
            except AqlaError as error:
                reports.append({"trade_id": trade_id, "error": str(error)})
                continue
            if trade["status"] in ("closed", "rejected") and trade["orders"] and not trade.get("cleanup_complete"):
                self.begin_cleanup(trade_id, trade["status"])
                trade = self.state.snapshot()["trades"][trade_id]
            if trade["status"] in OPEN:
                try:
                    if trade["status"] == "cleanup":
                        reports.append(await self.finish_cleanup(trade_id))
                    elif trade["entry_operation_id"] not in self.state.snapshot()["operations"]:
                        reports.append(await self.resume_unsent_entry(trade_id))
                    else:
                        for identity, intent in trade.get("close_intents", {}).items():
                            if identity not in self.state.snapshot()["operations"]:
                                reports.append(await self.dispatch_close_intent(trade_id, intent))
                        reports.append(await self.refresh_trade(trade_id))
                except Exception as error:
                    reports.append({"trade_id": trade_id, "error": str(error) if isinstance(error, AqlaError) else "Trade reconciliation is unavailable"})
        self.recovery_checked = not any(row.get("error") for row in reports)
        return reports
