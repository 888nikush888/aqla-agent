from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import Field

from aqla_hyperliquid_mcp.api import Hyperliquid, wire_number
from aqla_hyperliquid_mcp.models import (
    TERMINAL_ORDER,
    AqlaError,
    BackupValues,
    ExternalInterference,
    Interpretation,
    Model,
    Policy,
    SourceEvent,
    TradePlan,
    digest,
    margin_leverage,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import (
    backup_prices,
    latest_sources,
    make_plan,
    price_grid,
    quantity_grid,
    selected_targets,
    slippage_price,
    split_targets,
    verify_evidence,
)
from aqla_hyperliquid_mcp.state import State

from .accounting import foreign_entry_orders, occupied_markets
from .entry import execution_limit, gate_state, validate_entry_prices
from .pipelines import ConfigurationChanged, validate_ranking_binding

OPEN = {"waiting_entry", "preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted"}
UNFINISHED = {"pending", "interpreted", "needs_reconciliation"}
StopUpdatePhase = Literal["waiting_fill", "replacing", "unknown", "confirmed", "rejected", "superseded", "closed_without_application"]
STOP_UPDATE_ACTIVE = {"waiting_fill", "replacing", "unknown"}


class StopSourceBinding(Model):
    trade_id: str
    kind: Literal["move_stop", "break_even"]
    payload_hash: str


class PendingStopUpdate(Model):
    event_id: str
    operation_id: str
    stop: Decimal = Field(gt=0)
    reference_stop: Decimal = Field(gt=0)
    original_budget: Decimal = Field(gt=0)
    source_binding: StopSourceBinding
    source_revisions: dict[str, str]
    management_revision: int = Field(ge=0)
    phase: StopUpdatePhase
    accepted_at: int
    reason: str | None = None


class StopUpdateResult(TypedDict):
    pending_stop_update: bool
    phase: StopUpdatePhase
    stop: str
    reason: str | None


class StopEvidencePending(AqlaError):
    """Terminal entry quantity and immutable own fill facts are still inconsistent."""


class Trading:
    def __init__(self, exchange: Hyperliquid, state: State):
        self.exchange, self.state = exchange, state
        self.lock = asyncio.Lock()
        self.recovery_checked = False

    def ingest(self, source: SourceEvent, *, native_forward=False):
        snapshot = self.state.snapshot()
        if source.sent_at < snapshot.get("sources_live_after", 0):
            source = source.model_copy(update={"historical": True})
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
        for trade in state["trades"].values():
            pending = trade.get("pending_stop_update")
            if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
                protected_events.add(pending["event_id"])
                protected_events.update(pending["source_revisions"].values())
                protected_refs.update(pending["source_revisions"])
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
                      and row["status"] in ("report", "unrelated", "ambiguous", "incomplete", "context", "executed", "updated", "rejected", "superseded")]
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
                "source_names": snapshot.get("source_names", {}), "source_name": snapshot.get("source_names", {}).get(source["chat_id"]),
                "backup_profile": snapshot.get("profiles", {}).get("backup"),
                "policy_revision": snapshot["policy_revision"], "context_truncated": len(included) < len(messages)}

    def record_interpretation(self, event_id, interpretation: Interpretation):
        snapshot = self.state.snapshot()
        row = snapshot["events"].get(event_id)
        if row is None:
            raise AqlaError("Unknown source event")
        refs = [item for item in snapshot["events"].values() if item["source_id"] in interpretation.source_refs]
        if not refs or any(item["source"]["chat_id"] != row["source"]["chat_id"] for item in refs):
            raise AqlaError("A setup may only combine messages from the same source")
        if interpretation.kind in ("new_signal", "trade_update"):
            if row["source"].get("historical"):
                raise AqlaError("Historical context cannot authorize an exchange action")
            if row["source_id"] not in interpretation.source_refs:
                raise AqlaError("An actionable interpretation must reference its live anchor event")
            if latest_sources(snapshot["events"], [row["source_id"]])[row["source_id"]]["event_id"] != event_id:
                raise AqlaError("An actionable interpretation must use the latest anchor revision")
        verify_evidence(interpretation, snapshot["events"])
        previous = row.get("interpretation")
        if row["status"] in ("executed", "updated"):
            if previous is not None:
                previous = Interpretation.model_validate(previous).model_dump(mode="json")
            if previous != interpretation.model_dump(mode="json"):
                raise AqlaError("An executed revision is immutable; interpret the edit as a new update revision")
            return row
        self.state.update(lambda state: state["events"][event_id].update(
            interpretation=interpretation.model_dump(mode="json"),
            status=interpretation.kind if interpretation.kind not in ("new_signal", "trade_update") else "interpreted"))
        return self.state.snapshot()["events"][event_id]

    async def preview(self, event_id, interpretation: Interpretation):
        row = self.record_interpretation(event_id, interpretation)
        if interpretation.kind != "new_signal":
            return row
        if not interpretation.symbol or not interpretation.side:
            raise AqlaError("New trade needs an evidenced market and direction")
        snapshot = self.state.preview_snapshot() if hasattr(self.state, "preview_snapshot") else self.state.snapshot()
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
        if any(trade["plan"]["market"]["quote_token"] != market.quote_token for trade in active):
            raise AqlaError("One account risk budget cannot mix settlement currencies without conversion")
        if any(trade["plan"]["market"]["asset_id"] == market.asset_id for trade in active):
            raise AqlaError("An active trade already owns this net-position market")
        positions = await self.positions(market)
        if self.position_size(positions, market) != 0:
            raise AqlaError("Existing exchange exposure has no free logical owner; no additional entry")
        account = await self.exchange.account_snapshot(market)
        if account.positions is not None:
            if len(occupied_markets(snapshot, account)) >= policy.max_open_trades:
                raise AqlaError("Combined manual/Aqla market limit reached")
            if foreign_entry_orders(snapshot, account, market.symbol):
                raise AqlaError("Manual entry orders already reserve this market")
        refs = [item["source"]["sent_at"] for item in latest_sources(snapshot["events"], interpretation.source_refs).values()]
        risk = sum(Decimal(trade["reserved_risk"]) for trade in active)
        backup_profile = snapshot.get("profiles", {}).get("backup")
        backup = BackupValues.model_validate({key: backup_profile["parameters"][key] for key in BackupValues.model_fields}) if backup_profile else None
        plan = make_plan(interpretation, market, account, policy, snapshot["policy_revision"], min(refs),
                         (self.exchange.settings.network, self.exchange.account.lower()), reserved_risk=risk,
                         principal=self.exchange.settings.account.lower(), backup=backup,
                         backup_revision=backup_profile["revision"] if backup_profile else None)
        plan.source_revisions = self.source_revisions(snapshot, interpretation.source_refs)
        plan.ranking_policy_revision = snapshot["ranking_revision"]
        plan.ranking_enabled = snapshot["ranking_policy"]["enabled"]
        quote = await self.exchange.price_snapshot(market)
        if plan.entry_timing == "deferred":
            plan.entry_gate.update(origin_mid=str(quote.mid), direction="down" if quote.mid > plan.entry_price else "up" if quote.mid < plan.entry_price else "at_entry",
                                   origin_snapshot=quote.model_dump(mode="json"))
        else:
            validate_entry_prices(plan, quote)
        if hasattr(self.state, "stamp_plan"):
            self.state.stamp_plan(plan, snapshot)
        plan.plan_id = digest(plan.model_dump(mode="json", exclude={"plan_id"}))
        def save(state):
            self.assert_plan_binding(state, plan)
            try:
                self.validate_configuration(state, plan)
            except ConfigurationChanged as error:
                raise ConfigurationChanged("Configuration changed during preview; create a fresh preview: " + str(error)) from error
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

    def validate_configuration(self, snapshot, plan):
        try:
            validate_ranking_binding(snapshot, plan)
            if plan.backup_fields and snapshot["profiles"]["backup"]["revision"] != plan.backup_profile_revision:
                raise AqlaError("Backup profile changed; create a new unsent plan")
            if hasattr(self.state, "validate_route"):
                self.state.validate_route(plan)
            elif snapshot["paused"] or snapshot["policy_revision"] != plan.policy_revision:
                raise AqlaError("Pause or policy changed; create a new unsent plan")
        except AqlaError as error:
            raise ConfigurationChanged(str(error)) from error

    def validate_admission(self, snapshot, plan, account=None, *, owned=False, allowed_prepared=()):
        self.assert_plan_binding(snapshot, plan)
        if owned and snapshot["trades"][plan.trade_id]["status"] not in OPEN:
            raise AqlaError("Entry was terminated; no unsent exposure may execute")
        self.validate_configuration(snapshot, plan)
        if any(row.get("quantity_evidence_pending") and row["status"] in OPEN for row in snapshot["trades"].values()):
            raise AqlaError("Own entry quantity evidence requires reconciliation before new exposure")
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
        limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
        if len(active) >= limits.max_open_trades or any(trade["plan"]["market"]["asset_id"] == plan.market.asset_id for trade in active):
            raise AqlaError("Trade admission changed after preview")
        for ref in plan.source_refs:
            binding = snapshot["bindings"].get(ref)
            if binding and (not owned or binding["trade_id"] != plan.trade_id):
                raise AqlaError("Source has already been consumed")
        if any(row["phase"] in ("prepared", "sending", "unknown") and not (identity in allowed_prepared and row["phase"] == "prepared")
               for identity, row in snapshot["operations"].items()):
            raise AqlaError("Unresolved exchange operations must be reconciled before new exposure")
        if account is not None:
            reserved = sum(Decimal(trade["reserved_risk"]) for trade in active)
            if now_ms() - account.observed_at > 10000:
                raise AqlaError("Account observation is stale")
            if account.position_size is None:
                raise AqlaError("Current exchange position is unknown; no new exposure")
            if account.position_size != 0:
                raise AqlaError("Current exchange exposure has no free logical owner; no additional entry")
            if account.positions is not None:
                if len(occupied_markets(snapshot, account, exclude_trade=plan.trade_id if owned else None)) >= limits.max_open_trades:
                    raise AqlaError("Combined manual/Aqla market limit changed before entry")
                if foreign_entry_orders(snapshot, account, plan.market.symbol):
                    raise AqlaError("Manual entry orders now reserve this market")
            if (plan.risk_usdc + reserved > account.equity * limits.max_total_risk_percent / 100
                    or plan.risk_usdc > account.equity * plan.policy.risk_percent / 100
                    or plan.quantity * plan.entry_limit / margin_leverage(plan.effective_leverage, plan.market) > account.available_margin):
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
            if (current_market.asset_id, current_market.size_decimals, current_market.quote_token) != (plan.market.asset_id, plan.market.size_decimals, plan.market.quote_token):
                raise AqlaError("Exchange market metadata changed after preview")
            if plan.effective_leverage > current_market.max_leverage:
                raise AqlaError("Exchange leverage limit decreased after preview; create a new unsent plan")
            account = await self.exchange.account_snapshot(plan.market)
            quote = await self.exchange.price_snapshot(plan.market)
            if plan.entry_timing == "standard":
                validate_entry_prices(plan, quote)
            trade = {"trade_id": plan.trade_id, "chat_id": snapshot["events"][saved["event_id"]]["source"]["chat_id"],
                     "plan": plan.model_dump(mode="json"), "event_id": saved["event_id"], "status": "waiting_entry" if plan.entry_timing == "deferred" else "preparing",
                     "lifecycle_version": 1, "lifecycle_events": {}, "price_checks": [{"phase": "admission", "snapshot": quote.model_dump(mode="json")}],
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
            if plan.entry_timing == "deferred":
                self.state.update(lambda data: data["notifications"].setdefault("entry-waiting:" + plan.trade_id,
                                  {"trade_id": plan.trade_id, "change": {"status": "waiting_entry"}, "observed_at": now_ms()}))
                return await self.check_waiting_entry(plan.trade_id)
            return await self.guarded_entry_steps(plan, trade)

    async def check_waiting_entry(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        snapshot = self.state.snapshot()
        if now_ms() >= plan.deadline_at or self.source_revisions(snapshot, plan.source_refs) != plan.source_revisions:
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry expired or its source changed")
            return self.state.snapshot()["trades"][trade_id]
        configuration = self.state.root.snapshot() if hasattr(self.state, "root") else snapshot
        changed = (plan.profile_id is not None and configuration["profiles"].get(plan.profile_id, {}).get("revision") != plan.profile_revision
                   or plan.pipeline_id is not None and configuration["pipelines"].get(plan.pipeline_id, {}).get("revision") != plan.pipeline_revision
                   or plan.profile_id is None and snapshot["policy_revision"] != plan.policy_revision
                   or plan.ranking_policy_revision != configuration["ranking_revision"]
                   or plan.ranking_enabled != configuration["ranking_policy"]["enabled"]
                   or plan.backup_fields and configuration["profiles"]["backup"]["revision"] != plan.backup_profile_revision)
        if changed:
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry configuration changed")
            return self.state.snapshot()["trades"][trade_id]
        if snapshot["paused"]:
            return trade
        try:
            self.validate_admission(snapshot, plan, owned=True)
        except AqlaError as error:
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason=str(error))
            return self.state.snapshot()["trades"][trade_id]
        try:
            quote = await self.exchange.price_snapshot(plan.market)
            state = gate_state(plan, quote)
        except Exception as error:
            reason = str(error) if isinstance(error, AqlaError) else "Fresh target prices are unavailable"
            self._patch(trade_id, waiting_error=reason)
            return self.state.snapshot()["trades"][trade_id]
        self._patch(trade_id, waiting_error=None, waiting_snapshot=quote.model_dump(mode="json"))
        if state == "waiting":
            return self.state.snapshot()["trades"][trade_id]
        if state == "missed":
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Deferred entry was missed outside the source slippage tolerance")
            return self.state.snapshot()["trades"][trade_id]
        try:
            account = await self.exchange.account_snapshot(plan.market)
            self.validate_admission(self.state.snapshot(), plan, account, owned=True)
            quote = await self.exchange.price_snapshot(plan.market)
            validate_entry_prices(plan, quote, stop=trade["stop_price"], targets=trade["targets"])
            self._patch(trade_id, status="preparing", gate_activated_at=now_ms())
            return await self.guarded_entry_steps(plan, self.state.snapshot()["trades"][trade_id])
        except AqlaError as error:
            if self.state.snapshot()["trades"][trade_id]["entry_operation_id"] not in self.state.snapshot()["operations"]:
                self._patch(trade_id, status="rejected", reserved_risk="0", unsent_reason=str(error))
                return self.state.snapshot()["trades"][trade_id]
            raise

    async def live_entry_guard(self, plan):
        account = await self.exchange.account_snapshot(plan.market)
        snapshot = self.state.snapshot()
        allowed = ("leverage:" + plan.trade_id, snapshot["trades"][plan.trade_id]["entry_operation_id"])
        self.validate_admission(snapshot, plan, account, owned=True, allowed_prepared=allowed)
        quote = await self.exchange.price_snapshot(plan.market)
        trade = self.state.snapshot()["trades"][plan.trade_id]
        try:
            result = validate_entry_prices(plan, quote, stop=trade["stop_price"], targets=trade["targets"])
        except AqlaError as error:
            message = str(error)
            self.state.update(lambda data: data["trades"][plan.trade_id].setdefault("price_checks", []).append({"phase": "before_send", "snapshot": quote.model_dump(mode="json"), "error": message}))
            raise
        self.state.update(lambda data: data["trades"][plan.trade_id].setdefault("price_checks", []).append({"phase": "before_send", "snapshot": quote.model_dump(mode="json"), "result": result}))
        self._patch(plan.trade_id, entry_equity_snapshot={"equity": str(account.equity), "currency": plan.market.quote_token, "observed_at": account.observed_at})
        quote.fresh()
        return account, quote

    async def guarded_entry_steps(self, plan, trade):
        try:
            return await self._entry_steps(plan, trade)
        except AqlaError as error:
            operations = self.state.snapshot()["operations"]
            if trade["entry_operation_id"] in operations or any(row["phase"] in ("prepared", "sending", "unknown") for row in operations.values()):
                raise
            if self.state.snapshot()["trades"][plan.trade_id]["status"] not in OPEN:
                raise
            self._patch(plan.trade_id, status="rejected", reserved_risk="0", orders=[], unsent_reason=str(error))
            raise

    async def _entry_steps(self, plan, trade):
        current_market = await self.exchange.market(plan.market.symbol, plan.market.product)
        identity_fields = ("asset_id", "size_decimals", "quote_token", "price_tick", "quantity_step", "margin_leverage_scale")
        if any(getattr(current_market, key) != getattr(plan.market, key) for key in identity_fields) or plan.effective_leverage > current_market.max_leverage:
            raise AqlaError("Market identity, precision or leverage changed while entry was unsent")
        if (plan.quantity < current_market.min_quantity or plan.quantity * plan.entry_limit < current_market.min_notional
                or current_market.max_quantity is not None and plan.quantity > current_market.max_quantity
                or any(size < current_market.min_quantity or size * price < current_market.min_notional for size, price in zip(plan.tp_quantities, plan.take_profits, strict=True))):
            raise AqlaError("Exchange minimum/maximum order sizes changed while entry was unsent")
        latest_account, latest_quote = None, None
        async def live_guard():
            nonlocal latest_account, latest_quote
            latest_account, latest_quote = await self.live_entry_guard(plan)
        def final_guard(data, operation_id):
            latest_quote.fresh()
            self.validate_admission(data, plan, latest_account, owned=True, allowed_prepared=(operation_id,))
        await live_guard()
        guards = {"async_prepare_guard": live_guard}
        if plan.market.product == "perp":
            leverage = await self.exchange.mutate("leverage:" + plan.trade_id,
                                                 {"type": "updateLeverage", "asset": plan.market.asset_id,
                                                  "isCross": plan.policy.margin_mode == "cross", "leverage": plan.effective_leverage}, deadline=plan.deadline_at,
                                                  prepare_guard=lambda data: final_guard(data, "leverage:" + plan.trade_id), **guards)
            if leverage["phase"] not in ("acknowledged", "reconciled"):
                self._patch(plan.trade_id, status="unknown" if leverage["phase"] == "unknown" else "rejected",
                            **({"reserved_risk": "0", "unsent_reason": leverage.get("result", {}).get("error", "Leverage configuration was rejected")} if leverage["phase"] == "rejected" else {}))
                return self.state.snapshot()["trades"][plan.trade_id]
        account = await self.exchange.account_snapshot(plan.market)
        self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        current = self.state.snapshot()["trades"][plan.trade_id]
        stop = Decimal(current["stop_price"])
        entry = self.order(plan, plan.quantity, execution_limit(plan), cloid=trade["entry_cloid"])
        if plan.entry_timing == "deferred":
            entry["t"] = {"limit": {"tif": "Ioc"}}
        initial_stop = self.order(plan, plan.quantity, stop * (Decimal("0.95") if plan.side == "long" else Decimal("1.05")),
                                  reduce=True, trigger=("sl", stop), cloid=self.exchange.cloid(trade["entry_operation_id"], 1))
        initial_stop["p"] = wire_number(price_grid(Decimal(initial_stop["p"]), plan.market, up=plan.side == "short"))
        action = {"type": "order", "orders": [entry, initial_stop], "grouping": "normalTpsl"}
        self._patch(plan.trade_id, status="opening", orders=[{"kind": "sl", "cloid": initial_stop["c"], "quantity": str(plan.quantity), "price": str(stop)}])
        result = await self.exchange.mutate(trade["entry_operation_id"], action, deadline=plan.deadline_at,
                                            prepare_guard=lambda data: final_guard(data, trade["entry_operation_id"]), **guards)
        if result["phase"] == "unknown":
            self._patch(plan.trade_id, status="unknown")
        if result.get("outcome") == "not_sent":
            self._patch(plan.trade_id, status="rejected", reserved_risk="0", unsent_reason=result["result"]["error"], orders=[])
            return self.state.snapshot()["trades"][plan.trade_id]
        try:
            await self.refresh_trade(plan.trade_id)
        except ExternalInterference as error:
            self._patch(plan.trade_id, status="conflicted", external_conflict=str(error))
        current = self.state.snapshot()["trades"][plan.trade_id]
        if current["status"] == "opening" and Decimal(current["filled_quantity"]) == 0 and result["phase"] in ("acknowledged", "reconciled"):
            self.state.update(lambda data: data["notifications"].setdefault("entry-waiting:" + plan.trade_id,
                {"trade_id": plan.trade_id, "change": {"status": "opening"}, "previous": {"status": "preparing"}, "observed_at": now_ms(),
                 "source_id": current["chat_id"], "source_name": data.get("source_names", {}).get(current["chat_id"])}))
        return current

    async def resume_unsent_entry(self, trade_id):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        if trade["status"] == "waiting_entry":
            return await self.check_waiting_entry(trade_id)
        plan = TradePlan.model_validate(trade["plan"])
        if trade["entry_operation_id"] in snapshot["operations"]:
            return await self.refresh_trade(trade_id)
        self.assert_plan_binding(snapshot, plan)
        try:
            self.validate_admission(snapshot, plan, owned=True)
            market = await self.exchange.market(plan.market.symbol, plan.market.product)
            if (market.asset_id, market.size_decimals, market.quote_token) != (plan.market.asset_id, plan.market.size_decimals, plan.market.quote_token):
                raise AqlaError("Exchange market metadata changed after preview")
            if plan.effective_leverage > market.max_leverage:
                raise AqlaError("Exchange leverage limit decreased after preview; create a new unsent plan")
            if self.position_size(await self.positions(plan.market), plan.market) != 0:
                raise AqlaError("Unsent entry cannot resume against an existing foreign position")
            account = await self.exchange.account_snapshot(plan.market)
            self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        except AqlaError as error:
            if any(row["phase"] in ("prepared", "sending", "unknown") for row in self.state.snapshot()["operations"].values()):
                raise
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason=str(error))
            return self.state.snapshot()["trades"][trade_id]
        return await self.guarded_entry_steps(plan, trade)

    def _patch(self, trade_id, *, preserve_pending_stop=False, **values):
        old = self.state.snapshot()["trades"][trade_id]
        changed = {key: value for key, value in values.items() if old.get(key) != value}
        if not changed:
            return

        def patch(state):
            row = state["trades"][trade_id]
            pending = row.get("pending_stop_update")
            original_pending = old.get("pending_stop_update")
            if (preserve_pending_stop and "management_revision" in changed and pending and original_pending
                    and pending["phase"] in STOP_UPDATE_ACTIVE and pending["management_revision"] == row["management_revision"]
                    and all(pending[key] == original_pending[key] for key in ("stop", "source_binding", "operation_id"))
                    and state["bindings"].get(pending["operation_id"]) == pending["source_binding"]):
                pending["management_revision"] = changed["management_revision"]
            row.update(changed)
            important = {key: value for key, value in changed.items() if key in ("status", "filled_quantity", "remaining_quantity", "stop_price", "tp_filled")}
            if row.get("lifecycle_version") and row["status"] == "closed":
                from .lifecycle import record_lifecycle
                record_lifecycle(state, trade_id, "closed")
                return
            if row.get("lifecycle_version") and old["status"] in ("preparing", "opening") and row["status"] == "protecting":
                important = {}
            if row.get("lifecycle_version") and row["status"] == "open" and old["status"] == "protecting":
                important = {key: value for key, value in important.items() if key != "status"}
            if important and row["status"] not in ("preparing", "opening"):
                identity = digest([trade_id, important])[:32]
                chat = state["trades"][trade_id]["chat_id"]
                state["notifications"].setdefault(identity, {"trade_id": trade_id, "change": important, "observed_at": now_ms(),
                    "previous": {key: old.get(key) for key in important},
                    "source_id": chat, "source_name": state.get("source_names", {}).get(chat)})

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
        completed_prices = dict(trade.get("completed_target_prices", {}))
        if plan.policy.stop_management == "tp_ladder":
            # Freeze the effective target price before later source updates can change it.
            for index, budget in enumerate(trade.get("target_budgets", [])):
                if budget is not None and Decimal(budget) > 0 and target_fills[index] >= Decimal(budget):
                    completed_prices.setdefault(str(index), trade.get("targets", plan.take_profits)[index])
        self._patch(trade_id, fill_events=owned, filled_quantity=str(total), tp_filled=[str(size) for size in target_fills],
                    entry_vwap=str(cost / total) if total else trade["entry_vwap"],
                    **({"completed_target_prices": completed_prices} if plan.policy.stop_management == "tp_ladder" else {}))
        self.observe_orders(trade_id, observed)
        confirmed_stop = any(order["kind"] == "sl" and observed.get(order["cloid"], {}).get("order", {}).get("status") == "open"
                             and self._stop_matches(observed[order["cloid"]], plan, total, Decimal(trade["stop_price"]))
                             for order in trade["orders"]) if total else False
        if total:
            self._patch(trade_id, entry_stop_confirmed=confirmed_stop)
        from .lifecycle import record_lifecycle
        if total and self.state.snapshot()["trades"][trade_id].get("lifecycle_version") and "entry_fill" not in self.state.snapshot()["trades"][trade_id].get("lifecycle_events", {}):
            self.state.update(lambda data: record_lifecycle(data, trade_id, "entry_fill"))
        from .monthly import accounting_facts, entry_quantity_evidence
        confirmed_quantity = entry_quantity_evidence(entry_status)
        if confirmed_quantity is not None:
            self._patch(trade_id, entry_fill_quantity_confirmed=confirmed_quantity)
        self._patch(trade_id, **accounting_facts(self.state.snapshot()["trades"][trade_id]))
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
            if getattr(self.exchange.settings, "shared_account", False):
                raise ExternalInterference("Manual intervention changed this market; Aqla does not manage the mixed position")
            raise AqlaError("Exchange exposure differs from remaining owned fills; no automatic management")
        if observed < owned:
            if getattr(self.exchange.settings, "shared_account", False):
                raise ExternalInterference("Observed position is smaller than proved own fills; manual intervention requires review")
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
        current = self.state.snapshot()["trades"][trade_id]
        if (current.get("quantity_evidence_pending") and current.get("entry_fill_quantity_confirmed") is not None
                and Decimal(current["entry_fill_quantity_confirmed"]) == total):
            self._patch(trade_id, quantity_evidence_pending=False, waiting_error=None)
        if total and remaining:
            pending = trade.get("pending_stop_update")
            closing_pending = trade["status"] == "closing" and pending and pending["phase"] in STOP_UPDATE_ACTIVE
            self._patch(trade_id, filled_quantity=str(total), remaining_quantity=str(remaining), entry_vwap=str(cost / total),
                        status="closing" if closing_pending else "open" if trade["status"] == "open" else "protecting")
            if closing_pending:
                await self.resume_pending_stop(trade_id)
                return self.state.snapshot()["trades"][trade_id]
            try:
                if not await self.finalize_backup(trade_id):
                    return self.state.snapshot()["trades"][trade_id]
                if not await self.finalize_source_targets(trade_id):
                    return self.state.snapshot()["trades"][trade_id]
                await self.validate_actual_entry(trade_id)
                await self.ensure_protection(trade_id)
                current = self.state.snapshot()["trades"][trade_id]
                covered = not getattr(self.exchange, "fail_closed_protection", False)
                if not covered:
                    for order in current["orders"]:
                        if order["kind"] == "sl":
                            observed = await self.exchange.order_status(order["cloid"])
                            covered |= observed.get("order", {}).get("status") == "open" and self._stop_matches(observed, plan, remaining, Decimal(current["stop_price"]))
                if not covered and current["status"] not in ("closing", "cleanup", "closed"):
                    raise AqlaError("No confirmed effective stop loss")
            except ExternalInterference:
                raise
            except StopEvidencePending as error:
                self._patch(trade_id, status="unknown", waiting_error=str(error))
                if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
                    self.stop_phase(trade_id, "waiting_fill", str(error))
                return self.state.snapshot()["trades"][trade_id]
            except Exception as error:
                if not getattr(self.exchange, "fail_closed_protection", False) and not set(plan.backup_fields) & {"stop_loss", "take_profits"}:
                    raise
                reason = "Protection could not be confirmed; closing own exposure: " + (str(error) if isinstance(error, AqlaError) else type(error).__name__)
                self._patch(trade_id, execution_incident=reason)
                if hasattr(self.state, "pause_entries"):
                    self.state.pause_entries(reason)
                else:
                    self.state.update(lambda data: data.update(paused=True))
                return await self.close_position(trade_id, Decimal(100), "protection-failure:" + trade_id)
        elif stage == "filled" and not total:
            self._patch(trade_id, status="unknown", fill_evidence_pending=True)
        elif stage in TERMINAL_ORDER and not remaining:
            if not total:
                self._patch(trade_id, unsent_reason="Entry order terminated without any confirmed fill")
            self.begin_cleanup(trade_id, "closed" if total else "rejected")
            await self.finish_cleanup(trade_id)
        elif remaining and not total:
            self._patch(trade_id, status="unknown")
            raise AqlaError("Position exists but its entry fills are not proved; new entries remain blocked")
        elif now_ms() >= plan.deadline_at and stage == "open":
            await self.cancel_entry(trade_id)
            return await self.refresh_trade(trade_id)
        await self.resume_pending_stop(trade_id)
        return self.state.snapshot()["trades"][trade_id]

    async def validate_actual_entry(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        if not trade.get("lifecycle_version"):
            return
        entries = {key: row for key, row in trade["fill_events"].items() if row["kind"] == "entry"}
        fingerprint = digest(entries)
        if trade.get("entry_validated_fingerprint") == fingerprint:
            return
        plan = TradePlan.model_validate(trade["plan"])
        entry, size, stop = Decimal(trade["entry_vwap"]), Decimal(trade["filled_quantity"]), Decimal(trade["stop_price"])
        targets = [Decimal(price) for price in trade["targets"]]
        account = await self.exchange.account_snapshot(plan.market)
        quote = await self.exchange.price_snapshot(plan.market)
        safe = (stop < min(entry, quote.stop_reference) and all(price > entry for price in targets) if plan.side == "long"
                else stop > max(entry, quote.stop_reference) and all(price < entry for price in targets))
        notional = entry * size
        risk = size * (abs(entry - stop) + entry * plan.policy.cost_reserve_bps / 10000)
        active = [row for identity, row in self.state.snapshot()["trades"].items() if identity != trade_id and row["status"] in OPEN]
        other_risk = sum((Decimal(row["reserved_risk"]) for row in active), Decimal(0))
        limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
        within_entry_band = (plan.entry_timing != "deferred" or
                             plan.entry_price * (1 - plan.policy.max_slippage_percent / 100) <= entry <= plan.entry_price * (1 + plan.policy.max_slippage_percent / 100))
        target_sizes = split_targets(size, len(targets), plan.policy, plan.market)
        targets_feasible = all(amount >= plan.market.min_quantity and amount * price >= plan.market.min_notional for amount, price in zip(target_sizes, targets, strict=True))
        permitted = (safe and within_entry_band and targets_feasible and notional <= plan.policy.max_notional_usdc and risk <= plan.risk_usdc
                     and risk <= account.equity * plan.policy.risk_percent / 100
                     and risk + other_risk <= account.equity * limits.max_total_risk_percent / 100)
        check = {"phase": "after_fill", "snapshot": quote.model_dump(mode="json"), "entry_vwap": str(entry),
                 "notional": str(notional), "risk": str(risk), "permitted": bool(permitted)}
        self.state.update(lambda data: data["trades"][trade_id].setdefault("price_checks", []).append(check))
        if not permitted:
            raise AqlaError("Confirmed entry violates price structure, position value or SL risk limits")
        self._patch(trade_id, entry_validated_fingerprint=fingerprint)
        current = self.state.snapshot()["trades"][trade_id]
        if current.get("entry_accounting") and not current.get("initial_sl_risk"):
            self._patch(trade_id, initial_sl_risk={"amount": str(size * abs(entry - stop)), "entry": str(entry), "stop": str(stop),
                         "quantity": str(size), "currency": plan.market.quote_token, "entry_fingerprint": current["entry_accounting"]["entry_fingerprint"]})

    async def ensure_protection(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        remaining = Decimal(trade["remaining_quantity"])
        if remaining <= 0:
            return
        if not await self.finalize_backup(trade_id):
            return
        trade = self.state.snapshot()["trades"][trade_id]
        status_rows = [(order, await self.exchange.order_status(order["cloid"])) for order in trade["orders"] if order["kind"] == "sl"]
        covered = any(row.get("order", {}).get("status") == "open" and self._stop_matches(row, plan, remaining, Decimal(trade["stop_price"])) for _, row in status_rows)
        if not covered:
            await self.cancel_entry(trade_id)
            await self.replace_stop(trade_id, Decimal(trade["stop_price"]), initial_protection=True)
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
                      for i, (size, price) in enumerate(zip(sizes, trade.get("targets", plan.take_profits), strict=True))]
            def stage_initial(state):
                row = state["trades"][trade_id]
                row["orders"].extend({"kind": "tp", "cloid": order["c"], "quantity": order["s"], "price": order["p"], "index": i} for i, order in enumerate(orders))
                row["target_budgets"] = [str(size) for size in sizes]
                row["initial_targets"] = {"operation_id": operation, "action": {"type": "order", "orders": orders, "grouping": "na"}}
            self.state.update(stage_initial)
        staged = self.state.snapshot()["trades"][trade_id].get("initial_targets")
        if staged and staged["operation_id"] not in self.state.snapshot()["operations"]:
            result = await self.exchange.mutate(staged["operation_id"], staged["action"])
            if result["phase"] == "rejected" and "take_profits" in plan.backup_fields:
                raise AqlaError("Backup TP order was rejected")
            if result["phase"] not in ("acknowledged", "reconciled"):
                self._patch(trade_id, status="protecting")
                return
        active_targets = [order for order in self.state.snapshot()["trades"][trade_id]["orders"] if order["kind"] == "tp" and not order.get("retired")]
        if active_targets:
            observations = [await self.exchange.order_status(order["cloid"]) for order in active_targets]
            if "take_profits" in plan.backup_fields and any(row.get("order", {}).get("status") == "rejected" for row in observations):
                raise AqlaError("Backup TP could not be installed")
            if any(row.get("order", {}).get("status") not in {"open", "filled", "triggered"} for row in observations):
                self._patch(trade_id, status="protecting")
                return
        self._patch(trade_id, status="open")
        pending = self.state.snapshot()["trades"][trade_id].get("pending_stop_update")
        if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
            return
        if plan.policy.stop_management == "tp_ladder":
            await self.advance_stop_ladder(trade_id)
            return
        stage = plan.policy.break_even_after_tp
        if stage is not None:
            current = self.state.snapshot()["trades"][trade_id]
            budgets = [Decimal(value) if value is not None else Decimal(0) for value in current.get("target_budgets", [])]
            filled = [Decimal(value) for value in current["tp_filled"]]
            if stage <= len(budgets) and all(budgets[i] > 0 and filled[i] >= budgets[i] for i in range(stage)):
                entry = Decimal(trade["entry_vwap"])
                tighter = entry > Decimal(trade["stop_price"]) if plan.side == "long" else entry < Decimal(trade["stop_price"])
                if tighter:
                    await self.replace_stop(trade_id, entry, "break-even:" + trade_id + ":" + str(stage))

    async def finalize_source_targets(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        specification = trade.get("source_tp_override", plan.source_tp)
        if not specification or specification.get("unit") == "price":
            return True
        final = trade.get("percent_finalization")
        if final:
            if final["filled_quantity"] != trade["filled_quantity"] or final["entry_vwap"] != trade["entry_vwap"]:
                raise AqlaError("Final source percentage entry fills changed; do not recalculate frozen prices")
            return True
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        trade = self.state.snapshot()["trades"][trade_id]
        if not trade.get("entry_accounting"):
            raise AqlaError("Source percentage targets require confirmed final entry fills")
        from .percent_targets import prices
        reference = specification["reference_entry"] or trade["entry_vwap"]
        targets = prices(reference, plan.side, plan.market, specification["unit"], specification["values"], specification["signal_leverage"])
        entry = Decimal(trade["entry_vwap"])
        if any(value <= entry if plan.side == "long" else value >= entry for value in targets):
            raise AqlaError("Source percentage targets are no longer profitable relative to the executed entry")
        sizes = split_targets(Decimal(trade["filled_quantity"]), len(targets), plan.policy, plan.market)
        if any(size < plan.market.min_quantity or size * price < plan.market.min_notional for size, price in zip(sizes, targets, strict=True)):
            raise AqlaError("Final source percentage TP is below exchange minimum size/notional")
        final = {**specification, "reference_entry": str(reference), "entry_vwap": trade["entry_vwap"],
                 "filled_quantity": trade["filled_quantity"], "prices": [str(value) for value in targets], "phase": "applied"}
        self._patch(trade_id, percent_finalization=final, targets=final["prices"])
        return True

    async def finalize_backup(self, trade_id):
        """One durable fill reference, before TP intent construction or SL replacement."""
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        if not set(plan.backup_fields) & {"stop_loss", "take_profits"}:
            return True
        final = trade.get("backup_finalization")
        if final and (Decimal(trade["filled_quantity"]) != Decimal(final["filled_quantity"])
                      or Decimal(trade["entry_vwap"]) != Decimal(final["reference_entry"])):
            raise AqlaError("Finalized Backup entry fills changed; closing instead of recalculating frozen prices")
        if final and final["phase"] == "applied":
            return True
        if final is None:
            await self.cancel_entry(trade_id)
            await self.capture_fills(trade_id)
            trade = self.state.snapshot()["trades"][trade_id]
            reference = Decimal(trade["entry_vwap"])
            remaining = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
            if not remaining:
                return False
            stop, targets = backup_prices(reference, plan.side, plan.market,
                plan.backup_values.fallback_stop_loss_percent if "stop_loss" in plan.backup_fields else None,
                selected_targets(plan.backup_values.fallback_take_profit_percentages, plan.policy) if "take_profits" in plan.backup_fields else ())
            stop = stop if stop is not None else Decimal(trade["stop_price"])
            targets = targets if "take_profits" in plan.backup_fields else [Decimal(value) for value in trade["targets"]]
            if not (stop < reference < min(targets) if plan.side == "long" else stop > reference > max(targets)):
                raise AqlaError("Final Backup prices have invalid direction")
            sizes = split_targets(Decimal(trade["filled_quantity"]), len(targets), plan.policy, plan.market)
            if any(size < plan.market.min_quantity or size * price < plan.market.min_notional for size, price in zip(sizes, targets, strict=True)):
                raise AqlaError("Final Backup TP is below the exchange minimum")
            account = await self.exchange.account_snapshot(plan.market)
            snapshot = self.state.snapshot()
            limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
            others = sum(Decimal(row["reserved_risk"]) for identity, row in snapshot["trades"].items() if identity != trade_id and row["status"] in OPEN)
            risk = remaining * (abs(reference - stop) + reference * plan.policy.cost_reserve_bps / 10000)
            if (risk > plan.risk_usdc or risk > account.equity * plan.policy.risk_percent / 100
                    or risk + others > account.equity * limits.max_total_risk_percent / 100
                    or remaining * reference > plan.policy.max_notional_usdc or now_ms() - account.observed_at > 10000
                    or account.position_size != (remaining if plan.side == "long" else -remaining)):
                raise AqlaError("Final Backup protection exceeds the confirmed risk/notional budget")
            final = {"phase": "prepared", "reference_entry": str(reference), "filled_quantity": trade["filled_quantity"], "stop_loss": str(stop),
                     "take_profits": [str(value) for value in targets], "risk": str(risk), "stop_operation_id": "backup-stop:" + trade_id,
                     "percentages": plan.backup_values.model_dump(mode="json")}
            self._patch(trade_id, backup_finalization=final)
        if "stop_loss" in plan.backup_fields:
            result = await self.replace_stop(trade_id, Decimal(final["stop_loss"]), final["stop_operation_id"], initialize_backup=True)
            if result.get("replacement_unconfirmed") or result.get("already_closed") or self.state.snapshot()["trades"][trade_id]["status"] in ("closing", "cleanup", "closed"):
                return False
        self._patch(trade_id, targets=final["take_profits"], reserved_risk=final["risk"], backup_finalization={**final, "phase": "applied"})
        return True

    async def advance_stop_ladder(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        budgets = [Decimal(value) if value is not None else Decimal(0) for value in trade.get("target_budgets", [])]
        fills = [Decimal(value) for value in trade["tp_filled"]]
        reached = [stage for stage in (1, 3, 4) if stage <= len(budgets) and all(
            budgets[index] > 0 and fills[index] >= budgets[index] for index in range(stage))]
        if not reached:
            return
        confirmed = trade.get("stop_ladder_stage", 0)
        intents = trade.get("stop_ladder_intents", {})
        pending = sorted(int(stage) for stage in intents if int(stage) > confirmed)
        stage = pending[0] if pending else max(reached)
        if stage <= confirmed:
            return
        if str(stage) in intents:
            price = Decimal(intents[str(stage)])
        elif stage == 1:
            price = Decimal(trade["entry_vwap"])
        else:
            index = 0 if stage == 3 else 1
            reference = trade.get("completed_target_prices", {}).get(str(index))
            if reference is None:
                raise AqlaError("Completed TP reference is not proved; preserve existing stop")
            price = Decimal(reference)
        if str(stage) not in intents:
            self.state.update(lambda data: data["trades"][trade_id].setdefault("stop_ladder_intents", {}).__setitem__(str(stage), str(price)))
        tighter = price > Decimal(trade["stop_price"]) if plan.side == "long" else price < Decimal(trade["stop_price"])
        if tighter:
            await self.replace_stop(trade_id, price, "tp-ladder:" + trade_id + ":" + str(stage))
        current = self.state.snapshot()["trades"][trade_id]
        effective = Decimal(current["stop_price"]) >= price if plan.side == "long" else Decimal(current["stop_price"]) <= price
        if current["status"] == "open" and effective:
            self._patch(trade_id, stop_ladder_stage=stage)

    @staticmethod
    def _stop_matches(status, plan, size, stop):
        order = status.get("order", {}).get("order", {})
        return (order.get("coin") == plan.market.symbol and order.get("isTrigger") is True
                and order.get("reduceOnly") is (plan.market.product == "perp")
                and Decimal(order.get("sz", "0")) >= size and Decimal(order.get("triggerPx", "0")) == stop
                and order.get("side") == ("A" if plan.side == "long" else "B"))

    async def cancel_entry(self, trade_id):
        trade = self.state.snapshot()["trades"][trade_id]
        if trade["status"] == "waiting_entry":
            self._patch(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry cancelled by request")
            return {"cancelled_waiting_entry": True}
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        if trade["entry_operation_id"] not in self.state.snapshot()["operations"]:
            def cancel_unsent(state):
                row = state["trades"][trade_id]
                self.assert_plan_binding(state, plan)
                if row["entry_operation_id"] in state["operations"]:
                    raise AqlaError("Entry preparation began; its outcome requires reconciliation")
                if (Decimal(row["filled_quantity"]) or Decimal(row["remaining_quantity"])
                        or any(fill["kind"] == "entry" for fill in row.get("fill_events", {}).values())):
                    raise AqlaError("Entry fills exist without its operation; absence is not proved")
                if row["status"] in OPEN:
                    row.update(status="cancelled", reserved_risk="0", unsent_reason="Entry cancelled before preparation", cleanup_complete=True)
            self.state.update(cancel_unsent)
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

    @staticmethod
    def stop_update_result(pending) -> StopUpdateResult:
        request = PendingStopUpdate.model_validate(pending)
        return {"pending_stop_update": request.phase in STOP_UPDATE_ACTIVE, "phase": request.phase,
                "stop": str(request.stop), "reason": request.reason}

    @staticmethod
    def write_stop_phase(data, trade_id, pending, phase: StopUpdatePhase, reason=None):
        request = PendingStopUpdate.model_validate(pending)
        request.phase, request.reason = phase, reason
        saved = request.model_dump(mode="json", exclude_none=True)
        trade = data["trades"][trade_id]
        current = trade.get("pending_stop_update")
        if current is None or current["operation_id"] == request.operation_id:
            trade["pending_stop_update"] = saved
        if phase not in STOP_UPDATE_ACTIVE:
            trade.setdefault("stop_update_history", {})[request.operation_id] = saved
        status = "needs_reconciliation" if phase in STOP_UPDATE_ACTIVE else "updated" if phase == "confirmed" else "superseded" if phase == "superseded" else "rejected"
        data["events"][request.event_id].update(status=status, stop_update_reason=reason)
        identity = "stop-update:" + digest([request.operation_id, phase, reason])[:32]
        data["notifications"].setdefault(identity, {"trade_id": trade_id, "event_id": request.event_id,
            "stop_update": saved, "observed_at": now_ms()})
        return saved

    def stop_phase(self, trade_id, phase: StopUpdatePhase, reason=None):
        pending = self.state.snapshot()["trades"][trade_id]["pending_stop_update"]
        if pending["phase"] == phase and pending.get("reason") == reason:
            return pending
        return self.state.update(lambda data: self.write_stop_phase(data, trade_id,
            data["trades"][trade_id]["pending_stop_update"], phase, reason))

    @staticmethod
    def validate_stop_budget(trade, plan, stop, size=None, *, initialize_backup=False):
        old_stop = Decimal(trade["stop_price"])
        widening = stop < old_stop if plan.side == "long" else stop > old_stop
        if widening and not plan.policy.allow_stop_widening and not initialize_backup:
            raise AqlaError("Stop widening is not permitted")
        size = Decimal(trade["remaining_quantity"]) if size is None else size
        reference = Decimal(trade["entry_vwap"]) if trade["entry_vwap"] is not None else plan.entry_price
        if not Decimal(trade["filled_quantity"]):
            size = plan.quantity
        budget = min(plan.risk_usdc, Decimal(trade["reserved_risk"]))
        pending = trade.get("pending_stop_update")
        if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
            budget = min(budget, Decimal(pending["original_budget"]))
        if widening and size * abs(reference - stop) > budget:
            raise AqlaError("Stop widening exceeds the original loss budget")

    @staticmethod
    def validate_final_stop_quantity(trade):
        confirmed = trade.get("entry_fill_quantity_confirmed")
        if confirmed is not None and Decimal(confirmed) != Decimal(trade["filled_quantity"]):
            raise StopEvidencePending("Terminal entry quantity and own fills are not yet reconciled; preserve original protection")

    async def queue_stop_update(self, trade_id, event_id, signal, proposed):
        origin = self.state.snapshot()
        source_revisions = self.source_revisions(origin, signal.source_refs)
        trade = origin["trades"][trade_id]
        old = trade.get("pending_stop_update")
        if old and old["phase"] in STOP_UPDATE_ACTIVE:
            operation = self.state.snapshot()["operations"].get(old["operation_id"])
            if old["phase"] == "unknown" or operation and operation.get("outcome") != "not_sent":
                await self.resume_pending_stop(trade_id)
                old = self.state.snapshot()["trades"][trade_id]["pending_stop_update"]
                if old["phase"] in STOP_UPDATE_ACTIVE:
                    raise AqlaError("Reconcile the original pending stop operation before another stop instruction")
        trade = self.state.snapshot()["trades"][trade_id]
        management_revision = trade["management_revision"]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        stop = price_grid(proposed, plan.market, up=plan.side == "long")
        await self.capture_fills(trade_id)
        size = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        if trade["management_revision"] != management_revision or self.source_revisions(snapshot, signal.source_refs) != source_revisions:
            raise AqlaError("Source or management revision changed before stop acceptance")
        if signal.update_action == "break_even":
            if trade["entry_vwap"] is None:
                raise AqlaError("Break-even requires a proved entry VWAP")
            stop = price_grid(Decimal(trade["entry_vwap"]), plan.market, up=plan.side == "long")
        self.validate_stop_budget(trade, plan, stop, size)
        binding = StopSourceBinding(trade_id=trade_id, kind=signal.update_action,
                                    payload_hash=digest(signal.model_dump(mode="json")))
        request = PendingStopUpdate(event_id=event_id, operation_id="update:" + event_id, stop=stop,
            reference_stop=trade["stop_price"], original_budget=min(plan.risk_usdc, Decimal(trade["reserved_risk"])),
            source_binding=binding, source_revisions=source_revisions,
            management_revision=trade["management_revision"] + 1,
            phase="replacing" if size else "waiting_fill", accepted_at=now_ms())
        def accept(data):
            row = data["trades"][trade_id]
            self.assert_plan_binding(data, plan)
            if row["management_revision"] != trade["management_revision"] or self.source_revisions(data, signal.source_refs) != request.source_revisions:
                raise AqlaError("Source or management revision changed before stop acceptance")
            current = row.get("pending_stop_update")
            if current and current["phase"] in STOP_UPDATE_ACTIVE:
                operation = data["operations"].get(current["operation_id"])
                if current["phase"] == "unknown" or operation and operation.get("outcome") != "not_sent":
                    raise AqlaError("The original stop operation may have been sent; reconcile it first")
                self.write_stop_phase(data, trade_id, current, "superseded", "A later authorized stop instruction replaced this unsent request")
            row["management_revision"] = request.management_revision
            row["pending_stop_update"] = request.model_dump(mode="json", exclude_none=True)
            data["bindings"][request.operation_id] = binding.model_dump(mode="json")
            self.write_stop_phase(data, trade_id, row["pending_stop_update"], request.phase)
        self.state.update(accept)
        if size:
            await self.refresh_trade(trade_id)
        else:
            await self.resume_pending_stop(trade_id)
        return self.stop_update_result(self.state.snapshot()["trades"][trade_id]["pending_stop_update"])

    async def management_stop_preflight(self, trade_id, operation_id) -> PendingStopUpdate | None:
        pending = self.state.snapshot()["trades"][trade_id].get("pending_stop_update")
        if not pending or pending["phase"] not in STOP_UPDATE_ACTIVE or pending["operation_id"] == operation_id:
            return None
        request = PendingStopUpdate.model_validate(pending)
        operation = self.state.snapshot()["operations"].get(request.operation_id)
        if request.phase == "unknown" or operation and operation.get("outcome") != "not_sent":
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                await self.exchange.reconcile(request.operation_id)
            await self.resume_pending_stop(trade_id)
            pending = self.state.snapshot()["trades"][trade_id]["pending_stop_update"]
            if pending["phase"] in STOP_UPDATE_ACTIVE:
                raise AqlaError("Reconcile the original pending stop before an independent management instruction")
            return None
        return request

    def supersede_stop_for_management(self, data, trade_id, request, reason):
        row = data["trades"][trade_id]
        current = row.get("pending_stop_update")
        if current != request.model_dump(mode="json", exclude_none=True):
            raise AqlaError("Pending stop changed before independent management acceptance")
        operation = data["operations"].get(request.operation_id)
        if operation and operation.get("outcome") != "not_sent":
            raise AqlaError("Pending stop operation may have been sent; reconcile before superseding")
        self.write_stop_phase(data, trade_id, current, "superseded", reason)
        row["management_revision"] += 1

    async def resume_pending_stop(self, trade_id):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        pending = trade.get("pending_stop_update")
        if not pending or pending["phase"] not in STOP_UPDATE_ACTIVE:
            return
        request = PendingStopUpdate.model_validate(pending)
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(snapshot, plan)
        operation = snapshot["operations"].get(request.operation_id)
        if trade["status"] not in OPEN:
            phase = "rejected" if operation and operation["phase"] == "rejected" else "closed_without_application"
            reason = "Stop replacement was rejected; own exposure closure is confirmed" if phase == "rejected" else "Trade ended without a confirmed application of the requested stop"
            self.stop_phase(trade_id, phase, reason)
            return
        if trade["status"] in ("closing", "cleanup"):
            self.stop_phase(trade_id, "unknown", "Own exposure closure is not yet confirmed")
            return
        binding = snapshot["bindings"].get(request.operation_id)
        revision_changed = (trade["management_revision"] != request.management_revision
                            or binding != request.source_binding.model_dump(mode="json")
                            or self.source_revisions(snapshot, request.source_revisions) != request.source_revisions)
        if revision_changed:
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                await self.exchange.reconcile(request.operation_id)
                operation = self.state.snapshot()["operations"][request.operation_id]
                if operation["phase"] in ("prepared", "sending", "unknown"):
                    self.stop_phase(trade_id, "unknown", "Original stop operation must be reconciled before superseding")
                    return
            self.stop_phase(trade_id, "superseded", "Source or independent management revision superseded this request")
            return
        if not Decimal(trade["filled_quantity"]):
            entry = await self.exchange.order_status(trade["entry_cloid"])
            total, _ = await self.capture_fills(trade_id, entry)
            if not total:
                if entry.get("order", {}).get("status") in TERMINAL_ORDER:
                    self.stop_phase(trade_id, "closed_without_application", "Entry ended without any own fill; requested stop was not sent")
                return
        self.stop_phase(trade_id, "replacing")
        try:
            result = await self.replace_stop(trade_id, request.stop, request.operation_id)
        except ExternalInterference:
            raise
        except StopEvidencePending as error:
            self._patch(trade_id, status="unknown", quantity_evidence_pending=True, waiting_error=str(error))
            self.stop_phase(trade_id, "waiting_fill", str(error))
            return
        except AqlaError as error:
            operation = self.state.snapshot()["operations"].get(request.operation_id)
            phase = "unknown" if operation and operation["phase"] in ("prepared", "sending", "unknown") else "rejected"
            self.stop_phase(trade_id, phase, str(error))
            return
        current = self.state.snapshot()["trades"][trade_id]
        if current["status"] in ("closing", "cleanup"):
            self.stop_phase(trade_id, "unknown", "Own exposure closure is not yet confirmed")
        elif result.get("replacement_confirmed"):
            self.stop_phase(trade_id, "confirmed", "Matching open replacement stop confirmed")
        elif result.get("already_closed"):
            self.stop_phase(trade_id, "closed_without_application", "Own exposure ended before stop application")
        elif result.get("outcome") == "not_sent":
            self.stop_phase(trade_id, "rejected", result.get("result", {}).get("error", "Replacement was not sent"))
        else:
            self.stop_phase(trade_id, "unknown", "Replacement stop is not yet confirmed; original protection retained")

    async def replace_stop(self, trade_id, proposed: Decimal, operation_id=None, *, initialize_backup=False, initial_protection=False):
        if operation_id:
            operation = self.state.snapshot()["operations"].get(operation_id)
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                await self.exchange.reconcile(operation_id)
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        if trade["status"] == "conflicted":
            raise AqlaError("Manual market conflict requires review; existing orders are preserved")
        if trade["status"] not in OPEN:
            return {"already_closed": True}
        remaining = Decimal(trade["remaining_quantity"])
        if not remaining:
            return {"waiting_for_fill": True}
        if not initialize_backup and not initial_protection:
            request = await self.management_stop_preflight(trade_id, operation_id)
            if request:
                self.state.update(lambda data: self.supersede_stop_for_management(data, trade_id, request,
                    "An independent stop management instruction replaced this unsent request"))
                trade = self.state.snapshot()["trades"][trade_id]
        stop = price_grid(proposed, plan.market, up=plan.side == "long")
        if initialize_backup:
            final = trade.get("backup_finalization") or {}
            if (final.get("phase") != "prepared" or "stop_loss" not in plan.backup_fields or str(proposed) != final.get("stop_loss")
                    or operation_id != final.get("stop_operation_id") or any(order["kind"] == "tp" for order in trade["orders"])):
                raise AqlaError("Only the durable initial Backup SL may be finalized")
        self.validate_stop_budget(trade, plan, stop, initialize_backup=initialize_backup)
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        signed = self.position_size(await self.positions(plan.market), plan.market)
        remaining = self.owned_remaining(trade_id, signed)
        trade = self.state.snapshot()["trades"][trade_id]
        if not initial_protection:
            self.validate_final_stop_quantity(trade)
        self.validate_stop_budget(trade, plan, stop, remaining, initialize_backup=initialize_backup)
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
        entry_fingerprint = None
        def final_guard(data):
            self.assert_plan_binding(data, plan)
            row = data["trades"][trade_id]
            if row["status"] not in OPEN or row["status"] in ("closing", "cleanup", "conflicted"):
                raise AqlaError("Own exposure no longer permits this stop replacement")
            facts = {key: fill for key, fill in row["fill_events"].items() if fill["kind"] == "entry"}
            if digest(facts) != entry_fingerprint:
                raise AqlaError("Own entry facts changed before stop replacement")
            self.validate_stop_budget(row, plan, stop, remaining, initialize_backup=initialize_backup)
            if not initial_protection:
                self.validate_final_stop_quantity(row)
            pending = row.get("pending_stop_update")
            if pending and pending["operation_id"] == operation_id:
                if pending["phase"] not in STOP_UPDATE_ACTIVE or pending["management_revision"] != row["management_revision"]:
                    raise AqlaError("Pending stop management revision changed before replacement")
                if data["bindings"].get(operation_id) != pending["source_binding"] or self.source_revisions(data, pending["source_revisions"]) != pending["source_revisions"]:
                    raise AqlaError("Pending stop source binding changed before replacement")
                if pending["source_binding"]["kind"] == "break_even" and price_grid(Decimal(row["entry_vwap"]), plan.market, up=plan.side == "long") != stop:
                    raise AqlaError("Break-even reference changed after acceptance; original price identity will not be rebound")
        async def live_guard():
            nonlocal entry_fingerprint
            await self.capture_fills(trade_id)
            current_size = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
            row = self.state.snapshot()["trades"][trade_id]
            if not initial_protection:
                self.validate_final_stop_quantity(row)
            self.validate_stop_budget(row, plan, stop, current_size, initialize_backup=initialize_backup)
            if current_size != remaining:
                raise AqlaError("Final own position changed; reconcile before stop replacement")
            entry_fingerprint = digest({key: fill for key, fill in row["fill_events"].items() if fill["kind"] == "entry"})
            final_guard(self.state.snapshot())
        if not any(order["cloid"] == cloid for order in trade["orders"]):
            self.state.update(lambda state: state["trades"][trade_id]["orders"].append(
                {"kind": "sl", "cloid": cloid, "quantity": str(remaining), "price": str(stop)}))
        result = existing or await self.exchange.mutate(operation_id, {"type": "order", "orders": [replacement], "grouping": "na"},
                                                       async_prepare_guard=live_guard, prepare_guard=final_guard)
        confirmed = await self.exchange.order_status(cloid)
        if confirmed.get("order", {}).get("status") == "open" and self._stop_matches(confirmed, plan, remaining, stop):
            pending = self.state.snapshot()["trades"][trade_id].get("pending_stop_update")
            preserve = initialize_backup or initial_protection or pending and pending["operation_id"] == operation_id
            self._patch(trade_id, stop_price=str(stop), management_revision=trade["management_revision"] + 1,
                        preserve_pending_stop=preserve)
            old = [order for order in trade["orders"] if order["kind"] == "sl" and order["cloid"] != cloid]
            if pending and pending["phase"] in STOP_UPDATE_ACTIVE and (initialize_backup or initial_protection):
                pending_cloid = self.exchange.cloid(pending["operation_id"])
                old = [order for order in old if order["cloid"] != pending_cloid]
            for order in old:
                await self.exchange.mutate("cancel:" + operation_id + ":" + order["cloid"],
                                           {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": order["cloid"]}]})
            self._patch(trade_id, status="open")
            return {**result, "replacement_confirmed": True}
        if result.get("outcome") == "not_sent":
            return result
        if result["phase"] == "rejected":
            if getattr(self.exchange, "fail_closed_protection", False):
                self.state.pause_entries("Stop loss was rejected; closing own exposure")
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
        if trade["status"] == "waiting_entry":
            return await self.cancel_entry(trade_id)
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(snapshot, plan)
        if trade["status"] == "conflicted":
            raise AqlaError("Manual market conflict requires review; no mixed position is closed")
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
        if not operation_id.startswith(("stop-crossed:", "emergency:", "protection-failure:")):
            request = await self.management_stop_preflight(trade_id, operation_id)
            if request:
                self.state.update(lambda data: self.supersede_stop_for_management(data, trade_id, request,
                    "An authorized close or reduction replaced this unsent stop request"))
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
            reason = "source_close" if operation_id.startswith("update:") else "operator_close" if operation_id.startswith(("operator:", "operator-close:")) else "protection_failure" if operation_id.startswith(("protection-failure:", "stop-crossed:", "emergency:")) else "unconfirmed"
            row.setdefault("exit_orders", []).append({"kind": "close", "reason": reason, "cloid": order["c"], "quantity": order["s"], "price": order["p"]})
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
        self._patch(trade_id, status=trade["cleanup_target"], cleanup_complete=True, reserved_risk="0", cleanup_observed_at=now_ms())
        await self.resume_pending_stop(trade_id)
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
                named = Hyperliquid.canonical_symbol(signal.symbol) if trade["plan"].get("exchange", "hyperliquid") == "hyperliquid" else signal.symbol
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
                stop_request = trade.get("pending_stop_update")
                if stop_request and stop_request["event_id"] != event_id:
                    stop_request = trade.get("stop_update_history", {}).get("update:" + event_id)
                if stop_request and stop_request["event_id"] == event_id:
                    if stop_request["phase"] in STOP_UPDATE_ACTIVE:
                        await self.resume_pending_stop(trade["trade_id"])
                        stop_request = self.state.snapshot()["trades"][trade["trade_id"]]["pending_stop_update"]
                    return {"trade_id": trade["trade_id"], "result": self.stop_update_result(stop_request),
                            "duplicate": stop_request["phase"] not in STOP_UPDATE_ACTIVE}
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
            if trade["status"] == "conflicted":
                raise AqlaError("Manual market conflict requires review; no automatic source update")
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
                requested_stop = price_grid(signal.stop_loss, plan.market, up=plan.side == "long")
                if not plan.policy.allow_stop_widening and (requested_stop < old if plan.side == "long" else requested_stop > old):
                    raise AqlaError("Stop widening is not permitted")
            update_targets = signal.take_profits
            update_spec = {"unit": "price"}
            if signal.update_action == "replace_targets":
                if signal.take_profit_unit != "price":
                    from .percent_targets import prices
                    specification = trade.get("percent_finalization") or plan.source_tp
                    if not specification:
                        specification = {"reference_entry": str(plan.source_entry_reference) if plan.source_entry_reference is not None else trade.get("entry_vwap"),
                                         "signal_leverage": plan.signal_leverage_original or plan.signal_leverage}
                    reference = specification.get("reference_entry") or trade["entry_vwap"]
                    if reference is None:
                        raise AqlaError("Percentage TP update requires this trade's stored source reference")
                    converted = prices(reference, plan.side, plan.market, signal.take_profit_unit, selected_targets(signal.take_profits, plan.policy), specification.get("signal_leverage"))
                    original = signal.model_dump(mode="json")
                    update_targets = converted
                    update_spec = {"unit": signal.take_profit_unit, "values": [str(value) for value in selected_targets(signal.take_profits, plan.policy)],
                                   "reference_entry": str(reference), "signal_leverage": specification.get("signal_leverage")}
                    # Validate the quoted update before converting; computed prices
                    # have no invented price quotations.
                    self.state.update(lambda data: data["trades"][trade["trade_id"]].setdefault("percent_updates", {}).__setitem__(event_id,
                                      {"source": original, "reference": reference, "prices": [str(value) for value in converted]}))
                self.validate_targets(trade, plan, update_targets)
            if trade["status"] == "waiting_entry" and signal.update_action in ("break_even", "reduce"):
                raise AqlaError("Waiting entry has no filled position for this update")
            if trade["status"] != "waiting_entry" and signal.update_action in ("move_stop", "break_even"):
                price = Decimal(trade["entry_vwap"]) if signal.update_action == "break_even" and trade["entry_vwap"] else signal.stop_loss
                if not price:
                    raise AqlaError("Stop update needs a price or proved entry VWAP")
                result = await self.queue_stop_update(trade["trade_id"], event_id, signal, price)
                return {"trade_id": trade["trade_id"], "result": result}
            operation = "update:" + event_id
            update_revisions = self.source_revisions(self.state.snapshot(), signal.source_refs)
            pending_request = await self.management_stop_preflight(trade["trade_id"], operation)
            if not previous:
                def accept_update(data):
                    self.assert_plan_binding(data, plan)
                    if self.source_revisions(data, signal.source_refs) != update_revisions:
                        raise AqlaError("Source revision changed before independent management acceptance")
                    if pending_request:
                        self.supersede_stop_for_management(data, trade["trade_id"], pending_request,
                            "A later authorized " + signal.update_action + " instruction replaced this unsent stop request")
                    data["bindings"][operation] = binding
                self.state.update(accept_update)
            if trade["status"] == "waiting_entry":
                if signal.update_action in ("close", "cancel_entry"):
                    result = await self.cancel_entry(trade["trade_id"])
                elif signal.update_action == "move_stop":
                    stop = price_grid(signal.stop_loss, plan.market, up=plan.side == "long")
                    self._patch(trade["trade_id"], stop_price=str(stop), management_revision=trade["management_revision"] + 1)
                    result = {"waiting_stop_updated": True}
                else:
                    targets = self.validate_targets(trade, plan, update_targets)
                    self._patch(trade["trade_id"], targets=[str(price) for price in targets], source_tp_override=update_spec, management_revision=trade["management_revision"] + 1)
                    result = {"waiting_targets_updated": True}
                self.state.update(lambda data: data["events"][event_id].update(status="updated"))
                return {"trade_id": trade["trade_id"], "result": result}
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
                result = await self.replace_targets(trade["trade_id"], update_targets, operation, source_tp=update_spec)
            uncertain = result.get("phase") in ("prepared", "sending", "unknown") or result.get("replacement_unconfirmed")
            self.state.update(lambda state: state["events"][event_id].update(status="needs_reconciliation" if uncertain else "updated"))
            return {"trade_id": trade["trade_id"], "result": result}

    @staticmethod
    def validate_targets(trade, plan, prices):
        prices = selected_targets(prices, plan.policy)
        if not prices or len(prices) != len(plan.take_profits):
            raise AqlaError("Replace targets with the same target count")
        target_prices = [price_grid(price, plan.market, up=plan.side == "short") for price in prices]
        entry = Decimal(trade["entry_vwap"] or plan.entry_price)
        if target_prices != sorted(set(target_prices), reverse=plan.side == "short") or any(price <= entry if plan.side == "long" else price >= entry for price in target_prices):
            raise AqlaError("Targets must be strictly ordered on the profitable side of the actual entry")
        return target_prices

    async def replace_targets(self, trade_id, prices, operation_id, source_tp=None):
        trade = self.state.snapshot()["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        target_prices = self.validate_targets(trade, plan, prices)
        prior = self.state.snapshot()["operations"].get(operation_id)
        if prior:
            return prior
        request = await self.management_stop_preflight(trade_id, operation_id)
        if request:
            self.state.update(lambda data: self.supersede_stop_for_management(data, trade_id, request,
                "An authorized target update replaced this unsent stop request"))
        staged = trade.get("target_updates", {}).get(operation_id)
        if staged is None:
            staged = {"prices": [str(price) for price in target_prices],
                      "original_orders": [order for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")],
                      "action": None, "source_tp": source_tp}
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
            if staged.get("source_tp") is not None:
                row["source_tp_override"] = staged["source_tp"]
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
            if trade["status"] == "conflicted":
                reports.append({"trade_id": trade_id, "market_conflict": True, "market": trade['plan']['market']['symbol']})
                continue
            if trade["status"] in OPEN:
                try:
                    if trade["status"] == "cleanup":
                        reports.append(await self.finish_cleanup(trade_id))
                    elif trade["status"] == "waiting_entry":
                        await self.check_waiting_entry(trade_id)
                    elif trade["entry_operation_id"] not in self.state.snapshot()["operations"]:
                        reports.append(await self.resume_unsent_entry(trade_id))
                    else:
                        for identity, intent in trade.get("close_intents", {}).items():
                            if identity not in self.state.snapshot()["operations"]:
                                reports.append(await self.dispatch_close_intent(trade_id, intent))
                        reports.append(await self.refresh_trade(trade_id))
                except ExternalInterference as error:
                    self._patch(trade_id, status="conflicted", external_conflict=str(error))
                    reports.append({"trade_id": trade_id, "market_conflict": True, "market": trade['plan']['market']['symbol']})
                except Exception as error:
                    reports.append({"trade_id": trade_id, "error": str(error) if isinstance(error, AqlaError) else "Trade reconciliation is unavailable"})
        self.recovery_checked = not any(row.get("error") for row in reports)
        return reports
