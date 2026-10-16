from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Address = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Policy(Model):
    product: Literal["perp", "spot"] = "perp"
    sizing_mode: Literal["risk_percent", "fixed_notional"] = "risk_percent"
    risk_percent: Positive = Field(default=Decimal("0.5"), le=100)
    fixed_notional_usdc: Positive | None = None
    max_notional_usdc: Positive = Decimal("500")
    leverage_mode: Literal["fixed", "signal"] = Field(default="fixed",
        description="fixed uses the user's leverage value; signal copies evidenced signal leverage capped at the market maximum")
    leverage: int = Field(default=2, ge=1, le=100,
        description="Used only in fixed mode; never a fallback for missing signal leverage")
    fallback_stop_loss_percent: Positive | None = Field(default=None, lt=100)
    fallback_stop_loss_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="Backup SL magnitude: course_percent is price loss; roe_percent is loss of original position margin, before fees/funding, using the plan's effective leverage")
    fallback_take_profit_percentages: list[Positive] = Field(default_factory=list, max_length=20)
    fallback_take_profit_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="Backup TP percentages: course_percent is price movement; roe_percent is nominal profit on original position margin, before fees/funding, using the plan's effective leverage")
    margin_mode: Literal["cross", "isolated"] = "isolated"
    max_open_trades: int = Field(default=3, ge=1, le=100)
    max_total_risk_percent: Positive = Field(default=Decimal("1.5"), le=100)
    entry_mode: Literal["source", "source_or_market"] = "source_or_market"
    entry_execution: Literal["standard", "maker_only"] = Field(default="standard", repr=False)
    take_profit_execution: Literal["standard", "maker_only"] = Field(default="standard", repr=False)
    entry_range_choice: Literal["near", "far", "midpoint"] = "midpoint"
    max_slippage_percent: Positive = Field(default=Decimal("0.3"), le=10)
    max_signal_age_seconds: int = Field(default=300, ge=1, le=86400)
    take_profit_allocation: Literal["equal", "halving", "custom", "adaptive", "first_target"] = "equal"
    take_profit_weights: list[Positive] = Field(default_factory=list, max_length=20)
    break_even_after_tp: int | None = Field(default=None, ge=1, le=20)
    stop_management: Literal["break_even", "tp_ladder"] = "break_even"
    allowed_updates: list[Literal["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"]] = Field(default_factory=lambda: ["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"])
    allow_stop_widening: bool = False
    cost_reserve_bps: Nonnegative = Field(default=Decimal("10"), le=1000)

    def __repr_args__(self):
        # Historical plan hashes use default=str. Keep the legacy text exactly,
        # while explicit ROE remains part of the immutable plan identity.
        for name, value in super().__repr_args__():
            if name not in ("fallback_stop_loss_unit", "fallback_take_profit_unit") or value != "course_percent":
                yield name, value

    @model_serializer(mode="wrap")
    def compatible_execution_fields(self, handler):
        result = handler(self)
        for key in ("entry_execution", "take_profit_execution"):
            if getattr(self, key) == "standard":
                result.pop(key, None)
        for key in ("fallback_stop_loss_unit", "fallback_take_profit_unit"):
            if getattr(self, key) == "course_percent":
                result.pop(key, None)
        return result

    @model_validator(mode="after")
    def consistent(self):
        if (any(value >= 100 for value in self.fallback_take_profit_percentages)
                or self.fallback_take_profit_percentages != sorted(set(self.fallback_take_profit_percentages))):
            raise ValueError("Backup TP percentages must be unique, ascending and between 0 and 100")
        if self.sizing_mode == "fixed_notional" and self.fixed_notional_usdc is None:
            raise ValueError("fixed_notional_usdc is required for fixed_notional sizing")
        if self.take_profit_allocation == "custom" and sum(self.take_profit_weights) != 100:
            raise ValueError("Custom TP weights must total exactly 100")
        if self.product == "spot" and (self.leverage != 1 or self.leverage_mode != "fixed"):
            raise ValueError("Spot does not use leverage")
        return self


class GoogleSheetsSettings(Model):
    enabled: bool = False
    spreadsheet_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{20,150}$")
    credentials_file: Path | None = None
    sync_interval_seconds: int = Field(default=30, ge=10, le=3600)
    batch_size: int = Field(default=100, ge=1, le=500)
    tab_namespace: Literal["legacy", "mainnet", "testnet"] = "legacy"

    @model_validator(mode="after")
    def configured(self):
        if self.enabled and (not self.spreadsheet_id or not self.credentials_file):
            raise ValueError("Enabled Google Sheets requires spreadsheet_id and credentials_file")
        return self


class Settings(Model):
    network: Literal["mainnet", "testnet"]
    account: Address
    vault_address: Address | None = None
    signer_role: Literal["api_wallet", "master"] = "api_wallet"
    state_path: Path = Path("data/state.sqlite3")
    private_key_file: Path | None = None
    integration_key_file: Path | None = None
    setup_password_hash_file: Path | None = None
    sources: list[str] = Field(default_factory=list)
    policy: Policy | None = None
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    max_context_messages: int = Field(default=20, ge=1, le=200)
    max_events: int = Field(default=5000, ge=100, le=100000)
    shared_account: bool = False
    telegram_session_file: Path | None = None
    telegram_api_id: int | None = None
    telegram_api_hash_file: Path | None = None
    telegram_owner_id: str | None = None
    google_sheets: GoogleSheetsSettings = Field(default_factory=GoogleSheetsSettings)
    venues: dict[Literal["aster", "lighter"], VenueSettings] = Field(default_factory=dict)
    execution_accounts: dict[str, ExecutionSettings] = Field(default_factory=dict)
    ranking_enabled: bool = False

    @model_validator(mode="after")
    def journal_network(self):
        if self.google_sheets.tab_namespace not in ("legacy", self.network):
            raise ValueError("Journal namespace must match its execution network")
        return self

    @property
    def base_url(self):
        return "https://api.hyperliquid.xyz" if self.network == "mainnet" else "https://api.hyperliquid-testnet.xyz"

    def private_key(self):
        direct = os.getenv("HL_PRIVATE_KEY")
        if direct and self.private_key_file:
            raise ValueError("Configure exactly one private key source")
        return self.private_key_file.read_text().strip() if self.private_key_file else direct

    @classmethod
    def load(cls, path: str | Path):
        path = Path(path).resolve()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name in ("state_path", "private_key_file", "integration_key_file", "setup_password_hash_file", "telegram_session_file", "telegram_api_hash_file"):
            if raw.get(name):
                raw[name] = str((path.parent / raw[name]).resolve())
        sheets = raw.get("google_sheets") or {}
        if sheets.get("credentials_file"):
            sheets["credentials_file"] = str((path.parent / sheets["credentials_file"]).resolve())
            raw["google_sheets"] = sheets
        for venue in raw.get("venues", {}).values():
            for key in ("private_key_file",):
                if venue.get(key):
                    venue[key] = str((path.parent / venue[key]).resolve())
        for execution in raw.get("execution_accounts", {}).values():
            if execution.get("private_key_file"):
                execution["private_key_file"] = str((path.parent / execution["private_key_file"]).resolve())
        return cls.model_validate(raw)


class VenueSettings(Model):
    network: Literal["mainnet", "testnet"] = "mainnet"
    account: Address | None = None
    signer: Address | None = None
    account_index: int | None = Field(default=None, ge=0)
    api_key_index: int | None = Field(default=None, ge=4, le=254)
    private_key_file: Path | None = None


class ExecutionSettings(VenueSettings):
    exchange: Literal["hyperliquid", "aster", "lighter"]
    signer_role: Literal["api_wallet", "master"] = "api_wallet"
    vault_address: Address | None = None
    shared_account: bool = False


Settings.model_rebuild()


class SourceEvent(Model):
    chat_id: str
    message_id: int = Field(ge=1)
    sent_at: int = Field(ge=0)
    received_at: int = Field(default_factory=now_ms, ge=0)
    edited_at: int | None = Field(default=None, ge=0)
    reply_to_id: int | None = Field(default=None, ge=1)
    thread_id: int | None = Field(default=None, ge=1)
    album_id: str | None = None
    text: str = Field(default="", max_length=16000)
    forward_from: str | None = None
    media: Literal["text", "caption", "photo", "other"] = "text"
    historical: bool = False

    @property
    def source_id(self):
        return digest([self.chat_id, self.message_id])[:32]

    @property
    def event_id(self):
        values = [self.source_id, self.edited_at, self.text, self.media]
        contextual = [self.reply_to_id, self.thread_id, self.forward_from, self.album_id]
        if any(value is not None for value in contextual):
            values.append(contextual)
        return digest(values)[:32]


EVIDENCE_FIELDS = (
    "classification", "symbol", "side", "leverage", "stop_loss", "reduce_percent", "entry",
    "entry_timing", "source_exchange", "take_profit_unit", "update_action", "bound_trade_id", "margin_mode",
    *(f"entry_prices.{index}" for index in range(2)), *(f"take_profits.{index}" for index in range(20)),
)
ClassificationReason = Literal["percentage_basis", "missing_market", "missing_direction", "missing_reference",
    "contradictory_source", "missing_entry_price", "missing_source_text", "non_trade", "result_report", "other"]


class Evidence(Model):
    # Schema guidance is deliberately separate from deserialization: historical
    # interpretations can contain old field names without changing their hash.
    field: str = Field(min_length=1, max_length=100, json_schema_extra={"enum": list(EVIDENCE_FIELDS)},
        description="Exact field path. Lists require one indexed proof per value: take_profits.0, take_profits.1; entry_prices.0, entry_prices.1. Never use take_profits or entry_prices without an index.",
        examples=["take_profits.0", "take_profits.1", "classification"])
    source_id: str
    quote: str = Field(min_length=1, max_length=1500,
                       description="Exact original substring. Each numeric price/percentage field needs its own number (and % when present), without other numbers. Never quote a whole multi-price message for one numeric field.")


class SourceClassification(Model):
    """A non-actionable batch item cannot carry any trade instruction."""
    event_id: str = Field(min_length=1, max_length=256)
    kind: Literal["report", "unrelated", "incomplete", "ambiguous"]
    source_refs: list[str] = Field(min_length=1, max_length=20)
    evidence: list[Evidence] = Field(default_factory=list, max_length=60,
        description="Include field=classification, current anchor source_id and an exact original quote; every item needs its own proof. Empty-text incomplete is the only quote exception.")
    reason: str = Field(min_length=1, max_length=2000)
    reason_codes: list[ClassificationReason] = Field(default_factory=list, max_length=8,
        description="Independent reasons for a non-actionable classification. percentage_basis alone cannot block a source % TP: source-roe-v1 defines it as nominal ROE on own initial margin. Other genuine ambiguity remains non-actionable.")
    source_read_receipt: str = Field(min_length=1, max_length=32768)

    @model_serializer(mode="wrap")
    def compatible_reason_codes(self, handler):
        result = handler(self)
        if not self.reason_codes:
            result.pop("reason_codes", None)
        return result


class Interpretation(Model):
    kind: Literal["new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous"]
    source_refs: list[str] = Field(min_length=1, max_length=20,
        description="Source IDs supporting this setup, not every message read in context or receipt. Include the live instruction; every additional source must supply original evidence or, for trade_update, belong to an existing own setup binding. Keep genuine multi-message setup origins, whose earliest publication fixes the signal deadline.")
    symbol: str | None = Field(default=None, max_length=100)
    side: Literal["long", "short"] | None = None
    leverage: Annotated[Decimal, Field(ge=1, allow_inf_nan=False)] | None = Field(default=None,
        description="Original evidenced signal leverage, including decimals. Python floors then caps it. Null only when absent; never round before quoting evidence.")
    entry: Literal["market", "limit", "range", "unspecified"] = "unspecified"
    entry_timing: Literal["standard", "deferred"] = Field(default="standard", description="Explicitly delayed/not-now entry intent requires entry_timing evidence and a source price; Python waits, never substitute an immediate GTC order.")
    source_exchange: str | None = Field(default=None, min_length=1, max_length=100, description="Evidenced exchange named by the source. Metadata only; the operator pipeline chooses the execution exchange.")
    entry_prices: list[Positive] = Field(default_factory=list, max_length=2,
        description="Positive numbers using JSON numbers or dot-decimal strings, never comma decimals. Evidence uses entry_prices.0 / entry_prices.1.")
    stop_loss: Positive | None = Field(default=None,
        description="Positive JSON number or dot-decimal string, never a comma decimal. Keep the original spelling only in the exact evidence quote.")
    take_profits: list[Positive] = Field(default_factory=list, max_length=20,
        description="Positive JSON numbers or dot-decimal strings (e.g. [1.3, 1.33]), never comma decimals. Each value needs its own indexed evidence path take_profits.0, take_profits.1, etc.")
    take_profit_unit: Literal["price", "course_percent", "roi_percent", "roe_percent"] = Field(default="price", description="Absolute targets without a source % marker use price. Explicit new source TP percentages use roe_percent: nominal profit on own initial margin before fees/funding, effective executed leverage and final own fill VWAP. Rule source-roe-v1. Legacy course_percent/roi_percent remain readable only.")
    update_action: Literal["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"] | None = Field(default=None,
        description="For trade_update emit only changed action fields: move_stop needs stop_loss; replace_targets needs take_profits; reduce needs reduce_percent. Leave unrelated prices/targets null or empty; do not restate the whole original setup.")
    reduce_percent: Positive | None = Field(default=None, le=100)
    bound_trade_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=60)
    reason: str = Field(default="", max_length=2000)
    reason_codes: list[ClassificationReason] = Field(default_factory=list, max_length=8,
        description="For non-actionable classifications, independent blocking causes. percentage_basis alone is invalid for source % TPs under source-roe-v1; re-read and interpret with roe_percent, without bypassing any other uncertainty.")

    @model_serializer(mode="wrap")
    def compatible_entry_fields(self, handler):
        result = handler(self)
        if self.entry_timing == "standard":
            result.pop("entry_timing", None)
        if self.source_exchange is None:
            result.pop("source_exchange", None)
        if self.take_profit_unit == "price":
            result.pop("take_profit_unit", None)
        if not self.reason_codes:
            result.pop("reason_codes", None)
        return result

    @field_validator("entry_prices", "take_profits", "stop_loss", "reduce_percent", mode="before")
    @classmethod
    def dot_decimal_values(cls, value):
        values = value if isinstance(value, list) else [value]
        if any(isinstance(item, str) and "," in item for item in values):
            raise ValueError("Numeric values require dot decimals (e.g. 1.33), not comma decimals; preserve the exact original comma spelling in evidence.quote")
        return value

    @model_validator(mode="after")
    def deferred_has_price(self):
        if self.entry_timing == "deferred" and (self.entry not in ("limit", "range") or not self.entry_prices):
            raise ValueError("Deferred entry requires an explicit source limit or range")
        return self

    @field_validator("leverage", mode="before")
    @classmethod
    def original_leverage(cls, value):
        if isinstance(value, bool):
            raise ValueError("Boolean values are not leverage")
        return value.replace(",", ".") if isinstance(value, str) else value

    @field_serializer("leverage", when_used="json")
    def serialize_leverage(self, value):
        if value is None:
            return None
        value = Decimal(value)
        return int(value) if value == value.to_integral_value() else str(value)

    @field_validator("entry_prices")
    @classmethod
    def entry_ordered(cls, value):
        if len(value) == 2 and value[0] > value[1]:
            raise ValueError("Entry range must be ordered low to high")
        return value


TERMINAL_ORDER = {"filled", "canceled", "rejected", "marginCanceled", "vaultWithdrawalCanceled", "openInterestCapCanceled",
                  "selfTradeCanceled", "reduceOnlyCanceled", "siblingFilledCanceled", "delistedCanceled", "liquidatedCanceled",
                  "scheduledCancel", "tickRejected", "minTradeNtlRejected", "perpMarginRejected", "reduceOnlyRejected",
                  "badAloPxRejected", "iocCancelRejected", "badTriggerPxRejected", "marketOrderNoLiquidityRejected",
                  "positionIncreaseAtOpenInterestCapRejected", "positionFlipAtOpenInterestCapRejected",
                  "tooAggressiveAtOpenInterestCapRejected", "openInterestIncreaseRejected", "insufficientSpotBalanceRejected",
                  "oracleRejected", "perpMaxPositionRejected"}


class LeverageTier(Model):
    notional_floor: Nonnegative
    notional_cap: Positive
    max_leverage: int = Field(ge=1)


class Market(Model):
    symbol: str
    asset_id: int = Field(ge=0)
    product: Literal["perp", "spot"]
    size_decimals: int = Field(ge=0, le=18)
    mid: Positive
    max_leverage: int = Field(default=1, ge=1)
    dex: str = ""
    min_notional: Positive = Decimal("10")
    base_token: str | None = None
    quote_token: str = "USDC"
    collateral_token: int | None = Field(default=None, ge=0)
    native_symbol: str | None = None
    price_tick: Positive | None = None
    quantity_step: Positive | None = None
    min_quantity: Nonnegative = Decimal(0)
    max_quantity: Positive | None = None
    margin_leverage_scale: int | None = Field(default=None, ge=1)
    leverage_tiers: list[LeverageTier] = Field(default_factory=list, max_length=100)


def margin_leverage(leverage, market):
    if market.margin_leverage_scale is None:
        return Decimal(leverage)
    scale = Decimal(market.margin_leverage_scale)
    return scale / (scale / Decimal(leverage)).to_integral_value(rounding=ROUND_CEILING)


class AccountSnapshot(Model):
    equity: Positive
    available_margin: Nonnegative
    position_size: Decimal | None = None
    observed_at: int = Field(default_factory=now_ms)
    account_mode: str = "disabled"
    positions: dict[str, Decimal] | None = None
    entry_orders: list[dict] = Field(default_factory=list)


class BackupValues(Model):
    leverage: int = Field(ge=1, le=100)
    entry_mode: Literal["source", "source_or_market"] = "source_or_market"
    fallback_stop_loss_percent: Positive | None = Field(default=None, lt=100)
    fallback_stop_loss_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="Frozen backup SL unit: price loss or nominal loss on original position margin at effective leverage, before fees/funding")
    fallback_take_profit_percentages: list[Annotated[Decimal, Field(gt=0, lt=100)]] = Field(default_factory=list, max_length=20)
    fallback_take_profit_unit: Literal["course_percent", "roe_percent"] = Field(default="course_percent",
        description="Frozen backup TP unit: price movement or nominal ROE on original position margin at effective leverage, before fees/funding")

    def __repr_args__(self):
        for name, value in super().__repr_args__():
            if name not in ("fallback_stop_loss_unit", "fallback_take_profit_unit") or value != "course_percent":
                yield name, value

    @model_serializer(mode="wrap")
    def compatible_units(self, handler):
        result = handler(self)
        for key in ("fallback_stop_loss_unit", "fallback_take_profit_unit"):
            if getattr(self, key) == "course_percent":
                result.pop(key, None)
        return result

    def uses_roe(self, fields):
        return ("stop_loss" in fields and self.fallback_stop_loss_unit == "roe_percent"
                or "take_profits" in fields and self.fallback_take_profit_unit == "roe_percent")

    @model_validator(mode="after")
    def ordered(self):
        if self.fallback_take_profit_percentages != sorted(set(self.fallback_take_profit_percentages)):
            raise ValueError("Backup TP percentages must be unique and ascending")
        return self


class TradePlan(Model):
    plan_id: str
    trade_id: str
    source_refs: list[str]
    source_revisions: dict[str, str] = Field(default_factory=dict)
    policy_revision: int
    policy: Policy
    signal_leverage: int | None = Field(default=None, ge=1)
    signal_leverage_original: Annotated[Decimal, Field(ge=1)] | None = None
    effective_leverage: int | None = Field(default=None, ge=1)
    source_symbol: str | None = None
    backup_fields: list[Literal["leverage", "entry", "stop_loss", "take_profits"]] = Field(default_factory=list)
    backup_values: BackupValues | None = None
    backup_profile_revision: int | None = None
    network: str
    account: str
    principal: str | None = None
    market: Market
    side: Literal["long", "short"]
    entry_kind: Literal["market", "limit"]
    entry_execution: Literal["standard", "maker_only"] = Field(default="standard", repr=False)
    take_profit_execution: Literal["standard", "maker_only"] = Field(default="standard", repr=False)
    entry_timing: Literal["standard", "deferred"] = "standard"
    source_exchange: str | None = None
    entry_gate: dict | None = None
    account_key: str | None = None
    rank_snapshot: dict | None = None
    rank_revision: int | None = None
    ranking_policy_revision: int | None = Field(default=None, ge=1, strict=True)
    ranking_enabled: bool | None = Field(default=None, strict=True)
    base_parameters: dict | None = None
    requested_exchange: Literal["hyperliquid", "aster", "lighter"] | None = None
    routing_leg_id: str | None = None
    setup_id: str | None = None
    fallback_reason: str | None = None
    source_tp: dict | None = None
    source_entry_reference: Positive | None = None
    entry_price: Positive
    entry_limit: Positive
    stop_loss: Positive
    take_profits: list[Positive]
    source_take_profit_count: int | None = Field(default=None, ge=0, le=20)
    quantity: Positive
    tp_quantities: list[Positive]
    risk_usdc: Positive
    deadline_at: int
    created_at: int
    exchange: Literal["hyperliquid", "aster", "lighter"] = "hyperliquid"
    pipeline_id: str | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
    pipeline_revision: int | None = None

    @model_serializer(mode="wrap")
    def compatible_entry_fields(self, handler):
        result = handler(self)
        for key in ("entry_execution", "take_profit_execution"):
            if getattr(self, key) == "standard":
                result.pop(key, None)
        if self.entry_timing == "standard":
            result.pop("entry_timing", None)
        if self.source_exchange is None:
            result.pop("source_exchange", None)
        if self.entry_gate is None:
            result.pop("entry_gate", None)
        for key in ("account_key", "rank_snapshot", "rank_revision", "ranking_policy_revision", "ranking_enabled", "base_parameters", "requested_exchange", "routing_leg_id", "setup_id", "fallback_reason", "source_tp", "source_entry_reference"):
            if getattr(self, key) is None:
                result.pop(key, None)
        return result

    @model_validator(mode="after")
    def execution_consistent(self):
        if self.entry_execution != self.policy.entry_execution or self.take_profit_execution != self.policy.take_profit_execution:
            raise ValueError("Saved execution modes differ from the immutable policy")
        return self

    @model_validator(mode="after")
    def ranking_consistent(self):
        # Missing/partial legacy bindings remain readable, but admission rejects them.
        if self.ranking_policy_revision is not None and self.ranking_enabled is not None and self.rank_snapshot:
            if not self.ranking_enabled or self.rank_snapshot.get("policy_revision") != self.ranking_policy_revision:
                raise ValueError("Source rank snapshot differs from the plan's global ranking binding")
        return self

    @model_validator(mode="after")
    def leverage_consistent(self):
        if self.backup_values is not None and self.backup_values.uses_roe(self.backup_fields):
            if self.market.product != "perp" or self.effective_leverage is None:
                raise ValueError("Backup ROE requires a perpetual plan with explicit effective leverage")
        if self.signal_leverage_original is not None and self.signal_leverage != int(self.signal_leverage_original.to_integral_value(rounding=ROUND_FLOOR)):
            raise ValueError("Signal leverage differs from the original value rounded down")
        if self.policy.leverage_mode == "signal":
            if self.signal_leverage is None:
                if "leverage" not in self.backup_fields or self.backup_values is None:
                    raise ValueError("Signal leverage or explicit backup leverage is required for a signal-mode plan")
                selected = self.backup_values.leverage
            else:
                selected = self.signal_leverage
            expected = min(selected, self.market.max_leverage)
        else:
            expected = self.policy.leverage
        # Legacy plans contain only policy.leverage. Preserve their IDs and execution identities.
        if self.effective_leverage is None and self.policy.leverage_mode == "fixed":
            object.__setattr__(self, "effective_leverage", expected)
        if self.effective_leverage != expected:
            raise ValueError("Effective leverage differs from the immutable plan's leverage rule")
        return self


class AqlaError(ToolError, ValueError):
    """A safe, human-readable domain error; never contains credentials."""


class ExternalInterference(AqlaError):
    """The observed position no longer matches proved own fills."""


class SymbolUnavailable(AqlaError):
    """Confirmed absence/delisting, never a transport, collateral or ambiguity error."""


class ExchangeMinimum(AqlaError):
    """TP quantity/notional cannot fit this venue, without increasing entry size."""
