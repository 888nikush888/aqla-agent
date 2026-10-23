"""Pure, currency-scoped journal views of authoritative financial evidence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from aqla_hyperliquid_mcp.models import Model, digest, now_ms

from .monthly import BERLIN, CLOSED, month_bounds, native_monthly_bytes, report_row
from .ranking import TIERS, Tier, outcomes, source_key

MAX_PRESENTATION_BYTES = 600_000
Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Execution = Literal["standard", "maker_only"]


class PresentationScope(Model):
    exchange: Literal["all", "hyperliquid"] = "all"
    network: Literal["mainnet", "testnet"] = "mainnet"
    currency: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,15}$")] = "USDC"
    period: str = "current"

    @field_validator("period")
    @classmethod
    def valid_period(cls, value):
        if value not in ("current", "all", "today", "7d", "30d"):
            month_bounds(value)
        return value


class EvidenceMetric(Model):
    value: Money | None = None
    known: int = Field(ge=0)
    total: int = Field(ge=0)
    status: Literal["complete", "partial", "missing", "empty"]

    @model_validator(mode="after")
    def consistent(self):
        expected = "empty" if not self.total else "missing" if not self.known else "complete" if self.known == self.total else "partial"
        if self.known > self.total or self.status != expected or (self.value is None) != (self.known == 0):
            raise ValueError("Inconsistent presentation evidence metric")
        return self


class PresentationTrade(Model):
    trade_id: str
    source_id: str
    source: str
    symbol: str
    side: Literal["long", "short"]
    status: str
    cohort: Literal["realized", "open", "undated", "unfilled"]
    entry_at: int | None = None
    closed_at: int | None = None
    entry_price: Money | None = None
    exit_price: Money | None = None
    quantity: Money | None = None
    net_pnl: Money | None = None
    fees: Money | None = None
    funding: Money | None = None
    currency: str
    exchange: str
    network: Literal["mainnet", "testnet"]
    entry_execution: Execution = "standard"
    take_profit_execution: Execution = "standard"
    execution_roles: dict[Literal["maker", "taker", "unknown"], int]
    complete: bool
    issues: list[str]


class PresentationChannel(Model):
    source_id: str
    source: str
    rank: Tier
    origin: Literal["manual", "auto", "bootstrap", "unknown"]
    score: Money | None = None
    proof: int = Field(ge=0)
    drawdown_r: Money | None = None
    last_change: int | None = None
    net_r: Money | None = None
    scope_setups: int = Field(ge=0)
    excluded_setups: int = Field(ge=0)


class PresentationParameter(Model):
    section: str
    name: str
    value: str
    context: str


class PresentationMonth(Model):
    month: str
    revision: int = Field(ge=0)
    published_at: int | None = None


class PresentationPoint(Model):
    label: str
    value: Money | None = None


class PresentationSnapshot(Model):
    schema_version: Literal[1] = 1
    scope: PresentationScope
    scope_binding: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    revision: int = Field(strict=True, ge=0)
    generated_at: int = Field(strict=True, ge=0)
    resolved_period: str
    available_currencies: list[str]
    available_periods: list[str]
    period_start: int | None = None
    period_end_exclusive: int | None = None
    journal: list[PresentationTrade]
    channels: list[PresentationChannel]
    parameters: list[PresentationParameter]
    months: list[PresentationMonth]
    net_pnl: EvidenceMetric
    fees: EvidenceMetric
    funding: EvidenceMetric
    hit_rate_percent: Money | None = None
    open_trades: int = Field(ge=0)
    complete_trades: int = Field(ge=0)
    evaluated_trades: int = Field(ge=0)
    daily_pnl: list[PresentationPoint]
    monthly_pnl: list[PresentationPoint]
    cumulative_realized_net: list[PresentationPoint]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    net_r_basis: Literal["complete_setup_families_as_ranking"] = "complete_setup_families_as_ranking"
    equity_curve: Literal[False] = False

    @model_validator(mode="after")
    def valid_snapshot(self, info: ValidationInfo):
        data = self.model_dump(mode="json", exclude={"fingerprint", "revision", "generated_at"})
        if digest(data) != self.fingerprint:
            raise ValueError("Presentation content does not match its fingerprint")
        if any(row.network != self.scope.network or row.currency != self.scope.currency
               or self.scope.exchange != "all" and row.exchange != self.scope.exchange for row in self.journal):
            raise ValueError("Presentation rows do not match the selected financial scope")
        if self.scope.currency not in self.available_currencies or self.scope.period not in self.available_periods:
            raise ValueError("Presentation selection is unavailable")
        if (not info.context or info.context.get("transport_budget", True)) and native_monthly_bytes(self.model_dump(mode="json")) > MAX_PRESENTATION_BYTES:
            raise ValueError("Presentation exceeds its bounded transport budget")
        return self


def _metric(values):
    known = [Decimal(value) for value in values if value is not None]
    total = len(values)
    return EvidenceMetric(value=sum(known, Decimal(0)) if known else None, known=len(known), total=total,
                          status="empty" if not total else "missing" if not known else "complete" if len(known) == total else "partial")


def _decimal(value):
    return Decimal(str(value)) if value is not None else None


def _cohort(trade, row, start, end):
    closed, entered = row["closed_at"], row["entry_at"] or trade["plan"]["created_at"]
    if closed is not None and (start is None or start <= closed) and (end is None or closed < end):
        return "realized"
    entries = [fill for fill in trade.get("fill_events", {}).values() if fill["kind"] == "entry"]
    if not entries or end is not None and all(isinstance(fill.get("time"), int) and fill["time"] >= end for fill in entries):
        if (end is None or entered < end) and (trade["status"] not in CLOSED or start is None or start <= entered):
            return "unfilled"
        return None
    if (end is None or entered < end) and (trade["status"] not in CLOSED and closed is None or end is not None and closed is not None and closed >= end):
        return "open"
    if closed is None and trade.get("fill_events") and trade["status"] in CLOSED and (end is None or entered < end):
        return "undated"
    return None


def _price(fills):
    size = sum((Decimal(row["sz"]) for row in fills), Decimal(0))
    return sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in fills), Decimal(0)) / size if size else None


def _parameters(data):
    result = []
    for identity, profile in sorted(data.get("profiles", {}).items()):
        parameters = profile.get("parameters") or (data.get("policy") if identity == "standard" else None) or {}
        for name, value in sorted(parameters.items()):
            result.append(PresentationParameter(section="Profil", name=name, value=str(value), context=profile.get("name") or identity))
        for name in ("entry_execution", "take_profit_execution"):
            if name not in parameters:
                result.append(PresentationParameter(section="Profil", name=name, value="standard", context=profile.get("name") or identity))
    for identity, limits in sorted(data.get("account_limits", {}).items()):
        for name, value in sorted((limits or {}).items()):
            result.append(PresentationParameter(section="Kontolimit", name=name, value=str(value), context=identity))
        result.append(PresentationParameter(section="Konto", name="Aktiv", value="Ja" if data.get("account_enabled", {}).get(identity, False) else "Nein", context=identity))
    for identity, limits in sorted(data.get("venue_limits", {}).items()):
        for name, value in sorted((limits or {}).items()):
            result.append(PresentationParameter(section="Börsenlimit", name=name, value=str(value), context=identity))
    for identity, pipeline in sorted(data.get("pipelines", {}).items()):
        result.append(PresentationParameter(section="Pipeline", name=pipeline.get("name") or identity,
            value="Aktiv" if pipeline.get("enabled") else "Inaktiv", context=", ".join(target["exchange"] for target in pipeline.get("targets", []))))
    return result


def build_presentation(data, scope: PresentationScope | dict | None = None, clock=None, *, transport_budget=True) -> PresentationSnapshot:
    """Read only; preserve whole-family net-R eligibility and independent currencies."""
    scope = PresentationScope.model_validate(scope or {})
    clock = now_ms() if clock is None else clock
    current = datetime.fromtimestamp(clock / 1000, BERLIN).strftime("%Y-%m")
    period = current if scope.period == "current" else scope.period
    if period in ("today", "7d", "30d"):
        today = datetime.fromtimestamp(clock / 1000, BERLIN).replace(hour=0, minute=0, second=0, microsecond=0)
        days = {"today": 1, "7d": 7, "30d": 30}[period]
        start, end = int((today - timedelta(days=days - 1)).timestamp() * 1000), int((today + timedelta(days=1)).timestamp() * 1000)
    else:
        start, end = (None, None) if period == "all" else month_bounds(period)
    journal, selected_ids, report_rows = [], set(), {}
    for trade in data.get("trades", {}).values():
        plan = trade["plan"]
        currency = plan["market"].get("quote_token", "USDC")
        exchange = plan.get("exchange", "hyperliquid")
        if plan["network"] != scope.network or currency != scope.currency or scope.exchange != "all" and exchange != scope.exchange:
            continue
        row = report_row(data, trade)
        report_rows[id(trade)] = row
        cohort = _cohort(trade, row, start, end)
        if cohort is None:
            continue
        if cohort == "open":
            row = {**row, "closed_at": None, "fees": None, "funding": None,
                   "issues": ["Am Periodenende offen; kein realisiertes Ergebnis in dieser Summe"]}
        elif cohort == "undated":
            row = {**row, "issues": [*row["issues"], "Schließzeit unbekannt; keiner Monatssumme zugeordnet"]}
        elif cohort == "unfilled":
            row = {**row, "entry_at": None, "closed_at": None, "fees": None, "funding": None,
                   "issues": ["Kein eigener Entry-Fill im gewählten Zeitraum belegt"]}
        fills = trade.get("fill_events", {})
        if cohort in ("open", "unfilled") and end is not None:
            fills = {identity: fill for identity, fill in fills.items() if isinstance(fill.get("time"), int) and fill["time"] < end}
        entries = [fill for fill in fills.values() if fill["kind"] == "entry"]
        entered = min((fill["time"] for fill in entries if isinstance(fill.get("time"), int)), default=None)
        roles = {"maker": 0, "taker": 0, "unknown": 0}
        for identity in fills:
            role = trade.get("fill_execution_roles", {}).get(identity, "unknown")
            roles[role if role in roles else "unknown"] += 1
        journal.append(PresentationTrade(trade_id=trade["trade_id"], source_id=trade["chat_id"],
            source=data.get("source_names", {}).get(trade["chat_id"], "Kanal ohne Anzeigename"), symbol=row["symbol"], side=row["side"],
            status=trade["status"], cohort=cohort, entry_at=row["entry_at"] or entered, closed_at=row["closed_at"],
            entry_price=_price(entries),
            exit_price=_decimal(row["exit_vwap"]) if cohort == "realized" and row.get("exit_vwap") else _price([fill for fill in fills.values() if fill["kind"] != "entry"]) if cohort == "realized" else None,
            quantity=sum((Decimal(fill["sz"]) for fill in entries), Decimal(0)) if entries else None,
            net_pnl=_decimal(row["net_pnl"]) if cohort == "realized" else None,
            fees=_decimal(row["fees"]), funding=_decimal(row["funding"]), currency=currency, exchange=exchange, network=plan["network"],
            entry_execution=plan.get("entry_execution", "standard"), take_profit_execution=plan.get("take_profit_execution", "standard"),
            execution_roles=roles, complete=row["complete"] and cohort == "realized",
            issues=[*row["issues"], *([row["attribution_note"]] if cohort == "realized" and row.get("attribution_note") else [])]))
        selected_ids.add(trade["trade_id"])
    journal.sort(key=lambda row: (row.closed_at or row.entry_at or 0, row.trade_id))
    evaluated = [row for row in journal if row.cohort in ("realized", "undated")]
    net, fees, funding = (_metric([getattr(row, field) if row.cohort == "realized" or scope.period == "all" else None for row in evaluated]) for field in ("net_pnl", "fees", "funding"))
    known = [row for row in evaluated if row.net_pnl is not None]
    grouped_day, grouped_month = defaultdict(list), defaultdict(list)
    for row in evaluated:
        if row.closed_at is None:
            continue
        stamp = datetime.fromtimestamp(row.closed_at / 1000, BERLIN)
        grouped_day[stamp.strftime("%Y-%m-%d")].append(row.net_pnl)
        grouped_month[stamp.strftime("%Y-%m")].append(row.net_pnl)
    daily = [PresentationPoint(label=label, value=_metric(values).value) for label, values in sorted(grouped_day.items())]
    months_chart = [PresentationPoint(label=label, value=_metric(values).value) for label, values in sorted(grouped_month.items())]
    cumulative, running = [], Decimal(0)
    for point in daily:
        if point.value is not None:
            running += point.value
        cumulative.append(PresentationPoint(label=point.label, value=running if point.value is not None else None))
    complete, excluded = outcomes(data, selected_ids=selected_ids, report_rows=report_rows)
    channels = []
    sources = set(data.get("source_ranks", {})) | {source_key(data, row.source_id) for row in journal}
    last_changes = {}
    if sources:
        for row in data.get("ranking_history", {}).values():
            identity, stamp = row.get("source_id"), row.get("time")
            if isinstance(identity, str) and identity in sources and isinstance(stamp, int):
                last_changes[identity] = max(last_changes.get(identity, stamp), stamp)
    for identity in sorted(sources):
        rank = data.get("source_ranks", {}).get(identity, {})
        stats = rank.get("metrics", {})
        eligible = [row for row in complete.get(identity, []) if selected_ids.intersection(row["trade_ids"])
                    and (start is None or start <= row["closed_at"]) and (end is None or row["closed_at"] < end)]
        rejected = [row for row in excluded.get(identity, []) if selected_ids.intersection(row["trade_ids"])]
        origin = rank.get("origin", "bootstrap" if rank.get("bootstrap") else "auto" if rank.get("evaluated") else "unknown")
        channels.append(PresentationChannel(source_id=identity, source=data.get("source_names", {}).get(identity, "Kanal ohne Anzeigename"),
            rank=rank.get("tier", "F"), origin=origin if origin in ("manual", "auto", "bootstrap", "unknown") else "unknown",
            score=_decimal(stats.get("score")), proof=len(rank.get("evidence", [])), drawdown_r=_decimal(stats.get("drawdown_r")),
            last_change=last_changes.get(identity), net_r=sum((Decimal(row["net_r"]) for row in eligible), Decimal(0)) if eligible else None,
            scope_setups=len(eligible), excluded_setups=len(rejected)))
    channels.sort(key=lambda row: (-TIERS.index(row.rank), -(row.score or Decimal(0)), row.source.casefold(), row.source_id))
    currencies = sorted({"USDT", "USDC", *(trade["plan"]["market"].get("quote_token", "USDC") for trade in data.get("trades", {}).values())})
    periods = sorted({current, *data.get("monthly_reports", {}), *(datetime.fromtimestamp((trade.get("closed_at") or trade["plan"]["created_at"]) / 1000, BERLIN).strftime("%Y-%m") for trade in data.get("trades", {}).values())})
    if scope.period not in ("current", "all", "today", "7d", "30d"):
        periods = sorted({*periods, scope.period})
    body = dict(schema_version=1, scope=scope, scope_binding=digest({"account": data["account"], "trading_account": data.get("trading_account", data["account"]), "network": data["network"]}),
        revision=data["sequence"], generated_at=clock, resolved_period=period, period_start=start, period_end_exclusive=end,
        available_currencies=currencies, available_periods=["current", "today", "7d", "30d", "all", *periods],
        journal=journal, channels=channels, parameters=_parameters(data), months=[PresentationMonth(month=month, revision=row.get("revision", 0),
            published_at=row.get("published_at")) for month, row in sorted(data.get("monthly_reports", {}).items())],
        net_pnl=net, fees=fees, funding=funding,
        hit_rate_percent=Decimal(sum(row.net_pnl > 0 for row in known)) / len(known) * 100 if known else None,
        open_trades=sum(row.cohort == "open" for row in journal), complete_trades=sum(row.complete for row in evaluated),
        evaluated_trades=len(evaluated), daily_pnl=daily, monthly_pnl=months_chart, cumulative_realized_net=cumulative,
        net_r_basis="complete_setup_families_as_ranking", equity_curve=False)
    # Model JSON is the exact fingerprint representation consumed by the publisher.
    values = {key: value.model_dump(mode="json") if isinstance(value, Model) else [row.model_dump(mode="json") if isinstance(row, Model) else row for row in value]
              if isinstance(value, list) else value for key, value in body.items() if key not in ("revision", "generated_at")}
    # Decimal is string-encoded by Pydantic, including standalone scalar fields.
    values["hit_rate_percent"] = str(body["hit_rate_percent"]) if body["hit_rate_percent"] is not None else None
    return PresentationSnapshot.model_validate({**body, "fingerprint": digest(values)}, context={"transport_budget": transport_budget})
