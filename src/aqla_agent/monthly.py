"""Evidence-based monthly ROE. Reporting never changes exchange orders or rules."""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aqla_hyperliquid_mcp.models import (
    TERMINAL_ORDER,
    AqlaError,
    Market,
    digest,
    margin_leverage,
    now_ms,
)

BERLIN = ZoneInfo("Europe/Berlin")
FIRST_MONTH = "2026-10"
SUM_LABEL = "Summe der Trade-Renditen — keine Kontorendite"
CLOSED = {"closed", "cancelled", "rejected"}
MAX_NATIVE_MONTHLY_BYTES = 600_000














def native_monthly_bytes(payload):
    """Bound the complete native JSON/text envelope, including escaping and UTF-8."""
    return max(len(json.dumps({"result": json.dumps(payload, ensure_ascii=ascii_only, indent=2)},
                             ensure_ascii=False).encode("utf-8")) for ascii_only in (False, True))






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
    if trade.get("lifecycle_version") == 2:
        from .managed_entries import entry_final as managed_final
        entry_final = (managed_final(trade) and (not trade.get("waverider") or trade["waverider"].get("finished"))
            and trade.get("entry_fill_quantity_confirmed") is not None and total == Decimal(trade["entry_fill_quantity_confirmed"]))
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
        if trade.get("lifecycle_version") == 2:
            from .managed_entries import original_fills
            original = original_fills(trade)
            original_notional = sum((Decimal(row["sz"]) * Decimal(row["px"]) for row in original), Decimal(0))
            metrics.update(initial_margin=str(original_notional / leverage), original_entry_notional=str(original_notional),
                           original_entry_quantity=str(sum((Decimal(row["sz"]) for row in original), Decimal(0))), entry_turnover=str(notional))
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


def accounting_issues(trade, facts):
    """Own lifecycle completeness, independent of fees and funding coverage."""
    metric = facts.get("entry_accounting", {})
    fills = list(trade.get("fill_events", {}).values())
    exits = [row for row in fills if row["kind"] != "entry"]
    issues = []
    if not metric:
        issues.append("Entry-Fills oder endgültiger Margin-Hebel nicht vollständig belegt")
    entry_fills = [row for row in fills if row["kind"] == "entry"]
    confirmed = trade.get("entry_fill_quantity_confirmed")
    final_status = trade.get("order_observations", {}).get(trade.get("entry_cloid"), {}).get("status") in TERMINAL_ORDER
    if trade.get("lifecycle_version") == 2:
        from .managed_entries import entry_final as managed_final
        final_status = managed_final(trade)
    if (confirmed is None or sum((Decimal(row["sz"]) for row in entry_fills), Decimal(0)) != Decimal(confirmed) or not final_status):
        issues.append("Endgültige Entry-Ausführungsmenge nicht vollständig belegt")
    if facts.get("accounting_conflict"):
        issues.append(facts["accounting_conflict"])
    from .manual_accounting import verified_manual_accounting
    manual = verified_manual_accounting(trade)
    if (trade.get("external_conflict") or trade.get("manual_closure")) and not manual:
        issues.append("Manueller/gemischter Positionsverlauf")
    entered = Decimal(metric.get("entry_quantity", "0"))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
    if not entered or (entered != exited and not manual) or not facts.get("closed_at"):
        issues.append("Vollständige eigene Exit-Fills fehlen")
    return issues


def report_row(data, trade):
    plan = trade["plan"]
    facts = {**trade, **accounting_facts(trade)}
    metric = facts.get("entry_accounting", {})
    fills = list(trade.get("fill_events", {}).values())
    exits = [row for row in fills if row["kind"] != "entry"]
    currency = plan["market"].get("quote_token", "USDC")
    issues = accounting_issues(trade, facts)
    entered = Decimal(metric.get("entry_quantity", "0"))
    exited = sum((Decimal(row["sz"]) for row in exits), Decimal(0))
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
    from .manual_accounting import verified_manual_accounting
    manual = verified_manual_accounting(trade)
    if manual:
        gross, fees = Decimal(manual["gross_pnl"]), Decimal(manual["fees"])
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
            "roe_percent": str(roe) if roe is not None else None, "complete": complete, "issues": issues,
            **({"attribution_note": manual["note"], "exit_vwap": manual["exit_vwap"]} if manual else {})}


def monthly_report(data, month):
    start, end = month_bounds(month)
    rows, opened, undated = [], [], []
    for trade in data.get("trades", {}).values():
        # Never merge archived testnet or a different venue's network into Mainnet.
        if trade["plan"]["network"] != "mainnet":
            continue
        row = report_row(data, trade)
        entry_at = row["entry_at"] or min((fill["time"] for fill in trade.get("fill_events", {}).values()
            if fill["kind"] == "entry" and isinstance(fill.get("time"), int) and Decimal(fill["sz"]) > 0), default=None)
        if entry_at is not None and entry_at < end and (row["closed_at"] is None and trade["status"] not in CLOSED or row["closed_at"] is not None and row["closed_at"] >= end):
            open_row = {**row, "entry_at": entry_at, "closed_at": None, "gross_pnl": None, "fees": None, "funding": None, "net_pnl": None, "roe_percent": None,
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
    result["storage"] = "local SQLite"
    result["url"] = None
    result["published_current_revision"] = False
    return result


def scheduled_reports(data, clock=None):
    """One local monthly close notification, independent of models and external spreadsheets."""
    clock = now_ms() if clock is None else clock
    current = datetime.fromtimestamp(clock / 1000, BERLIN).strftime("%Y-%m")
    year, month = map(int, FIRST_MONTH.split("-"))
    selected = []
    while (candidate := f"{year:04d}-{month:02d}") < current:
        if due_at(candidate) <= clock and not data.get("monthly_notified", {}).get(candidate):
            report = monthly_report(data, candidate)
            report["url"] = None
            selected.append({"month": candidate, "summary": report["summary"], "open_trade_count": len(report["open_trades"]),
                             "undated_closed_count": len(report["undated_closed_trades"]), "fingerprint": report["fingerprint"]})
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return selected
