"""Profiles, routing and per-venue views over the one authoritative state writer."""
from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from aqla_hyperliquid_mcp.models import (
    DEFAULT_UPDATES,
    AqlaError,
    Model,
    Policy,
    Positive,
    UpdateAction,
    digest,
    now_ms,
)

Venue = Literal["hyperliquid"]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class AccountLimits(Model):
    max_open_trades: int = Field(ge=1, le=100)
    max_total_risk_percent: Positive = Field(le=100)


class Parameters(Model):
    sizing_mode: Literal["risk_percent", "fixed_notional"] = "risk_percent"
    risk_percent: Positive = Field(default=Decimal("0.5"), le=100)
    fixed_notional_quote: Positive | None = None
    max_notional_quote: Positive = Decimal("500")
    leverage_mode: Literal["fixed", "signal"] = "fixed"
    leverage: int = Field(default=2, ge=1, le=100)
    fallback_stop_loss_percent: Positive | None = Field(default=None, lt=100)
    fallback_stop_loss_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="course_percent: price loss; roe_percent: loss on original position margin at the plan's effective leverage, before fees/funding")
    fallback_take_profit_percentages: list[Positive] = Field(default_factory=list, max_length=20)
    fallback_take_profit_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="course_percent: price movement; roe_percent: nominal profit on original position margin at the plan's effective leverage, before fees/funding")
    margin_mode: Literal["cross", "isolated"] = "isolated"
    entry_mode: Literal["source", "source_or_market"] = "source_or_market"
    entry_range_choice: Literal["near", "far", "midpoint", "split"] = "split"
    management_mode: Literal["standard", "waverider"] = "standard"
    waverider_flex_percent: Positive = Field(default=Decimal(50), lt=100)
    waverider_until_tp: Annotated[int, Field(ge=1, le=20, strict=True)] | Literal["last"] = 1
    waverider_lifetime: Literal["signal_deadline", "until_target"] = "signal_deadline"
    entry_execution: Literal["standard", "maker_only"] = "standard"
    take_profit_execution: Literal["standard", "maker_only"] = "standard"
    max_slippage_percent: Positive = Field(default=Decimal("0.3"), le=10)
    max_signal_age_seconds: int = Field(default=300, ge=1, le=86400)
    take_profit_allocation: Literal["equal", "halving", "custom", "adaptive", "first_target"] = "equal"
    take_profit_weights: list[Positive] = Field(default_factory=list, max_length=20)
    break_even_after_tp: int | None = Field(default=None, ge=1, le=20)
    stop_management: Literal["break_even", "tp_ladder"] = "break_even"
    allowed_updates: list[UpdateAction] = Field(default_factory=lambda: list(DEFAULT_UPDATES))
    allow_stop_widening: bool = False
    cost_reserve_bps: Annotated[Decimal, Field(ge=0, le=1000)] = Decimal("10")

    @model_serializer(mode="wrap")
    def compatible_execution_defaults(self, handler):
        values = handler(self)
        for key, default in (("management_mode", "standard"), ("waverider_flex_percent", Decimal(50)), ("waverider_until_tp", 1), ("waverider_lifetime", "signal_deadline")):
            if getattr(self, key) == default:
                values.pop(key, None)
        for key in ("entry_execution", "take_profit_execution"):
            if values.get(key) == "standard":
                values.pop(key)
        for key in ("fallback_stop_loss_unit", "fallback_take_profit_unit"):
            if values.get(key) == "course_percent":
                values.pop(key)
        return values

    def policy(self, limits):
        values = self.model_dump()
        values["fixed_notional_usdc"] = values.pop("fixed_notional_quote")
        values["max_notional_usdc"] = values.pop("max_notional_quote")
        return Policy(**values, **limits.model_dump())

    @model_validator(mode="after")
    def consistent(self):
        self.policy(AccountLimits(max_open_trades=1, max_total_risk_percent=100))
        return self

    @classmethod
    def from_policy(cls, policy):
        values = Policy.model_validate(policy).model_dump()
        if values.pop("product") != "perp":
            raise AqlaError("Automatic pipelines support perpetuals only")
        values.pop("max_open_trades")
        values.pop("max_total_risk_percent")
        values["fixed_notional_quote"] = values.pop("fixed_notional_usdc")
        values["max_notional_quote"] = values.pop("max_notional_usdc")
        return cls(**values)


class TradingProfile(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=100)
    parameters: Parameters


class PipelineTarget(Model):
    exchange: Venue
    profile_id: Identifier


class Pipeline(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=100)
    sources: list[str] = Field(min_length=1, max_length=200)
    targets: list[PipelineTarget] = Field(min_length=1, max_length=1)
    enabled: bool = False

    @model_validator(mode="after")
    def unique(self):
        if len({target.exchange for target in self.targets}) != len(self.targets):
            raise ValueError("A pipeline may contain only one target per exchange")
        if any(not source.strip() for source in self.sources) or len(set(self.sources)) != len(self.sources):
            raise ValueError("Supply unique nonempty source selectors")
        return self


def migrate(data):
    """Add routing without rehashing any existing trade, order or operation."""
    if "profiles" in data:
        changed = retire_execution_configuration(data)
        if "execution_accounts" not in data:
            from .ranking import RankingPolicy
            data.update(execution_accounts={}, account_limits={}, account_enabled={}, account_bindings={}, account_nonces={}, account_events={},
                        ranking_policy=RankingPolicy().model_dump(mode="json"), ranking_revision=1,
                        source_ranks={source: {"tier": "C", "bootstrap": True, "revision": 1, "evaluated": [], "up": None, "down": 0} for source in data["sources"]},
                        ranking_history={})
            data["schema"] = 3
            changed = True
        if "source_names" not in data:
            data.update(source_names={}, source_names_revision=0)
            changed = True
        if "backup" not in data["profiles"]:
            selected = data["profiles"].get("standard", {}).get("parameters") or Parameters().model_dump(mode="json")
            values = {**selected, "leverage_mode": "fixed", "leverage": 2, "entry_mode": "source_or_market",
                      "fallback_stop_loss_percent": None, "fallback_take_profit_percentages": []}
            data["profiles"]["backup"] = {"id": "backup", "name": "Backup", "parameters": Parameters(**values).model_dump(mode="json"), "revision": 1}
            changed = True
        return changed
    selected = data.get("policy")
    parameters = Parameters.from_policy(selected).model_dump(mode="json") if selected and selected.get("product", "perp") == "perp" else None
    limits = {key: selected[key] for key in ("max_open_trades", "max_total_risk_percent")} if selected else None
    data.update(schema=2, configuration_revision=0,
                profiles={"standard": {"id": "standard", "name": "Standard", "parameters": parameters, "revision": data["policy_revision"]}},
                pipelines={"legacy": {"id": "legacy", "name": "Bestehende Hyperliquid-Quellen", "sources": ["*"],
                                      "targets": [{"exchange": "hyperliquid", "profile_id": "standard"}], "enabled": True, "revision": 0}},
                venue_limits={"hyperliquid": limits},
                venue_enabled={"hyperliquid": True},
                venue_bindings={}, venue_events={}, venue_accounts={})
    migrate(data)
    return True


def _check_retirement_safe(data):
    for trade in data.get("trades", {}).values():
        if (trade.get("plan", {}).get("exchange", "hyperliquid") != "hyperliquid"
                and (trade["status"] not in ("closed", "rejected", "cancelled", "expired")
                     or Decimal(trade.get("remaining_quantity") or "0") != 0)):
            raise AqlaError("An unsupported account still has active trading work; retire it before migration")
    if any(row.get("exchange", "hyperliquid") != "hyperliquid" and row["phase"] in ("prepared", "sending", "unknown")
           for row in data.get("operations", {}).values()):
        raise AqlaError("An unsupported account still has an unresolved operation")


def _retire_pipeline_targets(data, retired):
    for key, pipeline in list(data.get("pipelines", {}).items()):
        original = copy.deepcopy(pipeline)
        targets = [row for row in pipeline["targets"] if row["exchange"] == "hyperliquid"]
        for target in targets:
            target.pop("fallback_exchanges", None)
        if not targets:
            data["pipelines"].pop(key)
        else:
            pipeline["targets"] = targets
        if not targets or original != pipeline:
            retired.setdefault("pipelines", {})[key] = original


def retire_execution_configuration(data):
    """Keep historical finance intact while retiring unsupported execution configuration."""
    _check_retirement_safe(data)
    retired = {}
    for name in ("execution_accounts", "account_limits", "account_enabled", "account_recovery", "venue_accounts", "venue_limits", "venue_enabled"):
        values = data.get(name, {})
        removed = {key: value for key, value in values.items() if key != "hyperliquid" and not key.startswith("hyperliquid:")}
        if removed:
            retired[name] = copy.deepcopy(removed)
            for key in removed:
                values.pop(key)
    _retire_pipeline_targets(data, retired)
    if not retired:
        return False
    for name, values in retired.items():
        data.setdefault("retired_execution_configuration", {}).setdefault(name, {}).update(values)
    data["configuration_revision"] = data.get("configuration_revision", 0) + 1
    return True


def sync_legacy_policy(data):
    selected = data["policy"]
    data["profiles"]["standard"].update(parameters=Parameters.from_policy(selected).model_dump(mode="json") if selected and selected.get("product", "perp") == "perp" else None,
                                         revision=data["policy_revision"])
    data["venue_limits"]["hyperliquid"] = {key: selected[key] for key in ("max_open_trades", "max_total_risk_percent")} if selected else None


def _set_profile(data, profile):
    if profile is None:
        raise AqlaError("A complete profile is required")
    if profile.id == "backup" and profile.parameters.leverage_mode != "fixed":
        raise AqlaError("The global Backup profile requires a fixed leverage")
    previous = data["profiles"].get(profile.id, {})
    data["profiles"][profile.id] = {**profile.model_dump(mode="json"), "revision": previous.get("revision", 0) + 1}
    if profile.id == "standard":
        if data["venue_limits"]["hyperliquid"]:
            data["policy"] = profile.parameters.policy(AccountLimits(**data["venue_limits"]["hyperliquid"])).model_dump(mode="json")
        data["policy_revision"] = max(data["policy_revision"], previous.get("revision", 0)) + 1
        data["profiles"]["standard"]["revision"] = data["policy_revision"]


def _set_pipeline(data, pipeline):
    if pipeline is None or any(target.profile_id not in data["profiles"] for target in pipeline.targets):
        raise AqlaError("A complete pipeline with existing profiles is required")
    from .sources import resolve_source
    pipeline = pipeline.model_copy(update={"sources": [resolve_source(data, source) for source in pipeline.sources]})
    if len(set(pipeline.sources)) != len(pipeline.sources):
        raise AqlaError("Pipeline sources resolve to duplicate Telegram IDs")
    if any(source not in ("*", "owner_forward") and source not in {*data["sources"], *data.get("source_aliases", {}).values()} for source in pipeline.sources):
        raise AqlaError("Subscribe the requested Telegram sources first")
    previous = data["pipelines"].get(pipeline.id, {})
    data["pipelines"][pipeline.id] = {**pipeline.model_dump(mode="json"), "revision": previous.get("revision", 0) + 1}


def _set_exchange_limits(data, exchange, limits):
    if exchange is None or limits is None:
        raise AqlaError("Exchange and complete account limits are required")
    data["venue_limits"][exchange] = limits.model_dump(mode="json")
    if exchange == "hyperliquid" and data.get("policy"):
        data["policy"].update(limits.model_dump(mode="json"))
        data["policy_revision"] += 1


def _set_exchange_enabled(data, exchange, enabled):
    if exchange is None or enabled is None:
        raise AqlaError("Exchange and explicit enabled value are required")
    if enabled and data["venue_limits"].get(exchange) is None:
        raise AqlaError("Set explicit account limits before activating the exchange")
    data["venue_enabled"][exchange] = enabled


def _delete_routing_entry(data, action, identity):
    if identity in ("legacy", "standard", "backup"):
        raise AqlaError("Keep the migration compatibility entries; disable the legacy pipeline instead")
    if action == "delete_profile" and any(target["profile_id"] == identity for row in data["pipelines"].values() for target in row["targets"]):
        raise AqlaError("Remove pipeline references before deleting a profile")
    data["pipelines" if action == "delete_pipeline" else "profiles"].pop(identity, None)


def route_control(data, action, *, expected_revision, profile=None, pipeline=None, exchange=None, limits=None, enabled=None, identity=None):
    if exchange is not None and exchange != "hyperliquid":
        raise AqlaError("Only Hyperliquid exchange limits and activation are supported")
    if action in ("set_account_limits", "set_account_enabled") and identity not in ("hyperliquid:mainnet", "hyperliquid:testnet"):
        raise AqlaError("Only Hyperliquid execution accounts are supported")
    if expected_revision != data["configuration_revision"]:
        raise AqlaError("Read configuration_revision and submit a complete current configuration change")
    if action == "set_profile":
        _set_profile(data, profile)
    elif action == "set_pipeline":
        _set_pipeline(data, pipeline)
    elif action == "set_exchange_limits":
        _set_exchange_limits(data, exchange, limits)
    elif action == "set_exchange_enabled":
        _set_exchange_enabled(data, exchange, enabled)
    elif action in ("delete_pipeline", "delete_profile"):
        _delete_routing_entry(data, action, identity)
    else:
        raise AqlaError("Unknown routing control")
    data["configuration_revision"] += 1
    return {key: data[key] for key in ("configuration_revision", "profiles", "pipelines", "venue_limits", "venue_enabled")}


class ConfigurationChanged(AqlaError):
    """The immutable entry configuration no longer matches its preview basis."""


def validate_ranking_binding(data, plan):
    if plan.ranking_policy_revision is None or plan.ranking_enabled is None:
        raise AqlaError("Legacy ranking binding is incomplete; create a fresh preview before any new send")
    if data["ranking_revision"] != plan.ranking_policy_revision or data["ranking_policy"]["enabled"] != plan.ranking_enabled:
        raise AqlaError("Ranking policy changed; create a new unsent plan")
    if plan.ranking_enabled and not plan.rank_snapshot:
        raise AqlaError("Ranked entry requires a source rank snapshot; create a fresh routed preview")
    if plan.rank_snapshot:
        from .ranking import snapshot_rank
        current = snapshot_rank(data, plan.rank_snapshot["source_id"])
        if (not plan.ranking_enabled or plan.rank_snapshot["policy_revision"] != plan.ranking_policy_revision
                or current["source_revision"] != plan.rank_revision
                or current["policy_revision"] != plan.rank_snapshot["policy_revision"] or current["network"] != plan.network):
            raise AqlaError("Source rank/risk/network changed; old unsent entry is no longer eligible")


class VenueState:
    """A filtered view, never a second writer or state file."""
    def __init__(self, root, exchange, settings, *, legacy=True):
        self.root, self.exchange, self.settings = root, exchange, settings
        self.account_key = exchange + ":" + settings.network
        self.legacy = legacy
        self.route = ContextVar("aqla_route_" + exchange, default=None)
        self.preview_basis = ContextVar("aqla_preview_" + exchange, default=None)

    @contextmanager
    def scope(self, route, *, preview_snapshot=None):
        token = self.route.set(route)
        basis_token = self.preview_basis.set(preview_snapshot)
        try:
            yield
        finally:
            self.preview_basis.reset(basis_token)
            self.route.reset(token)

    def project_account_state(self, result, data, legacy):
        result["bindings"] = data["bindings"] if self.exchange == "hyperliquid" and legacy else data["account_bindings"].get(self.account_key, data["venue_bindings"].get(self.exchange, {}) if legacy else {})
        result["nonces"] = data["nonces"] if self.exchange == "hyperliquid" and legacy else data["account_nonces"].get(self.account_key, data.get("venue_nonces", {}).get(self.exchange, {}) if legacy else {})
        event_states = data["account_events"].get(self.account_key, data["venue_events"].get(self.exchange, {}) if legacy else {})
        result["events"] = {key: {**value, **event_states.get(key, {})} for key, value in data["events"].items()}

    def project_policy(self, result, data, route, legacy):
        profile = data["profiles"].get(route["profile_id"] if route else "standard", {})
        limits = data["venue_limits"].get(self.exchange) if legacy else data["account_limits"].get(self.account_key)
        parameters = profile.get("parameters")
        if route and route.get("rank_snapshot") and parameters:
            from .ranking import weighted_parameters
            parameters = weighted_parameters(parameters, route["rank_snapshot"])
        result["policy"] = Parameters(**parameters).policy(AccountLimits(**limits)).model_dump(mode="json") if parameters and limits else None
        result["policy_revision"] = profile.get("revision", 0)

    def project(self, data):
        result = dict(data)
        for key in ("trades", "plans", "operations"):
            result[key] = {identity: row for identity, row in data[key].items() if self.record_key(row, operation=key == "operations", default_network=self.settings.network) == self.account_key}
        legacy = self.legacy_context(data)
        self.project_account_state(result, data, legacy)
        route = self.route.get()
        self.project_policy(result, data, route, legacy)
        target = getattr(self.settings, "vault_address", None) or self.settings.account
        result["network"], result["account"], result["trading_account"] = self.settings.network, self.settings.account.lower(), target.lower()
        result["paused"] = data["paused"] or not (data["venue_enabled"].get(self.exchange, False) if legacy else data["account_enabled"].get(self.account_key, False))
        if route:
            result["paused"] |= not data["pipelines"].get(route["pipeline_id"], {}).get("enabled", False)
        return result

    @staticmethod
    def record_key(row, operation=False, default_network=""):
        plan = row.get("plan", {})
        return row.get("account_key") if operation and row.get("account_key") else plan.get("account_key") or (
            row.get("exchange", "hyperliquid") + ":" + row.get("request", {}).get("network", default_network) if operation else plan.get("exchange", "hyperliquid") + ":" + plan.get("network", default_network))

    def legacy_context(self, data):
        if not self.legacy:
            return False
        binding = data["venue_accounts"].get(self.exchange)
        return bool(binding and binding["network"] == self.settings.network) or not binding and (self.exchange != "hyperliquid" or data["network"] == self.settings.network)

    def snapshot(self):
        return self.root.venue_snapshot(self)

    def read(self, *roots, entries=None):
        dependencies = {"network", "venue_accounts", "profiles", "venue_limits", "account_limits", "paused",
                        "venue_enabled", "account_enabled", "pipelines"}
        groups = {"nonces": {"nonces", "account_nonces", "venue_nonces"},
                  "bindings": {"bindings", "account_bindings", "venue_bindings"},
                  "events": {"events", "account_events", "venue_events"}}
        needed = set(roots) | dependencies
        for root, extra in groups.items():
            if root in roots:
                needed |= extra
        return self.root.venue_snapshot(self, roots=roots, needed=needed, entries=entries)

    def preview_snapshot(self):
        basis = self.preview_basis.get()
        result = copy.deepcopy(self.project(basis)) if basis is not None else self.snapshot()
        result["preview_route"] = copy.deepcopy(self.route.get())
        return result

    def update(self, callback):
        def change(data):
            legacy = self.legacy_context(data)
            if not (self.exchange == "hyperliquid" and legacy):
                data["account_bindings"].setdefault(self.account_key, copy.deepcopy(data["venue_bindings"].get(self.exchange, {})) if legacy else {})
                data["account_nonces"].setdefault(self.account_key, copy.deepcopy(data.get("venue_nonces", {}).get(self.exchange, {})) if legacy else {})
            local = self.project(data)
            original_events = copy.deepcopy(local["events"])
            result = callback(local)
            for key in ("trades", "plans", "operations"):
                for identity, row in local[key].items():
                    if key == "operations":
                        row["exchange"] = self.exchange
                        row["account_key"] = self.account_key
                    data[key][identity] = row
            states = data["account_events"].setdefault(self.account_key, {})
            for identity, row in local["events"].items():
                if row != original_events[identity]:
                    states[identity] = {key: value for key, value in row.items() if key not in ("source", "source_id", "event_id")}
            if self.legacy_context(data) and self.exchange != "hyperliquid":
                data["venue_bindings"][self.exchange] = copy.deepcopy(local["bindings"])
                data.setdefault("venue_nonces", {})[self.exchange] = copy.deepcopy(local["nonces"])
                data["venue_events"][self.exchange] = copy.deepcopy(states)
            return result
        return self.root.update(change)

    def assert_binding(self, settings):
        binding = {"network": settings.network, "account": settings.account.lower()}
        data = self.root.read("execution_accounts", "venue_accounts", "network")
        expected = data["execution_accounts"].get(self.account_key)
        if expected:
            expected = {key: expected[key] for key in ("network", "account")}
        else:
            expected = data["venue_accounts"].get(self.exchange) if self.legacy_context(data) else None
        if expected and expected != binding:
            raise AqlaError("Exchange account/network changed; existing execution identities cannot be rebound")
        if self.exchange == "hyperliquid" and self.legacy_context(data):
            self.root.assert_binding(settings)

    def assert_operation_binding(self, settings, record):
        self.assert_binding(settings)
        if self.exchange == "hyperliquid" and self.legacy_context(self.root.read("network", "venue_accounts")):
            return self.root.assert_operation_binding(settings, record)
        target = getattr(settings, "vault_address", None) or settings.account
        if record["request"].get("network") != settings.network or record["request"].get("account", "").lower() != target.lower():
            raise AqlaError("Operation belongs to another venue account/network")

    def stamp_plan(self, plan, snapshot):
        route = snapshot["preview_route"]
        plan.exchange = self.exchange
        if route:
            plan.pipeline_id, plan.profile_id = route["pipeline_id"], route["profile_id"]
            plan.profile_revision = snapshot["profiles"][plan.profile_id]["revision"]
            plan.pipeline_revision = snapshot["pipelines"][plan.pipeline_id]["revision"]
            if self.exchange != "hyperliquid" or plan.pipeline_id != "legacy":
                plan.trade_id = digest([self.exchange, plan.network, plan.account, plan.pipeline_id, sorted(plan.source_refs)])[:32]
            if route.get("rank_snapshot"):
                plan.account_key = self.account_key
                plan.rank_snapshot = copy.deepcopy(route["rank_snapshot"])
                plan.base_parameters = copy.deepcopy(snapshot["profiles"][plan.profile_id]["parameters"])
                plan.requested_exchange = route.get("requested_exchange", self.exchange)
                plan.routing_leg_id = route.get("leg_id", plan.pipeline_id + ":" + plan.requested_exchange)
                plan.fallback_reason = route.get("fallback_reason")
                plan.setup_id = route["setup_id"]
                plan.rank_revision = route["rank_snapshot"]["source_revision"]

    @staticmethod
    def validate_legacy_route(data, plan):
        if not data["pipelines"]["legacy"]["enabled"]:
            raise AqlaError("The legacy entry pipeline is disabled")
        anchors = [row for row in data["events"].values() if row["source_id"] in plan.source_refs]
        aliases = {value: key for key, value in data.get("source_aliases", {}).items()}
        if any(pipeline["id"] != "legacy" and pipeline["enabled"] and any(
            "*" in pipeline["sources"] or row["source"]["chat_id"] in pipeline["sources"]
            or aliases.get(row["source"]["chat_id"]) in pipeline["sources"] or row.get("native_forward") and "owner_forward" in pipeline["sources"]
            for row in anchors) for pipeline in data["pipelines"].values()):
            raise AqlaError("Explicit routing replaced this legacy unsent plan")

    def validate_profile_route(self, data, plan):
        profile = data["profiles"].get(plan.profile_id, {})
        pipeline = data["pipelines"].get(plan.pipeline_id, {})
        if not pipeline.get("enabled") or profile.get("revision") != plan.profile_revision or pipeline.get("revision") != plan.pipeline_revision:
            raise AqlaError("Relevant pipeline/profile changed; create a new unsent plan")
        requested = plan.requested_exchange or self.exchange
        if not any(target["exchange"] == requested and target["profile_id"] == plan.profile_id for target in pipeline.get("targets", [])):
            raise AqlaError("The plan no longer belongs to the configured pipeline target")

    def validate_route(self, plan, *, snapshot=None):
        data = snapshot if snapshot is not None and plan.pipeline_id else self.root.snapshot()
        validate_ranking_binding(data, plan)
        if plan.backup_fields and data["profiles"]["backup"]["revision"] != plan.backup_profile_revision:
            raise AqlaError("Backup profile changed; create a new unsent plan")
        enabled = data["venue_enabled"].get(self.exchange) if self.legacy_context(data) else data["account_enabled"].get(self.account_key)
        if data["paused"] or not enabled:
            raise AqlaError("New entries are paused on this exchange")
        if self.exchange == "hyperliquid" and plan.pipeline_id in (None, "legacy"):
            self.validate_legacy_route(data, plan)
        if plan.pipeline_id:
            self.validate_profile_route(data, plan)
        elif data["policy_revision"] != plan.policy_revision:
            raise AqlaError("Policy changed; legacy unsent plan cannot execute")

    def limits(self, plan=None):
        # Existing callers supply a plan; account scope alone selects these limits.
        # skipcq: PTC-W0043
        del plan
        data = self.root.read("network", "venue_accounts", "venue_limits", "account_limits")
        selected = data["venue_limits"].get(self.exchange) if self.legacy_context(data) else data["account_limits"].get(self.account_key)
        if selected is None:
            raise AqlaError("Explicit exchange account limits are required")
        return AccountLimits(**selected)

    def pause_entries(self, reason):
        def pause(data):
            if self.legacy_context(data):
                data["venue_enabled"][self.exchange] = False
            data["account_enabled"][self.account_key] = False
            data["notifications"]["protection:" + self.account_key] = {"exchange": self.exchange, "account_key": self.account_key, "network": self.settings.network, "error": reason,
                                                                    "generation_id": uuid.uuid4().hex, "observed_at": now_ms()}
        self.root.update(pause)
