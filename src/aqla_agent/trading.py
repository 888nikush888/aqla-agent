from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from pydantic import Field

from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import (
    OPEN,
    STOP_UPDATE_ACTIVE,
    TERMINAL_ORDER,
    UNFINISHED,
    AqlaError,
    BackupValues,
    ExternalInterference,
    Interpretation,
    Model,
    Policy,
    SourceEvent,
    StopUpdatePhase,
    TradePlan,
    digest,
    margin_leverage,
    now_ms,
    require_first,
    wire_number,
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
    verify_entry_shape,
    verify_evidence,
    verify_setup_references,
    verify_update_shape,
)

from . import managed_entries
from . import waverider as wave_manager
from .account_work import AccountLock
from .accounting import foreign_entry_orders, occupied_markets
from .entry import execution_limit, gate_state, passive_entry_price, validate_entry_prices
from .evidence import market_fills, order_observations
from .execution_roles import merge_role, native_role
from .observations import TemporaryObservation, diagnostic, read_observation, retry_due, retry_marker
from .pipelines import ConfigurationChanged, validate_ranking_binding
from .source_queue import is_source_row

if TYPE_CHECKING:
    from aqla_hyperliquid_mcp.state import State

    from .pipelines import VenueState


class EntryBudgetChanged(AqlaError):
    pass




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
    def __init__(self, exchange: Hyperliquid, state: State | VenueState):
        self.exchange, self.state = exchange, state
        self.lock = AccountLock()
        self.recovery_lock = asyncio.Lock()
        self.recovery_checked = False
        self.waves = wave_manager
        self.express_ingress: Callable | None = None
        self.express_admission: Callable | None = None
        self.entry_wake: Callable | None = None
        self.disposition_key: bytes | None = None
        self.recovery_cursor = 0

    def invoke_hook(self, name, *args):
        callback = getattr(self, name, None)
        if callback is None:
            return
        if not callable(callback):
            raise AqlaError("Configured trading callback is unavailable")
        return callback(*args)

    def ingest(self, source: SourceEvent, *, native_forward=False):
        legacy_event_id = digest([source.source_id, source.edited_at, source.text, source.media])[:32]
        snapshot = self.state.read("sources_live_after", "sources", "source_aliases", "events", "event_tombstones",
            entries={"events": [source.event_id], "event_tombstones": [source.event_id, legacy_event_id]})
        if source.sent_at < snapshot.get("sources_live_after", 0):
            source = source.model_copy(update={"historical": True})
        trusted_forward = native_forward and self.exchange.settings.telegram_owner_id and source.chat_id == self.exchange.settings.telegram_owner_id
        if not trusted_forward and source.chat_id not in {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}:
            raise AqlaError("Source is not subscribed")
        if source.event_id in snapshot["events"]:
            return snapshot["events"][source.event_id]
        if legacy_event_id != source.event_id and legacy_event_id in snapshot.get("event_tombstones", {}):
            return {"event_id": legacy_event_id, "source_id": source.source_id, "status": "already_processed", "reason": "legacy contextual revision retained as processed"}
        if source.event_id in snapshot.get("event_tombstones", {}):
            return {"event_id": source.event_id, "source_id": source.source_id, "status": "already_processed"}

        def insert(state):
            if source.event_id in state["events"]:
                return state["events"][source.event_id]
            if legacy_event_id != source.event_id and legacy_event_id in state.get("event_tombstones", {}):
                return {"event_id": legacy_event_id, "source_id": source.source_id, "status": "already_processed", "reason": "legacy contextual revision retained as processed"}
            if source.event_id in state.get("event_tombstones", {}):
                return {"event_id": source.event_id, "source_id": source.source_id, "status": "already_processed"}
            row = {"event_id": source.event_id, "source_id": source.source_id, "source": source.model_dump(mode="json"),
                   "status": "context" if source.historical else "pending", "interpretation": None, "native_forward": bool(trusted_forward)}
            state["events"][source.event_id] = row
            state["cursors"][source.chat_id] = max(source.message_id, state["cursors"].get(source.chat_id, 0))
            from .source_queue import repair_queue
            repair_queue(state)
            self.invoke_hook("express_ingress", state, row)
            self.prune_events(state, inserted_event=source.event_id)
            return row

        return self.state.update(insert)

    @staticmethod
    def protected_source_events(state):
        protected_refs = {ref for trade in state["trades"].values() if trade["status"] in OPEN for ref in trade["plan"]["source_refs"]}
        protected_events = {event for plan in state["plans"].values() if plan["plan"]["deadline_at"] > now_ms() for event in plan["plan"].get("source_revisions", {}).values()}
        for trade in state["trades"].values():
            for sequence in trade.get("management_sequences", {}).values():
                if not sequence.get("completed"):
                    protected_events.add(sequence["event_id"])
                    protected_events.update(sequence["source_revisions"].values())
                    protected_refs.update(sequence["source_revisions"])
            pending = trade.get("pending_stop_update")
            if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
                protected_events.add(pending["event_id"])
                protected_events.update(pending["source_revisions"].values())
                protected_refs.update(pending["source_revisions"])
        return protected_refs, protected_events


    def prune_events(self, state, *, inserted_event=None):
        maximum = self.exchange.settings.max_events
        if len(state["events"]) <= maximum:
            return
        protected_refs, protected_events = self.protected_source_events(state)
        if inserted_event:
            protected_events.add(inserted_event)
        by_chat = {}
        for row in state["events"].values():
            if not is_source_row(row):
                continue
            by_chat.setdefault(row["source"]["chat_id"], []).append(row)
        for rows in by_chat.values():
            rows.sort(key=lambda row: (row["source"]["sent_at"], row["source"].get("edited_at") or 0))
            protected_events.update(row["event_id"] for row in rows[-self.exchange.settings.max_context_messages:])
        candidates = [row for row in state["events"].values() if is_source_row(row)
                       and row["source_id"] not in protected_refs and row["event_id"] not in protected_events
                       and row.get("status") in ("report", "unrelated", "ambiguous", "incomplete", "context", "executed", "updated", "rejected", "superseded")]
        candidates.sort(key=lambda row: row["source"]["received_at"])
        for row in candidates[:max(0, len(state["events"]) - maximum)]:
            state.setdefault("event_tombstones", {})[row["event_id"]] = row["source_id"]
            del state["events"][row["event_id"]]

    @staticmethod
    def context_index(snapshot):
        messages, parents, trades, bound = {}, {}, {}, {}
        for row in snapshot["events"].values():
            if is_source_row(row):
                messages.setdefault(row["source"]["chat_id"], []).append(row)
        for chat, rows in messages.items():
            rows.sort(key=lambda item: (item["source"]["sent_at"], item["source"].get("edited_at") or 0))
            parents[chat] = {row["source"]["message_id"]: row for row in rows}
        for row in snapshot["trades"].values():
            bound.setdefault(row["chat_id"], set()).update(row["plan"]["source_refs"])
            if row["status"] in OPEN:
                trades.setdefault(row["chat_id"], []).append(row)
        return messages, parents, trades, bound

    def context(self, event_id, *, compact_receipt=False, snapshot=None, index=None):
        snapshot = self.state.snapshot() if snapshot is None else snapshot
        row = snapshot["events"].get(event_id)
        if not is_source_row(row):
            raise AqlaError("Unknown source event")
        source = row["source"]
        channels, parents, own_trades, bound = self.context_index(snapshot) if index is None else index
        messages = channels.get(source["chat_id"], [])
        included = messages[-self.exchange.settings.max_context_messages:]
        if row not in included:
            included.insert(0, row)
        reply = source.get("reply_to_id")
        seen = set()
        while reply and reply not in seen:
            seen.add(reply)
            parent = parents.get(source["chat_id"], {}).get(reply)
            if parent is None:
                break
            if parent not in included:
                included.insert(0, parent)
            reply = parent["source"].get("reply_to_id")
        trades = own_trades.get(source["chat_id"], [])
        result = {"event": row, "messages": included, "trades": trades, "policy": snapshot["policy"],
                "bound_source_refs": sorted(bound.get(source["chat_id"], set()) & {item["source_id"] for item in included}),
                "classification_constraints": {"missing_text": not source.get("text", "").strip(),
                    "media": source.get("media", "text"), "empty_body_only_kind": "incomplete"},
                "source_names": snapshot.get("source_names", {}), "source_name": snapshot.get("source_names", {}).get(source["chat_id"]),
                "backup_profile": snapshot.get("profiles", {}).get("backup"),
                "policy_revision": snapshot["policy_revision"], "context_truncated": len(included) < len(messages)}
        if getattr(self, "disposition_key", None):
            from .source_disposition import issue_receipt
            try:
                result["source_read_receipt"] = issue_receipt(snapshot["events"], event_id, self.disposition_key, context_rows=included,
                                                            compact=compact_receipt)
            except AqlaError as error:
                result["source_read_receipt"] = None
                result["source_read_error"] = str(error)
        return result

    @staticmethod
    def validate_actionable_anchor(snapshot, row, event_id, interpretation):
        if not row["source"].get("text", "").strip():
            raise AqlaError("Live anchor has no textual source intent; read source_context and classify incomplete")
        if row["source"].get("historical"):
            raise AqlaError("Historical context cannot authorize an exchange action")
        if row["source_id"] not in interpretation.source_refs:
            raise AqlaError("An actionable interpretation must reference its live anchor event")
        if latest_sources(snapshot["events"], [row["source_id"]])[row["source_id"]]["event_id"] != event_id:
            raise AqlaError("An actionable interpretation must use the latest anchor revision")


    def validate_interpretation_sources(self, snapshot, row, event_id, interpretation):
        refs = [item for item in snapshot["events"].values() if is_source_row(item) and item["source_id"] in interpretation.source_refs]
        if not refs or any(item["source"]["chat_id"] != row["source"]["chat_id"] for item in refs):
            raise AqlaError("A setup may only combine messages from the same source")
        if interpretation.kind in ("new_signal", "trade_update"):
            self.validate_actionable_anchor(snapshot, row, event_id, interpretation)
        verify_evidence(interpretation, snapshot["events"])
        verify_entry_shape(interpretation)
        verify_update_shape(interpretation)
        if interpretation.kind not in ("new_signal", "trade_update"):
            from .source_disposition import verify_classification_policy
            verify_classification_policy(interpretation, row["source"].get("text", ""))


    def validate_interpretation_intent(self, snapshot, row, event_id, interpretation):
        from .semantic_contract import (
            supported_action_failure,
            unbound_setup_update_failure,
            update_identity_failure,
        )
        failure = supported_action_failure(row["source"].get("text", ""), interpretation)
        if failure is None and interpretation.kind == "trade_update":
            from .source_candidates import build_packet
            packet = build_packet(self.context(event_id, snapshot=snapshot))
            failure = unbound_setup_update_failure(packet, interpretation) or update_identity_failure(packet, interpretation)
        if failure:
            raise AqlaError(failure)
        if interpretation.take_profit_unit in ("course_percent", "roi_percent") and interpretation.take_profits:
            raise AqlaError("New source TP percentages must use roe_percent under source-roe-v1")


    @staticmethod
    def validate_interpretation_setup_refs(snapshot, row, interpretation):
        own_refs = (ref for trade in snapshot["trades"].values()
                    if trade["chat_id"] == row["source"]["chat_id"] and interpretation.bound_trade_id in (None, trade["trade_id"])
                    for ref in trade["plan"]["source_refs"])
        verify_setup_references(interpretation, row["source_id"], own_refs)


    def commit_interpretation(self, snapshot, event_id, interpretation, read_receipt, result_view):
        row = snapshot["events"].get(event_id)
        if not is_source_row(row):
            raise AqlaError("Unknown source event")
        from .express import require_source_access
        require_source_access(snapshot, event_id)
        previous = row.get("interpretation")
        from .efficiency import classification_ack
        def result():
            changed = previous != row.get("interpretation")
            return classification_ack(row, changed) if result_view == "ack" else row
        if row["status"] in ("executed", "updated"):
            previous = Interpretation.model_validate(previous).model_dump(mode="json") if previous is not None else None
            if previous != interpretation.model_dump(mode="json"):
                raise AqlaError("An executed revision is immutable; interpret the edit as a new update revision")
            return result()
        from .source_queue import recovery_ids
        if event_id in recovery_ids(snapshot):
            prior = Interpretation.model_validate(previous).model_dump(mode="json") if previous is not None else None
            if prior == interpretation.model_dump(mode="json"):
                return result()
            raise AqlaError("Financial recovery revision is immutable; reconcile its existing operations")
        if interpretation.kind not in ("new_signal", "trade_update") and getattr(self, "disposition_key", None):
            from .source_disposition import verify_disposition
            verify_disposition(snapshot, event_id, interpretation, read_receipt, self.disposition_key)
        self.validate_interpretation_sources(snapshot, row, event_id, interpretation)
        self.validate_interpretation_setup_refs(snapshot, row, interpretation)
        self.validate_interpretation_intent(snapshot, row, event_id, interpretation)
        row.update(interpretation=interpretation.model_dump(mode="json"),
                   status=interpretation.kind if interpretation.kind not in ("new_signal", "trade_update") else "interpreted")
        if interpretation.kind not in ("new_signal", "trade_update"):
            row.pop("last_error", None)
        return result()


    def record_interpretation(self, event_id, interpretation: Interpretation, *, read_receipt="", result_view="full"):
        def commit(snapshot):
            return self.commit_interpretation(snapshot, event_id, interpretation, read_receipt, result_view)
        return (self.state.update_if_changed if hasattr(self.state, "update_if_changed") else self.state.update)(commit)

    def preview_policy(self, snapshot):
        if snapshot["paused"]:
            raise AqlaError("New entries are paused; existing exits and protection continue")
        if snapshot["policy"] is None:
            raise AqlaError("User trading rules have not been configured")
        self.state.assert_binding(self.exchange.settings)
        policy = Policy.model_validate(snapshot["policy"])
        if "maker_only" in (policy.entry_execution, policy.take_profit_execution) and not getattr(self.exchange, "supports_maker_orders", False):
            raise AqlaError("Selected exchange does not support strict native maker orders")
        if policy.product != "perp":
            raise AqlaError("Automatic source trading requires perpetual markets with native reduce-only protection; Spot remains available through the full MCP")
        return policy

    def preview_reservations(self, snapshot, interpretation, policy, _refresh_from):
        refresh_trade = self.budget_refresh_trade(snapshot, TradePlan.model_validate(snapshot["plans"][_refresh_from]["plan"])) if _refresh_from else None
        active = [trade for trade in snapshot["trades"].values() if trade["status"] in OPEN and trade is not refresh_trade]
        if len(active) >= policy.max_open_trades:
            raise AqlaError("Maximum open trade count reached")
        if any(ref in snapshot["bindings"] and (not refresh_trade or snapshot["bindings"][ref]["trade_id"] != refresh_trade["trade_id"]) for ref in interpretation.source_refs):
            raise AqlaError("This source already owns a trade; process its edit as an update")
        return active

    async def preview_market(self, interpretation, policy, active, _market):
        # The router may resolve this once under the same account lock for fallback.
        market = _market if _market is not None else await read_observation(lambda: self.exchange.market(interpretation.symbol, policy.product), "market")
        if any(trade["plan"]["market"]["quote_token"] != market.quote_token for trade in active):
            raise AqlaError("One account risk budget cannot mix settlement currencies without conversion")
        if any(trade["plan"]["market"]["asset_id"] == market.asset_id for trade in active):
            raise AqlaError("An active trade already owns this net-position market")
        positions = await self.positions(market)
        if self.position_size(positions, market) != 0:
            raise AqlaError("Existing exchange exposure has no free logical owner; no additional entry")
        return market

    @staticmethod
    def validate_preview_inventory(snapshot, account, market, policy):
        if account.positions is not None:
            if len(occupied_markets(snapshot, account)) >= policy.max_open_trades:
                raise AqlaError("Combined manual/Aqla market limit reached")
            if foreign_entry_orders(snapshot, account, market.symbol):
                raise AqlaError("Manual entry orders already reserve this market")

    async def preview_maker_price(self, interpretation, market, policy):
        maker_quote = None
        maker_price = None
        if policy.entry_execution == "maker_only" and interpretation.entry not in ("limit", "range"):
            maker_quote = await read_observation(lambda: self.exchange.price_snapshot(market), "price")
            maker_price = passive_entry_price(market, interpretation.side, maker_quote)
        return maker_quote, maker_price

    @staticmethod
    def initialize_preview_price(plan, quote):
        if plan.entry_timing == "deferred":
            if plan.entry_gate is None:
                raise AqlaError("Deferred entry requires its saved source gate")
            plan.entry_gate.update(origin_mid=str(quote.mid), direction="down" if quote.mid > plan.entry_price else "up" if quote.mid < plan.entry_price else "at_entry",
                                   origin_snapshot=quote.model_dump(mode="json"))
        else:
            validate_entry_prices(plan, quote)

    async def build_preview_plan(self, interpretation, snapshot, active, market, account, policy, _quantity_cap):
        refs = [item["source"]["sent_at"] for item in latest_sources(snapshot["events"], interpretation.source_refs).values()]
        risk = sum(Decimal(trade["reserved_risk"]) for trade in active)
        backup_profile = snapshot.get("profiles", {}).get("backup")
        backup = BackupValues.model_validate({key: backup_profile["parameters"][key] for key in BackupValues.model_fields
                                             if key in backup_profile["parameters"]}) if backup_profile else None
        maker_quote, maker_price = await self.preview_maker_price(interpretation, market, policy)
        plan = make_plan(interpretation, market, account, policy, snapshot["policy_revision"], min(refs),
                         (self.exchange.settings.network, self.exchange.account.lower()), reserved_risk=risk,
                         principal=self.exchange.settings.account.lower(), backup=backup,
                         backup_revision=backup_profile["revision"] if backup_profile else None, maker_price=maker_price)
        if _quantity_cap is not None:
            plan = managed_entries.cap_quantity(plan, _quantity_cap)
        plan.source_revisions = self.source_revisions(snapshot, interpretation.source_refs)
        plan.ranking_policy_revision = snapshot["ranking_revision"]
        plan.ranking_enabled = snapshot["ranking_policy"]["enabled"]
        quote = maker_quote or await read_observation(lambda: self.exchange.price_snapshot(market), "price")
        self.initialize_preview_price(plan, quote)
        if hasattr(self.state, "stamp_plan"):
            self.state.stamp_plan(plan, snapshot)
        plan.plan_id = digest(plan.model_dump(mode="json", exclude={"plan_id"}))
        return plan

    @staticmethod
    def validate_preview_refresh_identity(plan, original, _refresh_from):
        identity_fields = ("asset_id", "symbol", "product", "dex", "size_decimals", "price_tick", "quantity_step",
                           "quote_token", "collateral_token", "base_token", "native_symbol", "margin_leverage_scale")
        if (any(getattr(plan.market, key) != getattr(original.market, key) for key in identity_fields)
                or len(plan.take_profits) > len(original.take_profits)):
            raise EntryBudgetChanged("Budget refresh cannot change the original market or add TP targets")
        if (plan.plan_id == _refresh_from or plan.trade_id != original.trade_id or plan.source_revisions != original.source_revisions
                or plan.deadline_at != original.deadline_at or plan.effective_leverage != original.effective_leverage
                or plan.quantity > original.quantity or plan.risk_usdc > original.risk_usdc
                or plan.quantity * plan.entry_limit > original.quantity * original.entry_limit):
            raise EntryBudgetChanged("Refreshed entry must stay within the original quantity, risk, notional and deadline")

    def refresh_owned_preview(self, owned, record, plan, _refresh_from):
        owned.update(plan=record["plan"], reserved_risk=str(plan.risk_usdc), admission_refresh_from=_refresh_from)
        if owned.get("lifecycle_version") == 2:
            gates = {row["index"]: row.get("entry_gate") for row in owned["entry_orders"]}
            managed_entries.initialize_trade(owned, plan, None, self.exchange)
            for row in owned["entry_orders"]:
                if gates.get(row["index"]):
                    row["entry_gate"] = gates[row["index"]]
            owned.update(stop_price=str(plan.stop_loss), targets=[str(value) for value in plan.take_profits],
                         tp_filled=["0"] * len(plan.take_profits))
        if owned.get("observation_retry"):
            record.update(observation_retry=owned["observation_retry"].copy(), execution_requested=True)

    def link_preview_refresh(self, state, _refresh_from, plan, record):
        prior = state["plans"][_refresh_from]
        original = TradePlan.model_validate(prior["plan"])
        self.validate_configuration(state, original)
        if plan.policy != original.policy:
            raise ConfigurationChanged("Configuration changed during budget refresh; create a fresh preview")
        owned = self.budget_refresh_trade(state, original)
        if (prior.get("refreshed_plan_id") or prior.get("refreshed_from_plan_id")
                or not owned and any(prefix + original.trade_id in state["operations"] for prefix in ("entry:", "leverage:"))):
            raise AqlaError("Entry preparation or budget refresh already started; reconcile it")
        self.validate_preview_refresh_identity(plan, original, _refresh_from)
        # Link both immutable plans in the same transaction, including across crashes.
        prior["refreshed_plan_id"] = plan.plan_id
        record["refreshed_from_plan_id"] = _refresh_from
        if owned:
            self.refresh_owned_preview(owned, record, plan, _refresh_from)

    def persist_preview_plan(self, event_id, plan, _refresh_from, state):
        self.assert_plan_binding(state, plan)
        try:
            self.validate_configuration(state, plan)
        except ConfigurationChanged as error:
            raise ConfigurationChanged("Configuration changed during preview; create a fresh preview: " + str(error)) from error
        record = {"event_id": event_id, "plan": plan.model_dump(mode="json")}
        if _refresh_from is not None:
            self.link_preview_refresh(state, _refresh_from, plan, record)
        state["plans"][plan.plan_id] = record

    async def preview(self, event_id, interpretation: Interpretation, *, _market=None, _refresh_from=None, _quantity_cap=None):
        row = self.record_interpretation(event_id, interpretation)
        if interpretation.kind != "new_signal":
            return row
        if not interpretation.symbol or not interpretation.side:
            raise AqlaError("New trade needs an evidenced market and direction")
        snapshot = self.state.preview_snapshot() if hasattr(self.state, "preview_snapshot") else self.state.snapshot()
        policy = self.preview_policy(snapshot)
        active = self.preview_reservations(snapshot, interpretation, policy, _refresh_from)
        market = await self.preview_market(interpretation, policy, active, _market)
        account = await read_observation(lambda: self.exchange.account_snapshot(market), "account")
        self.validate_preview_inventory(snapshot, account, market, policy)
        plan = await self.build_preview_plan(interpretation, snapshot, active, market, account, policy, _quantity_cap)
        self.state.update(partial(self.persist_preview_plan, event_id, plan, _refresh_from))
        return plan.model_dump(mode="json")

    @staticmethod
    def validate_unsent_budget_refresh(snapshot, plan, trade, saved, leverage):
        if (saved.get("refreshed_plan_id") or saved.get("refreshed_from_plan_id")
                or trade["plan"]["plan_id"] != plan.plan_id or trade["status"] != "preparing"
                or plan.entry_timing != "standard" or Decimal(trade["filled_quantity"]) or trade.get("orders")
                or any(key == "entry:" + plan.trade_id or key.startswith("entry:" + plan.trade_id + ":") for key in snapshot["operations"])
                or leverage and (leverage["phase"] not in ("acknowledged", "reconciled") or leverage.get("outcome") != "accepted")):
            raise EntryBudgetChanged("Budget refresh requires a completely unsent entry and is allowed only once")


    def budget_refresh_trade(self, snapshot, plan):
        trade = snapshot["trades"].get(plan.trade_id)
        if not trade:
            return None
        saved = snapshot["plans"][plan.plan_id]
        leverage = snapshot["operations"].get(trade.get("leverage_operation_id", "leverage:" + plan.trade_id))
        self.validate_unsent_budget_refresh(snapshot, plan, trade, saved, leverage)
        if trade.get("lifecycle_version") == 2 and trade.get("management_revision"):
            raise EntryBudgetChanged("A managed source update prevents automatic budget-plan replacement")
        self.validate_admission(snapshot, plan, owned=True)
        return trade

    async def refresh_unsent_budget(self, plan):
        snapshot = self.state.snapshot()
        self.budget_refresh_trade(snapshot, plan)
        saved = snapshot["plans"][plan.plan_id]
        signal = Interpretation.model_validate(snapshot["events"][saved["event_id"]]["interpretation"])
        fresh = TradePlan.model_validate(await self.preview(saved["event_id"], signal, _refresh_from=plan.plan_id))
        return fresh, self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]

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
                self.state.validate_route(plan, snapshot=snapshot)
            elif snapshot["paused"] or snapshot["policy_revision"] != plan.policy_revision:
                raise AqlaError("Pause or policy changed; create a new unsent plan")
        except AqlaError as error:
            raise ConfigurationChanged(str(error)) from error

    def validate_selected_execution(self, snapshot, plan):
        saved = snapshot["plans"].get(plan.plan_id, {})
        if saved.get("liquidity_candidate") and not saved.get("liquidity_selected"):
            raise AqlaError("Another venue owns this setup's selected execution")
        if parent_id := saved.get("rerouted_from_trade_id"):
            root = getattr(self.state, "root", self.state)
            parent = root.read("trades", entries={"trades": [parent_id]})["trades"].get(parent_id, {})
            if (parent.get("status") != "cancelled" or not parent.get("cleanup_complete")
                    or Decimal(parent.get("filled_quantity", "0")) or not parent.get("entries_disabled")
                    or parent.get("rerouted_to_plan_id") != plan.plan_id):
                raise AqlaError("Previous venue has not released this setup with proved zero fills")

    @staticmethod
    def validate_unsent_source_authority(snapshot, plan, owned):
        if not owned:
            from .express import require_source_access
            for event_id in plan.source_revisions.values():
                require_source_access(snapshot, event_id)

    def validate_entry_readiness(self, snapshot, plan, owned):
        if any(row.get("protection_observation_pending") and row["status"] in OPEN for row in snapshot["trades"].values()):
            raise TemporaryObservation("read_unavailable", "protection", "transport")
        if owned and snapshot["trades"][plan.trade_id]["status"] not in OPEN:
            raise AqlaError("Entry was terminated; no unsent exposure may execute")
        self.validate_configuration(snapshot, plan)
        if any((row.get("quantity_evidence_pending") or row.get("exit_quantity_evidence_pending")) and row["status"] in OPEN for row in snapshot["trades"].values()):
            raise AqlaError("Own quantity evidence requires reconciliation before new exposure")

    @staticmethod
    def entry_matches_cycle(row, wave, allowed_prepared, allowed_sending):

        return (row["kind"] == "waverider" and not row.get("not_sent")
                and row["operation_id"] == wave.get("operation_id")
                and row.get("cycle", 0) == wave.get("cycle", 0)
                and row["operation_id"] in (*allowed_prepared, *allowed_sending))

    def validate_cycle_admission(self, snapshot, plan, owned, allowed_prepared, allowed_sending):
        from .waverider import ended
        trade = snapshot["trades"].get(plan.trade_id, {})
        wave = trade.get("waverider", {})
        if trade.get("pending_stop_update", {}).get("phase") in STOP_UPDATE_ACTIVE:
            raise AqlaError("A pending stop update blocks Waverider reentry")
        if (not owned or trade.get("lifecycle_version") != 2 or plan.policy.management_mode != "waverider"
                or trade.get("entries_disabled") or wave.get("phase") != "pending_reenter"
                or ended(plan, trade) or not any(self.entry_matches_cycle(row, wave, allowed_prepared, allowed_sending) for row in trade["entry_orders"])):
            raise AqlaError("Waverider reentry has no active owned cycle")

    @staticmethod
    def confirmed_stop_revisions(snapshot, plan):
        return {ref: event for request in snapshot["trades"].get(plan.trade_id, {}).get("stop_update_history", {}).values()
            if request["phase"] == "confirmed" and request["source_binding"]["trade_id"] == plan.trade_id
            and request["source_binding"]["kind"] in ("move_stop", "break_even")
            and snapshot["bindings"].get(request["operation_id"]) == request["source_binding"]
            for ref, event in request["source_revisions"].items() if snapshot["events"].get(event, {}).get("status") == "updated"}

    def validate_source_revision(self, snapshot, plan, waverider):
        revisions = self.source_revisions(snapshot, plan.source_refs)
        if revisions != plan.source_revisions:
            confirmed = self.confirmed_stop_revisions(snapshot, plan)
            # An acknowledged stop edit authorizes this owned wave, never an old original entry.
            if not (waverider and revisions.keys() == plan.source_revisions.keys()
                    and all(event == plan.source_revisions[ref] or confirmed.get(ref) == event for ref, event in revisions.items())):
                raise AqlaError("Source changed after preview; the old unsent plan cannot execute")

    def validate_source_subscription(self, snapshot, plan):
        subscribed = {*snapshot["sources"], *snapshot.get("source_aliases", {}).values()}
        for row in latest_sources(snapshot["events"], plan.source_refs).values():
            source = row["source"]
            forwarded = row.get("native_forward") and source["chat_id"] == self.exchange.settings.telegram_owner_id
            if source["chat_id"] not in subscribed and not forwarded:
                raise AqlaError("Source is no longer subscribed; no unsent exposure may execute")

    def validate_entry_reservations(self, snapshot, plan, owned, active, limits, allowed_prepared, allowed_sending):
        if len(active) >= limits.max_open_trades or any(trade["plan"]["market"]["asset_id"] == plan.market.asset_id for trade in active):
            raise AqlaError("Trade admission changed after preview")
        for ref in plan.source_refs:
            binding = snapshot["bindings"].get(ref)
            if binding and (not owned or binding["trade_id"] != plan.trade_id):
                raise AqlaError("Source has already been consumed")
        self.validate_unresolved_entry_operations(snapshot, allowed_prepared, allowed_sending)

    @staticmethod
    def validate_unresolved_entry_operations(snapshot, allowed_prepared, allowed_sending):
        if any(row["phase"] in ("prepared", "sending", "unknown") and not (identity in allowed_prepared and row["phase"] == "prepared" or identity in allowed_sending and row["phase"] == "sending")
               for identity, row in snapshot["operations"].items()):
            raise AqlaError("Unresolved exchange operations must be reconciled before new exposure")

    def validate_entry_ownership(self, snapshot, plan, account, owned):
        if account.position_size is None:
            raise AqlaError("Current exchange position is unknown; no new exposure")
        if account.position_size != 0:
            trade = snapshot["trades"].get(plan.trade_id, {}) if owned else {}
            if trade.get("lifecycle_version") != 2 or self.entry_not_sent(snapshot, trade):
                raise AqlaError("Current exchange exposure has no free logical owner; no additional entry")
            self.owned_remaining(plan.trade_id, account.position_size)

    @staticmethod
    def validate_entry_inventory(snapshot, plan, account, limits, owned):
        if account.positions is not None:
            markets = occupied_markets(snapshot, account, exclude_trade=plan.trade_id if owned else None)
            if owned and snapshot["trades"][plan.trade_id].get("lifecycle_version") == 2:
                markets.discard(plan.market.symbol)
            if len(markets) >= limits.max_open_trades:
                raise AqlaError("Combined manual/Aqla market limit changed before entry")
            if foreign_entry_orders(snapshot, account, plan.market.symbol):
                raise AqlaError("Manual entry orders now reserve this market")

    def validate_entry_account(self, snapshot, plan, account, active, limits, owned):
        reserved = sum(Decimal(trade["reserved_risk"]) for trade in active)
        if not 0 <= now_ms() - account.observed_at <= 10000:
            raise TemporaryObservation("account_stale", "account")
        self.validate_entry_ownership(snapshot, plan, account, owned)
        self.validate_entry_inventory(snapshot, plan, account, limits, owned)
        if (plan.risk_usdc + reserved > account.equity * limits.max_total_risk_percent / 100
                or plan.risk_usdc > account.equity * plan.policy.risk_percent / 100
                or plan.quantity * plan.entry_limit / margin_leverage(plan.effective_leverage, plan.market) > account.available_margin):
            raise EntryBudgetChanged("Current equity/margin no longer permits this exact plan")


    def validate_admission(self, snapshot, plan, account=None, *, owned=False, allowed_prepared=(), allowed_sending=(), waverider=False):
        self.assert_plan_binding(snapshot, plan)
        self.validate_selected_execution(snapshot, plan)
        self.invoke_hook("express_admission", snapshot, plan)
        self.validate_unsent_source_authority(snapshot, plan, owned)
        self.validate_entry_readiness(snapshot, plan, owned)
        if waverider:
            self.validate_cycle_admission(snapshot, plan, owned, allowed_prepared, allowed_sending)
        if now_ms() >= plan.deadline_at and not (waverider and plan.policy.waverider_lifetime == "until_target"):
            raise AqlaError("Entry plan deadline expired")
        self.validate_source_revision(snapshot, plan, waverider)
        self.validate_source_subscription(snapshot, plan)
        active = [trade for key, trade in snapshot["trades"].items() if trade["status"] in OPEN and (not owned or key != plan.trade_id)]
        limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
        self.validate_entry_reservations(snapshot, plan, owned, active, limits, allowed_prepared, allowed_sending)
        if account is not None:
            self.validate_entry_account(snapshot, plan, account, active, limits, owned)

    async def positions(self, market):
        if market.product == "spot":
            return await self.exchange.info("spotClearinghouseState", user=self.exchange.account)
        return await read_observation(lambda: self.exchange.info("clearinghouseState", user=self.exchange.account, **({"dex": market.dex} if market.dex else {})), "position")

    async def order_status(self, identity):
        try:
            return await read_observation(lambda: self.exchange.order_status(identity), "order_status")
        except TemporaryObservation:
            self.recovery_checked = False
            raise

    @staticmethod
    def position_size(raw, market):
        if market.product == "spot":
            row = next((item for item in raw["balances"] if item["coin"] == market.base_token), None)
            return Decimal(row["total"]) if row else Decimal(0)
        row = next((item["position"] for item in raw["assetPositions"] if item["position"]["coin"] == market.symbol), None)
        return Decimal(row["szi"]) if row else Decimal(0)

    @staticmethod
    def order(plan, quantity, price, *, reduce=False, trigger=None, cloid=None, take_profit=False):
        order = {"a": plan.market.asset_id, "b": (plan.side == "long") != reduce, "p": wire_number(price),
                 "s": wire_number(quantity), "r": reduce if plan.market.product == "perp" else False,
                 "t": {"limit": {"tif": "Ioc" if not reduce and plan.entry_kind == "market" else "Gtc"}}}
        if not trigger and (not reduce and getattr(plan, "entry_execution", "standard") == "maker_only" or take_profit and getattr(plan, "take_profit_execution", "standard") == "maker_only"):
            order["t"] = {"limit": {"tif": "Alo"}}
        if trigger:
            order["t"] = {"trigger": {"isMarket": True, "triggerPx": wire_number(trigger[1]), "tpsl": trigger[0]}}
        if cloid:
            order["c"] = cloid
        return order

    def new_trade(self, snapshot, saved, plan, quote=None):
        trade = {"trade_id": plan.trade_id, "chat_id": snapshot["events"][saved["event_id"]]["source"]["chat_id"],
                 "plan": plan.model_dump(mode="json"), "event_id": saved["event_id"], "status": "waiting_entry" if plan.entry_timing == "deferred" else "preparing",
                 "lifecycle_version": 1, "lifecycle_events": {}, "price_checks": [{"phase": "admission", "snapshot": quote.model_dump(mode="json")}] if quote else [],
                 "reserved_risk": str(plan.risk_usdc), "entry_operation_id": "entry:" + plan.trade_id,
                 "entry_cloid": self.exchange.cloid("entry:" + plan.trade_id), "orders": [],
                 "filled_quantity": "0", "remaining_quantity": "0", "entry_vwap": None,
                 "stop_price": str(plan.stop_loss), "tp_filled": ["0"] * len(plan.take_profits), "management_revision": 0,
                 "fill_events": {}, "exit_orders": [], "updates": {}, "targets": [str(price) for price in plan.take_profits]}
        if saved.get("refreshed_from_plan_id"):
            trade["admission_refresh_from"] = saved["refreshed_from_plan_id"]
        if saved.get("rerouted_from_trade_id"):
            trade.update(rerouted_from_trade_id=saved["rerouted_from_trade_id"], venue_switch_count=1)
        if saved.get("managed_entry_retry"):
            trade["managed_entry_retry"] = saved["managed_entry_retry"].copy()
        if managed_entries.enabled(plan):
            managed_entries.initialize_trade(trade, plan, quote, self.exchange)
        return trade

    async def execute(self, plan_id):
        if not self.recovery_checked:
            await self.recover()
        async with self.lock:
            if not self.recovery_checked:
                raise AqlaError("Startup reconciliation is incomplete; new exposure remains blocked")
            try:
                return await self.execute_locked(plan_id)
            except TemporaryObservation as error:
                saved = self.state.read("plans", entries={"plans": [plan_id]})["plans"][plan_id]
                return self.retain_observation_retry(saved.get("refreshed_plan_id", plan_id), error)

    async def retry_unsent_admission_budget(self, saved, plan, plan_id, refresh_budget, error):
        current = self.state.snapshot()
        if (not refresh_budget or plan.trade_id in current["trades"]
                or any(prefix + plan.trade_id in current["operations"] for prefix in ("entry:", "leverage:"))):
            raise error
        signal = Interpretation.model_validate(current["events"][saved["event_id"]]["interpretation"])
        fresh = await self.preview(saved["event_id"], signal, _refresh_from=plan_id)
        fresh = TradePlan.model_validate(fresh)
        return await self.execute_locked(fresh.plan_id, refresh_budget=False)

    async def execute_locked(self, plan_id, *, refresh_budget=True):
        snapshot = self.state.snapshot()
        saved = snapshot["plans"].get(plan_id)
        if not saved:
            raise AqlaError("Unknown immutable plan")
        plan = TradePlan.model_validate(saved["plan"])
        self.assert_plan_binding(snapshot, plan)
        if saved.get("refreshed_plan_id"):
            return await self.execute_locked(saved["refreshed_plan_id"], refresh_budget=False)
        refresh_budget = refresh_budget and not saved.get("refreshed_from_plan_id")
        if plan.trade_id in snapshot["trades"]:
            trade = snapshot["trades"][plan.trade_id]
            if trade.get("observation_retry"):
                return await self.resume_unsent_entry(plan.trade_id)
            return trade
        self.validate_admission(snapshot, plan)
        if self.position_size(await self.positions(plan.market), plan.market) != 0:
            raise AqlaError("Exchange position changed after preview")
        await read_observation(self.exchange.verify_signer, "signer")
        current_market = await read_observation(lambda: self.exchange.market(plan.market.symbol, plan.market.product), "market")
        if (current_market.asset_id, current_market.size_decimals, current_market.quote_token) != (plan.market.asset_id, plan.market.size_decimals, plan.market.quote_token):
            raise AqlaError("Exchange market metadata changed after preview")
        if plan.effective_leverage > current_market.max_leverage:
            raise AqlaError("Exchange leverage limit decreased after preview; create a new unsent plan")
        account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
        quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
        if plan.entry_timing == "standard":
            validate_entry_prices(plan, quote)
        trade = self.new_trade(snapshot, saved, plan, quote)

        def admit(state):
            self.validate_admission(state, plan, account)
            state["trades"][plan.trade_id] = trade
            for ref in plan.source_refs:
                state["bindings"][ref] = {"trade_id": plan.trade_id, "kind": "entry"}
            state["events"][saved["event_id"]]["status"] = "executed"

        try:
            self.state.update(admit)
        except EntryBudgetChanged as error:
            return await self.retry_unsent_admission_budget(saved, plan, plan_id, refresh_budget, error)
        self.invoke_hook("entry_wake")
        if plan.entry_timing == "deferred":
            self.state.update(lambda data: data["notifications"].setdefault("entry-waiting:" + plan.trade_id,
                              {"trade_id": plan.trade_id, "change": {"status": "waiting_entry"}, "observed_at": now_ms()}))
            return await self.check_waiting_entry(plan.trade_id)
        return await self.guarded_entry_steps(plan, trade)


    def waiting_configuration_changed(self, plan, snapshot):
        configuration = self.state.root.snapshot() if hasattr(self.state, "root") else snapshot
        changed = (plan.profile_id is not None and configuration["profiles"].get(plan.profile_id, {}).get("revision") != plan.profile_revision
                   or plan.pipeline_id is not None and configuration["pipelines"].get(plan.pipeline_id, {}).get("revision") != plan.pipeline_revision
                   or plan.profile_id is None and snapshot["policy_revision"] != plan.policy_revision
                   or plan.ranking_policy_revision != configuration["ranking_revision"]
                   or plan.ranking_enabled != configuration["ranking_policy"]["enabled"]
                   or plan.backup_fields and configuration["profiles"]["backup"]["revision"] != plan.backup_profile_revision)
        return changed


    async def activate_waiting_entry(self, trade_id, plan, trade):
        try:
            account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
            self.validate_admission(self.state.snapshot(), plan, account, owned=True)
            quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
            validate_entry_prices(plan, quote, stop=trade["stop_price"], targets=trade["targets"])
            self.patch_trade(trade_id, status="preparing", gate_activated_at=now_ms())
            return await self.guarded_entry_steps(plan, self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id])
        except TemporaryObservation as error:
            return self.retain_observation_retry(plan.plan_id, error)
        except AqlaError as error:
            if self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["entry_operation_id"] not in self.state.read("operations")["operations"]:
                self.patch_trade(trade_id, status="rejected", reserved_risk="0", unsent_reason=str(error))
                return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            raise


    async def observe_waiting_entry(self, trade_id, trade, plan):
        try:
            quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
            state = gate_state(plan, quote)
        except TemporaryObservation as error:
            return self.retain_observation_retry(plan.plan_id, error)
        except Exception as error:
            reason = str(error) if isinstance(error, AqlaError) else "Fresh target prices are unavailable"
            self.patch_trade(trade_id, waiting_error=reason)
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        self.patch_trade(trade_id, waiting_error=None, waiting_snapshot=quote.model_dump(mode="json"))
        if state == "waiting":
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if state == "missed":
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Deferred entry was missed outside the source slippage tolerance")
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        return await self.activate_waiting_entry(trade_id, plan, trade)


    async def check_waiting_entry(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if (
            ((marker := trade.get('observation_retry')))
            and (not marker.get('completed') and (not marker['exhausted']) and (not retry_due(marker)))
        ):
            return trade
        plan = TradePlan.model_validate(trade["plan"])
        snapshot = self.state.snapshot()
        if now_ms() >= plan.deadline_at or self.source_revisions(snapshot, plan.source_refs) != plan.source_revisions:
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry expired or its source changed")
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        changed = self.waiting_configuration_changed(plan, snapshot)
        if changed:
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry configuration changed")
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if snapshot["paused"] or any(row.get("protection_observation_pending") and row["status"] in OPEN for row in snapshot["trades"].values()):
            return trade
        try:
            self.validate_admission(snapshot, plan, owned=True)
        except AqlaError as error:
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason=str(error))
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        return await self.observe_waiting_entry(trade_id, trade, plan)

    async def fresh_entry_guard(self, plan, dispatch_operation, account, allowed, waverider, quote, targets):
        for attempt in range(2):
            self.validate_admission(self.state.snapshot(), plan, account, owned=True, allowed_prepared=allowed, allowed_sending=(dispatch_operation,), waverider=waverider)
            try:
                quote.fresh()
                # Leave time for atomic preparation; the transport's 2 s limit stays unchanged.
                if not attempt and now_ms() - min(quote.exchange_time, quote.started_at, quote.observed_at) > 1000:
                    raise TemporaryObservation("price_stale", "price")
                return account, quote
            except TemporaryObservation:
                if attempt:
                    raise
                # Local checks can age a valid quote; refresh once and recheck every guard.
                quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
                trade = self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
                validate_entry_prices(plan, quote, stop=trade["stop_price"], targets=targets)
                if waverider:
                    from .waverider import ended
                    if ended(plan, trade, quote):
                        raise AqlaError("Waverider endpoint was reached before reentry") from None


    async def live_entry_guard(self, plan, *, dispatch_operation=None, account=None, audit=False, waverider=False):
        # Refresh before the remaining checks can consume the existing 10 s account window.
        if account is None or not 0 <= now_ms() - account.observed_at <= 7000:
            account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
        snapshot = self.state.snapshot()
        allowed = (snapshot["trades"][plan.trade_id].get("leverage_operation_id", "leverage:" + plan.trade_id), snapshot["trades"][plan.trade_id]["entry_operation_id"])
        if dispatch_operation is not None:
            allowed = (*allowed, dispatch_operation)
        self.validate_admission(snapshot, plan, account, owned=True, allowed_prepared=allowed, allowed_sending=(dispatch_operation,), waverider=waverider)
        # Costly local persistence precedes the final price read. A quote audit
        # is retained, but its write cannot invalidate the final observation.
        if audit:
            self.patch_trade(plan.trade_id, entry_equity_snapshot={"equity": str(account.equity), "currency": plan.market.quote_token, "observed_at": account.observed_at})
        quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
        trade = self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
        targets = [value for index, value in enumerate(trade["targets"]) if trade.get("lifecycle_version") != 2 or index not in trade.get("completed_target_indices", [])]
        try:
            result = validate_entry_prices(plan, quote, stop=trade["stop_price"], targets=targets)
        except AqlaError as error:
            message = str(error)
            self.state.update(lambda data: data["trades"][plan.trade_id].setdefault("price_checks", []).append({"phase": "before_send", "snapshot": quote.model_dump(mode="json"), "error": message}))
            raise
        if audit:
            self.state.update(lambda data: data["trades"][plan.trade_id].setdefault("price_checks", []).append({"phase": "before_send", "snapshot": quote.model_dump(mode="json"), "result": result}))
        return await self.fresh_entry_guard(plan, dispatch_operation, account, allowed, waverider, quote, targets)

    async def guarded_entry_steps(self, plan, trade):
        try:
            try:
                return await self._entry_steps(plan, trade)
            except EntryBudgetChanged:
                plan, trade = await self.refresh_unsent_budget(plan)
                return await self._entry_steps(plan, trade)
        except TemporaryObservation as error:
            return self.retain_observation_retry(plan.plan_id, error)
        except AqlaError as error:
            operations = self.state.read("operations")["operations"]
            if trade["entry_operation_id"] in operations or any(row["phase"] in ("prepared", "sending", "unknown") for row in operations.values()):
                raise
            if self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]["status"] not in OPEN:
                raise
            self.patch_trade(plan.trade_id, status="rejected", reserved_risk="0", orders=[], unsent_reason=str(error), entry_diagnostic=diagnostic(error, "entry_preparation"))
            raise
        except Exception as error:
            operations = self.state.read("operations")["operations"]
            if trade["entry_operation_id"] not in operations and not any(row["phase"] in ("prepared", "sending", "unknown") for row in operations.values()):
                self.patch_trade(plan.trade_id, status="rejected", reserved_risk="0", orders=[], unsent_reason="Entry preparation failed before any entry send", entry_diagnostic=diagnostic(error, "entry_preparation"))
            raise

    async def validate_unsent_market(self, plan):
        current_market = await read_observation(lambda: self.exchange.market(plan.market.symbol, plan.market.product), "market")
        identity_fields = ("asset_id", "size_decimals", "quote_token", "price_tick", "quantity_step", "margin_leverage_scale")
        if any(getattr(current_market, key) != getattr(plan.market, key) for key in identity_fields) or plan.effective_leverage > current_market.max_leverage:
            raise AqlaError("Market identity, precision or leverage changed while entry was unsent")
        if (plan.quantity < current_market.min_quantity or plan.quantity * plan.entry_limit < current_market.min_notional
                or current_market.max_quantity is not None and plan.quantity > current_market.max_quantity
                or any(size < current_market.min_quantity or size * price < current_market.min_notional for size, price in zip(plan.tp_quantities, plan.take_profits, strict=True))):
            raise AqlaError("Exchange minimum/maximum order sizes changed while entry was unsent")


    async def complete_entry_dispatch(self, plan, result):
        if result["phase"] == "unknown":
            self.patch_trade(plan.trade_id, status="unknown")
        if result.get("outcome") == "not_sent":
            self.raise_temporary_not_sent(result)
            self.patch_trade(plan.trade_id, status="rejected", reserved_risk="0", unsent_reason=result["result"]["error"], orders=[])
            return self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
        self.complete_observation_retry(plan.trade_id)
        if any(leg["index"] == 1 and leg["disposition"] == "rejected" for leg in result.get("legs", [])):
            await self.cancel_entry(plan.trade_id)
        try:
            await self.refresh_trade(plan.trade_id)
        except ExternalInterference as error:
            self.patch_trade(plan.trade_id, status="conflicted", external_conflict=str(error))
        current = self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
        if current["status"] == "opening" and Decimal(current["filled_quantity"]) == 0 and result["phase"] in ("acknowledged", "reconciled"):
            self.state.update(lambda data: data["notifications"].setdefault("entry-waiting:" + plan.trade_id,
                {"trade_id": plan.trade_id, "change": {"status": "opening"}, "previous": {"status": "preparing"}, "observed_at": now_ms(),
                 "source_id": current["chat_id"], "source_name": data.get("source_names", {}).get(current["chat_id"])}))
        return current


    async def _entry_steps(self, plan, trade):
        if trade.get("lifecycle_version") == 2:
            return await managed_entries.start(self, plan, trade)
        await self.validate_unsent_market(plan)
        latest_account, latest_quote = None, None
        dispatch_operation = trade.get("leverage_operation_id", "leverage:" + plan.trade_id)
        async def live_guard():
            nonlocal latest_account, latest_quote
            latest_account, latest_quote = await self.live_entry_guard(plan, dispatch_operation=dispatch_operation, account=latest_account, audit=latest_quote is None)
        def invalidate_account():
            nonlocal latest_account
            latest_account = None
        live_guard.invalidate_account = invalidate_account
        def final_guard(data, operation_id):
            self.validate_admission(data, plan, latest_account, owned=True, allowed_prepared=(operation_id,), allowed_sending=(operation_id,))
            latest_quote.fresh()
        await live_guard()
        guards = {"async_prepare_guard": live_guard}
        if plan.market.product == "perp":
            leverage = await self.exchange.mutate(dispatch_operation,
                                                 {"type": "updateLeverage", "asset": plan.market.asset_id,
                                                  "isCross": plan.policy.margin_mode == "cross", "leverage": plan.effective_leverage}, deadline=plan.deadline_at,
                                                  prepare_guard=lambda data: final_guard(data, dispatch_operation), **guards)
            if leverage["phase"] not in ("acknowledged", "reconciled"):
                self.raise_temporary_not_sent(leverage)
                self.patch_trade(plan.trade_id, status="unknown" if leverage["phase"] == "unknown" else "rejected",
                            **({"reserved_risk": "0", "unsent_reason": leverage.get("result", {}).get("error", "Leverage configuration was rejected")} if leverage["phase"] == "rejected" else {}))
                return self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
        account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
        latest_account = account
        self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        current = self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]
        stop = Decimal(current["stop_price"])
        entry = self.order(plan, plan.quantity, execution_limit(plan), cloid=trade["entry_cloid"])
        if plan.entry_timing == "deferred" and plan.entry_execution == "standard":
            entry["t"] = {"limit": {"tif": "Ioc"}}
        initial_stop = self.order(plan, plan.quantity, stop * (Decimal("0.95") if plan.side == "long" else Decimal("1.05")),
                                  reduce=True, trigger=("sl", stop), cloid=self.exchange.cloid(trade["entry_operation_id"], 1))
        initial_stop["p"] = wire_number(price_grid(Decimal(initial_stop["p"]), plan.market, up=plan.side == "short"))
        action = {"type": "order", "orders": [entry, initial_stop], "grouping": "normalTpsl"}
        self.patch_trade(plan.trade_id, status="opening", orders=[{"kind": "sl", "cloid": initial_stop["c"], "quantity": str(plan.quantity), "price": str(stop)}])
        dispatch_operation = trade["entry_operation_id"]
        result = await self.exchange.mutate(trade["entry_operation_id"], action, deadline=plan.deadline_at,
                                            prepare_guard=lambda data: final_guard(data, trade["entry_operation_id"]), **guards)
        return await self.complete_entry_dispatch(plan, result)

    @staticmethod
    def raise_temporary_not_sent(operation):
        fact = (operation.get("result") or {}).get("diagnostic", {})
        if operation.get("phase") == "rejected" and operation.get("outcome") == "not_sent" and not operation.get("legs"):
            if fact.get("retryable"):
                raise TemporaryObservation(fact["code"], fact["stage"], fact["category"])
            if fact.get("exception_type") == "CancelledError" and fact.get("stage") in ("prepared_guard", "transport_guard"):
                raise TemporaryObservation("read_unavailable", fact["stage"], "transport")

    @staticmethod
    def entry_not_sent(snapshot, trade):
        entries = [trade["entry_operation_id"], *trade.get("entry_attempts", []),
                   *(row["operation_id"] for row in trade.get("entry_orders", []))]
        return (not Decimal(trade["filled_quantity"]) and not Decimal(trade["remaining_quantity"])
                and not any(row.get("kind") == "entry" for row in trade.get("fill_events", {}).values())
                and all(identity not in snapshot["operations"] or (snapshot["operations"][identity].get("outcome") == "not_sent"
                        and snapshot["operations"][identity]["phase"] == "rejected" and not snapshot["operations"][identity].get("legs")) for identity in entries))

    def complete_observation_retry(self, trade_id):
        def complete(data):
            trade = data["trades"][trade_id]
            if marker := trade.get("observation_retry"):
                marker.update(exhausted=True, completed=True)
                data["plans"][trade["plan"]["plan_id"]]["observation_retry"] = marker.copy()
        if self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("observation_retry"):
            self.state.update(complete)

    def retain_observation_retry(self, plan_id, error):
        def retain(data):
            saved = data["plans"][plan_id]
            plan = TradePlan.model_validate(saved["plan"])
            trade = data["trades"].get(plan.trade_id)
            if trade and not self.entry_not_sent(data, trade):
                raise error
            if any(row["phase"] in ("prepared", "sending", "unknown") for row in data["operations"].values()):
                raise error
            previous = trade.get("observation_retry") if trade else saved.get("observation_retry")
            marker = retry_marker(previous, error, plan.deadline_at, "execute")
            if not trade:
                self.validate_admission(data, plan)
                trade = self.new_trade(data, saved, plan)
                data["trades"][plan.trade_id] = trade
                for ref in plan.source_refs:
                    data["bindings"][ref] = {"trade_id": plan.trade_id, "kind": "entry"}
            trade.update(observation_retry=marker, entry_diagnostic=error.diagnostic(), orders=[],
                         status="rejected" if marker["exhausted"] else "waiting_entry" if plan.entry_timing == "deferred" and not trade.get("gate_activated_at") else "preparing",
                         reserved_risk="0" if marker["exhausted"] else str(plan.risk_usdc))
            saved.update(observation_retry=marker, execution_requested=True)
            data["events"][saved["event_id"]]["status"] = "executed"
            return trade
        return self.state.update(retain)

    def rearm_proven_unsent(self, trade_id):
        """New attempt IDs/nonces; old signed requests are retained verbatim."""
        def rearm(data):
            trade = data["trades"][trade_id]
            marker = trade["observation_retry"]
            if marker.get("authority") != "execute" or not retry_due(marker):
                return None
            if marker["attempts"] >= 5:
                raise AqlaError("Original entry observation retry budget exhausted")
            self.validate_admission(data, TradePlan.model_validate(trade["plan"]), owned=True)
            marker["attempts"] += 1
            marker["attempt_inflight"] = True
            for field, prefix in (("entry_operation_id", "entry"), ("leverage_operation_id", "leverage")):
                identity = trade.get(field, prefix + ":" + trade_id)
                prior = data["operations"].get(identity)
                if prior is None or prefix == "leverage" and prior["phase"] in ("acknowledged", "reconciled"):
                    continue
                if prior.get("outcome") != "not_sent" or prior["phase"] != "rejected" or prior.get("legs"):
                    raise AqlaError("Only a locally proven completely unsent entry may resume")
                history = "entry_attempts" if prefix == "entry" else "leverage_attempts"
                trade.setdefault(history, []).append(identity)
                identity = prefix + ":" + trade_id + ":observation:" + str(marker["attempts"])
                if identity in data["operations"]:
                    raise AqlaError("Observation attempt already has a durable request; reconcile it")
                trade[field] = identity
                if prefix == "entry":
                    trade["entry_cloid"] = self.exchange.cloid(identity)
            # An interrupted active attempt cannot immediately bypass the delay.
            marker["next_retry_at"] = min(now_ms() + 300000, marker["deadline_at"])
            data["plans"][trade["plan"]["plan_id"]]["observation_retry"] = marker.copy()
            return trade
        return self.state.update(rearm)

    async def resume_unsent_admission(self, snapshot, plan, trade, trade_id):
        self.assert_plan_binding(snapshot, plan)
        try:
            self.validate_admission(snapshot, plan, owned=True)
            market = await read_observation(lambda: self.exchange.market(plan.market.symbol, plan.market.product), "market")
            if (market.asset_id, market.size_decimals, market.quote_token) != (plan.market.asset_id, plan.market.size_decimals, plan.market.quote_token):
                raise AqlaError("Exchange market metadata changed after preview")
            if plan.effective_leverage > market.max_leverage:
                raise AqlaError("Exchange leverage limit decreased after preview; create a new unsent plan")
            if self.position_size(await self.positions(plan.market), plan.market) != 0:
                raise AqlaError("Unsent entry cannot resume against an existing foreign position")
            account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
            self.validate_admission(self.state.snapshot(), plan, account, owned=True)
        except TemporaryObservation as error:
            return self.retain_observation_retry(plan.plan_id, error)
        except EntryBudgetChanged:
            return await self.guarded_entry_steps(plan, trade)
        except AqlaError as error:
            if any(row["phase"] in ("prepared", "sending", "unknown") for row in self.state.read("operations")["operations"].values()):
                raise
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason=str(error))
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        return await self.guarded_entry_steps(plan, trade)


    async def resume_entry_observation_retry(self, trade_id, snapshot, trade, marker):
        operation = snapshot["operations"].get(trade["entry_operation_id"])
        if operation and operation.get("outcome") != "not_sent":
            self.complete_observation_retry(trade_id)
            return False, await self.refresh_trade(trade_id)
        if marker.get("completed"):
            return False, trade
        if marker.get("authority") != "execute" or trade["status"] not in OPEN:
            return False, trade
        if marker["exhausted"] or now_ms() >= marker["deadline_at"]:
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", orders=[], observation_retry={**marker, "exhausted": True}, unsent_reason="Original entry observation retry deadline/budget expired")
            return False, self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if not retry_due(marker):
            return False, trade
        try:
            trade = self.rearm_proven_unsent(trade_id)
            if trade is None:
                return False, self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        except AqlaError as error:
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", orders=[], observation_retry={**marker, "exhausted": True}, unsent_reason=str(error), entry_diagnostic=diagnostic(error, "retry_admission"))
            return False, self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        return True, trade


    async def resume_unsent_entry(self, trade_id):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        if not Decimal(trade["filled_quantity"]) and any(row.get("protection_observation_pending") and row["status"] in OPEN for row in snapshot["trades"].values()):
            return trade
        if marker := trade.get("observation_retry"):
            ready, trade = await self.resume_entry_observation_retry(trade_id, snapshot, trade, marker)
            if not ready:
                return trade
            snapshot = self.state.snapshot()
        if trade["status"] == "waiting_entry":
            return await self.check_waiting_entry(trade_id)
        plan = TradePlan.model_validate(trade["plan"])
        if trade["entry_operation_id"] in snapshot["operations"]:
            return await self.refresh_trade(trade_id)
        parent = trade.get("configuration_parent_id", trade.get("leverage_operation_id", "leverage:" + trade_id))
        configuration = snapshot["operations"].get(parent, {})
        if configuration.get("configuration_retry_needed") and configuration["phase"] in ("prepared", "sending", "unknown"):
            return await self.repair_unsent_configuration(plan, parent)
        return await self.resume_unsent_admission(snapshot, plan, trade, trade_id)

    def configuration_repair_guard(self, data, plan, parent, child, action, latest_account):
        current, prior = data["trades"][plan.trade_id], data["operations"][parent]
        if (prior["wire_action"] != action or Decimal(current["filled_quantity"]) or current.get("orders")
                or current.get("configuration_parent_id", parent) != parent
                or current.get("leverage_operation_id", parent) not in (parent, child)
                or any(key == "entry:" + plan.trade_id or key.startswith("entry:" + plan.trade_id + ":") for key in data["operations"])):
            raise AqlaError("Configuration correction requires the exact original goal and a completely unsent entry")
        pending = prior["phase"] in ("prepared", "sending", "unknown")
        proof = prior.get("configuration_retry_needed")
        if pending and (not proof or not 0 <= now_ms() - proof[0]["observed_at"] <= 10000):
            raise TemporaryObservation("account_stale", "configuration")
        # Only this non-exposure correction may pass its own unresolved
        # parent. Normal entry admission still sees the complete ledger.
        basis = {**data, "operations": {key: value for key, value in data["operations"].items() if not pending or key != parent}}
        self.validate_admission(basis, plan, latest_account, owned=True, allowed_prepared=(child,), allowed_sending=(child,))


    async def repair_unsent_configuration(self, plan, parent):
        child = "leverage:" + plan.trade_id + ":configuration:1"
        action = {"type": "updateLeverage", "asset": plan.market.asset_id,
                  "isCross": plan.policy.margin_mode == "cross", "leverage": plan.effective_leverage}
        latest_account = None
        def guard(data):
            self.configuration_repair_guard(data, plan, parent, child, action, latest_account)
        async def observe():
            nonlocal latest_account
            await self.exchange.reconcile(parent)
            latest_account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
            await read_observation(lambda: self.exchange.configuration_entry_clear(plan.market), "inventory")
            guard(self.state.snapshot())
        existing = self.state.read("operations")["operations"].get(child)
        if existing is None:
            await observe()
            def claim(data):
                guard(data)
                current = data["trades"][plan.trade_id]
                if current.get("configuration_retry") and current.get("leverage_operation_id") != child:
                    raise AqlaError("Configuration correction was already attempted")
                if not current.get("configuration_retry"):
                    current.setdefault("leverage_attempts", []).append(parent)
                current.update(configuration_retry=True, configuration_parent_id=parent, leverage_operation_id=child)
            self.state.update(claim)
            existing = await self.exchange.mutate(child, action, deadline=plan.deadline_at,
                                                 prepare_guard=guard, async_prepare_guard=observe)
        if existing["phase"] in ("acknowledged", "reconciled") and (await self.exchange.reconcile(parent))["resolved"]:
            # Entry has its own recovery-cycle time budget after this correction.
            self.patch_trade(plan.trade_id, status="preparing")
        return self.state.read("trades", entries={"trades": [plan.trade_id]})["trades"][plan.trade_id]

    @staticmethod
    def preserve_stop_revision(state, row, old, changed, preserve_pending_stop):
        pending = row.get("pending_stop_update")
        original_pending = old.get("pending_stop_update")
        if (preserve_pending_stop and "management_revision" in changed and pending and original_pending
                and pending["phase"] in STOP_UPDATE_ACTIVE and pending["management_revision"] == row["management_revision"]
                and all(pending[key] == original_pending[key] for key in ("stop", "source_binding", "operation_id"))
                and state["bindings"].get(pending["operation_id"]) == pending["source_binding"]):
            pending["management_revision"] = changed["management_revision"]

    @staticmethod
    def propagate_observation_retry(state, row, trade_id):
        if row["status"] in ("rejected", "cancelled") and row.get("observation_retry"):
            row["observation_retry"].update(exhausted=True, completed=True, attempt_inflight=False)
        if row.get("observation_retry"):
            state["plans"][row["plan"]["plan_id"]]["observation_retry"] = row["observation_retry"].copy()
            for target in state["events"][row["event_id"]].get("targets", {}).values():
                if target.get("trade_id") == trade_id or target.get("plan_id") == row["plan"]["plan_id"]:
                    target.update(status=row["status"], observation_retry=row["observation_retry"].copy())

    @staticmethod
    def visible_trade_changes(state, row, old, changed, trade_id):
        important = {key: value for key, value in changed.items() if key in ("status", "filled_quantity", "remaining_quantity", "stop_price", "tp_filled", "maker_target_rejection")}
        if row.get("lifecycle_version") and row["status"] == "closed":
            from .lifecycle import record_lifecycle
            record_lifecycle(state, trade_id, "closed")
            return {}
        if row.get("lifecycle_version") and old["status"] in ("preparing", "opening") and row["status"] == "protecting":
            important = {}
        if row.get("lifecycle_version") and row["status"] == "open" and old["status"] == "protecting":
            important = {key: value for key, value in important.items() if key != "status"}
        return important

    @staticmethod
    def announce_trade_change(state, row, old, changed, trade_id):
        important = Trading.visible_trade_changes(state, row, old, changed, trade_id)
        if important and row["status"] not in ("preparing", "opening"):
            identity = digest([trade_id, important])[:32]
            chat = state["trades"][trade_id]["chat_id"]
            state["notifications"].setdefault(identity, {"trade_id": trade_id, "change": important, "observed_at": now_ms(),
                "previous": {key: old.get(key) for key in important},
                "source_id": chat, "source_name": state.get("source_names", {}).get(chat)})

    def patch_trade(self, trade_id, *, preserve_pending_stop=False, **values):
        old = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        changed = {key: value for key, value in values.items() if old.get(key) != value}
        if not changed:
            return

        def patch(state):
            row = state["trades"][trade_id]
            self.preserve_stop_revision(state, row, old, changed, preserve_pending_stop)
            row.update(changed)
            self.propagate_observation_retry(state, row, trade_id)
            self.announce_trade_change(state, row, old, changed, trade_id)

        self.state.update(patch)

    _patch = patch_trade

    async def fill_history_for_plan(self, plan):
        result = {}
        pages = 0

        async def window(start, end):
            nonlocal pages
            pages += 1
            if pages > 64:
                raise AqlaError("Fill history requires more than the bounded page budget")
            rows = await read_observation(lambda: market_fills(self.exchange, plan.market, start, end), "fills")
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

    _fills = fill_history_for_plan

    async def capture_fills(self, trade_id, entry_status=None):
        with order_observations(self.exchange):
            return await self._capture_fills(trade_id, entry_status)

    @staticmethod
    def cached_terminal_order(trade, cloid):
        fact = trade.get("order_observations", {}).get(cloid, {})
        if trade.get("lifecycle_version") == 2 and fact.get("status") in TERMINAL_ORDER and fact.get("oid") is not None:
            return {"status": "order", "order": {"status": fact["status"], "order": {"oid": fact["oid"]}}}
        return None


    async def capture_wave_order_status(self, trade, order, status, reentering):
        if order.get("kind") not in ("waverider", "waverider_retirement"):
            return status
        active = order.get("phase") != "terminal" if reentering else order.get("operation_id") == trade.get("suspended_wave_targets")
        if not active:
            return status
        if status.get("order", {}).get("order", {}).get("cloid") is None:
            status = await self.order_status(order["cloid"])
        identity = order["operation_id"]
        operation = self.state.read("operations", entries={"operations": [identity]})["operations"].get(identity)
        return status if self.waves.cycle_native_order_matches(trade, identity, order["cloid"], operation, status, reentering) else None

    async def capture_managed_entry_identities(self, trade, trade_id, entry_status, terminal_cache, observed, mapping):
        for entry in managed_entries.entry_orders(trade):
            if entry.get("phase") == "pending" or entry.get("not_sent"):
                continue
            status = entry_status if entry["cloid"] == trade["entry_cloid"] else terminal_cache(entry["cloid"]) or await self.order_status(entry["cloid"])
            status = await self.capture_wave_order_status(trade, entry, status, reentering=True)
            if status is None:
                continue
            observed[entry["cloid"]] = status
            oid = status.get("order", {}).get("order", {}).get("oid")
            if oid is not None:
                mapping[oid] = {"kind": "entry", "entry_role": entry["kind"], "entry_leg": entry["index"]}
            from .accounting import entry_quantity_evidence
            confirmed = entry_quantity_evidence(status)
            if confirmed is not None:
                def confirm(data, cloid=entry["cloid"], quantity=confirmed):
                    row = require_first(row for row in data["trades"][trade_id]["entry_orders"] if row["cloid"] == cloid)
                    row["confirmed_filled"] = quantity
                    row["phase"] = "terminal"
                self.state.update(confirm)


    async def capture_order_identities(self, trade, plan, trade_id, entry_status):
        def terminal_cache(cloid):
            return self.cached_terminal_order(trade, cloid)
        entry_status = entry_status or terminal_cache(trade["entry_cloid"]) or await self.order_status(trade["entry_cloid"])
        observed = {trade["entry_cloid"]: entry_status}
        mapping = {}
        entry_oid = entry_status.get("order", {}).get("order", {}).get("oid")
        if entry_oid is not None:
            mapping[entry_oid] = {"kind": "entry"}
        if trade.get("lifecycle_version") == 2:
            await self.capture_managed_entry_identities(trade, trade_id, entry_status, terminal_cache, observed, mapping)
        for order in [*trade["orders"], *trade.get("exit_orders", [])]:
            if order.get("not_sent"):
                continue
            status = terminal_cache(order["cloid"]) or await self.order_status(order["cloid"])
            status = await self.capture_wave_order_status(trade, order, status, reentering=False)
            if status is None:
                continue
            observed[order["cloid"]] = status
            oid = status.get("order", {}).get("order", {}).get("oid")
            if oid is not None:
                mapping[oid] = order
        return entry_status, observed, mapping


    @staticmethod
    def retain_owned_fill(row, trade, mapping, owned):
        identity = digest([row["oid"], row.get("tid"), row.get("time"), row.get("sz"), row.get("px")])
        fact = {key: row.get(key) for key in ("oid", "tid", "time", "sz", "px", "fee", "feeToken", "coin")}
        fact.update(kind=mapping[row["oid"]]["kind"], index=mapping[row["oid"]].get("index"))
        if trade.get("lifecycle_version") == 2 and fact["kind"] == "entry":
            fact.update(entry_role=mapping[row["oid"]].get("entry_role", "original"), entry_leg=mapping[row["oid"]].get("entry_leg", 0))
        if trade.get("lifecycle_version") == 2 and fact["kind"] == "tp" and mapping[row["oid"]].get("pool") == "core":
            fact["exit_pool"] = "core"
        if identity in owned and owned[identity] != fact:
            raise AqlaError("An immutable owned fill changed; reconcile before managing quantities")
        owned[identity] = fact
        return identity


    async def capture_owned_fill_facts(self, trade, plan, mapping):
        owned = dict(trade.get("fill_events", {}))
        roles = dict(trade.get("fill_execution_roles", {}))
        conflicts = dict(trade.get("fill_execution_role_conflicts", {}))
        for row in await self.fill_history_for_plan(plan):
            if row.get("oid") not in mapping or row.get("coin") != plan.market.symbol:
                continue
            identity = self.retain_owned_fill(row, trade, mapping, owned)
            if all(row.get(key) is not None for key in ("oid", "tid", "time", "sz", "px")):
                merge_role(roles, conflicts, identity, native_role(getattr(self.exchange.settings, "venue", "hyperliquid"), row))
        return owned, roles, conflicts


    @staticmethod
    def completed_target_prices(trade, plan, target_fills):
        completed_prices = dict(trade.get("completed_target_prices", {}))
        if plan.policy.stop_management == "tp_ladder":
            # Freeze the effective target price before later source updates can change it.
            for index, budget in enumerate(trade.get("target_budgets", [])):
                if budget is not None and Decimal(budget) > 0 and target_fills[index] >= Decimal(budget):
                    completed_prices.setdefault(str(index), trade.get("targets", plan.take_profits)[index])
        return completed_prices


    def record_owned_fill_totals(self, trade_id, trade, plan, owned, roles, conflicts):
        entry_fills = [row for row in owned.values() if row["kind"] == "entry"]
        total = sum((Decimal(row["sz"]) for row in entry_fills), Decimal(0))
        cost = sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in entry_fills), Decimal(0))
        target_fills = [sum((Decimal(row["sz"]) for row in owned.values() if row["kind"] == "tp" and row["index"] == index), Decimal(0)) for index in range(len(plan.take_profits))]
        completed_prices = self.completed_target_prices(trade, plan, target_fills)
        self.patch_trade(trade_id, fill_events=owned, filled_quantity=str(total), tp_filled=[str(size) for size in target_fills],
                    fill_execution_roles=roles, **({"fill_execution_role_conflicts": conflicts} if conflicts else {}),
                    entry_vwap=str(cost / total) if total else trade["entry_vwap"],
                    **({"completed_target_prices": completed_prices} if plan.policy.stop_management == "tp_ladder" else {}))
        return total, cost


    def record_fill_stop_evidence(self, trade_id, trade, plan, total, observed):
        confirmed_stop = any(order["kind"] == "sl" and observed.get(order["cloid"], {}).get("order", {}).get("status") == "open"
                             and self.stop_matches(observed[order["cloid"]], plan, total, Decimal(trade["stop_price"]))
                             for order in trade["orders"]) if total else False
        if total:
            self.patch_trade(trade_id, entry_stop_confirmed=confirmed_stop)
        from .lifecycle import record_lifecycle
        if total and self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("lifecycle_version") and "entry_fill" not in self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("lifecycle_events", {}):
            self.state.update(lambda data: record_lifecycle(data, trade_id, "entry_fill"))


    def confirm_managed_fill_quantity(self, trade_id, total):
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        pending_evidence = False
        for entry in current["entry_orders"]:
            if entry.get("not_sent") or entry.get("confirmed_filled") is None:
                continue
            oid = current.get("order_observations", {}).get(entry["cloid"], {}).get("oid")
            entered = sum((Decimal(row["sz"]) for row in current["fill_events"].values() if row["kind"] == "entry" and row["oid"] == oid), Decimal(0))
            pending_evidence |= entered != Decimal(entry["confirmed_filled"])
        if pending_evidence:
            self.patch_trade(trade_id, quantity_evidence_pending=True)
            raise TemporaryObservation("read_unavailable", "entry_quantity", "transport")
        confirmed = sum((Decimal(row.get("confirmed_filled") or 0) for row in current["entry_orders"]), Decimal(0)) if managed_entries.entry_final(current) else None
        self.patch_trade(trade_id, entry_fill_quantity_confirmed=str(confirmed) if confirmed is not None else None)
        if confirmed is not None and confirmed != total:
            self.patch_trade(trade_id, quantity_evidence_pending=True)
            raise TemporaryObservation("read_unavailable", "entry_quantity", "transport")
        if confirmed is not None:
            self.patch_trade(trade_id, quantity_evidence_pending=False)


    async def _capture_fills(self, trade_id, entry_status=None):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        entry_status, observed, mapping = await self.capture_order_identities(trade, plan, trade_id, entry_status)
        owned, roles, conflicts = await self.capture_owned_fill_facts(trade, plan, mapping)
        total, cost = self.record_owned_fill_totals(trade_id, trade, plan, owned, roles, conflicts)
        self.observe_orders(trade_id, observed)
        self.record_fill_stop_evidence(trade_id, trade, plan, total, observed)
        from .accounting import entry_quantity_evidence
        from .monthly import accounting_facts
        confirmed_quantity = entry_quantity_evidence(entry_status)
        if confirmed_quantity is not None:
            self.patch_trade(trade_id, entry_fill_quantity_confirmed=confirmed_quantity)
        if trade.get("lifecycle_version") == 2:
            self.confirm_managed_fill_quantity(trade_id, total)
        self.patch_trade(trade_id, **accounting_facts(self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]))
        return total, cost

    def observe_orders(self, trade_id, observations):
        previous = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("order_observations", {})
        proposed = dict(previous)
        for identity, raw in observations.items():
            detail = raw.get("order", {})
            fact = {"status": detail.get("status") or raw.get("status", "unknown"), "oid": detail.get("order", {}).get("oid")}
            old = previous.get(identity, {})
            if {key: old.get(key) for key in fact} != fact:
                proposed[identity] = {**fact, "changed_at": now_ms()}
        if proposed != previous:
            self.patch_trade(trade_id, order_observations=proposed)

    def validate_owned_exposure(self, observed, owned, signed, plan):
        if observed > owned or (signed < 0 and plan.side == "long") or (signed > 0 and plan.side == "short" and plan.market.product == "perp"):
            if getattr(self.exchange.settings, "shared_account", False):
                raise ExternalInterference("Manual intervention changed this market; Aqla does not manage the mixed position")
            raise AqlaError("Exchange exposure differs from remaining owned fills; no automatic management")
        if observed < owned:
            if getattr(self.exchange.settings, "shared_account", False):
                raise ExternalInterference("Observed position is smaller than proved own fills; manual intervention requires review")
            raise AqlaError("Position and owned fills are inconsistent; preserve protection until fresh reconciliation")


    def owned_remaining(self, trade_id, signed):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        fills = trade.get("fill_events", {}).values()
        entered = sum((Decimal(row["sz"]) for row in fills if row["kind"] == "entry"), Decimal(0))
        exited = sum((Decimal(row["sz"]) for row in fills if row["kind"] != "entry"), Decimal(0))
        owned = max(Decimal(0), entered - exited)
        observed = abs(signed)
        if owned == 0 and entered:
            return Decimal(0)
        self.validate_owned_exposure(observed, owned, signed, plan)
        return observed

    async def observe_owned_remaining(self, trade_id, signed):
        try:
            remaining = self.owned_remaining(trade_id, signed)
        except AqlaError:
            trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
            try:
                pending = await self.waves.exit_fill_history_pending(self, trade_id, signed)
            except TemporaryObservation:
                self.patch_trade(trade_id, exit_quantity_evidence_pending=True)
                raise
            if pending:
                self.patch_trade(trade_id, exit_quantity_evidence_pending=True)
                raise TemporaryObservation("read_unavailable", "exit_quantity", "transport") from None
            raise
        return remaining


    async def effective_stop_coverage(self, trade_id, plan, remaining):
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        covered = not getattr(self.exchange, "fail_closed_protection", False)
        if not covered:
            for order in current["orders"]:
                if order["kind"] == "sl":
                    observed = await self.order_status(order["cloid"])
                    covered |= observed.get("order", {}).get("status") == "open" and self.stop_matches(observed, plan, remaining, Decimal(current["stop_price"]))
        return current, covered


    async def refresh_position_protection(self, trade_id, plan, remaining):
        if not await self.finalize_backup(trade_id):
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if not await self.finalize_source_targets(trade_id):
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        await self.validate_actual_entry(trade_id)
        await self.ensure_protection(trade_id)
        current, covered = await self.effective_stop_coverage(trade_id, plan, remaining)
        if not covered and current["status"] not in ("closing", "cleanup", "closed"):
            raise AqlaError("No confirmed effective stop loss")
        if covered and current["status"] == "open" and current.get("protection_observation_pending"):
            self.patch_trade(trade_id, protection_observation_pending=None, waiting_error=None)


    def handle_protection_failure(self, trade_id, plan, error):
        if not getattr(self.exchange, "fail_closed_protection", False) and not set(plan.backup_fields) & {"stop_loss", "take_profits"}:
            raise error
        reason = "Protection could not be confirmed; emergency close required: " + (str(error) if isinstance(error, AqlaError) else type(error).__name__)
        self.patch_trade(trade_id, execution_incident=reason)
        if hasattr(self.state, "pause_entries"):
            self.state.pause_entries(reason)
        else:
            self.state.update(lambda data: data.update(paused=True))


    async def refresh_filled_position(self, trade_id, trade, plan, total, cost, remaining):
        pending = trade.get("pending_stop_update")
        closing_pending = trade["status"] == "closing" and pending and pending["phase"] in STOP_UPDATE_ACTIVE
        self.patch_trade(trade_id, filled_quantity=str(total), remaining_quantity=str(remaining), entry_vwap=str(cost / total),
                    status="closing" if closing_pending else "open" if trade["status"] == "open" else "protecting")
        if closing_pending:
            await self.resume_pending_stop(trade_id)
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        try:
            return await self.refresh_position_protection(trade_id, plan, remaining)
        except ExternalInterference:
            raise
        except TemporaryObservation as error:
            self.recovery_checked = False
            self.patch_trade(trade_id, protection_observation_pending=error.diagnostic(),
                        waiting_error="Protection observation unavailable; existing orders retained pending reconciliation")
            raise
        except StopEvidencePending as error:
            self.patch_trade(trade_id, status="unknown", waiting_error=str(error))
            if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
                self.stop_phase(trade_id, "waiting_fill", str(error))
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        except Exception as error:
            self.handle_protection_failure(trade_id, plan, error)
            return await self.close_position(trade_id, Decimal(100), "protection-failure:" + trade_id)


    def clear_quantity_evidence(self, trade_id, total):
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if (current.get("quantity_evidence_pending") and current.get("entry_fill_quantity_confirmed") is not None
                and Decimal(current["entry_fill_quantity_confirmed"]) == total):
            self.patch_trade(trade_id, quantity_evidence_pending=False, waiting_error=None)


    async def refresh_unfilled_entry(self, trade_id, plan, stage, total, remaining):
        if stage == "filled" and not total:
            self.patch_trade(trade_id, status="unknown", fill_evidence_pending=True)
        elif stage in TERMINAL_ORDER and not remaining:
            if not total:
                self.patch_trade(trade_id, unsent_reason="Entry order terminated without any confirmed fill")
            self.begin_cleanup(trade_id, "closed" if total else "rejected")
            await self.finish_cleanup(trade_id)
        elif remaining and not total:
            self.patch_trade(trade_id, status="unknown")
            raise AqlaError("Position exists but its entry fills are not proved; new entries remain blocked")
        elif now_ms() >= plan.deadline_at and stage == "open":
            await self.cancel_entry(trade_id)
            return await self.refresh_trade(trade_id)


    async def refresh_legacy_trade(self, trade_id, trade):
        plan = TradePlan.model_validate(trade["plan"])
        entry_status = await self.order_status(trade["entry_cloid"])
        total, cost = await self.capture_fills(trade_id, entry_status)
        stage = entry_status.get("order", {}).get("status")
        if total and stage == "open":
            # Freeze the actual fill budget before installing partial target legs.
            await self.cancel_entry(trade_id)
            entry_status = await self.order_status(trade["entry_cloid"])
            stage = entry_status.get("order", {}).get("status")
            total, cost = await self.capture_fills(trade_id, entry_status)
        signed = self.position_size(await self.positions(plan.market), plan.market)
        remaining = self.owned_remaining(trade_id, signed) if total else abs(signed)
        self.clear_quantity_evidence(trade_id, total)
        if total and remaining:
            result = await self.refresh_filled_position(trade_id, trade, plan, total, cost, remaining)
            if result is not None:
                return result
        else:
            result = await self.refresh_unfilled_entry(trade_id, plan, stage, total, remaining)
            if result is not None:
                return result
        await self.resume_pending_stop(trade_id)
        return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]


    async def refresh_trade(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        self.assert_plan_binding(self.state.read("network"), TradePlan.model_validate(trade["plan"]))
        if not getattr(self, "_sequence_running", False) and any(not row.get("completed") for row in trade.get("management_sequences", {}).values()):
            from .management_sequences import resume
            await resume(self, trade_id)
            trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade["status"] in OPEN and not await self.resume_close_intents(trade_id):
            return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("lifecycle_version") == 2:
            return await managed_entries.refresh(self, trade_id)
        if trade["status"] == "cleanup":
            return await self.finish_cleanup(trade_id)
        return await self.refresh_legacy_trade(trade_id, trade)

    @staticmethod
    def actual_entry_structure(plan, stop, entry, quote, targets):
        safe = (stop < min(entry, quote.stop_reference) and all(price > entry for price in targets) if plan.side == "long"
                else stop > max(entry, quote.stop_reference) and all(price < entry for price in targets))
        return safe


    def other_reserved_risk(self, trade_id):
        active = [row for identity, row in self.state.read("trades")["trades"].items() if identity != trade_id and row["status"] in OPEN]
        other_risk = sum((Decimal(row["reserved_risk"]) for row in active), Decimal(0))
        return other_risk


    def actual_entry_limits(self, trade_id, trade, plan, entry, size, stop, targets, account, quote):
        accepted_equity = Decimal(trade.get("entry_equity_snapshot", {}).get("equity", str(account.equity)))
        safe = self.actual_entry_structure(plan, stop, entry, quote, targets)
        notional = entry * size
        risk = size * (abs(entry - stop) + entry * plan.policy.cost_reserve_bps / 10000)
        other_risk = self.other_reserved_risk(trade_id)
        limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
        within_entry_band = (plan.entry_timing != "deferred" or
                             plan.entry_price * (1 - plan.policy.max_slippage_percent / 100) <= entry <= plan.entry_price * (1 + plan.policy.max_slippage_percent / 100))
        target_sizes = split_targets(size, len(targets), plan.policy, plan.market)
        targets_feasible = all(amount >= plan.market.min_quantity and amount * price >= plan.market.min_notional for amount, price in zip(target_sizes, targets, strict=True))
        permitted = (safe and within_entry_band and targets_feasible and notional <= plan.policy.max_notional_usdc and risk <= plan.risk_usdc
                     and risk <= accepted_equity * plan.policy.risk_percent / 100
                     and risk + other_risk <= account.equity * limits.max_total_risk_percent / 100)
        return permitted, notional, risk, accepted_equity


    async def validate_actual_entry(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("lifecycle_version") == 2:
            return await managed_entries.validate_actual(self, trade_id)
        if not trade.get("lifecycle_version"):
            return
        entries = {key: row for key, row in trade["fill_events"].items() if row["kind"] == "entry"}
        fingerprint = digest(entries)
        if trade.get("entry_validated_fingerprint") == fingerprint:
            return
        plan = TradePlan.model_validate(trade["plan"])
        entry, size, stop = Decimal(trade["entry_vwap"]), Decimal(trade["filled_quantity"]), Decimal(trade["stop_price"])
        targets = [Decimal(price) for price in trade["targets"]]
        account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
        quote = await read_observation(lambda: self.exchange.price_snapshot(plan.market), "price")
        permitted, notional, risk, accepted_equity = self.actual_entry_limits(trade_id, trade, plan, entry, size, stop, targets, account, quote)
        check = {"phase": "after_fill", "snapshot": quote.model_dump(mode="json"), "entry_vwap": str(entry),
                 "notional": str(notional), "risk": str(risk), "permitted": bool(permitted),
                 "accepted_equity": str(accepted_equity), "observed_equity": str(account.equity)}
        self.state.update(lambda data: data["trades"][trade_id].setdefault("price_checks", []).append(check))
        if not permitted:
            raise AqlaError("Confirmed entry violates price structure, position value or SL risk limits")
        self.patch_trade(trade_id, entry_validated_fingerprint=fingerprint)
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if current.get("entry_accounting") and not current.get("initial_sl_risk"):
            self.patch_trade(trade_id, initial_sl_risk={"amount": str(size * abs(entry - stop)), "entry": str(entry), "stop": str(stop),
                         "quantity": str(size), "currency": plan.market.quote_token, "entry_fingerprint": current["entry_accounting"]["entry_fingerprint"]})

    async def ensure_stop_coverage(self, trade_id, trade, plan, remaining):
        status_rows = [(order, await self.order_status(order["cloid"])) for order in trade["orders"] if order["kind"] == "sl"]
        covered = any(row.get("order", {}).get("status") == "open" and self.stop_matches(row, plan, remaining, Decimal(trade["stop_price"])) for _, row in status_rows)
        if trade.get("lifecycle_version") == 2:
            covered = managed_entries.protected(self, trade, status_rows)
        if not covered:
            await self.cancel_entry(trade_id)
            await self.replace_stop(trade_id, Decimal(trade["stop_price"]), initial_protection=True)
            refreshed = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            if refreshed["status"] in ("protecting", "closing"):
                return
        return True


    async def ready_protection_targets(self, trade_id):
        try:
            targets_ready = await self.resume_target_update(trade_id)
        except ExternalInterference:
            raise
        except TemporaryObservation:
            raise
        except Exception as error:
            detail = str(error) if isinstance(error, AqlaError) else type(error).__name__
            self.patch_trade(trade_id, status="protecting", waiting_error="Pending target replacement needs review: " + detail)
            return
        if not targets_ready:
            self.patch_trade(trade_id, status="protecting")
            return
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("suspended_wave_targets"):
            self.patch_trade(trade_id, status="protecting")
            return
        return trade


    def stage_initial_targets(self, trade_id, trade, plan):
        sizes = split_targets(Decimal(trade["remaining_quantity"]), len(plan.take_profits), plan.policy, plan.market)
        if trade.get("lifecycle_version") == 2 and trade.get("target_budgets"):
            sizes = [max(Decimal(0), Decimal(budget) - Decimal(spent)) for budget, spent in zip(trade["target_budgets"], trade["tp_filled"], strict=True)]
        generation = trade.get("managed_target_generation", 0) if trade.get("lifecycle_version") == 2 else 0
        operation = f"targets:{trade_id}:{generation}"
        indices = [index for index, size in enumerate(sizes) if size > 0]
        orders = [self.order(plan, sizes[i], Decimal(trade.get("targets", plan.take_profits)[i]), reduce=True, take_profit=True, cloid=self.exchange.cloid(operation, i)) for i in indices]
        def stage_initial(state):
            row = state["trades"][trade_id]
            row["orders"].extend({"kind": "tp", "cloid": order["c"], "quantity": order["s"], "price": order["p"], "index": i} for i, order in zip(indices, orders, strict=True))
            if trade.get("lifecycle_version") != 2:
                row["target_budgets"] = [str(size) for size in sizes]
            if previous := row.get("initial_targets"):
                row.setdefault("initial_target_history", {})[previous["operation_id"]] = previous
            row["initial_targets"] = {"operation_id": operation, "action": {"type": "order", "orders": orders, "grouping": "na"}}
        self.state.update(stage_initial)


    async def dispatch_initial_targets(self, trade_id, plan, staged):
        if staged and staged["operation_id"] not in self.state.read("operations")["operations"]:
            result = await self.exchange.mutate(staged["operation_id"], staged["action"])
            if result["phase"] == "rejected" and "take_profits" in plan.backup_fields and plan.take_profit_execution != "maker_only":
                raise AqlaError("Backup TP order was rejected")
            if result["phase"] not in ("acknowledged", "reconciled"):
                self.record_maker_target_rejection(trade_id, plan, result)
                self.patch_trade(trade_id, status="protecting")
                return
        return True


    @staticmethod
    def maker_targets_confirmed(plan, active_targets, observations):
        confirmed = all(row.get("status") == "order"
                        and row.get("order", {}).get("order", {}).get("cloid") == order["cloid"]
                        and row["order"]["order"].get("coin") == plan.market.symbol
                        and row["order"]["order"].get("side") == ("A" if plan.side == "long" else "B")
                        and row["order"]["order"].get("reduceOnly") is (plan.market.product == "perp")
                        and row["order"]["order"].get("isTrigger") is False
                        and Decimal(row["order"]["order"].get("origSz") or "0") == Decimal(order["quantity"])
                        for order, row in zip(active_targets, observations, strict=True))
        return confirmed


    async def observe_active_targets(self, trade_id, plan, active_targets, current_operations, active_cloids):
        observations = [await self.order_status(order["cloid"]) for order in active_targets]
        if "take_profits" in plan.backup_fields and plan.take_profit_execution != "maker_only" and any(row.get("order", {}).get("status") == "rejected" for row in observations):
            raise AqlaError("Backup TP could not be installed")
        if any(row.get("order", {}).get("status") not in {"open", "filled", "triggered"} for row in observations):
            rejected = {order["cloid"] for order, row in zip(active_targets, observations, strict=True)
                        if row.get("order", {}).get("status") == "rejected"}
            for operation in current_operations:
                self.record_maker_target_rejection(trade_id, plan, operation, rejected_cloids=rejected)
            self.patch_trade(trade_id, status="protecting")
            return
        # An ACK does not resolve a rejection. Only positive observations of
        # every current own TP identity can resolve the active incident.
        if plan.take_profit_execution == "maker_only":
            confirmed = self.maker_targets_confirmed(plan, active_targets, observations)
            if not confirmed:
                self.patch_trade(trade_id, status="protecting")
                return
            incident = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("maker_target_rejection")
            if not isinstance(incident, dict) or not active_cloids.intersection(incident["target_cloids"]):
                self.patch_trade(trade_id, maker_target_rejection=None)
        return True


    async def confirm_active_targets(self, trade_id, plan, staged):
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        active_targets = [order for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")]
        active_cloids = {order["cloid"] for order in active_targets}
        generations = {**({staged["operation_id"]: staged} if staged else {}), **trade.get("target_updates", {})}
        current_operations = [snapshot["operations"].get(operation, {}) for operation, generation in generations.items()
                              if any(order["c"] in active_cloids for order in (generation.get("action") or {}).get("orders", []))]
        for operation in current_operations:
            self.record_maker_target_rejection(trade_id, plan, operation)
        if active_targets:
            return await self.observe_active_targets(trade_id, plan, active_targets, current_operations, active_cloids)
        return True


    async def advance_break_even(self, trade_id, trade, plan):
        stage = plan.policy.break_even_after_tp
        if stage is not None:
            current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
            budgets = [Decimal(value) if value is not None else Decimal(0) for value in current.get("target_budgets", [])]
            filled = [Decimal(value) for value in current["tp_filled"]]
            if stage <= len(budgets) and all(0 < budgets[i] <= filled[i] for i in range(stage)):
                entry = managed_entries.current_reference(trade)
                tighter = entry > Decimal(trade["stop_price"]) if plan.side == "long" else entry < Decimal(trade["stop_price"])
                if tighter:
                    await self.replace_stop(trade_id, entry, "break-even:" + trade_id + ":" + str(stage))


    async def resize_legacy_targets(self, trade_id, trade, plan):
        if (trade is None or trade.get("lifecycle_version") == 2
                or trade.get("pending_stop_update", {}).get("phase") in STOP_UPDATE_ACTIVE
                or not any(order["kind"] == "tp" and not order.get("retired") for order in trade["orders"])):
            return trade
        budgets = trade.get("target_budgets") or plan.tp_quantities
        if any(budget is None for budget in budgets):
            return trade
        outstanding = sum(max(Decimal(0), Decimal(budget) - Decimal(spent))
                          for budget, spent in zip(budgets, trade["tp_filled"], strict=True))
        if outstanding <= Decimal(trade["remaining_quantity"]):
            return trade
        identity = "resize-targets:" + trade_id + ":" + digest([trade["remaining_quantity"], trade["tp_filled"], trade["targets"]])
        await self.replace_targets(trade_id, list(map(Decimal, trade["targets"])), identity)
        trade = await self.ready_protection_targets(trade_id)
        if trade is not None and Decimal(trade["remaining_quantity"]) > 0:
            return trade
        return None


    async def ensure_protection(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        remaining = Decimal(trade["remaining_quantity"])
        if remaining <= 0:
            return
        if not await self.finalize_backup(trade_id):
            return
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if not await self.ensure_stop_coverage(trade_id, trade, plan, remaining):
            return
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade["status"] == "closing":
            return
        trade = await self.ready_protection_targets(trade_id)
        trade = await self.resize_legacy_targets(trade_id, trade, plan)
        if trade is None:
            return
        if not any(order["kind"] == "tp" and not order.get("retired") for order in trade["orders"]):
            self.stage_initial_targets(trade_id, trade, plan)
        staged = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("initial_targets")
        if not await self.dispatch_initial_targets(trade_id, plan, staged):
            return
        if not await self.confirm_active_targets(trade_id, plan, staged):
            return
        self.patch_trade(trade_id, status="open")
        pending = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("pending_stop_update")
        if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
            return
        if plan.policy.stop_management == "tp_ladder":
            await self.advance_stop_ladder(trade_id)
            return
        await self.advance_break_even(trade_id, trade, plan)

    @staticmethod
    def final_source_target_prices(trade, plan, specification):
        from .percent_targets import prices
        reference = specification["reference_entry"] or trade["entry_vwap"]
        targets = prices(reference, plan.side, plan.market, specification["unit"], specification["values"], specification.get("margin_leverage") if specification["unit"] == "roe_percent" else specification["signal_leverage"])
        entry = Decimal(trade["entry_vwap"])
        if any(value <= entry if plan.side == "long" else value >= entry for value in targets):
            raise AqlaError("Source percentage targets are no longer profitable relative to the executed entry")
        sizes = split_targets(Decimal(trade["filled_quantity"]), len(targets), plan.policy, plan.market)
        if any(size < plan.market.min_quantity or size * price < plan.market.min_notional for size, price in zip(sizes, targets, strict=True)):
            raise AqlaError("Final source percentage TP is below exchange minimum size/notional")
        final = {**specification, "reference_entry": str(reference), "entry_vwap": trade["entry_vwap"],
                 "filled_quantity": trade["filled_quantity"], "prices": [str(value) for value in targets], "phase": "applied"}
        return final


    async def finalize_source_targets(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("lifecycle_version") == 2:
            return managed_entries.finalize_prices(self, trade_id)
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
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if not trade.get("entry_accounting"):
            raise AqlaError("Source percentage targets require confirmed final entry fills")
        final = self.final_source_target_prices(trade, plan, specification)
        self.patch_trade(trade_id, percent_finalization=final, targets=final["prices"])
        return True

    @staticmethod
    def final_backup_prices(trade, plan, reference):
        stop, targets = backup_prices(reference, plan.side, plan.market,
            plan.backup_values.fallback_stop_loss_percent if "stop_loss" in plan.backup_fields else None,
            selected_targets(plan.backup_values.fallback_take_profit_percentages, plan.policy)[:len(plan.take_profits)] if "take_profits" in plan.backup_fields else (),
            stop_unit=plan.backup_values.fallback_stop_loss_unit,
            take_profit_unit=plan.backup_values.fallback_take_profit_unit, leverage=plan.effective_leverage)
        stop = stop if stop is not None else Decimal(trade["stop_price"])
        targets = targets if "take_profits" in plan.backup_fields else [Decimal(value) for value in trade["targets"]]
        if not (stop < reference < min(targets) if plan.side == "long" else stop > reference > max(targets)):
            raise AqlaError("Final Backup prices have invalid direction")
        sizes = split_targets(Decimal(trade["filled_quantity"]), len(targets), plan.policy, plan.market)
        if any(size < plan.market.min_quantity or size * price < plan.market.min_notional for size, price in zip(sizes, targets, strict=True)):
            raise AqlaError("Final Backup TP is below the exchange minimum")
        return stop, targets


    async def final_backup_risk(self, trade_id, plan, remaining, reference, stop):
        account = await read_observation(lambda: self.exchange.account_snapshot(plan.market), "account")
        snapshot = self.state.snapshot()
        limits = self.state.limits(plan) if hasattr(self.state, "limits") else plan.policy
        others = sum(Decimal(row["reserved_risk"]) for identity, row in snapshot["trades"].items() if identity != trade_id and row["status"] in OPEN)
        risk = remaining * (abs(reference - stop) + reference * plan.policy.cost_reserve_bps / 10000)
        if (risk > plan.risk_usdc or risk > account.equity * plan.policy.risk_percent / 100
                or risk + others > account.equity * limits.max_total_risk_percent / 100
                or remaining * reference > plan.policy.max_notional_usdc or now_ms() - account.observed_at > 10000
                or account.position_size != (remaining if plan.side == "long" else -remaining)):
            raise AqlaError("Final Backup protection exceeds the confirmed risk/notional budget")
        return risk


    async def prepare_final_backup(self, trade_id, plan):
        await self.cancel_entry(trade_id)
        await self.capture_fills(trade_id)
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        reference = Decimal(trade["entry_vwap"])
        remaining = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        if not remaining:
            return None
        stop, targets = self.final_backup_prices(trade, plan, reference)
        risk = await self.final_backup_risk(trade_id, plan, remaining, reference, stop)
        final = {"phase": "prepared", "reference_entry": str(reference), "filled_quantity": trade["filled_quantity"], "stop_loss": str(stop),
                 "take_profits": [str(value) for value in targets], "risk": str(risk), "stop_operation_id": "backup-stop:" + trade_id,
                 "percentages": plan.backup_values.model_dump(mode="json")}
        self.patch_trade(trade_id, backup_finalization=final)
        return final


    async def finalize_backup(self, trade_id):
        """One durable fill reference, before TP intent construction or SL replacement."""
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("lifecycle_version") == 2:
            return managed_entries.finalize_prices(self, trade_id)
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
            final = await self.prepare_final_backup(trade_id, plan)
            if final is None:
                return False
        if "stop_loss" in plan.backup_fields:
            result = await self.replace_stop(trade_id, Decimal(final["stop_loss"]), final["stop_operation_id"], initialize_backup=True)
            if result.get("replacement_unconfirmed") or result.get("already_closed") or self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["status"] in ("closing", "cleanup", "closed"):
                return False
        self.patch_trade(trade_id, targets=final["take_profits"], reserved_risk=final["risk"], backup_finalization={**final, "phase": "applied"})
        return True

    @staticmethod
    def reached_stop_stages(trade):
        budgets = [Decimal(value) if value is not None else Decimal(0) for value in trade.get("target_budgets", [])]
        fills = [Decimal(value) for value in trade["tp_filled"]]
        reached = [stage for stage in (1, 3, 4) if stage <= len(budgets) and all(
            0 < budgets[index] <= fills[index] for index in range(stage))]
        return reached


    @staticmethod
    def stop_ladder_price(trade, stage, intents):
        if str(stage) in intents:
            price = Decimal(intents[str(stage)])
        elif stage == 1:
            price = managed_entries.current_reference(trade)
        else:
            index = 0 if stage == 3 else 1
            reference = trade.get("completed_target_prices", {}).get(str(index))
            if reference is None:
                raise AqlaError("Completed TP reference is not proved; preserve existing stop")
            price = Decimal(reference)
        return price


    async def advance_stop_ladder(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        reached = self.reached_stop_stages(trade)
        if not reached:
            return
        confirmed = trade.get("stop_ladder_stage", 0)
        intents = trade.get("stop_ladder_intents", {})
        pending = sorted(int(stage) for stage in intents if int(stage) > confirmed)
        stage = pending[0] if pending else max(reached)
        if stage <= confirmed:
            return
        price = self.stop_ladder_price(trade, stage, intents)
        if str(stage) not in intents:
            self.state.update(lambda data: data["trades"][trade_id].setdefault("stop_ladder_intents", {}).__setitem__(str(stage), str(price)))
        tighter = price > Decimal(trade["stop_price"]) if plan.side == "long" else price < Decimal(trade["stop_price"])
        if tighter:
            await self.replace_stop(trade_id, price, "tp-ladder:" + trade_id + ":" + str(stage))
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        effective = Decimal(current["stop_price"]) >= price if plan.side == "long" else Decimal(current["stop_price"]) <= price
        if current["status"] == "open" and effective:
            self.patch_trade(trade_id, stop_ladder_stage=stage)

    @staticmethod
    def stop_matches(status, plan, size, stop):
        order = status.get("order", {}).get("order", {})
        return (order.get("coin") == plan.market.symbol and order.get("isTrigger") is True
                and order.get("reduceOnly") is (plan.market.product == "perp")
                and (Decimal(order.get("sz", "0")) == size if plan.entry_execution == "maker_only" else Decimal(order.get("sz", "0")) >= size)
                and Decimal(order.get("triggerPx", "0")) == stop
                and order.get("side") == ("A" if plan.side == "long" else "B"))

    _stop_matches = stop_matches

    async def cancel_entry(self, trade_id, *, keep_waves=False):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("lifecycle_version") == 2:
            return await managed_entries.cancel(self, trade_id, permanent=not keep_waves)
        if trade["status"] == "waiting_entry":
            self.patch_trade(trade_id, status="cancelled", reserved_risk="0", unsent_reason="Waiting entry cancelled by request")
            return {"cancelled_waiting_entry": True}
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        if trade["entry_operation_id"] not in self.state.read("operations")["operations"]:
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
        status = await self.order_status(trade["entry_cloid"])
        if status.get("order", {}).get("status") in TERMINAL_ORDER:
            return status
        result = await self.exchange.mutate("cancel-entry:" + trade_id,
                                            {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": trade["entry_cloid"]}]})
        status = await self.order_status(trade["entry_cloid"])
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
        if request.event_id.startswith("operator:"):
            data["operator_requests"][request.event_id].update(status="pending" if phase in STOP_UPDATE_ACTIVE else "done" if phase == "confirmed" else "superseded" if phase in ("superseded", "closed_without_application") else "rejected",
                                                             stop_update_reason=reason)
        else:
            data["events"][request.event_id].update(status=status, stop_update_reason=reason)
        identity = "stop-update:" + digest([request.operation_id, phase, reason])[:32]
        data["notifications"].setdefault(identity, {"trade_id": trade_id, "event_id": request.event_id,
            "stop_update": saved, "observed_at": now_ms()})
        return saved

    def stop_phase(self, trade_id, phase: StopUpdatePhase, reason=None):
        pending = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"]
        if pending["phase"] == phase and pending.get("reason") == reason:
            return pending
        return self.state.update(lambda data: self.write_stop_phase(data, trade_id,
            data["trades"][trade_id]["pending_stop_update"], phase, reason))

    @staticmethod
    def validate_stop_budget(trade, plan, stop, size=None, *, initialize_backup=False):
        old_stop = Decimal(trade["stop_price"])
        widening = stop < old_stop if plan.side == "long" else stop > old_stop
        if widening and (not plan.policy.allow_stop_widening or plan.policy.management_mode == "waverider") and not initialize_backup:
            raise AqlaError("Stop widening is not permitted")
        size = Decimal(trade["remaining_quantity"]) if size is None else size
        reference = Decimal(trade["entry_vwap"]) if trade["entry_vwap"] is not None else plan.entry_price
        if not Decimal(trade["filled_quantity"]):
            size = plan.quantity
        budget = min(plan.risk_usdc, Decimal(trade["reserved_risk"]))
        pending = trade.get("pending_stop_update")
        if pending and pending["phase"] in STOP_UPDATE_ACTIVE:
            budget = min(budget, Decimal(pending["original_budget"]))
        if widening and size * (abs(reference - stop) + reference * plan.policy.cost_reserve_bps / 10000) > budget:
            raise AqlaError("Stop widening exceeds the original loss budget")

    @staticmethod
    def validate_final_stop_quantity(trade):
        confirmed = trade.get("entry_fill_quantity_confirmed")
        if confirmed is not None and Decimal(confirmed) != Decimal(trade["filled_quantity"]):
            raise StopEvidencePending("Terminal entry quantity and own fills are not yet reconciled; preserve original protection")

    def accept_stop_update(self, data, trade_id, trade, plan, signal, request, binding):
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


    async def queue_stop_update(self, trade_id, event_id, signal, proposed, *, operation_id=None):
        origin = self.state.snapshot()
        source_revisions = self.source_revisions(origin, signal.source_refs)
        trade = origin["trades"][trade_id]
        old = trade.get("pending_stop_update")
        if old and old["phase"] in STOP_UPDATE_ACTIVE:
            operation = self.state.read("operations")["operations"].get(old["operation_id"])
            if old["phase"] == "unknown" or operation and operation.get("outcome") != "not_sent":
                await self.resume_pending_stop(trade_id)
                old = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"]
                if old["phase"] in STOP_UPDATE_ACTIVE:
                    raise AqlaError("Reconcile the original pending stop operation before another stop instruction")
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        management_revision = trade["management_revision"]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        stop = price_grid(proposed, plan.market, up=plan.side == "long")
        if trade.get("lifecycle_version") == 2:
            await self.cancel_entry(trade_id, keep_waves=plan.policy.management_mode == "waverider")
        await self.capture_fills(trade_id)
        size = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        if trade["management_revision"] != management_revision or self.source_revisions(snapshot, signal.source_refs) != source_revisions:
            raise AqlaError("Source or management revision changed before stop acceptance")
        if signal.update_action == "break_even":
            if trade["entry_vwap"] is None:
                raise AqlaError("Break-even requires a proved entry VWAP")
            stop = price_grid(managed_entries.current_reference(trade), plan.market, up=plan.side == "long")
        self.validate_stop_budget(trade, plan, stop, size)
        binding = StopSourceBinding(trade_id=trade_id, kind=signal.update_action,
                                    payload_hash=digest(signal.model_dump(mode="json")))
        request = PendingStopUpdate(event_id=event_id, operation_id=operation_id or "update:" + event_id, stop=stop,
            reference_stop=trade["stop_price"], original_budget=min(plan.risk_usdc, Decimal(trade["reserved_risk"])),
            source_binding=binding, source_revisions=source_revisions,
            management_revision=trade["management_revision"] + 1,
            phase="replacing" if size else "waiting_fill", accepted_at=now_ms())
        def accept(data):
            self.accept_stop_update(data, trade_id, trade, plan, signal, request, binding)
        self.state.update(accept)
        if size:
            await self.refresh_trade(trade_id)
        else:
            await self.resume_pending_stop(trade_id)
        return self.stop_update_result(self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"])

    async def management_stop_preflight(self, trade_id, operation_id) -> PendingStopUpdate | None:
        pending = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("pending_stop_update")
        if not pending or pending["phase"] not in STOP_UPDATE_ACTIVE or pending["operation_id"] == operation_id:
            return None
        request = PendingStopUpdate.model_validate(pending)
        operation = self.state.read("operations")["operations"].get(request.operation_id)
        if request.phase == "unknown" or operation and operation.get("outcome") != "not_sent":
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                await self.exchange.reconcile(request.operation_id)
            await self.resume_pending_stop(trade_id)
            pending = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["pending_stop_update"]
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

    async def supersede_changed_stop(self, trade_id, request, operation):
        if operation and operation["phase"] in ("prepared", "sending", "unknown"):
            await self.exchange.reconcile(request.operation_id)
            operation = self.state.read("operations", entries={"operations": [request.operation_id]})["operations"][request.operation_id]
            if operation["phase"] in ("prepared", "sending", "unknown"):
                self.stop_phase(trade_id, "unknown", "Original stop operation must be reconciled before superseding")
                return
        self.stop_phase(trade_id, "superseded", "Source or independent management revision superseded this request")
        return


    async def apply_pending_stop(self, trade_id, request):
        self.stop_phase(trade_id, "replacing")
        try:
            result = await self.replace_stop(trade_id, request.stop, request.operation_id)
        except ExternalInterference:
            raise
        except StopEvidencePending as error:
            self.patch_trade(trade_id, status="unknown", quantity_evidence_pending=True, waiting_error=str(error))
            self.stop_phase(trade_id, "waiting_fill", str(error))
            return
        except AqlaError as error:
            operation = self.state.read("operations")["operations"].get(request.operation_id)
            phase = "unknown" if operation and operation["phase"] in ("prepared", "sending", "unknown") else "rejected"
            self.stop_phase(trade_id, phase, str(error))
            return
        current = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
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
            await self.supersede_changed_stop(trade_id, request, operation)
            return
        if not Decimal(trade["filled_quantity"]):
            entry = await self.order_status(trade["entry_cloid"])
            total, _ = await self.capture_fills(trade_id, entry)
            if not total:
                if entry.get("order", {}).get("status") in TERMINAL_ORDER:
                    self.stop_phase(trade_id, "closed_without_application", "Entry ended without any own fill; requested stop was not sent")
                return
        await self.apply_pending_stop(trade_id, request)

    def pending_replacement_stop_guard(self, data, row, plan, stop, operation_id):
        pending = row.get("pending_stop_update")
        if pending and pending["operation_id"] == operation_id:
            if pending["phase"] not in STOP_UPDATE_ACTIVE or pending["management_revision"] != row["management_revision"]:
                raise AqlaError("Pending stop management revision changed before replacement")
            if data["bindings"].get(operation_id) != pending["source_binding"] or self.source_revisions(data, pending["source_revisions"]) != pending["source_revisions"]:
                raise AqlaError("Pending stop source binding changed before replacement")
            if pending["source_binding"]["kind"] == "break_even" and price_grid(managed_entries.current_reference(row), plan.market, up=plan.side == "long") != stop:
                raise AqlaError("Break-even reference changed after acceptance; original price identity will not be rebound")


    def replacement_stop_guard(self, data, trade_id, plan, stop, remaining, operation_id, entry_fingerprint, initialize_backup, initial_protection):
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
        self.pending_replacement_stop_guard(data, row, plan, stop, operation_id)


    async def accept_replacement_stop(self, trade_id, plan, trade, cloid, stop, operation_id, result, initialize_backup, initial_protection):
        pending = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id].get("pending_stop_update")
        preserve = initialize_backup or initial_protection or pending and pending["operation_id"] == operation_id
        self.patch_trade(trade_id, stop_price=str(stop), management_revision=trade["management_revision"] + 1,
                    preserve_pending_stop=preserve)
        old = [order for order in trade["orders"] if order["kind"] == "sl" and order["cloid"] != cloid]
        if pending and pending["phase"] in STOP_UPDATE_ACTIVE and (initialize_backup or initial_protection):
            pending_cloid = self.exchange.cloid(pending["operation_id"])
            old = [order for order in old if order["cloid"] != pending_cloid]
        for order in old:
            await self.exchange.mutate("cancel:" + operation_id + ":" + order["cloid"],
                                       {"type": "cancelByCloid", "cancels": [{"asset": plan.market.asset_id, "cloid": order["cloid"]}]})
        self.patch_trade(trade_id, status="open")
        return {**result, "replacement_confirmed": True}


    async def confirm_replacement_stop(self, trade_id, plan, trade, cloid, remaining, stop, operation_id, result, initialize_backup, initial_protection):
        confirmed = await self.order_status(cloid)
        if confirmed.get("order", {}).get("status") == "open" and self.stop_matches(confirmed, plan, remaining, stop):
            return await self.accept_replacement_stop(trade_id, plan, trade, cloid, stop, operation_id, result, initialize_backup, initial_protection)
        if result.get("outcome") == "not_sent":
            return result
        if result["phase"] == "rejected":
            if getattr(self.exchange, "fail_closed_protection", False):
                # Router enables this flag only on its account-scoped state view.
                cast("VenueState", self.state).pause_entries("Stop loss was rejected; emergency close required")
            return await self.close_position(trade_id, Decimal(100), operation_id="emergency:" + operation_id)
        self.patch_trade(trade_id, status="protecting")
        return {"operation": result, "old_stop_retained": True, "replacement_unconfirmed": True}


    @staticmethod
    def validate_backup_stop_request(trade, plan, proposed, operation_id):
        final = trade.get("backup_finalization") or {}
        if (final.get("phase") != "prepared" or "stop_loss" not in plan.backup_fields or str(proposed) != final.get("stop_loss")
                or operation_id != final.get("stop_operation_id") or any(order["kind"] == "tp" for order in trade["orders"])):
            raise AqlaError("Only the durable initial Backup SL may be finalized")


    async def dispatch_stop_replacement(self, trade_id, trade, plan, remaining, stop, operation_id, initialize_backup, initial_protection):
        operation_id = operation_id or "stop:" + trade_id + ":" + str(trade["management_revision"])
        cloid = self.exchange.cloid(operation_id)
        limit = price_grid(stop * (Decimal("0.95") if plan.side == "long" else Decimal("1.05")), plan.market, up=plan.side == "short")
        existing = self.state.read("operations")["operations"].get(operation_id)
        replacement = self.order(plan, remaining, limit, reduce=True, trigger=("sl", stop), cloid=cloid)
        entry_fingerprint = None
        def final_guard(data):
            self.replacement_stop_guard(data, trade_id, plan, stop, remaining, operation_id, entry_fingerprint, initialize_backup, initial_protection)
        async def live_guard():
            nonlocal entry_fingerprint
            await self.capture_fills(trade_id)
            current_size = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
            row = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
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
        return await self.confirm_replacement_stop(trade_id, plan, trade, cloid, remaining, stop, operation_id, result, initialize_backup, initial_protection)


    async def reconcile_stop_replacement(self, operation_id):
        if operation_id:
            operation = self.state.read("operations")["operations"].get(operation_id)
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                await self.exchange.reconcile(operation_id)


    async def stop_replacement_position(self, trade_id, plan, stop, initialize_backup, initial_protection):
        await self.cancel_entry(trade_id, keep_waves=plan.policy.management_mode == "waverider")
        await self.capture_fills(trade_id)
        signed = self.position_size(await self.positions(plan.market), plan.market)
        remaining = self.owned_remaining(trade_id, signed)
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if not initial_protection:
            self.validate_final_stop_quantity(trade)
        self.validate_stop_budget(trade, plan, stop, remaining, initialize_backup=initialize_backup)
        return trade, remaining


    async def replace_stop(self, trade_id, proposed: Decimal, operation_id=None, *, initialize_backup=False, initial_protection=False):
        await self.reconcile_stop_replacement(operation_id)
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
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
                trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        stop = price_grid(proposed, plan.market, up=plan.side == "long")
        if initialize_backup:
            self.validate_backup_stop_request(trade, plan, proposed, operation_id)
        self.validate_stop_budget(trade, plan, stop, initialize_backup=initialize_backup)
        trade, remaining = await self.stop_replacement_position(trade_id, plan, stop, initialize_backup, initial_protection)
        if not remaining:
            return {"already_closed": True}
        market = await read_observation(lambda: self.exchange.market(plan.market.symbol, plan.market.product), "market")
        if (plan.side == "long" and stop >= market.mid) or (plan.side == "short" and stop <= market.mid):
            return await self.close_position(trade_id, Decimal(100), operation_id="stop-crossed:" + (operation_id or trade_id))
        return await self.dispatch_stop_replacement(trade_id, trade, plan, remaining, stop, operation_id, initialize_backup, initial_protection)

    async def resume_close_intents(self, trade_id):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        for identity, intent in trade.get("close_intents", {}).items():
            if intent.get("completed"):
                continue
            operation = self.state.read("operations", entries={"operations": [identity]})["operations"].get(identity)
            if operation is None:
                if any(row["phase"] in ("prepared", "sending", "unknown") for row in self.state.read("operations")["operations"].values()):
                    self.patch_trade(trade_id, status="closing")
                    return False
                operation = await self.dispatch_close_intent(trade_id, intent)
            if operation.get("phase") in ("prepared", "sending", "unknown"):
                self.patch_trade(trade_id, status="closing")
                return False
            if not operation.get("already_closed") and operation.get("outcome") != "not_sent":
                for order in intent["action"]["orders"]:
                    status = await self.order_status(order["c"])
                    if status.get("order", {}).get("status") not in TERMINAL_ORDER:
                        self.patch_trade(trade_id, status="closing")
                        return False
                await self.capture_fills(trade_id)
            self.state.update(lambda data, identity=identity: data["trades"][trade_id]["close_intents"][identity].update(completed=True))
        return True

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

    def stage_close_intent(self, data, plan, trade_id, operation_id, intent, order):
        self.assert_plan_binding(data, plan)
        row = data["trades"][trade_id]
        row.setdefault("close_intents", {})[operation_id] = intent
        reason = "source_close" if operation_id.startswith("update:") else "operator_close" if operation_id.startswith(("operator:", "operator-close:")) else "protection_failure" if operation_id.startswith(("protection-failure:", "stop-crossed:", "emergency:")) else "unconfirmed"
        row.setdefault("exit_orders", []).append({"kind": "close", "reason": reason, "cloid": order["c"], "quantity": order["s"], "price": order["p"], "operation_id": operation_id})
        row.update(status="closing", management_revision=row["management_revision"] + 1)


    async def close_position(self, trade_id, percent=Decimal(100), operation_id=None, *, quantity=None):
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
        amount = quantity_grid(min(size, quantity) if quantity is not None else size * percent / 100, plan.market)
        if amount <= 0:
            raise AqlaError("Reduction rounds to zero")
        market = await read_observation(lambda: self.exchange.market(plan.market.symbol, plan.market.product), "market")
        limit = slippage_price(market.mid, plan.policy.max_slippage_percent, plan.market, buy=plan.side == "short")
        order = self.order(plan, amount, limit, reduce=True, cloid=self.exchange.cloid(operation_id))
        order["t"] = {"limit": {"tif": "Ioc"}}
        intent = {"operation_id": operation_id, "action": {"type": "order", "orders": [order], "grouping": "na"}, "created_at": now_ms()}
        def stage(data):
            self.stage_close_intent(data, plan, trade_id, operation_id, intent, order)
        self.state.update(stage)
        return await self.dispatch_close_intent(trade_id, intent)

    @staticmethod
    def submitted_owned_order(trade, row):
        return (not row.get("not_sent")
                and trade.get("order_observations", {}).get(row["cloid"], {}).get("status") not in TERMINAL_ORDER)

    @staticmethod
    def managed_exit_orders(trade):
        return [row for row in trade.get("exit_orders", [])
                if row["kind"] in ("waverider", "waverider_retirement") and not row.get("not_sent")]

    async def cancel_owned_orders(self, trade_id, include_sl=False, operation_id=None):
        from .target_recovery import absence_proved
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        orders = [order for order in trade["orders"] if include_sl or order["kind"] != "sl"]
        if include_sl and trade.get("lifecycle_version") == 2:
            orders.extend(self.managed_exit_orders(trade))
        if trade.get("lifecycle_version") == 2:
            orders = [row for row in orders if self.submitted_owned_order(trade, row)]
        operations = self.state.read("operations")["operations"]
        orders = [row for row in orders if not absence_proved(operations, row)]
        if not orders:
            return
        cancels = [{"asset": plan.market.asset_id, "cloid": order["cloid"]} for order in orders]
        return await self.exchange.mutate(operation_id or "cleanup:" + trade_id + ":" + str(trade["management_revision"]), {"type": "cancelByCloid", "cancels": cancels})

    _cancel_owned_orders = cancel_owned_orders

    def begin_cleanup(self, trade_id, terminal_status):
        from .target_recovery import absence_proved
        snapshot = self.state.snapshot()
        trade = snapshot["trades"][trade_id]
        self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
        identities = []
        for order in [*trade["orders"], *(row for row in trade.get("exit_orders", []) if row["kind"] in ("waverider", "waverider_retirement"))]:
            if absence_proved(snapshot["operations"], order):
                continue
            if trade.get("lifecycle_version") == 2 and (order.get("not_sent") or trade.get("order_observations", {}).get(order["cloid"], {}).get("status") in TERMINAL_ORDER):
                continue
            for operation in snapshot["operations"].values():
                for index, leg in enumerate(operation["wire_action"].get("orders", [])):
                    rejected = operation["phase"] == "rejected" or any(row["index"] == index and row["disposition"] == "rejected" for row in operation.get("legs", []))
                    if leg.get("c") == order["cloid"] and not rejected:
                        identities.append(order["cloid"])
        self.patch_trade(trade_id, status="cleanup", remaining_quantity="0", cleanup_target=terminal_status,
                    cleanup_cloids=sorted(set(identities)), cleanup_complete=False)

    async def finish_cleanup(self, trade_id):
        from .target_recovery import absence_proved
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        self.assert_plan_binding(self.state.snapshot(), TradePlan.model_validate(trade["plan"]))
        if trade.get("cleanup_cloids"):
            await self.cancel_owned_orders(trade_id, include_sl=True)
            operations = self.state.read("operations")["operations"]
            proved = {row["cloid"] for row in trade["orders"] if absence_proved(operations, row)}
            identities = [identity for identity in trade["cleanup_cloids"] if identity not in proved]
            observations = [await self.order_status(identity) for identity in identities]
            self.observe_orders(trade_id, dict(zip(identities, observations, strict=True)))
            if any(row.get("order", {}).get("status") not in TERMINAL_ORDER for row in observations):
                raise AqlaError("Owned exit cancellation is unconfirmed; market ownership remains reserved")
        self.patch_trade(trade_id, status=trade["cleanup_target"], cleanup_complete=True, reserved_risk="0", cleanup_observed_at=now_ms())
        await self.resume_pending_stop(trade_id)
        return self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]

    @staticmethod
    def same_update_symbol(trade, signal):
        named = Hyperliquid.canonical_symbol(signal.symbol) if trade["plan"].get("exchange", "hyperliquid") == "hyperliquid" else signal.symbol
        if trade["plan"]["market"]["product"] == "perp" and ":" not in named:
            named = named.upper().removesuffix(".P")
            for suffix in ("/USDT", "/USDC", "USDT", "USDC"):
                if named.endswith(suffix):
                    named = named[:-len(suffix)]
                    break
        return trade["plan"]["market"]["symbol"] == named


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
                return self.same_update_symbol(trade, signal)
            trades = [trade for trade in trades if same_symbol(trade)]
        if signal.side:
            trades = [trade for trade in trades if trade["plan"]["side"] == signal.side]
        if signal.bound_trade_id:
            trades = [trade for trade in trades if trade["trade_id"] == signal.bound_trade_id]
        return trades

    async def replay_existing_source_update(self, event_id, signal, snapshot, previous):
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
                stop_request = self.state.read("trades", entries={"trades": [trade["trade_id"]]})["trades"][trade["trade_id"]]["pending_stop_update"]
            return {"trade_id": trade["trade_id"], "result": self.stop_update_result(stop_request),
                    "duplicate": stop_request["phase"] not in STOP_UPDATE_ACTIVE}
        if snapshot["events"][event_id]["status"] == "updated":
            return {"duplicate": True, "trade": trade}
        if trade["status"] not in OPEN:
            operation = snapshot["operations"].get("update:" + event_id)
            uncertain = operation and operation["phase"] in ("prepared", "sending", "unknown")
            self.state.update(lambda data: data["events"][event_id].update(status="needs_reconciliation" if uncertain else "updated"))
            return {"trade_id": trade["trade_id"], "result": operation or {"already_closed": True}}
        return None

    async def resolve_source_update(self, event_id, signal):
        snapshot = self.state.snapshot()
        previous = snapshot["bindings"].get("update:" + event_id)
        if previous:
            cached = await self.replay_existing_source_update(event_id, signal, snapshot, previous)
            if cached is not None:
                return None, cached
            trade = snapshot["trades"][previous["trade_id"]]
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
        return trade, None

    def source_update_binding(self, event_id, signal, trade):
        previous = self.state.snapshot()["bindings"].get("update:" + event_id)
        binding = {"trade_id": trade["trade_id"], "kind": signal.update_action, "payload_hash": digest(signal.model_dump(mode="json"))}
        if previous and previous != binding:
            raise AqlaError("Update source is already bound to another trade or action")
        if previous and self.state.snapshot()["events"][event_id]["status"] == "updated":
            return previous, binding, {"duplicate": True, "trade": self.state.read("trades", entries={"trades": [trade["trade_id"]]})["trades"][trade["trade_id"]]}
        return previous, binding, None

    @staticmethod
    def validate_source_update_values(signal, trade, plan):
        if signal.update_action == "reduce" and signal.reduce_percent is None:
            raise AqlaError("Reduction update needs an explicit percentage")
        if signal.update_action == "move_stop":
            if signal.stop_loss is None:
                raise AqlaError("Stop update needs a source-backed price")
            old = Decimal(trade["stop_price"])
            requested_stop = price_grid(signal.stop_loss, plan.market, up=plan.side == "long")
            if not plan.policy.allow_stop_widening and (requested_stop < old if plan.side == "long" else requested_stop > old):
                raise AqlaError("Stop widening is not permitted")

    @staticmethod
    def percentage_update_reference(trade, plan, signal):
        if signal.take_profit_unit == "roe_percent" and plan.effective_leverage is None:
            raise AqlaError("Cannot establish own initial margin leverage for this legacy trade; reconcile instead of guessing")
        specification = trade.get("percent_finalization") or plan.source_tp
        if not specification:
            specification = {"reference_entry": str(plan.source_entry_reference) if plan.source_entry_reference is not None else trade.get("entry_vwap"),
                             "signal_leverage": plan.signal_leverage_original or plan.signal_leverage}
        reference = (trade.get("source_entry_vwap", trade.get("entry_vwap")) or str(plan.entry_price)) if signal.take_profit_unit == "roe_percent" else specification.get("reference_entry") or trade["entry_vwap"]
        if reference is None:
            raise AqlaError("Percentage TP update requires this trade's stored source reference")
        return reference, specification

    def convert_percentage_update(self, event_id, trade, plan, signal):
        from .percent_targets import prices
        reference, specification = self.percentage_update_reference(trade, plan, signal)
        selected = selected_targets(signal.take_profits, plan.policy)
        if plan.policy.take_profit_allocation == "adaptive":
            selected = selected[:len(plan.take_profits)]
        converted = prices(reference, plan.side, plan.market, signal.take_profit_unit, selected, margin_leverage(plan.effective_leverage, plan.market) if signal.take_profit_unit == "roe_percent" else specification.get("signal_leverage"))
        original = signal.model_dump(mode="json")
        update_targets = converted
        update_spec = {"unit": signal.take_profit_unit, "values": [str(value) for value in selected],
                       "reference_entry": str(reference), "signal_leverage": specification.get("signal_leverage")}
        if signal.take_profit_unit == "roe_percent":
            update_spec.update(margin_leverage=str(margin_leverage(plan.effective_leverage, plan.market)), reference_kind="fill_vwap", reference_entry=trade.get("entry_vwap"), rules_version="source-roe-v1")
        # Validate the quoted update before converting; computed prices
        # have no invented price quotations.
        self.state.update(lambda data: data["trades"][trade["trade_id"]].setdefault("percent_updates", {}).__setitem__(event_id,
                          {"source": original, "reference": reference, "prices": [str(value) for value in converted]}))
        return update_targets, update_spec

    def source_update_targets(self, event_id, trade, plan, signal):
        update_targets = signal.take_profits
        update_spec: dict[str, Any] = {"unit": "price"}
        if signal.update_action == "replace_targets":
            if signal.take_profit_unit != "price":
                update_targets, update_spec = self.convert_percentage_update(event_id, trade, plan, signal)
            self.validate_targets(trade, plan, update_targets)
        return update_targets, update_spec

    async def queue_source_stop(self, event_id, trade, signal):
        if trade["status"] == "waiting_entry" and signal.update_action in ("break_even", "reduce"):
            raise AqlaError("Waiting entry has no filled position for this update")
        if trade["status"] != "waiting_entry" and signal.update_action in ("move_stop", "break_even"):
            price = Decimal(trade["entry_vwap"]) if signal.update_action == "break_even" and trade["entry_vwap"] else signal.stop_loss
            if not price:
                raise AqlaError("Stop update needs a price or proved entry VWAP")
            result = await self.queue_stop_update(trade["trade_id"], event_id, signal, price)
            return {"trade_id": trade["trade_id"], "result": result}
        return None

    def accept_source_update(self, signal, plan, trade, previous, binding, update_revisions, pending_request, operation):
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

    async def apply_waiting_source_update(self, event_id, trade, plan, signal, update_targets, update_spec):
        if signal.update_action in ("close", "cancel_entry"):
            result = await self.cancel_entry(trade["trade_id"])
        elif signal.update_action == "move_stop":
            if signal.stop_loss is None:
                raise AqlaError("Stop update needs a source-backed price")
            stop = price_grid(signal.stop_loss, plan.market, up=plan.side == "long")
            self.patch_trade(trade["trade_id"], stop_price=str(stop), management_revision=trade["management_revision"] + 1)
            result = {"waiting_stop_updated": True}
        else:
            targets = self.validate_targets(trade, plan, update_targets)
            self.patch_trade(trade["trade_id"], targets=[str(price) for price in targets], source_tp_override=update_spec, management_revision=trade["management_revision"] + 1)
            result = {"waiting_targets_updated": True}
        self.state.update(lambda data: data["events"][event_id].update(status="updated"))
        return {"trade_id": trade["trade_id"], "result": result}

    async def dispatch_source_update(self, trade, signal, update_targets, update_spec, operation):
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
        return result

    async def update_trade(self, event_id, signal: Interpretation):
        async with self.lock.work(1):
            if signal.update_action == "sequence":
                from .management_sequences import start
                return await start(self, event_id, signal)
            if signal.kind != "trade_update" or not signal.update_action:
                raise AqlaError("An update needs a definite action")
            trade, cached = await self.resolve_source_update(event_id, signal)
            if cached is not None:
                return cached
            plan = TradePlan.model_validate(trade["plan"])
            self.assert_plan_binding(self.state.snapshot(), plan)
            if signal.update_action not in plan.policy.allowed_updates:
                raise AqlaError("Update action is not allowed by this trade's user rules")
            previous, binding, cached = self.source_update_binding(event_id, signal, trade)
            if cached is not None:
                return cached
            self.validate_source_update_values(signal, trade, plan)
            update_targets, update_spec = self.source_update_targets(event_id, trade, plan, signal)
            stop_result = await self.queue_source_stop(event_id, trade, signal)
            if stop_result is not None:
                return stop_result
            operation = "update:" + event_id
            update_revisions = self.source_revisions(self.state.snapshot(), signal.source_refs)
            pending_request = await self.management_stop_preflight(trade["trade_id"], operation)
            self.accept_source_update(signal, plan, trade, previous, binding, update_revisions, pending_request, operation)
            if trade["status"] == "waiting_entry":
                return await self.apply_waiting_source_update(event_id, trade, plan, signal, update_targets, update_spec)
            result = await self.dispatch_source_update(trade, signal, update_targets, update_spec, operation)
            uncertain = result.get("phase") in ("prepared", "sending", "unknown") or result.get("replacement_unconfirmed")
            self.state.update(lambda state: state["events"][event_id].update(status="needs_reconciliation" if uncertain else "updated"))
            return {"trade_id": trade["trade_id"], "result": result}

    @staticmethod
    def validate_targets(trade, plan, prices):
        prices = selected_targets(prices, plan.policy)
        if plan.policy.take_profit_allocation == "adaptive":
            prices = prices[:len(plan.take_profits)]
        if not prices or len(prices) != len(plan.take_profits):
            raise AqlaError("Replace targets with the same target count")
        target_prices = [price_grid(price, plan.market, up=plan.side == "short") for price in prices]
        entry = managed_entries.current_reference(trade)
        completed = set(trade.get("completed_target_indices", [])) if trade.get("lifecycle_version") == 2 else set()
        if target_prices != sorted(set(target_prices), reverse=plan.side == "short") or any((price <= entry if plan.side == "long" else price >= entry) for index, price in enumerate(target_prices) if index not in completed):
            raise AqlaError("Targets must be strictly ordered on the profitable side of the actual entry")
        return target_prices

    def accept_target_update(self, data, trade_id, operation_id, staged):
        row = data["trades"][trade_id]
        previous = self.pending_target_identity(data, row)
        if previous and previous != operation_id:
            old = row["target_updates"][previous]
            old["superseded"] = True
            operation = data["operations"].get(previous)
            if old.get("action") is not None and (operation is None or operation.get("outcome") == "not_sent"):
                unsent = {order["c"] for order in old["action"]["orders"]}
                for order in row["orders"]:
                    if order["cloid"] in unsent:
                        order["retired"] = True
                staged["original_orders"] = old["original_orders"]
        row.setdefault("target_updates", {})[operation_id] = staged
        row["pending_target_update"] = operation_id
        row["management_revision"] += 1
        staged["accepted_revision"] = row["management_revision"]


    @staticmethod
    def complete_target_update(data, trade_id, operation_id, result):
        row = data["trades"][trade_id]
        saved = row["target_updates"][operation_id]
        if not saved.get("completed"):
            saved["completed"] = True
            saved["rejected"] = result.get("phase") == "rejected"
            if ("accepted_revision" not in saved and row.get("pending_target_update") in (None, operation_id)
                    and not saved.get("superseded")):
                pending = row.get("pending_stop_update")
                if (pending and pending["phase"] in STOP_UPDATE_ACTIVE
                        and pending["management_revision"] == row["management_revision"]
                        and data["bindings"].get(pending["operation_id"]) == pending["source_binding"]):
                    pending["management_revision"] += 1
                row["management_revision"] += 1
        if row.get("pending_target_update") == operation_id:
            row.pop("pending_target_update")


    def finish_target_update(self, result, trade_id, plan, operation_id):
        self.record_maker_target_rejection(trade_id, plan, result)
        if result.get("phase") in ("acknowledged", "reconciled", "rejected") or result.get("no_unfilled_targets"):
            def complete(data):
                self.complete_target_update(data, trade_id, operation_id, result)
            self.state.update(complete)
        return result


    def target_replacement_guard(self, data, trade_id, plan, operation_id, expected_action):
        row = data["trades"][trade_id]
        self.assert_plan_binding(data, plan)
        if (row["status"] not in ("open", "protecting")
                or row.get("pending_target_update") not in (None, operation_id)
                or row["target_updates"][operation_id].get("completed") or row["target_updates"][operation_id].get("superseded")):
            raise AqlaError("Target replacement is no longer the active own management intent")
        active = {order["cloid"] for order in row["orders"] if order["kind"] == "tp" and not order.get("retired")}
        action = row["target_updates"][operation_id]["action"]
        if action is None or action != expected_action or any(order["c"] not in active for order in action["orders"]):
            raise AqlaError("Target replacement generation was superseded")
        if sum(Decimal(order["s"]) for order in action["orders"]) > Decimal(row["remaining_quantity"]):
            raise AqlaError("Saved target replacement exceeds proved own remaining quantity")


    async def replacement_target_budgets(self, trade_id, plan, trade):
        current = self.owned_remaining(trade_id, self.position_size(await self.positions(plan.market), plan.market))
        self.patch_trade(trade_id, remaining_quantity=str(current))
        budgets = [Decimal(value) for value in trade.get("target_budgets", plan.tp_quantities)]
        consumed = [Decimal(value) for value in trade["tp_filled"]]
        outstanding = [max(Decimal(0), budget - spent) for budget, spent in zip(budgets, consumed, strict=True)]
        total_outstanding = sum(outstanding)
        if total_outstanding > current:
            live_indices = [i for i, size in enumerate(outstanding) if size > 0]
            outstanding = [quantity_grid(size * current / total_outstanding, plan.market) for size in outstanding]
            if live_indices:
                outstanding[live_indices[-1]] += current - sum(outstanding)
        return current, consumed, outstanding


    async def replacement_target_orders(self, trade_id, staged, plan, target_prices, operation_id):
        old_orders = staged["original_orders"]
        cancels = [{"asset": plan.market.asset_id, "cloid": order["cloid"]} for order in old_orders]
        if cancels:
            await self.exchange.mutate("cancel-targets:" + operation_id, {"type": "cancelByCloid", "cancels": cancels})
        await self.capture_fills(trade_id)
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        # Cancellation confirmation and refreshed fill budgets are required before replacement.
        unfilled = []
        for order in old_orders:
            status = await self.order_status(order["cloid"])
            if status.get("order", {}).get("status") not in TERMINAL_ORDER:
                raise AqlaError("Old target cancellation is not confirmed")
            if status["order"]["status"] != "filled":
                unfilled.append(order)
        current, consumed, outstanding = await self.replacement_target_budgets(trade_id, plan, trade)
        quantity_left = current
        new = []
        for index in sorted({order["index"] for order in unfilled}):
            size = min(outstanding[index], quantity_left)
            if size <= 0:
                continue
            price = target_prices[index]
            new.append((index, self.order(plan, size, price, reduce=True, take_profit=True, cloid=self.exchange.cloid(operation_id, index))))
            quantity_left -= size
        return consumed, outstanding, new


    @staticmethod
    def record_target_generation(data, trade_id, operation_id, staged, new, target_prices, consumed, outstanding):
        row = data["trades"][trade_id]
        if row.get("pending_target_update") not in (None, operation_id):
            raise AqlaError("Target replacement generation was superseded before staging")
        if staged.get("source_tp") is not None:
            row["source_tp_override"] = staged["source_tp"]
        for old in row["orders"]:
            if old["kind"] == "tp":
                old["retired"] = True
        row["orders"].extend({"kind": "tp", "cloid": order["c"], "quantity": order["s"], "price": order["p"], "index": index} for index, order in new)
        row["targets"] = [str(price) for price in target_prices]
        row["target_budgets"] = [str(spent + future) for spent, future in zip(consumed, outstanding, strict=True)]
        row["target_updates"][operation_id]["action"] = {"type": "order", "orders": [order for _, order in new], "grouping": "na"}


    async def stage_target_update(self, trade_id, operation_id, target_prices, source_tp, trade):
        request = await self.management_stop_preflight(trade_id, operation_id)
        if request:
            self.state.update(lambda data: self.supersede_stop_for_management(data, trade_id, request,
                "An authorized target update replaced this unsent stop request"))
        staged = {"prices": [str(price) for price in target_prices],
                  "original_orders": [order for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")],
                  "action": None, "source_tp": source_tp, "completed": False}
        def accept(data):
            self.accept_target_update(data, trade_id, operation_id, staged)
        self.state.update(accept)
        return staged


    async def replace_targets(self, trade_id, prices, operation_id, source_tp=None):
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade.get("suspended_wave_targets"):
            raise AqlaError("Optional Waverider exit must be reconciled before target replacement")
        plan = TradePlan.model_validate(trade["plan"])
        self.assert_plan_binding(self.state.snapshot(), plan)
        target_prices = self.validate_targets(trade, plan, prices)
        prior = self.state.read("operations")["operations"].get(operation_id)
        staged = trade.get("target_updates", {}).get(operation_id)
        if prior and staged is None:
            return prior
        if staged is None:
            staged = await self.stage_target_update(trade_id, operation_id, target_prices, source_tp, trade)
        elif staged["prices"] != [str(price) for price in target_prices]:
            raise AqlaError("Target update ID already identifies different prices")
        elif trade.get("pending_target_update") not in (None, operation_id):
            return {"target_update_superseded": True}

        def finish(result):
            return self.finish_target_update(result, trade_id, plan, operation_id)
        expected_action = staged["action"]
        def guard(data):
            self.target_replacement_guard(data, trade_id, plan, operation_id, expected_action)
        if prior:
            return finish(prior)
        if staged.get("completed"):
            return {"no_unfilled_targets": True}
        if staged["action"] is not None:
            result = await self.exchange.mutate(operation_id, staged["action"], prepare_guard=guard) if staged["action"]["orders"] else {"no_unfilled_targets": True}
            return finish(result)
        consumed, outstanding, new = await self.replacement_target_orders(trade_id, staged, plan, target_prices, operation_id)
        def record(data):
            self.record_target_generation(data, trade_id, operation_id, staged, new, target_prices, consumed, outstanding)
        self.state.update(record)
        expected_action = {"type": "order", "orders": [order for _, order in new], "grouping": "na"}
        result = await self.exchange.mutate(operation_id, expected_action, prepare_guard=guard) if new else {"no_unfilled_targets": True}
        return finish(result)

    @staticmethod
    def legacy_pending_targets(snapshot, trade):
        active = {order["cloid"] for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")}
        legacy = [key for key, saved in trade.get("target_updates", {}).items()
            if key not in snapshot["operations"] and not saved.get("completed") and not saved.get("superseded") and
            (any(order["c"] in active for order in (saved.get("action") or {}).get("orders", []))
             or saved.get("action") is None and {order["cloid"] for order in saved["original_orders"]} == active)]
        return legacy


    @staticmethod
    def pending_target_identity(snapshot, trade):
        identity = trade.get("pending_target_update")
        if identity is None:
            legacy = Trading.legacy_pending_targets(snapshot, trade)
            if not legacy:
                return None
            if len(legacy) != 1:
                raise AqlaError("Legacy pending target generation is ambiguous; preserve own stop for review")
            identity = legacy[0]
        return identity

    async def resume_target_update(self, trade_id):
        snapshot = self.state.read("trades", "operations", entries={"trades": [trade_id]})
        trade = snapshot["trades"][trade_id]
        identity = self.pending_target_identity(snapshot, trade)
        if identity is None:
            return True
        saved = trade["target_updates"][identity]
        if trade["status"] in ("closed", "rejected", "cancelled"):
            operation = snapshot["operations"].get(identity)
            if operation and operation["phase"] in ("prepared", "sending", "unknown"):
                return False
            def terminal(data):
                row = data["trades"][trade_id]
                stage = row["target_updates"][identity]
                stage["completed"] = True
                stage["closed_without_application"] = operation is None
                stage["rejected"] = operation is not None and operation["phase"] == "rejected"
                if row.get("pending_target_update") == identity:
                    row.pop("pending_target_update")
            self.state.update(terminal)
            return True
        if (identity not in snapshot["operations"] and saved.get("action") is not None
                and sum(Decimal(order["s"]) for order in saved["action"]["orders"]) > Decimal(trade["remaining_quantity"])):
            def resize_unsent(data):
                row = data["trades"][trade_id]
                if identity in data["operations"]:
                    raise AqlaError("Target operation was already prepared; reconcile its exact request")
                stage = row["target_updates"][identity]
                unsent = {order["c"] for order in stage["action"]["orders"]}
                for order in row["orders"]:
                    if order["cloid"] in unsent:
                        order["retired"] = True
                stage["action"] = None
            self.state.update(resize_unsent)
        result = await self.replace_targets(trade_id, list(map(Decimal, saved["prices"])), identity, source_tp=saved.get("source_tp"))
        return result.get("phase") not in ("prepared", "sending", "unknown")

    def record_maker_target_rejection(self, trade_id, plan, result, *, rejected_cloids=()):
        if plan.take_profit_execution != "maker_only":
            return
        trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        active = {order["cloid"] for order in trade["orders"] if order["kind"] == "tp" and not order.get("retired")}
        orders = result.get("wire_action", {}).get("orders", [])
        current = {order["c"] for order in orders if order.get("c") in active}
        rejected = (result.get("phase") == "rejected" or current.intersection(rejected_cloids)
                    or any(leg.get("disposition") == "rejected" and 0 <= leg.get("index", -1) < len(orders)
                           and orders[leg["index"]].get("c") in current for leg in result.get("legs", [])))
        if current and rejected:
            self.patch_trade(trade_id, maker_target_rejection={"operation_id": result["operation_id"], "target_cloids": sorted(current),
                        "reason": "Native maker TP was rejected; target remains unfilled"})

    async def recover(self):
        async with self.recovery_lock:
            with order_observations(self.exchange):
                return await self._recover()

    async def _recover(self):
        async with self.lock.work(0):
            self.recovery_checked = False
        snapshot = self.state.read("operations")
        reports = []
        for operation_id, row in snapshot["operations"].items():
            if row["phase"] in ("prepared", "sending", "unknown"):
                try:
                    async with self.lock.work(0), asyncio.timeout(getattr(self, 'recovery_packet_timeout', 40)):
                        reports.append(await self.exchange.reconcile(operation_id))
                except Exception:
                    reports.append({"operation_id": operation_id, "error": "Exact operation reconciliation is unavailable"})
        trades = list(self.state.read("trades")["trades"].items())
        positions = {identity: index for index, (identity, _) in enumerate(trades)}
        ordered_ids = list(positions)
        root = getattr(self.state, "root", self.state)
        cursor_key = "account-recovery:" + getattr(self.state, "account_key", "hyperliquid:" + self.exchange.settings.network)
        saved_cursor = root.read("cursors").get("cursors", {}).get(cursor_key)
        cursor = positions.get(saved_cursor, getattr(self, "recovery_cursor", 0)) % max(1, len(trades))
        trades = trades[cursor:] + trades[:cursor]
        trades.sort(key=lambda item: 0 if item[1]["status"] in ("closing", "unknown", "protecting", "cleanup") else 1)
        for trade_id, _ in trades:
            self.recovery_cursor = positions[trade_id] + 1
            next_id = ordered_ids[self.recovery_cursor % len(trades)]
            def save_cursor(part, next_id=next_id):
                part["cursors"][cursor_key] = next_id
            try:
                async with self.lock.work(20), asyncio.timeout(getattr(self, 'recovery_packet_timeout', 40)):
                    trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"].get(trade_id)
                    if trade:
                        reports.extend(await self._recover_trade(trade_id, trade))
            except TimeoutError:
                root.update_metadata_if_changed(("cursors",), save_cursor)
                reports.append({"trade_id": trade_id, "error": "Trade reconciliation packet timed out; existing protection retained"})
            except asyncio.CancelledError:
                root.update_metadata_if_changed(("cursors",), save_cursor)
                raise
            except Exception as error:
                reports.append({"trade_id": trade_id, "error": "Trade reconciliation is unavailable", "diagnostic": diagnostic(error, "reconciliation")})
            await asyncio.sleep(0)
        root.update_metadata_if_changed(("cursors",), lambda part: part["cursors"].pop(cursor_key, None))
        self.recovery_checked = (not any(row.get("error") for row in reports)
                                 and not any(row.get("protection_observation_pending") for row in self.state.read("trades")["trades"].values() if row["status"] in OPEN))
        return reports

    async def recover_conflicted_trade(self, trade_id, trade, reports):
        from .conflict_review import commit_conflict, observe_conflict
        try:
            review, evidence = await observe_conflict(self, trade_id)
            if not review["resolvable"]:
                reports.append({"trade_id": trade_id, "market_conflict": True, "market": trade['plan']['market']['symbol']})
                return None
            root = getattr(self.state, "root", self.state)
            root.update(lambda data, identity=trade_id, fact=review, proof=evidence: commit_conflict(self, data, identity, fact, proof))
            await self.resume_pending_stop(trade_id)
            reports.append({"trade_id": trade_id, "conflict_resolved": True})
            trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        except Exception:
            reports.append({"trade_id": trade_id, "error": "Conflict closure reconciliation is unavailable"})
            return None
        return trade


    async def refresh_recovery_state(self, trade_id, trade, reports):
        if trade["status"] == "cleanup":
            reports.append(await self.finish_cleanup(trade_id))
        elif trade["status"] == "waiting_entry":
            await self.check_waiting_entry(trade_id)
        elif trade.get("observation_retry") or trade["entry_operation_id"] not in self.state.read("operations")["operations"]:
            reports.append(await self.resume_unsent_entry(trade_id))
        else:
            reports.append(await self.refresh_trade(trade_id))


    async def recover_open_trade(self, trade_id, trade, reports):
        try:
            if trade.get("lifecycle_version") == 2:
                reports.append(await self.refresh_trade(trade_id))
                return True
            current = self.state.snapshot()
            entry = current["operations"].get(trade["entry_operation_id"], {})
            if entry.get("phase") == "rejected" and entry.get("outcome") == "not_sent" and self.entry_not_sent(current, trade):
                try:
                    self.raise_temporary_not_sent(entry)
                except TemporaryObservation as error:
                    if not trade.get("observation_retry") or trade["observation_retry"].get("attempt_inflight"):
                        trade = self.retain_observation_retry(trade["plan"]["plan_id"], error)
                else:
                    self.patch_trade(trade_id, status="rejected", reserved_risk="0", orders=[],
                                unsent_reason=entry.get("result", {}).get("error", "Entry guard rejected before exchange transport"),
                                entry_diagnostic=entry.get("result", {}).get("diagnostic", {}))
                    return True
            await self.refresh_recovery_state(trade_id, trade, reports)
        except ExternalInterference as error:
            self.patch_trade(trade_id, status="conflicted", external_conflict=str(error))
            reports.append({"trade_id": trade_id, "market_conflict": True, "market": trade['plan']['market']['symbol']})
        except Exception as error:
            reports.append({"trade_id": trade_id, "error": str(error) if isinstance(error, AqlaError) else "Trade reconciliation is unavailable"})


    async def _recover_trade(self, trade_id, trade):
        reports = []
        try:
            self.assert_plan_binding(self.state.read("network"), TradePlan.model_validate(trade["plan"]))
        except AqlaError as error:
            reports.append({"trade_id": trade_id, "error": str(error)})
            return reports
        if trade["status"] in ("closed", "rejected") and trade["orders"] and not trade.get("cleanup_complete"):
            self.begin_cleanup(trade_id, trade["status"])
            trade = self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]
        if trade["status"] == "conflicted":
            trade = await self.recover_conflicted_trade(trade_id, trade, reports)
            if trade is None:
                return reports
        if (
            (trade['status'] in OPEN)
            and (await self.recover_open_trade(trade_id, trade, reports))
        ):
            return reports
        if self.state.read("trades", entries={"trades": [trade_id]})["trades"][trade_id]["status"] in ("closed", "rejected", "cancelled"):
            try:
                if not await self.resume_target_update(trade_id):
                    reports.append({"trade_id": trade_id, "error": "Closed trade target operation still needs exact reconciliation"})
            except Exception as error:
                reports.append({"trade_id": trade_id, "error": str(error) if isinstance(error, AqlaError) else "Terminal target intent review is unavailable"})
        return reports

# Keep the established trades-module names bound to shared contracts.
__all__ = ['EntryBudgetChanged', 'OPEN', 'PendingStopUpdate', 'STOP_UPDATE_ACTIVE', 'StopEvidencePending', 'StopSourceBinding', 'StopUpdatePhase', 'StopUpdateResult', 'Trading', 'UNFINISHED']
