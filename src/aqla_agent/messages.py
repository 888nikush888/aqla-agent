"""Read-only Telegram views over durable facts; delivery remains native Hermes."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

VENUES = {"hyperliquid": "Hyperliquid", "aster": "Aster", "lighter": "Lighter"}
OPEN = {"preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted"}
ERRORS = {
    "SL, entry and TPs have invalid direction/ordering after rounding": "Preisordnung nach Rundung unzulässig; auch die Slippage-Ordergrenze wird geprüft.",
    "Existing exchange exposure has no free logical owner; no additional entry": "Bereits vorhandene Börsenposition; Aqla eröffnet keinen zusätzlichen Trade.",
    "Signal SL is missing and Backup SL percentage is not configured": "SL fehlt; im Backup ist noch kein SL-Prozentwert gesetzt.",
    "Signal TPs are missing and Backup TP percentages are not configured": "TPs fehlen; im Backup sind noch keine Prozentziele gesetzt.",
    "Insufficient risk/margin budget or exchange minimum notional": "Risiko-/Marginbudget oder Mindestpositionswert reicht nicht aus.",
    "Signal deadline expired; it is never renewed": "Die Gültigkeitsfrist des Signals ist abgelaufen.",
    "Source market cannot be resolved uniquely; no substitute market is used": "Der Signalmarkt ist nicht eindeutig auflösbar.",
}
CHUNK_UNITS = 3000
MAX_NOTIFICATIONS = 100


def literal(value):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    # Protect dynamic labels from becoming links, HTML, emphasis or code fences.
    if any(char in text for char in "*_`[]<>~|#\\"):
        return "`" + text.replace("`", "ˋ").replace("\\", "⧵") + "`"
    return text


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def number(value):
    value = decimal(value)
    if value is None:
        return "nicht bestätigt"
    raw = format(value, "f")
    whole, _, fraction = raw.partition(".")
    sign = "−" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    groups = [whole[max(0, end - 3):end] for end in range(len(whole), 0, -3)]
    fraction = fraction.rstrip("0")
    return sign + ".".join(reversed(groups)) + ("," + fraction if fraction else "")


def split_blocks(text, limit=CHUNK_UNITS):
    """Keep lines/Markdown constructs whole; never truncate facts."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        units = len(block.encode("utf-16-le")) // 2
        pieces = [block] if units <= limit else block.splitlines()
        for piece in pieces:
            separator = "\n\n" if pieces == [block] else "\n"
            candidate = current + separator + piece if current else piece
            if current and len(candidate.encode("utf-16-le")) // 2 > limit:
                parts.append(current)
                current = piece
            else:
                current = candidate
    if current:
        parts.append(current)
    return parts


def source_name(data, chat):
    return literal(data.get("source_names", {}).get(chat) or chat or "Quelle nicht zugeordnet")


def profile_name(data, identity):
    return literal(data.get("profiles", {}).get(identity, {}).get("name") or identity or "Standard")


def context(data, plan):
    venue = plan.get("exchange", "hyperliquid")
    network = plan.get("network") or data.get("venue_accounts", {}).get(venue, {}).get("network") or (data.get("network") if venue == "hyperliquid" else "mainnet")
    return f"**Börse:** {VENUES.get(venue, literal(venue))} · {literal(network).capitalize()}\n**Profil:** {profile_name(data, plan.get('profile_id', 'standard'))}"


def pnl(trade):
    entry = decimal(trade.get("entry_vwap"))
    fills = list(trade.get("fill_events", {}).values())
    quote = trade["plan"]["market"].get("quote_token", "USDC")
    if entry is None or not fills or any(decimal(row.get("fee")) is None or row.get("feeToken") not in (None, quote) for row in fills):
        return None
    if any(decimal(row.get("sz")) is None or decimal(row.get("px")) is None for row in fills):
        return None
    entered = sum((decimal(row["sz"]) for row in fills if row["kind"] == "entry"), Decimal(0))
    exited = sum((decimal(row["sz"]) for row in fills if row["kind"] != "entry"), Decimal(0))
    if entered <= 0 or entered != exited:
        return None
    sign = 1 if trade["plan"]["side"] == "long" else -1
    gross = sum((decimal(row["sz"]) * (decimal(row["px"]) - entry) * sign for row in fills if row["kind"] != "entry"), Decimal(0))
    return gross - sum((decimal(row["fee"]) for row in fills), Decimal(0))


def trade_block(data, trade, notices):
    plan, stage = trade["plan"], trade["status"]
    market = literal(plan["market"]["symbol"])
    direction = "LONG" if plan["side"] == "long" else "SHORT"
    changes = {key: value for notice in notices for key, value in notice.get("change", {}).items()}
    previous = {key: value for notice in reversed(notices) for key, value in notice.get("previous", {}).items()}
    opening = any((notice.get("previous", {}).get("status") in ("opening", "preparing") and notice.get("change", {}).get("status") in ("open", "protecting"))
                  or (decimal(notice.get("previous", {}).get("filled_quantity")) == 0 and (decimal(notice.get("change", {}).get("filled_quantity")) or 0) > 0) for notice in notices)
    filled = decimal(trade.get("filled_quantity")) or Decimal(0)
    planned = decimal(plan.get("quantity"))
    if stage == "conflicted":
        title = "⚠️ **Manueller Marktkonflikt**"
    elif stage == "unknown":
        title = "⏳ **Ausführung ungeklärt**"
    elif stage in ("closing", "cleanup"):
        title = "⏳ **Schließung wird abgeglichen**"
    elif stage in ("closed", "cancelled", "rejected"):
        title = {"closed": "🏁 **Trade geschlossen**", "cancelled": "⚪ **Entry storniert**", "rejected": "⚠️ **Trade abgelehnt**"}[stage]
    elif stage == "waiting_entry":
        title = "🕓 **Warte auf Einstieg**"
    elif filled == 0:
        title = "🕓 **Entry wartet**"
    elif planned and filled < planned:
        title = "🟡 **Entry teilweise ausgeführt**" if opening else "🔄 **Trade aktualisiert**"
    elif stage != "open":
        title = "⏳ **Schutz wird abgeglichen**"
    else:
        title = "✅ **Trade eröffnet**" if opening else "🔄 **Trade aktualisiert**"
    lines = [f"{title} · {market} {direction}", context(data, plan)]
    if stage == "waiting_entry":
        lines.append("**Status:** lokaler Wartezustand · noch keine Börsenorder")
    quote = literal(plan["market"].get("quote_token", "USDC"))
    full = opening or filled == 0 or stage in ("unknown", "protecting")
    if full:
        entry = trade.get("entry_vwap") if filled else plan.get("entry_price")
        label = "Entry realisiert" if filled else "Entry geplant"
        lines.append(f"**{label}:** {number(entry)} {quote} · **Hebel:** {number(plan.get('effective_leverage', plan.get('policy', {}).get('leverage')))}×")
        if filled and planned and filled < planned:
            lines.append(f"**Gefüllt:** {number(filled / planned * 100)} % der geplanten Menge")
        lines.append(f"**SL:** {number(trade.get('stop_price', plan.get('stop_loss')))} {quote}" + (" · vorgesehen" if stage == "waiting_entry" else " · Bestätigung ausstehend" if stage in ("unknown", "protecting", "opening") else ""))
        targets = trade.get("targets", plan.get("take_profits", []))
        budgets = trade.get("target_budgets") or plan.get("tp_quantities", [])
        total = sum((decimal(value) or Decimal(0) for value in budgets), Decimal(0))
        legs = [f"TP{index + 1}: {number(price)} {quote}" + (f" · {number((decimal(budgets[index]) or Decimal(0)) / total * 100)} %" if total and index < len(budgets) else " · Anteil nicht bestätigt") for index, price in enumerate(targets)]
        if legs:
            lines.append("**Ziele · Positionsanteil**\n" + "\n".join(legs))
        raw, rounded, effective = plan.get("signal_leverage_original", plan.get("signal_leverage")), plan.get("signal_leverage"), plan.get("effective_leverage")
        if raw is not None and (decimal(raw) != decimal(rounded) or decimal(rounded) != decimal(effective)):
            lines.append(f"**Hebel angepasst:** Signal {number(raw)}× → abgerundet {number(rounded)}× → wirksam {number(effective)}×")
        if plan.get("backup_fields"):
            labels = {"leverage": "Hebel", "entry": "Entry", "stop_loss": "SL", "take_profits": "TPs"}
            lines.append("**Backup verwendet:** " + ", ".join(labels.get(field, literal(field)) for field in plan["backup_fields"]))
            if set(plan["backup_fields"]) & {"stop_loss", "take_profits"}:
                lines.append("**Backup-Preise:** " + ("endgültig ab realem Entry" if trade.get("backup_finalization", {}).get("phase") == "applied" else "vorläufig"))
    else:
        if "stop_price" in changes:
            old = previous.get("stop_price")
            text = (number(old) + " → " if old is not None else "") + number(trade["stop_price"]) + " " + quote
            if filled and decimal(trade.get("stop_price")) == decimal(trade.get("entry_vwap")):
                text += " · Einstand"
            lines.append("**SL:** " + text)
        if "tp_filled" in changes:
            before = previous.get("tp_filled", [])
            for index, value in enumerate(trade.get("tp_filled", [])):
                if index < len(before) and (decimal(value) or 0) > (decimal(before[index]) or 0):
                    budget = (trade.get("target_budgets") or [])[index] if index < len(trade.get("target_budgets", [])) else None
                    complete = budget is not None and decimal(value) >= decimal(budget)
                    lines.append(f"**TP{index + 1}:** " + ("vollständig ausgeführt" if complete else "teilweise ausgeführt"))
        if "remaining_quantity" in changes and filled:
            lines.append(f"**Position verbleibend:** {number((decimal(trade.get('remaining_quantity')) or 0) / filled * 100)} %")
    if stage == "closed":
        from .lifecycle import close_reason
        lines.append("**Grund:** " + literal(trade.get("close_reason", close_reason(trade))["label"]))
        result = pnl(trade)
        if result is not None:
            lines.append(f"**Netto vor Funding:** {number(result)} {quote}")
        else:
            lines.append("**Ergebnis:** PnL nicht vollständig bestätigt")
    if trade.get("unsent_reason"):
        lines.append("**Grund:** " + literal(ERRORS.get(trade["unsent_reason"], trade["unsent_reason"])))
    if trade.get("external_conflict"):
        lines.append("**Verwaltung angehalten:** " + literal(trade['external_conflict']))
    return "\n\n".join(lines)


def lifecycle_block(data, event):
    trade = event["facts"]
    plan = trade["plan"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    time = datetime.fromtimestamp(event["time"] / 1000, ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M:%S")
    if event["kind"] == "closed":
        result = trade_block(data, trade, [{"change": {"status": "closed"}}]) + "\n\n**Schließung:** " + time + " · Berlin"
        exits = [row for row in trade.get("fill_events", {}).values() if row["kind"] != "entry"]
        size = sum((decimal(row["sz"]) for row in exits), Decimal(0))
        if size:
            exit_price = sum((decimal(row["sz"]) * decimal(row["px"]) for row in exits), Decimal(0)) / size
            result += "\n\n**Exit realisiert:** " + number(exit_price) + " " + literal(plan["market"].get("quote_token", "USDC"))
        return result
    title = literal(plan["market"]["symbol"]) + " " + ("LONG" if plan["side"] == "long" else "SHORT")
    quote = literal(plan["market"].get("quote_token", "USDC"))
    protected = trade.get("entry_stop_confirmed", False)
    planned = decimal(plan["quantity"])
    partial = planned and decimal(trade["filled_quantity"]) < planned
    lines = ["✅ **Einstieg ausgeführt** · " + title, context(data, plan), "**Ausführung:** " + time + " · Berlin",
             "**Entry realisiert:** " + number(trade["entry_vwap"]) + " " + quote + " · **Hebel:** " + number(plan["effective_leverage"]) + "×",
             "**Menge:** " + number(trade["filled_quantity"]) + (" · Teilfill" if partial else ""),
             "**SL vorgesehen:** " + number(trade["stop_price"]) + " " + quote,
             "**Schutz:** " + ("bestätigt" if protected else "zum Belegzeitpunkt nicht bestätigt")]
    if plan.get("source_exchange"):
        lines.append("**Signalbörse:** " + literal(plan["source_exchange"]))
    targets = trade.get("targets", plan.get("take_profits", []))
    budgets = trade.get("target_budgets") or plan.get("tp_quantities", [])
    total = sum((decimal(value) or Decimal(0) for value in budgets), Decimal(0))
    if targets:
        lines.append("**Ziele vorgesehen · Positionsanteil**\n" + "\n".join(
            f"TP{index + 1}: {number(price)} {quote}" + (" · " + number((decimal(budgets[index]) or 0) / total * 100) + " %" if total and index < len(budgets) else "")
            for index, price in enumerate(targets)))
    return "\n\n".join(lines)


def status_text(data, exchanges, sheets_enabled, inventory):
    active = [row for row in data["trades"].values() if row["status"] in OPEN]
    pending = sum(row["status"] in ("pending", "interpreted", "needs_reconciliation") for row in data["events"].values())
    unknown = sum(row["phase"] in ("prepared", "sending", "unknown") for row in data["operations"].values())
    enabled = any(row.get("enabled") and row.get("configured") for row in exchanges.values())
    lines = ["📊 **Aqla · Systemstatus**", "**Entries:** " + ("pausiert" if data["paused"] else "nach Profilregeln freigegeben" if enabled else "keine Börse freigegeben")]
    lines += [f"**{VENUES.get(venue, literal(venue))}:** {literal(row.get('network', ''))} · " + ("aktiv" if row.get("enabled") and row.get("configured") else "deaktiviert" if not row.get("enabled") else "nicht bereit") for venue, row in exchanges.items()]
    lines.append(f"**Offene Trades:** {len(active)} · **Ausstehende Quellen:** {pending}\n**Ungeklärte Vorgänge:** {unknown}")
    queue = len(data.get("journal_pending", {}))
    lines.append("**Sheets:** " + ("deaktiviert" if not sheets_enabled else f"{queue} Übertragungen ausstehend" if queue else "Journal synchronisiert"))
    for venue, row in inventory.items():
        if not row.get('available'):
            lines.append(f"**{VENUES.get(venue, venue)}-Konto:** vollständige Übersicht nicht verfügbar")
            continue
        lines.append(f"**Kontoplätze:** {row['occupied_count']} / {row['limit']} · **Frei:** {row['free_slots']}\n"
                     f"**Manuell:** {len(row['manual_markets'])} · **Aqla:** {len(row['aqla_markets'])} · **Reserviert:** {len(row['reserved_markets'])}\n"
                     f"**Freie Margin:** {number(row['available_margin_usdc'])} USDC")
        if row['conflicted_markets']:
            lines.append("**Marktkonflikte:** " + ', '.join(literal(value) for value in row['conflicted_markets']))
    for trade in active:
        plan = trade["plan"]
        lines.append(f"• {literal(plan['market']['symbol'])} {plan['side'].upper()} · {VENUES.get(plan.get('exchange', 'hyperliquid'), literal(plan.get('exchange')))} · {literal(trade['status'])}")
    return "\n\n".join(lines)


def monthly_message(report):
    summary = report["summary"]
    lines = ["📊 **Monatsbericht · " + report["month"] + "**",
             f'**Geschlossene Trades:** {summary["trades"]} · **Vollständig:** {summary["complete"]}',
             f'**Gewinne / Verluste:** {summary["wins"]} / {summary["losses"]} · **Gewinnquote:** ' + (number(summary["win_rate_percent"]) + " %" if summary["win_rate_percent"] is not None else "nicht berechenbar"),
             "**Summe der Trade-Renditen — keine Kontorendite:** " + number(summary["roe_sum_percent"]) + " %"]
    lines.extend("**Netto-PnL:** " + number(amount) + " " + currency for currency, amount in sorted(summary["net_by_currency"].items()))
    opened = report.get("open_trade_count", len(report.get("open_trades", [])))
    undated = report.get("undated_closed_count", len(report.get("undated_closed_trades", [])))
    lines += [f'**Ausgeschlossen:** {summary["excluded"]} unvollständige Ergebnisse · **Offen am Monatsende:** {opened}']
    if undated:
        lines.append(f'**Ohne belegten Schließmonat:** {undated} · keiner Summe zugeordnet')
    lines += ["ROE auf berechnete Anfangsmargin, einschließlich belegter Gebühren und Funding."]
    if report.get("url"):
        lines.append("[Vollständige Trade-Tabelle](" + report["url"] + ")")
    return "\n\n".join(lines)


def telegram_view(data, exchanges, *, sheets_enabled=True, inventory=None):
    notifications = data.get("notifications", {})
    # Urgent protection notices are independent of signal batching.
    chosen = [identity for _, identity in sorted(enumerate(notifications), key=lambda item: (
        not item[1].startswith("protection:"), notifications[item[1]].get("lifecycle", {}).get("time", notifications[item[1]].get("observed_at", 0)),
        notifications[item[1]].get("lifecycle", {}).get("kind") == "closed", item[0]))][:MAX_NOTIFICATIONS]
    groups = {}
    for identity in chosen:
        notice = notifications[identity]
        trade = data["trades"].get(notice.get("trade_id"))
        event = data["events"].get(notice.get("event_id") or (trade or {}).get("event_id"))
        if notice.get("monthly_report"):
            key = ("monthly", identity)
        elif identity.startswith("protection:"):
            key = ("urgent", identity)
        elif notice.get("lifecycle"):
            key = ("lifecycle", notice["lifecycle"]["kind"], tuple(sorted(trade["plan"].get("source_refs", [trade["trade_id"]]))))
        elif trade:
            key = ("signal", tuple(sorted(trade["plan"].get("source_refs", [trade["trade_id"]]))))
        else:
            key = ("event", (event or {}).get("source_id", identity))
        if event and key[0] == "event":
            refs = (event.get("interpretation") or {}).get("source_refs", [])
            if refs:
                key = ("signal", tuple(sorted(refs)))
        groups.setdefault(key, []).append((identity, notice, trade, event))
    messages = []
    for key, items in groups.items():
        ids = [row[0] for row in items]
        if key[0] == "lifecycle":
            blocks = ["**Quelle:** " + source_name(data, items[0][2]["chat_id"])]
            blocks += [lifecycle_block(data, row[1]["lifecycle"]) for row in items]
            messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks("\n\n".join(blocks))})
            continue
        if key[0] == "monthly":
            messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks(monthly_message(items[0][1]["monthly_report"]))})
            continue
        if key[0] == "urgent":
            notice = items[0][1]
            text = "🚨 **Schutzvorfall**\n\n**Börse:** " + VENUES.get(notice.get("exchange"), literal(notice.get("exchange", "nicht zugeordnet"))) + "\n**Entries:** gesperrt\n\n**Grund:** " + literal(ERRORS.get(notice.get("error"), notice.get("error", "Schutz nicht bestätigt")))
            messages.append({"notification_ids": ids, "severity": "urgent", "texts": split_blocks(text)})
            continue
        chat = next((row[2]["chat_id"] if row[2] else row[3]["source"]["chat_id"] for row in items if row[2] or row[3]), None)
        blocks = ["**Quelle:** " + source_name(data, chat)]
        by_trade = {}
        for _, notice, trade, _ in items:
            if trade:
                by_trade.setdefault(trade["trade_id"], (trade, []))[1].append(notice)
        blocks += [trade_block(data, trade, notices) for trade, notices in by_trade.values()]
        for _, notice, _, event in items:
            for target in notice.get("targets", []):
                if target.get("trade_id") in by_trade:
                    continue
                plan = {"exchange": target["exchange"], "profile_id": target.get("profile_id")}
                signal = (event or {}).get("interpretation") or {}
                title = literal(signal.get("symbol") or "Markt nicht zugeordnet")
                reason = target.get("error", "Ergebnis nicht bestätigt")
                heading = "⏳ **Börsenziel ungeklärt**" if target.get("status") in ("unknown", "pending", "planned", "opening", "protecting", "needs_reconciliation") else "⚠️ **Signal abgelehnt**"
                blocks.append(f"{heading} · {title}\n\n{context(data, plan)}\n\n**Grund:** {literal(ERRORS.get(reason, reason))}")
        if not by_trade and len(blocks) == 1:
            notice = items[0][1]
            blocks.append("ℹ️ **Systemmeldung**\n" + literal(notice.get("error") or "Gespeichertes Ereignis ohne vollständige Trade-Zuordnung"))
        messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks("\n\n".join(blocks))})
    return {"status": split_blocks(status_text(data, exchanges, sheets_enabled, inventory or {})), "messages": messages,
            "remaining_notification_ids": [identity for identity in notifications if identity not in chosen]}
