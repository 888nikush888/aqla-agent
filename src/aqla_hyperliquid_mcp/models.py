from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    margin_mode: Literal["cross", "isolated"] = "isolated"
    max_open_trades: int = Field(default=3, ge=1, le=100)
    max_total_risk_percent: Positive = Field(default=Decimal("1.5"), le=100)
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
    cost_reserve_bps: Nonnegative = Field(default=Decimal("10"), le=1000)

    @model_validator(mode="after")
    def consistent(self):
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
    sources: list[str] = Field(default_factory=list)
    policy: Policy | None = None
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    max_context_messages: int = Field(default=20, ge=1, le=200)
    max_events: int = Field(default=5000, ge=100, le=100000)
    telegram_session_file: Path | None = None
    telegram_api_id: int | None = None
    telegram_api_hash_file: Path | None = None
    telegram_owner_id: str | None = None
    google_sheets: GoogleSheetsSettings = Field(default_factory=GoogleSheetsSettings)
    venues: dict[Literal["aster", "lighter"], VenueSettings] = Field(default_factory=dict)

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
        for name in ("state_path", "private_key_file", "integration_key_file", "telegram_session_file", "telegram_api_hash_file"):
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
        return cls.model_validate(raw)


class VenueSettings(Model):
    network: Literal["mainnet", "testnet"] = "mainnet"
    account: Address | None = None
    signer: Address | None = None
    account_index: int | None = Field(default=None, ge=0)
    api_key_index: int | None = Field(default=None, ge=4, le=254)
    private_key_file: Path | None = None


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
        return digest([self.source_id, self.edited_at, self.text, self.media])[:32]


class Evidence(Model):
    field: str = Field(min_length=1, max_length=100)
    source_id: str
    quote: str = Field(min_length=1, max_length=1500)


class Interpretation(Model):
    kind: Literal["new_signal", "trade_update", "report", "unrelated", "incomplete", "ambiguous"]
    source_refs: list[str] = Field(min_length=1, max_length=20)
    symbol: str | None = Field(default=None, max_length=100)
    side: Literal["long", "short"] | None = None
    leverage: int | None = Field(default=None, ge=1, strict=True,
        description="Explicit positive integer signal leverage; null if missing or ambiguous. Quote evidence under field leverage.")
    entry: Literal["market", "limit", "range", "unspecified"] = "unspecified"
    entry_prices: list[Positive] = Field(default_factory=list, max_length=2)
    stop_loss: Positive | None = None
    take_profits: list[Positive] = Field(default_factory=list, max_length=20)
    update_action: Literal["move_stop", "break_even", "replace_targets", "reduce", "close", "cancel_entry"] | None = None
    reduce_percent: Positive | None = Field(default=None, le=100)
    bound_trade_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=60)
    reason: str = Field(default="", max_length=2000)

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


class TradePlan(Model):
    plan_id: str
    trade_id: str
    source_refs: list[str]
    source_revisions: dict[str, str] = Field(default_factory=dict)
    policy_revision: int
    policy: Policy
    signal_leverage: int | None = Field(default=None, ge=1)
    effective_leverage: int | None = Field(default=None, ge=1)
    network: str
    account: str
    principal: str | None = None
    market: Market
    side: Literal["long", "short"]
    entry_kind: Literal["market", "limit"]
    entry_price: Positive
    entry_limit: Positive
    stop_loss: Positive
    take_profits: list[Positive]
    source_take_profit_count: int | None = Field(default=None, ge=1, le=20)
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

    @model_validator(mode="after")
    def leverage_consistent(self):
        if self.policy.leverage_mode == "signal":
            if self.signal_leverage is None:
                raise ValueError("Signal leverage is required for a signal-mode plan")
            expected = min(self.signal_leverage, self.market.max_leverage)
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
