"""Profiles, routing and per-venue views over the one authoritative state writer."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import AqlaError, Model, Policy, Positive, digest

Venue = Literal["hyperliquid", "aster", "lighter"]
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
    margin_mode: Literal["cross", "isolated"] = "isolated"
    entry_mode: Literal["source", "source_or_market"] = "source_or_market"
    entry_range_choice: Literal["near", "far", "midpoint"] = "midpoint"
    max_slippage_percent: Positive = Field(default=Decimal("0.3"), le=10)
    max_signal_age_seconds: int = Field(default=300, ge=1, le=86400)
    take_profit_allocation: Literal["equal", "halving", "custom", "adaptive", "first_target"] = "equal"
    take_profit_weights: list[Positive] = Field(default_factory=list, max_length=20)
    break_even_after_tp: int | None = Field(default=None, ge=1, le=20)
    stop_management: Literal["break_even", "tp_ladder"] = "break_even"
    allowed_updates: list[Literal["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"]] = Field(default_factory=lambda: ["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"])
    allow_stop_widening: bool = False
    cost_reserve_bps: Annotated[Decimal, Field(ge=0, le=1000)] = Decimal("10")

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
    targets: list[PipelineTarget] = Field(min_length=1, max_length=3)
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
        return False
    selected = data.get("policy")
    parameters = Parameters.from_policy(selected).model_dump(mode="json") if selected and selected.get("product", "perp") == "perp" else None
    limits = {key: selected[key] for key in ("max_open_trades", "max_total_risk_percent")} if selected else None
    data.update(schema=2, configuration_revision=0,
                profiles={"standard": {"id": "standard", "name": "Standard", "parameters": parameters, "revision": data["policy_revision"]}},
                pipelines={"legacy": {"id": "legacy", "name": "Bestehende Hyperliquid-Quellen", "sources": ["*"],
                                      "targets": [{"exchange": "hyperliquid", "profile_id": "standard"}], "enabled": True, "revision": 0}},
                venue_limits={"hyperliquid": limits, "aster": None, "lighter": None},
                venue_enabled={"hyperliquid": True, "aster": False, "lighter": False},
                venue_bindings={}, venue_events={}, venue_accounts={})
    return True


def sync_legacy_policy(data):
    selected = data["policy"]
    data["profiles"]["standard"].update(parameters=Parameters.from_policy(selected).model_dump(mode="json") if selected and selected.get("product", "perp") == "perp" else None,
                                         revision=data["policy_revision"])
    data["venue_limits"]["hyperliquid"] = {key: selected[key] for key in ("max_open_trades", "max_total_risk_percent")} if selected else None


def route_control(data, action, *, expected_revision, profile=None, pipeline=None, exchange=None, limits=None, enabled=None, identity=None):
    if expected_revision != data["configuration_revision"]:
        raise AqlaError("Read configuration_revision and submit a complete current configuration change")
    if action == "set_profile":
        if profile is None:
            raise AqlaError("A complete profile is required")
        previous = data["profiles"].get(profile.id, {})
        data["profiles"][profile.id] = {**profile.model_dump(mode="json"), "revision": previous.get("revision", 0) + 1}
        if profile.id == "standard" and data["venue_limits"]["hyperliquid"]:
            data["policy"] = profile.parameters.policy(AccountLimits(**data["venue_limits"]["hyperliquid"])).model_dump(mode="json")
            data["policy_revision"] += 1
            data["profiles"]["standard"]["revision"] = data["policy_revision"]
    elif action == "set_pipeline":
        if pipeline is None or any(target.profile_id not in data["profiles"] for target in pipeline.targets):
            raise AqlaError("A complete pipeline with existing profiles is required")
        if any(source not in ("*", "owner_forward") and source not in {*data["sources"], *data.get("source_aliases", {}).values()} for source in pipeline.sources):
            raise AqlaError("Subscribe the requested Telegram sources first")
        previous = data["pipelines"].get(pipeline.id, {})
        data["pipelines"][pipeline.id] = {**pipeline.model_dump(mode="json"), "revision": previous.get("revision", 0) + 1}
    elif action == "set_exchange_limits":
        if exchange is None or limits is None:
            raise AqlaError("Exchange and complete account limits are required")
        data["venue_limits"][exchange] = limits.model_dump(mode="json")
        if exchange == "hyperliquid" and data.get("policy"):
            data["policy"].update(limits.model_dump(mode="json"))
            data["policy_revision"] += 1
    elif action == "set_exchange_enabled":
        if exchange is None or enabled is None:
            raise AqlaError("Exchange and explicit enabled value are required")
        if enabled and data["venue_limits"].get(exchange) is None:
            raise AqlaError("Set explicit account limits before activating the exchange")
        data["venue_enabled"][exchange] = enabled
    elif action in ("delete_pipeline", "delete_profile"):
        if identity in ("legacy", "standard"):
            raise AqlaError("Keep the migration compatibility entries; disable the legacy pipeline instead")
        if action == "delete_profile" and any(target["profile_id"] == identity for row in data["pipelines"].values() for target in row["targets"]):
            raise AqlaError("Remove pipeline references before deleting a profile")
        data["pipelines" if action == "delete_pipeline" else "profiles"].pop(identity, None)
    else:
        raise AqlaError("Unknown routing control")
    data["configuration_revision"] += 1
    return {key: data[key] for key in ("configuration_revision", "profiles", "pipelines", "venue_limits", "venue_enabled")}


class VenueState:
    """A filtered view, never a second writer or state file."""
    def __init__(self, root, exchange, settings):
        self.root, self.exchange, self.settings = root, exchange, settings
        self.route = ContextVar("aqla_route_" + exchange, default=None)

    @contextmanager
    def scope(self, route):
        token = self.route.set(route)
        try:
            yield
        finally:
            self.route.reset(token)

    def project(self, data):
        result = dict(data)
        for key in ("trades", "plans", "operations"):
            result[key] = {identity: row for identity, row in data[key].items() if (row.get("plan", {}).get("exchange", "hyperliquid") if key != "operations" else row.get("exchange", "hyperliquid")) == self.exchange}
        result["bindings"] = data["bindings"] if self.exchange == "hyperliquid" else data["venue_bindings"].setdefault(self.exchange, {})
        result["nonces"] = data["nonces"] if self.exchange == "hyperliquid" else data.setdefault("venue_nonces", {}).setdefault(self.exchange, {})
        event_states = data["venue_events"].get(self.exchange, {})
        result["events"] = {key: {**value, **event_states.get(key, {})} for key, value in data["events"].items()}
        route = self.route.get()
        profile = data["profiles"].get(route["profile_id"] if route else "standard", {})
        limits = data["venue_limits"].get(self.exchange)
        result["policy"] = Parameters(**profile["parameters"]).policy(AccountLimits(**limits)).model_dump(mode="json") if profile.get("parameters") and limits else None
        result["policy_revision"] = profile.get("revision", 0)
        result["network"], result["account"], result["trading_account"] = self.settings.network, self.settings.account.lower(), self.settings.account.lower()
        result["paused"] = data["paused"] or not data["venue_enabled"].get(self.exchange, False)
        if route:
            result["paused"] |= not data["pipelines"].get(route["pipeline_id"], {}).get("enabled", False)
        return result

    def snapshot(self):
        return copy.deepcopy(self.project(self.root.snapshot()))

    def update(self, callback):
        def change(data):
            local = self.project(data)
            original_events = copy.deepcopy(local["events"])
            result = callback(local)
            for key in ("trades", "plans", "operations"):
                for identity, row in local[key].items():
                    if key == "operations":
                        row["exchange"] = self.exchange
                    data[key][identity] = row
            states = data["venue_events"].setdefault(self.exchange, {})
            for identity, row in local["events"].items():
                if row != original_events[identity]:
                    states[identity] = {key: value for key, value in row.items() if key not in ("source", "source_id", "event_id")}
            return result
        return self.root.update(change)

    def assert_binding(self, settings):
        binding = {"network": settings.network, "account": settings.account.lower()}
        expected = self.root.snapshot()["venue_accounts"].get(self.exchange)
        if expected and expected != binding:
            raise AqlaError("Exchange account/network changed; existing execution identities cannot be rebound")
        if self.exchange == "hyperliquid":
            self.root.assert_binding(settings)

    def assert_operation_binding(self, settings, record):
        self.assert_binding(settings)
        if self.exchange == "hyperliquid":
            return self.root.assert_operation_binding(settings, record)
        if record["request"].get("network") != settings.network or record["request"].get("account", "").lower() != settings.account.lower():
            raise AqlaError("Operation belongs to another venue account/network")

    def stamp_plan(self, plan):
        route = self.route.get()
        plan.exchange = self.exchange
        if route:
            plan.pipeline_id, plan.profile_id = route["pipeline_id"], route["profile_id"]
            plan.profile_revision = self.root.snapshot()["profiles"][plan.profile_id]["revision"]
            plan.pipeline_revision = self.root.snapshot()["pipelines"][plan.pipeline_id]["revision"]
            if self.exchange != "hyperliquid" or plan.pipeline_id != "legacy":
                plan.trade_id = digest([self.exchange, plan.network, plan.account, plan.pipeline_id, sorted(plan.source_refs)])[:32]

    def validate_route(self, plan):
        data = self.root.snapshot()
        if data["paused"] or not data["venue_enabled"].get(self.exchange):
            raise AqlaError("New entries are paused on this exchange")
        if self.exchange == "hyperliquid" and plan.pipeline_id in (None, "legacy"):
            if not data["pipelines"]["legacy"]["enabled"]:
                raise AqlaError("The legacy entry pipeline is disabled")
            anchors = [row for row in data["events"].values() if row["source_id"] in plan.source_refs]
            aliases = {value: key for key, value in data.get("source_aliases", {}).items()}
            if any(pipeline["id"] != "legacy" and pipeline["enabled"] and any(
                "*" in pipeline["sources"] or row["source"]["chat_id"] in pipeline["sources"]
                or aliases.get(row["source"]["chat_id"]) in pipeline["sources"] or row.get("native_forward") and "owner_forward" in pipeline["sources"]
                for row in anchors) for pipeline in data["pipelines"].values()):
                raise AqlaError("Explicit routing replaced this legacy unsent plan")
        if plan.pipeline_id:
            profile = data["profiles"].get(plan.profile_id, {})
            pipeline = data["pipelines"].get(plan.pipeline_id, {})
            if not pipeline.get("enabled") or profile.get("revision") != plan.profile_revision or pipeline.get("revision") != plan.pipeline_revision:
                raise AqlaError("Relevant pipeline/profile changed; create a new unsent plan")
            if {"exchange": self.exchange, "profile_id": plan.profile_id} not in pipeline.get("targets", []):
                raise AqlaError("The plan no longer belongs to the configured pipeline target")
        elif data["policy_revision"] != plan.policy_revision:
            raise AqlaError("Policy changed; legacy unsent plan cannot execute")

    def limits(self, plan=None):
        selected = self.root.snapshot()["venue_limits"].get(self.exchange)
        if selected is None:
            raise AqlaError("Explicit exchange account limits are required")
        return AccountLimits(**selected)

    def pause_entries(self, reason):
        def pause(data):
            data["venue_enabled"][self.exchange] = False
            data["notifications"]["protection:" + self.exchange] = {"exchange": self.exchange, "error": reason}
        self.root.update(pause)
