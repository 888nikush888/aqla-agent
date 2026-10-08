"""Evidence-based monthly ROE. Reporting never changes exchange orders or rules."""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from aqla_hyperliquid_mcp.models import (
    TERMINAL_ORDER,
    AqlaError,
    Market,
    Model,
    digest,
    margin_leverage,
    now_ms,
)

BERLIN = ZoneInfo("Europe/Berlin")
FIRST_MONTH = "2026-10"
SUM_LABEL = "Summe der Trade-Renditen — keine Kontorendite"
CLOSED = {"closed", "cancelled", "rejected"}
MAX_PUBLICATION_ROWS = 10_000
MAX_NATIVE_MONTHLY_BYTES = 600_000
MonthlySection = Literal["rows", "open_trades", "undated_closed_trades"]
MonthlyErrorCode = Literal["temporary", "schema", "budget", "conflict", "integrity"]
Counter = Annotated[int, Field(strict=True, ge=0)]
Revision = Annotated[int, Field(strict=True, ge=1)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MonthlyJobBinding(Model):
    month: Annotated[str, Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")]
    fingerprint: Fingerprint
    revision: Revision
    request_generation: Counter = 0
    attempt_count: Counter = 0


class MonthlyJob(MonthlyJobBinding):
    status: Literal["pending", "publishing", "retry", "blocked", "published"] = "pending"
    last_error: MonthlyErrorCode | None = None
    last_attempt: Counter | None = None
    next_retry_at: Counter | None = None


class MonthlyFailure(Model):
    job: MonthlyJobBinding
    error_code: MonthlyErrorCode


class MonthlySelection(Model):
    status: Literal["ready", "idle"]
    report: dict | None = None
    job: MonthlyJobBinding | None = None
    scheduled: bool = False
    blocked: list[MonthlyJob] = Field(default_factory=list, max_length=10)
    paged: bool = False
    totals: dict[MonthlySection, Counter] = Field(default_factory=dict)


class MonthlyPage(Model):
    status: Literal["page", "blocked"]
    job: MonthlyJobBinding
    section: MonthlySection
    offset: Counter
    rows: list[dict] = Field(default_factory=list, max_length=100)
    total: Counter
    next_offset: Counter | None = None
    complete: bool = False
    error_code: MonthlyErrorCode | None = None


class MonthlyPayloadError(AqlaError):
    def __init__(self, code: MonthlyErrorCode):
        self.code = code
        super().__init__("Monthly internal payload rejected: " + code)


def native_monthly_bytes(payload):
    """Bound the complete native JSON/text envelope, including escaping and UTF-8."""
    return max(len(json.dumps({"result": json.dumps(payload, ensure_ascii=ascii_only, indent=2)},
                             ensure_ascii=False).encode("utf-8")) for ascii_only in (False, True))


class MonthlyPublication(Model):
    month: Annotated[str, Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")]
    fingerprint: Fingerprint
    revision: Revision
    url: str


class MonthlyPublishOutcome(Model):
    status: Literal["published", "blocked", "retry"]
    publication: MonthlyPublication | None = None
    error_code: MonthlyErrorCode | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "published":
            if self.publication is None or self.error_code is not None:
                raise ValueError("Published outcome requires a confirmed publication")
        elif self.publication is not None or self.error_code is None or (self.status == "retry") != (self.error_code == "temporary"):
            raise ValueError("Failure outcome requires a safe error category")
        return self


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise AqlaError("Monat als YYYY-MM zwischen 2000 und 2099 angeben")
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=BERLIN)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=BERLIN)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def due_at(month):
    _, end = month_bounds(month)
    next_month = datetime.fromtimestamp(end / 1000, BERLIN).replace(hour=9)
    return int(next_month.timestamp() * 1000)


def stamp(value):
    return datetime.fromtimestamp(value / 1000, BERLIN).isoformat() if value is not None else None


def fill_identity(row):
    return digest([row["oid"], row.get("tid"), row.get("time"), row.get("sz"), row.get("px")])


def entry_quantity_evidence(raw):
    detail = raw.get("order", {})
    status = detail.get("status")
    order = detail.get("order", {})
    if status not in TERMINAL_ORDER or order.get("origSz") is None or order.get("sz") is None:
        return None
    original = Decimal(str(order["origSz"]))
    remaining = Decimal(str(order["sz"]))
    value = original if status == "filled" else original - remaining
    return str(value) if value.is_finite() and value >= 0 else None


def accounting_facts(trade):
    """Freeze only final entry fills; old records can be reconstructed without new IDs."""
    fills = list(trade.get("fill_events", {}).values())
    entries = [row for row in fills if row["kind"] == "entry"]
    exits = [row for row in fills if row["kind"] != "entry"]
    total = sum((Decimal(row["sz"]) for row in entries), Decimal(0))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
    entry_status = trade.get("order_observations", {}).get(trade.get("entry_cloid"), {}).get("status")
    entry_final = entry_status in TERMINAL_ORDER and trade.get("entry_fill_quantity_confirmed") is not None and total == Decimal(trade["entry_fill_quantity_confirmed"])
    result = {}
    if total and entry_final and all(isinstance(row.get("time"), int) for row in entries):
        notional = sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in entries), Decimal(0))
        plan = trade["plan"]
        leverage = margin_leverage(plan["effective_leverage"], Market.model_validate(plan["market"]))
        metrics = {"entry_notional": str(notional), "margin_leverage": str(leverage),
                   "initial_margin": str(notional / leverage), "entry_quantity": str(total),
                   "entry_at": min(row["time"] for row in entries), "entry_final_at": max(row["time"] for row in entries),
                   "entry_fingerprint": digest(sorted(fill_identity(row) for row in entries)),
                   "basis": "calculated_initial_margin"}
        if not trade.get("entry_accounting"):
            result["entry_accounting"] = metrics
        elif trade["entry_accounting"] != metrics:
            result["accounting_conflict"] = "Festgeschriebene Entry-Belege stimmen nicht mehr überein"
    if total and total == exited and entry_final and all(isinstance(row.get("time"), int) for row in exits):
        closed = max(row["time"] for row in exits)
        if trade.get("closed_at") not in (None, closed):
            result["accounting_conflict"] = "Schließzeit stimmt nicht mit vollständigen eigenen Exit-Fills überein"
        else:
            result["closed_at"] = closed
    return result


def report_row(data, trade):
    plan = trade["plan"]
    facts = {**trade, **accounting_facts(trade)}
    metric = facts.get("entry_accounting", {})
    fills = list(trade.get("fill_events", {}).values())
    exits = [row for row in fills if row["kind"] != "entry"]
    currency = plan["market"].get("quote_token", "USDC")
    issues = []
    if not metric:
        issues.append("Entry-Fills oder endgültiger Margin-Hebel nicht vollständig belegt")
    entry_fills = [row for row in fills if row["kind"] == "entry"]
    confirmed = trade.get("entry_fill_quantity_confirmed")
    if (confirmed is None or sum((Decimal(row["sz"]) for row in entry_fills), Decimal(0)) != Decimal(confirmed)
            or trade.get("order_observations", {}).get(trade.get("entry_cloid"), {}).get("status") not in TERMINAL_ORDER):
        issues.append("Endgültige Entry-Ausführungsmenge nicht vollständig belegt")
    if facts.get("accounting_conflict"):
        issues.append(facts["accounting_conflict"])
    if trade.get("external_conflict") or trade.get("manual_closure"):
        issues.append("Manueller/gemischter Positionsverlauf")
    entered = Decimal(metric.get("entry_quantity", "0"))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
    if not entered or entered != exited or not facts.get("closed_at"):
        issues.append("Vollständige eigene Exit-Fills fehlen")
    fee_evidence = trade.get("report_fee_evidence", {})
    fees_known = bool(fills) and all((fee_evidence.get(fill_identity(row), row)).get("fee") is not None
                                   and (fee_evidence.get(fill_identity(row), row)).get("feeToken") == currency for row in fills)
    if not fees_known:
        issues.append("Gebühren oder Gebührenwährung nicht vollständig belegt")
    coverage = trade.get("funding_coverage", {})
    funding_known = not (not coverage.get("complete") or coverage.get("start") != metric.get("entry_at") or coverage.get("end") != facts.get("closed_at")
            or coverage.get("fill_fingerprint") != digest(trade.get("fill_events", {}))
            or coverage.get("payments_fingerprint") != digest(trade.get("funding_events", {})))
    if not funding_known:
        issues.append(coverage.get("reason") or "Funding-Abdeckung fehlt")
    gross = None
    if entered and entered == exited:
        entry_price = Decimal(metric["entry_notional"]) / entered
        gross = sum((Decimal(row["sz"]) * (Decimal(row["px"]) - entry_price) for row in exits), Decimal(0)) * (1 if plan["side"] == "long" else -1)
    fees = sum((Decimal(fee_evidence.get(fill_identity(row), row)["fee"]) for row in fills), Decimal(0)) if fees_known else None
    funding_rows = trade.get("funding_events", {}).values()
    funding = sum((Decimal(row["amount"]) for row in funding_rows), Decimal(0)) if funding_known else None
    complete = not issues
    net = gross - fees + funding if complete else None
    roe = net / Decimal(metric["initial_margin"]) * 100 if complete else None
    chat = trade.get("chat_id")
    return {"trade_id": trade["trade_id"], "entry_at": metric.get("entry_at"), "closed_at": facts.get("closed_at"),
            "source_id": chat, "source_name": data.get("source_names", {}).get(chat, chat),
            "symbol": plan["market"]["symbol"], "side": plan["side"], "exchange": plan.get("exchange", "hyperliquid"),
            "network": plan["network"], "profile": plan.get("profile_name") or data.get("profiles", {}).get(plan.get("profile_id"), {}).get("name") or plan.get("profile_id", "Standard"),
            "currency": currency, **{key: metric.get(key) for key in ("entry_notional", "margin_leverage", "initial_margin")},
            "gross_pnl": str(gross) if gross is not None else None, "fees": str(fees) if fees is not None else None,
            "funding": str(funding) if funding is not None else None, "net_pnl": str(net) if net is not None else None,
            "roe_percent": str(roe) if roe is not None else None, "complete": complete, "issues": issues}


def monthly_report(data, month):
    start, end = month_bounds(month)
    rows, opened, undated = [], [], []
    for trade in data.get("trades", {}).values():
        # Never merge archived testnet or a different venue's network into Mainnet.
        if trade["plan"]["network"] != "mainnet":
            continue
        row = report_row(data, trade)
        if row["entry_at"] is not None and row["entry_at"] < end and (row["closed_at"] is None and trade["status"] not in CLOSED or row["closed_at"] is not None and row["closed_at"] >= end):
            open_row = {**row, "closed_at": None, "gross_pnl": None, "fees": None, "funding": None, "net_pnl": None, "roe_percent": None,
                        "complete": False, "issues": ["Am Monatsende offen; kein Ergebnis in der Monatssumme"]}
            opened.append(open_row)
        elif trade["status"] in CLOSED:
            if not trade.get("fill_events"):
                continue
            if row["closed_at"] is None and trade["plan"]["created_at"] < end:
                undated.append(row)
            elif row["closed_at"] is not None and start <= row["closed_at"] < end:
                rows.append(row)
    rows.sort(key=lambda row: (row["closed_at"], row["trade_id"]))
    completed = [row for row in rows if row["complete"]]
    wins = sum(Decimal(row["net_pnl"]) > 0 for row in completed)
    losses = sum(Decimal(row["net_pnl"]) < 0 for row in completed)
    currencies = {}
    for row in completed:
        currencies[row["currency"]] = str(Decimal(currencies.get(row["currency"], "0")) + Decimal(row["net_pnl"]))
    result = {"month": month, "timezone": "Europe/Berlin", "start": start, "end_exclusive": end,
              "rows": rows, "open_trades": opened, "undated_closed_trades": undated,
              "summary": {"trades": len(rows), "complete": len(completed), "excluded": len(rows) - len(completed),
                          "wins": wins, "losses": losses, "break_even": len(completed) - wins - losses,
                          "win_rate_percent": str(Decimal(wins) / len(completed) * 100) if completed else None,
                          "roe_sum_percent": str(sum((Decimal(row["roe_percent"]) for row in completed), Decimal(0))),
                          "roe_sum_label": SUM_LABEL, "net_by_currency": currencies},
              "margin_basis": "Berechnete Anfangsmargin; im Cross-/Unified-Konto kein separat abgegrenztes Guthaben"}
    result["fingerprint"] = digest(result)
    publication = data.get("monthly_reports", {}).get(month, {})
    result["revision"] = publication.get("revision", 0) + (publication.get("fingerprint") != result["fingerprint"])
    job = data.get("monthly_jobs", {}).get(month, {})
    if job:
        result["revision"] = max(result["revision"], job["revision"] + (job["fingerprint"] != result["fingerprint"]))
    result["url"] = publication.get("url")
    result["published_current_revision"] = publication.get("fingerprint") == result["fingerprint"] and publication.get("revision") == result["revision"]
    return result


def next_job(data, clock=None):
    clock = now_ms() if clock is None else clock
    current = datetime.fromtimestamp(clock / 1000, BERLIN).strftime("%Y-%m")
    wanted = set(data.get("monthly_requests", [])) | set(data.get("monthly_reports", {})) | set(data.get("monthly_jobs", {}))
    year, month = map(int, FIRST_MONTH.split("-"))
    while (candidate := f"{year:04d}-{month:02d}") < current:
        if due_at(candidate) <= clock:
            wanted.add(candidate)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    blocked, eligible = [], []
    for candidate in sorted(wanted):
        report = monthly_report(data, candidate)
        scheduled = candidate >= FIRST_MONTH and due_at(candidate) <= clock
        published = data.get("monthly_reports", {}).get(candidate, {})
        if not report["published_current_revision"] or scheduled and not published.get("scheduled_notified") or candidate in data.get("monthly_requests", []):
            generation = data.get("monthly_request_generations", {}).get(candidate, 0)
            stored = data.setdefault("monthly_jobs", {}).get(candidate)
            job = MonthlyJob.model_validate(stored) if stored else MonthlyJob(month=candidate, fingerprint=report["fingerprint"], revision=report["revision"])
            if (job.fingerprint, job.revision, job.request_generation) != (report["fingerprint"], report["revision"], generation):
                job = MonthlyJob(month=candidate, fingerprint=report["fingerprint"], revision=report["revision"], request_generation=generation)
            if job.status == "blocked":
                blocked.append(job)
                continue
            if job.next_retry_at is not None and clock < job.next_retry_at:
                continue
            # Reject the same bounded publication size before a large native handoff.
            publication_rows = 19 + len(report["summary"]["net_by_currency"]) + sum(
                len(report[key]) for key in ("rows", "open_trades", "undated_closed_trades"))
            if publication_rows > MAX_PUBLICATION_ROWS:
                job.status, job.last_error, job.next_retry_at = "blocked", "budget", None
                job.attempt_count += 1
                job.last_attempt = clock
                data["monthly_jobs"][candidate] = job.model_dump(mode="json")
                blocked.append(job)
                continue
            eligible.append((job, report, scheduled))
    for job, report, scheduled in sorted(eligible, key=lambda item: (item[0].last_attempt if item[0].last_attempt is not None else -1, item[0].month)):
        job.status = "publishing"
        job.attempt_count += 1
        job.last_attempt, job.next_retry_at = clock, clock + 60_000
        data["monthly_jobs"][job.month] = job.model_dump(mode="json")
        binding = MonthlyJobBinding.model_validate({key: getattr(job, key) for key in MonthlyJobBinding.model_fields})
        selection = MonthlySelection(status="ready", report=report, scheduled=scheduled, job=binding, blocked=blocked[:10])
        result = selection.model_dump(mode="json")
        if native_monthly_bytes(result) > MAX_NATIVE_MONTHLY_BYTES:
            sections = ("rows", "open_trades", "undated_closed_trades")
            selection.paged = True
            selection.totals = {key: len(report[key]) for key in sections}
            selection.report = {key: value for key, value in report.items() if key not in sections}
            result = selection.model_dump(mode="json")
            if native_monthly_bytes(result) > MAX_NATIVE_MONTHLY_BYTES:
                job.status, job.last_error, job.next_retry_at = "blocked", "budget", None
                data["monthly_jobs"][job.month] = job.model_dump(mode="json")
                blocked.append(job)
                continue
        return result
    return MonthlySelection(status="idle", blocked=blocked[:10]).model_dump(mode="json")


def request_publication(data, month):
    month_bounds(month)
    report = monthly_report(data, month)
    if report["published_current_revision"]:
        return {"requested_month": month, "already_published": True}
    wanted = data.setdefault("monthly_requests", [])
    if month not in wanted:
        wanted.append(month)
    generations = data.setdefault("monthly_request_generations", {})
    generations[month] = generations.get(month, 0) + 1
    return {"requested_month": month}


def _bound_job(data, binding, *, allow_published=False):
    binding = MonthlyJobBinding.model_validate(binding)
    stored = data.get("monthly_jobs", {}).get(binding.month)
    report = monthly_report(data, binding.month)
    if not stored:
        raise AqlaError("stale monthly job: request a current publication job")
    job = MonthlyJob.model_validate(stored)
    current = {key: getattr(job, key) for key in MonthlyJobBinding.model_fields}
    if (current != binding.model_dump() or binding.request_generation != data.get("monthly_request_generations", {}).get(binding.month, 0)
            or (report["fingerprint"], report["revision"]) != (binding.fingerprint, binding.revision)
            or job.status not in ({"publishing", "published"} if allow_published else {"publishing"})):
        raise AqlaError("stale monthly job: financial evidence or publication attempt changed")
    return job


def fail_publication(data, failure, clock=None):
    failure = MonthlyFailure.model_validate(failure)
    job = _bound_job(data, failure.job)
    clock = now_ms() if clock is None else clock
    job.last_error = failure.error_code
    if failure.error_code == "temporary":
        job.status = "retry"
        job.next_retry_at = clock + min(60 * 2 ** min(job.attempt_count - 1, 6), 3600) * 1000
    else:
        job.status, job.next_retry_at = "blocked", None
    data["monthly_jobs"][job.month] = job.model_dump(mode="json")
    return data["monthly_jobs"][job.month]


def monthly_page(data, binding, section: MonthlySection, offset=0, limit=100):
    binding = MonthlyJobBinding.model_validate(binding)
    _bound_job(data, binding)
    report = monthly_report(data, binding.month)
    if section not in ("rows", "open_trades", "undated_closed_trades") or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AqlaError("Monthly internal page requires a known section, offset >= 0 and limit between 1 and 100")
    total = len(report[section])
    if offset > total:
        raise AqlaError("Monthly internal page offset exceeds its bound section")
    rows = []
    result = MonthlyPage(status="page", job=binding, section=section, offset=offset, total=total)
    for row in report[section][offset:offset + limit]:
        result.rows = [*rows, row]
        consumed = offset + len(result.rows)
        result.next_offset, result.complete = (consumed if consumed < total else None), consumed == total
        if native_monthly_bytes(result.model_dump(mode="json")) > MAX_NATIVE_MONTHLY_BYTES:
            result.rows = rows
            break
        rows = result.rows
    if not rows and offset < total:
        fail_publication(data, MonthlyFailure(job=binding, error_code="budget"))
        return MonthlyPage(status="blocked", job=binding, section=section, offset=offset, total=total,
                           error_code="budget").model_dump(mode="json")
    consumed = offset + len(rows)
    result.rows, result.next_offset, result.complete = rows, consumed if consumed < total else None, consumed == total
    return result.model_dump(mode="json")


def acknowledge_publication(data, month, fingerprint, url, revision, clock=None, *, job=None):
    report = monthly_report(data, month)
    if report["fingerprint"] != fingerprint or report["revision"] != revision:
        raise AqlaError("Monatsbelege wurden während der Veröffentlichung geändert; erneut abgleichen")
    bound = _bound_job(data, job, allow_published=True) if job is not None else None
    if bound is not None and (bound.month, bound.fingerprint, bound.revision) != (month, fingerprint, revision):
        raise AqlaError("stale monthly job: publication does not match the selected job")
    clock = now_ms() if clock is None else clock
    previous = data.setdefault("monthly_reports", {}).get(month, {})
    scheduled = month >= FIRST_MONTH and due_at(month) <= clock
    publication = {"fingerprint": fingerprint, "url": url, "revision": revision,
                   "published_at": clock, "scheduled_notified": bool(previous.get("scheduled_notified"))}
    notice = {"month": month, "summary": report["summary"], "url": url,
              "open_trade_count": len(report["open_trades"]), "undated_closed_count": len(report["undated_closed_trades"])}
    if scheduled and not publication["scheduled_notified"]:
        data["notifications"].setdefault("monthly:" + month, {"monthly_report": notice, "observed_at": clock})
        publication["scheduled_notified"] = True
    elif month in data.get("monthly_requests", []):
        data["notifications"].setdefault("monthly-request:" + month + ":" + fingerprint,
                                        {"monthly_report": notice, "observed_at": clock})
    data["monthly_reports"][month] = publication
    if bound is not None:
        bound.status, bound.last_error, bound.next_retry_at = "published", None, None
        data["monthly_jobs"][month] = bound.model_dump(mode="json")
    data["monthly_requests"] = [item for item in data.get("monthly_requests", []) if item != month]
    return publication
