"""Read-only Telegram views over durable facts; delivery remains native Hermes."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .execution_roles import observed_roles

VENUES = {"hyperliquid": "Hyperliquid", "aster": "Aster", "lighter": "Lighter"}
OPEN = {"waiting_entry", "entry_pending", "preparing", "opening", "open", "protecting", "unknown", "closing", "cleanup", "conflicted"}
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


def execution_lines(trade):
    plan = trade["plan"]
    labels = {"standard": "Standard", "maker_only": "Maker only"}
    line = "**Ausführungsmodus:** Entry " + labels[plan.get("entry_execution", "standard")] + " · TP " + labels[plan.get("take_profit_execution", "standard")]
    roles = observed_roles(trade)
    if roles:
        line += "\n**Fillrolle belegt:** " + ", ".join({"maker": "Maker", "taker": "Taker", "unknown": "unbekannt"}[role] for role in roles)
    if trade.get("fill_execution_role_conflicts"):
        line += " · widersprüchliche Börsenbelege"
    return line


def literal(value):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    # Protect dynamic labels from becoming links, HTML, emphasis or code fences.
    if any(char in text for char in "*_`[]<>~|#\\") or "MEDIA:" in text.upper():
        # The pinned native HTML-image extractor also scans inline code. Keep
        # dynamic angle brackets visible without leaving an executable HTML tag.
        return "`" + text.replace("`", "ˋ").replace("\\", "⧵").replace("<", "‹").replace(">", "›") + "`"
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
    text = f"**Börse:** {VENUES.get(venue, literal(venue))} · {literal(network).capitalize()}\n**Profil:** {profile_name(data, plan.get('profile_id', 'standard'))}"
    rank = plan.get("rank_snapshot")
    if rank:
        text += f"\n**Kanalrang:** {literal(rank['tier'])} · **Risikofaktor:** {number(rank['factor'])}×"
        if rank.get("bootstrap") and rank.get("provisional"):
            text += " · Startfreigabe"
    if plan.get("fallback_reason"):
        text += "\n**Börsenersatz:** " + VENUES.get(plan.get("requested_exchange"), literal(plan.get("requested_exchange"))) + " → " + VENUES[venue]
        text += " · ursprünglicher Markt dort nicht gelistet"
    return text


def risk_block(trade):
    plan = trade["plan"]
    if not plan.get("rank_snapshot"):
        return None
    quote = literal(plan["market"].get("quote_token", "USDC"))
    text = "**RPR-Budget:** " + number(plan["policy"]["risk_percent"]) + " %"
    equity = trade.get("entry_equity_snapshot", {}).get("equity")
    if equity is not None:
        text += " · " + number(decimal(equity) * decimal(plan["policy"]["risk_percent"]) / 100) + " " + quote
    risk = trade.get("initial_sl_risk", {}).get("amount")
    provisional = risk is None and decimal(trade.get("filled_quantity")) and decimal(trade.get("entry_vwap")) and decimal(trade.get("stop_price"))
    if provisional:
        risk = decimal(trade["filled_quantity"]) * abs(decimal(trade["entry_vwap"]) - decimal(trade["stop_price"]))
    text += "\n**Anfängliches SL-Risiko:** " + (number(risk) + " " + quote if risk is not None else "noch nicht finalisiert")
    if provisional:
        text += " · vorläufig für bestätigte Fills"
    return text


def target_label(plan, index):
    spec = plan.get("source_tp")
    if spec and spec.get("unit") != "price" and index < len(spec["values"]):
        return " · " + number(spec["values"][index]) + (" % ROI vor Kosten" if spec["unit"] == "roi_percent" else " % Kursbewegung")
    return ""


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
    plan = {**plan, "source_tp": trade.get("source_tp_override", plan.get("source_tp"))}
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
    lines.append(execution_lines(trade))
    if risk_block(trade):
        lines.append(risk_block(trade))
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
        legs = [f"TP{index + 1}: {number(price)} {quote}" + target_label(plan, index) + (f" · {number((decimal(budgets[index]) or Decimal(0)) / total * 100)} % Position" if total and index < len(budgets) else " · Anteil nicht bestätigt") for index, price in enumerate(targets)]
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
    if trade.get("maker_target_rejection"):
        lines.append("**TP abgelehnt:** Maker-Ziel bleibt unausgeführt; vorhandener SL bleibt aktiv.")
    if trade.get("external_conflict"):
        lines.append("**Verwaltung angehalten:** " + literal(trade['external_conflict']))
    stop_requests = [notice["stop_update"] for notice in notices if notice.get("stop_update")]
    if not stop_requests and trade.get("pending_stop_update"):
        stop_requests = [trade["pending_stop_update"]]
    stop_labels = {"waiting_fill": "⏳ SL-Update wartet auf eigenen Fill", "replacing": "⏳ SL-Ersatz wird geprüft",
                   "unknown": "⏳ SL-Update noch nicht bestätigt", "confirmed": "✅ SL-Update bestätigt",
                   "rejected": "⚠️ SL-Update abgelehnt", "superseded": "↪️ SL-Update durch neue Anweisung ersetzt",
                   "closed_without_application": "⚪ SL-Update ohne Anwendung beendet"}
    for request in stop_requests:
        lines.append("**" + stop_labels[request["phase"]] + ":** " + number(request["stop"]) + " " + quote
                     + ("\n**Grund:** " + literal(request["reason"]) if request.get("reason") else ""))
    return "\n\n".join(lines)


def _berlin_time(timestamp):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(timestamp / 1000, ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M:%S")


def lifecycle_block(data, event):
    trade = event["facts"]
    current = data.get("trades", {}).get(trade["trade_id"], {})
    # Native-role enrichment is a read-only sidecar, never a rewrite of the
    # immutable lifecycle facts or their financial fill fingerprints.
    trade = {**trade, **{key: {identity: role for identity, role in current.get(key, {}).items()
                             if identity in trade.get("fill_events", {})}
                        for key in ("fill_execution_roles", "fill_execution_role_conflicts")}}
    plan = trade["plan"]
    plan = {**plan, "source_tp": trade.get("source_tp_override", plan.get("source_tp"))}
    time = _berlin_time(event["time"])
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
    if risk_block(trade):
        lines.append(risk_block(trade))
    if plan.get("source_exchange"):
        lines.append("**Signalbörse:** " + literal(plan["source_exchange"]))
    lines.append(execution_lines(trade))
    targets = trade.get("targets", plan.get("take_profits", []))
    budgets = trade.get("target_budgets") or plan.get("tp_quantities", [])
    total = sum((decimal(value) or Decimal(0) for value in budgets), Decimal(0))
    if targets:
        lines.append("**Ziele vorgesehen · Positionsanteil**\n" + "\n".join(
            f"TP{index + 1}: {number(price)} {quote}" + target_label(plan, index) + (" · " + number((decimal(budgets[index]) or 0) / total * 100) + " % Position" if total and index < len(budgets) else "")
            for index, price in enumerate(targets)))
    return "\n\n".join(lines)


def status_text(data, exchanges, sheets_enabled, inventory):
    active = [row for row in data["trades"].values() if row["status"] in OPEN]
    pending = sum(row["status"] in ("pending", "interpreted", "needs_reconciliation") for row in data["events"].values())
    unknown = sum(row["phase"] in ("prepared", "sending", "unknown") for row in data["operations"].values())
    enabled = any(row.get("enabled") and row.get("configured") for row in exchanges.values())
    lines = ["📊 **Aqla · Systemstatus**", "**Entries:** " + ("pausiert" if data["paused"] else "nach Profilregeln freigegeben" if enabled else "keine Börse freigegeben")]
    lines += [f"**{VENUES.get(row.get('exchange', venue), literal(venue))}:** {literal(row.get('network', ''))} · " + ("aktiv" if row.get("enabled") and row.get("configured") else "Einrichtung fehlt" if not row.get("configured") else "deaktiviert") for venue, row in exchanges.items()]
    if data.get("ranking_policy", {}).get("enabled"):
        ranks = data.get("source_ranks", {})
        lines.append("**Kanalränge:** " + " · ".join(f"{source_name(data, source)}: {literal(row['tier'])}" + (" (Startfreigabe)" if row.get("bootstrap") and row.get("metrics", {}).get("score") is None else "") for source, row in ranks.items()))
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


def summary_block(notice):
    title = {"entry_fill": "✅ **Einstieg ausgeführt**", "closed": "🏁 **Trade geschlossen**", "protection": "🚨 **Schutzvorfall**"}.get(notice["kind"], "ℹ️ **Systemmeldung**")
    lines = [title]
    timestamp = notice.get("observed_at") if notice["kind"] == "protection" else notice.get("time", notice.get("observed_at"))
    if timestamp is not None:
        label = {"entry_fill": "Ausführung", "closed": "Schließung", "protection": "Beobachtet"}.get(notice["kind"], "Belegzeitpunkt")
        lines.append(f"**{label}:** " + _berlin_time(timestamp) + " · Berlin")
    labels = {"trade_id": "Trade", "account_key": "Konto", "account": "Handelskonto",
              "exchange": "Börse", "network": "Netzwerk", "market": "Markt", "direction": "Richtung", "status": "Status"}
    for name, label in labels.items():
        if name in notice:
            value = str(notice[name])
            lines.append(f"**{label}:** " + literal(value[:299] + "…" if len(value) > 300 else value))
    if notice["kind"] == "protection":
        lines.append("**Entries:** gesperrt")
    if "reason" in notice:
        lines.append(("**Grund (gekürzt):** " if notice.get("reason_shortened") else "**Grund:** ") + literal(notice["reason"]))
    lines.append("**Details:** Umfangreiche gespeicherte Belege; vollständige Daten separat abrufbar.")
    return "\n\n".join(lines)


def telegram_view(data, exchanges, *, sheets_enabled=True, inventory=None, notification_versions=None, include_status=True):
    from aqla_hyperliquid_mcp.models import digest

    from .notifications import notification_sort_key
    notifications = data.get("notifications", {})
    # Urgent protection notices are independent of signal batching.
    chosen = sorted(notifications, key=lambda identity: notification_sort_key(identity, notifications[identity]))[:MAX_NOTIFICATIONS]
    groups = {}
    for identity in chosen:
        notice = notifications[identity]
        trade = data["trades"].get(notice.get("trade_id"))
        event = data["events"].get(notice.get("event_id") or (trade or {}).get("event_id"))
        if notice.get("detail_level") == "summary":
            key = ("summary", identity)
        elif notice.get("rank_change"):
            key = ("rank", identity)
        elif notice.get("monthly_report"):
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
        if key[0] == "summary":
            messages.append({"notification_ids": ids, "severity": "urgent" if ids[0].startswith("protection:") else "normal",
                             "texts": split_blocks(summary_block(items[0][1]))})
            continue
        if key[0] == "rank":
            event = items[0][1]["rank_change"]
            score = event["metrics"].get("score")
            text = "🏅 **Kanalrang geändert** · " + source_name(data, event["source_id"])
            text += "\n\n**Rang:** " + event["old"] + " → " + event["new"]
            text += "\n**Score:** " + number(score) + " · **Drawdown:** " + number(event["metrics"]["drawdown_r"]) + " R"
            if event.get("reason"):
                text += "\n**Grund:** " + literal(event["reason"])
            text += "\n**Neue Entries:** " + event["network"].capitalize() + " · **Risikofaktor:** " + number(event.get("factor", data["ranking_policy"]["factors"][event["new"]])) + "×"
            text += "\nBestehende Positionen behalten ihre Ausführung und Parameter."
            messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks(text)})
            continue
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
                plan = {name: target.get(name) for name in ("exchange", "network", "profile_id", "rank_snapshot", "requested_exchange", "fallback_reason")}
                signal = (event or {}).get("interpretation") or {}
                title = literal(signal.get("symbol") or "Markt nicht zugeordnet")
                reason = target.get("error", "Ergebnis nicht bestätigt")
                heading = "⏳ **Börsenziel ungeklärt**" if target.get("status") in ("unknown", "pending", "planned", "opening", "protecting", "needs_reconciliation") else "⚠️ **Signal abgelehnt**"
                blocks.append(f"{heading} · {title}\n\n{context(data, plan)}\n\n**Grund:** {literal(ERRORS.get(reason, reason))}")
        if not by_trade and len(blocks) == 1:
            notice = items[0][1]
            blocks.append("ℹ️ **Systemmeldung**\n" + literal(notice.get("error") or "Gespeichertes Ereignis ohne vollständige Trade-Zuordnung"))
        messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks("\n\n".join(blocks))})
    for message in messages:
        message["notification_versions"] = {identity: notification_versions[identity] if notification_versions is not None else digest(notifications[identity])
                                            for identity in message["notification_ids"]}
    return {"status": split_blocks(status_text(data, exchanges, sheets_enabled, inventory or {})) if include_status else [], "messages": messages,
            "remaining_notification_ids": [identity for identity in notifications if identity not in chosen]}
