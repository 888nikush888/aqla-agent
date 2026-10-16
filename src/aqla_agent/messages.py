"""Read-only Telegram views over durable facts; delivery remains native Hermes."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .execution_outcome import execution_counts, retry_view, trade_outcome
from .execution_roles import observed_roles
from .source_queue import is_source_row

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
    "Entry price observation is stale or has an invalid timestamp": "Die aktuelle Preisbeobachtung ist nicht frisch oder zeitlich nicht bestätigt.",
    "A TP leg is below the exchange notional minimum": "Mindestens eine TP-Order unterschreitet die Börsenmindestgröße.",
    "A TP leg rounds to zero; choose fewer targets or different weights": "Mindestens eine TP-Order ist nach Börsenrundung zu klein.",
    "Current equity/margin no longer permits this exact plan": "Aktuelles Eigenkapital oder freie Margin reicht für diesen unveränderten Plan nicht mehr aus.",
    "Refreshed entry must stay within the original quantity, risk, notional and deadline": "Die Budgetkorrektur würde ursprüngliche Ausführungsgrenzen überschreiten; kein Orderversand.",
    "Budget refresh cannot change the original market or add TP targets": "Die Budgetkorrektur darf weder den ursprünglichen Markt ändern noch TP-Ziele hinzufügen.",
}
CHUNK_UNITS = 3000
MAX_NOTIFICATIONS = 100


def observation_lines(row):
    retry = retry_view(row)
    if not retry:
        return None
    text = "**Technische Prüfung:** " + ("Wiederaufnahme beendet" if retry.get("exhausted") else "Lesebeleg noch nicht bestätigt; begrenzte Wiederaufnahme ausstehend")
    labels = {"account": "Konto", "price": "Preis", "quote": "Preis", "market": "Markt"}
    if retry.get("stage"):
        text += " · " + literal(labels.get(retry["stage"], retry["stage"]))
    if retry.get("code"):
        text += " · " + literal(retry["code"])
    if retry.get("exception_type"):
        text += " · " + literal(retry["exception_type"])
    if not retry.get("exhausted") and type(retry.get("next_retry_at")) is int:
        text += "\n**Nächste Prüfung:** " + _berlin_time(retry["next_retry_at"]) + " · Berlin"
    if type(retry.get("deadline_at")) is int:
        text += "\n**Unveränderte Signalfrist:** " + _berlin_time(retry["deadline_at"]) + " · Berlin"
    if retry.get("authority") == "preview":
        text += "\nNur Vorschau: keine automatische Ausführung freigegeben."
    return text


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
    # Native delivery scans valid MEDIA directives even inside inline code.
    text = re.sub(r"(?i)MEDIA:", lambda match: match.group()[:-1] + "：", text)
    text = text.replace("[[", "［［").replace("]]", "］］")
    # Protect dynamic labels from becoming links, HTML, emphasis or code fences.
    if any(char in text for char in "*_`[]<>~|#\\") or "MEDIA：" in text.upper():
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


def number(value, *, places=None):
    value = decimal(value)
    if value is None:
        return "nicht bestätigt"
    if places is not None:
        try:
            value = value.quantize(Decimal(1).scaleb(-places))
        except InvalidOperation:
            return "nicht bestätigt"
    raw = format(value, "f")
    whole, _, fraction = raw.partition(".")
    sign = "−" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    groups = [whole[max(0, end - 3):end] for end in range(len(whole), 0, -3)]
    fraction = fraction.rstrip("0")
    return sign + ".".join(reversed(groups)) + ("," + fraction if fraction else "")


RECOVERY_LABELS = {
    "market_conflict": "Manueller/gemischter Positionsverlauf; Eigentumsprüfung erforderlich",
    "replacement_unconfirmed": "SL-Ersatz unbestätigt; vorhandenen Schutz erhalten",
    "protection_unconfirmed": "Kein wirksamer SL bestätigt; Positionsschutz dringend prüfen",
    "reconciliation_unavailable": "Börsenabgleich vorübergehend nicht bestätigt",
    "recovery_timeout": "Börsenabgleich hat das Zeitlimit erreicht",
    "owned_order_cancellation_unconfirmed": "Aufhebung eigener Orders unbestätigt; Markt bleibt reserviert",
    "entry_cancellation_unconfirmed": "Entry-Aufhebung/Fill-Abgleich unbestätigt",
    "account_binding_mismatch": "Ursprüngliche Kontobindung stimmt nicht überein",
}


def recovery_block(notice):
    from .recovery_notices import safe_report
    reports = notice.get("recovery_reports")
    if reports is None:
        reports = [fact for row in notice.get("reports", []) if (fact := safe_report(row))]
    lines = ["✅ **Börsenabgleich wiederhergestellt**" if notice.get("resolved") else "⚠️ **Börsenabgleich benötigt Aufmerksamkeit**"]
    for row in reports:
        label = RECOVERY_LABELS.get(row.get("code"), RECOVERY_LABELS["reconciliation_unavailable"])
        details = [literal(row[key]) for key in ("account_key", "market", "trade_id", "operation_id") if row.get(key)]
        lines.append((" · ".join(details) + "\n" if details else "") + label)
    if not notice.get("resolved"):
        lines.append("Aktuellen Status prüfen; ungeklärte Operationen abgleichen. Bei Marktkonflikten /aqla_konflikt <Trade-ID> verwenden.")
    return "\n\n".join(lines)


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
        text += " · " + literal(ERRORS.get(plan["fallback_reason"], plan["fallback_reason"]))
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
    provisional = risk is None and trade_outcome(trade)["entry_fill_confirmed"] and decimal(trade.get("filled_quantity")) and decimal(trade.get("entry_vwap")) and decimal(trade.get("stop_price"))
    if provisional:
        risk = decimal(trade["filled_quantity"]) * abs(decimal(trade["entry_vwap"]) - decimal(trade["stop_price"]))
    text += "\n**Anfängliches SL-Risiko:** " + (number(risk) + " " + quote if risk is not None else "noch nicht finalisiert")
    if provisional:
        text += " · vorläufig für bestätigte Fills"
    return text


def target_label(plan, index):
    spec = plan.get("source_tp")
    if spec and spec.get("unit") != "price" and index < len(spec["values"]):
        return " · " + number(spec["values"][index]) + (" % ROE eigene Margin vor Kosten" if spec["unit"] == "roe_percent" else " % ROI vor Kosten" if spec["unit"] == "roi_percent" else " % Kursbewegung")
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
    facts = trade_outcome(trade, data.get("operations", {}))
    if stage == "conflicted":
        title = "⚠️ **Manueller Marktkonflikt**"
    elif stage == "unknown":
        title = "⏳ **Ausführung ungeklärt**"
    elif facts["state"] == "pending_observation":
        title = "⏳ **Technische Prüfung ausstehend**"
    elif stage in ("closing", "cleanup"):
        title = "⏳ **Schließung wird abgeglichen**"
    elif stage in ("closed", "cancelled", "rejected"):
        title = {"closed": "🏁 **Trade geschlossen**", "cancelled": "⚪ **Entry storniert**", "rejected": "⚠️ **Trade abgelehnt**"}[stage]
    elif facts["historical_fill_evidence_missing"]:
        title = "⚠️ **Ausführungsbelege fehlen**"
    elif facts["state"] == "unknown":
        title = "⏳ **Belegabgleich ungeklärt**"
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
    if facts["historical_fill_evidence_missing"]:
        lines.append("**Eigene Entry-Fills:** historisch nicht vollständig belegt; gespeicherte Mengen allein bestätigen keine Ausführung.")
    elif facts["state"] == "unknown" and stage != "unknown":
        lines.append("**Mengenabgleich:** gespeicherte Fills und Positionsmengen bestätigen den aktuellen Zustand nicht vollständig.")
    elif facts["state"] in ("not_sent", "no_entry_fill"):
        lines.append("**Eigene Entry-Fills:** keine bestätigt" + (" · Entry nicht gesendet" if facts["state"] == "not_sent" else ""))
    if stage == "conflicted" or trade.get("external_conflict"):
        lines.append("**Eigene Restposition:** wegen Konflikt nicht zuverlässig bestätigt.")
    if facts["state"] == "unknown" and facts["observation_retry"] and not facts["observation_retry"].get("exhausted"):
        lines.append("**Wiederaufnahme:** gesperrt, bis der ungeklärte Börsenausgang beziehungsweise Mengenabgleich geklärt ist.")
    elif observation_lines(trade):
        lines.append(observation_lines(trade))
    lines.append(execution_lines(trade))
    if risk_block(trade):
        lines.append(risk_block(trade))
    if stage == "waiting_entry":
        lines.append("**Status:** lokaler Wartezustand · noch keine Börsenorder")
    quote = literal(plan["market"].get("quote_token", "USDC"))
    full = opening or filled == 0 or stage in ("unknown", "protecting")
    if full:
        receipt_filled = facts["entry_fill_confirmed"] is True
        entry = trade.get("entry_vwap") if filled else plan.get("entry_price")
        label = "Entry realisiert" if filled and receipt_filled else "Entry gespeichert · unbelegt" if filled else "Entry geplant"
        lines.append(f"**{label}:** {number(entry)} {quote} · **Hebel:** {number(plan.get('effective_leverage', plan.get('policy', {}).get('leverage')))}×")
        if filled and receipt_filled and planned and filled < planned:
            lines.append(f"**Gefüllt:** {number(filled / planned * 100, places=2)} % der geplanten Menge")
        if trade.get("admission_refresh_from"):
            lines.append("**Menge vor Versand:** an das aktuelle Kontobudget angepasst")
        lines.append(f"**SL:** {number(trade.get('stop_price', plan.get('stop_loss')))} {quote}" + (" · vorgesehen" if stage == "waiting_entry" else " · Bestätigung ausstehend" if stage in ("unknown", "protecting", "opening") else ""))
        targets = trade.get("targets", plan.get("take_profits", []))
        budgets = trade.get("target_budgets") or plan.get("tp_quantities", [])
        total = sum((decimal(value) or Decimal(0) for value in budgets), Decimal(0))
        legs = [f"TP{index + 1}: {number(price)} {quote}" + target_label(plan, index) + (f" · {number((decimal(budgets[index]) or Decimal(0)) / total * 100, places=2)} % Position" if total and index < len(budgets) else " · Anteil nicht bestätigt") for index, price in enumerate(targets)]
        if legs:
            lines.append("**Ziele · Positionsanteil**\n" + "\n".join(legs))
            source_count = plan.get("source_take_profit_count")
            if plan.get("policy", {}).get("take_profit_allocation") == "adaptive" and source_count and source_count > len(targets):
                lines.append(f"**Adaptive TP-Auswahl:** {len(targets)} von {source_count} Signalzielen")
        raw, rounded, effective = plan.get("signal_leverage_original", plan.get("signal_leverage")), plan.get("signal_leverage"), plan.get("effective_leverage")
        if raw is not None and (decimal(raw) != decimal(rounded) or decimal(rounded) != decimal(effective)):
            lines.append(f"**Hebel angepasst:** Signal {number(raw)}× → abgerundet {number(rounded)}× → wirksam {number(effective)}×")
        if plan.get("backup_fields"):
            labels = {"leverage": "Hebel", "entry": "Entry", "stop_loss": "SL", "take_profits": "TPs"}
            lines.append("**Backup verwendet:** " + ", ".join(labels.get(field, literal(field)) for field in plan["backup_fields"]))
            if set(plan["backup_fields"]) & {"stop_loss", "take_profits"}:
                lines.append("**Backup-Preise:** " + ("endgültig ab realem Entry" if trade.get("backup_finalization", {}).get("phase") == "applied" else "vorläufig"))
                backup = plan.get("backup_values") or {}
                roe = []
                if "stop_loss" in plan["backup_fields"] and backup.get("fallback_stop_loss_unit") == "roe_percent":
                    roe.append("SL: " + number(backup.get("fallback_stop_loss_percent")) + " % Marginverlust")
                if "take_profits" in plan["backup_fields"] and backup.get("fallback_take_profit_unit") == "roe_percent":
                    roe.append("TPs: " + ", ".join(number(value) for value in backup.get("fallback_take_profit_percentages", [])[:len(targets)]) + " % ROE")
                if roe:
                    lines.append("**Backup-Ziele:** " + " · ".join(roe) + "\nBasis: ursprüngliche Positionsmargin vor Gebühren und Funding.")
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
        if "remaining_quantity" in changes and filled and facts["current_ownership_confirmed"]:
            lines.append(f"**Position verbleibend:** {number((decimal(trade.get('remaining_quantity')) or 0) / filled * 100, places=2)} %")
    if stage == "closed":
        from .lifecycle import close_reason
        lines.append("**Grund:** " + literal(trade.get("close_reason", close_reason(trade))["label"]))
        result = pnl(trade)
        if result is not None:
            lines.append(f"**Netto vor Funding:** {number(result)} {quote}")
        else:
            lines.append("**Ergebnis:** PnL nicht vollständig bestätigt")
    if trade.get("unsent_reason") and not (facts["observation_retry"] and not facts["observation_retry"].get("exhausted")):
        lines.append("**Grund:** " + literal(ERRORS.get(trade["unsent_reason"], trade["unsent_reason"])))
    remaining = decimal(trade.get("remaining_quantity")) or Decimal(0)
    if trade.get("maker_target_rejection") and stage in ("open", "protecting", "unknown", "conflicted") and remaining > 0:
        stop_confirmed = stage in ("open", "protecting") and not trade.get("execution_incident") and any(
            order.get("kind") == "sl" and not order.get("retired")
            and trade.get("order_observations", {}).get(order["cloid"], {}).get("status") == "open"
            and (decimal(order.get("quantity")) or Decimal(0)) >= remaining
            and decimal(order.get("price")) == decimal(trade.get("stop_price"))
            for order in trade.get("orders", []))
        lines.append("**TP abgelehnt:** Maker-Ziel bleibt unausgeführt; "
                     + ("vorhandener SL bleibt aktiv." if stop_confirmed else "SL-Schutz wird abgeglichen."))
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
            f"TP{index + 1}: {number(price)} {quote}" + target_label(plan, index) + (" · " + number((decimal(budgets[index]) or 0) / total * 100, places=2) + " % Position" if total and index < len(budgets) else "")
            for index, price in enumerate(targets)))
    return "\n\n".join(lines)


def status_text(data, exchanges, sheets_enabled, inventory):
    active = [row for row in data["trades"].values() if row["status"] in OPEN]
    from .source_queue import queue_overview
    source_queue = queue_overview(data)
    pending = source_queue["ready_total"]
    unknown = sum(row["phase"] in ("prepared", "sending", "unknown") for row in data["operations"].values())
    enabled = any(row.get("enabled") and row.get("configured") for row in exchanges.values())
    lines = ["📊 **Aqla · Systemstatus**", "**Entries:** " + ("pausiert" if data["paused"] else "nach Profilregeln freigegeben" if enabled else "keine Börse freigegeben")]
    lines += [f"**{VENUES.get(row.get('exchange', venue), literal(venue))}:** {literal(row.get('network', ''))} · " + ("aktiv" if row.get("enabled") and row.get("configured") else "Einrichtung fehlt" if not row.get("configured") else "deaktiviert") for venue, row in exchanges.items()]
    if data.get("ranking_policy", {}).get("enabled"):
        ranks = data.get("source_ranks", {})
        lines.append("**Kanalränge:** " + " · ".join(f"{source_name(data, source)}: {literal(row['tier'])}" + (" (Startfreigabe)" if row.get("bootstrap") and row.get("metrics", {}).get("score") is None else "") for source, row in ranks.items()))
    counts = execution_counts(data)
    lines.append(f"**Belegte eigene offene Positionen:** {counts['known_own_open']} · **Entries ausstehend:** {counts['pending_entries']} · **Eigene Position ungeklärt:** {counts['unsettled_ownership']}\n"
                 f"**Aktive Trade-Vorgänge:** {len(active)} · **Ausstehende Quellen:** {pending} · **Blockierte Quellen:** {source_queue['blocked_total']}\n"
                 f"**Quellen-Recovery:** {source_queue['recovery_source_total']} · **Ungeklärte Vorgänge:** {unknown}\nBasis: gespeicherte eigene Fills; Gesamtbestand der Börsenkonten separat.")
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
        elif "recovery_reports" in notice or "reports" in notice or "source_notice" in notice:
            key = ("recovery", identity)
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
        if key[0] == "recovery":
            notice = items[0][1]
            if "source_notice" in notice:
                code = notice["source_notice"].get("code", "source_status_changed")
                text = "ℹ️ **Quellenstatus**\n" + literal(ERRORS.get(code, code))
                sources = notice["source_notice"].get("sources", [])
                if sources:
                    text += "\nKanäle: " + ", ".join(literal(value) for value in sources)
            else:
                text = recovery_block(notice)
            messages.append({"notification_ids": ids, "severity": notice.get("severity", "normal"), "texts": split_blocks(text)})
            continue
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
        chat = next((row[2]["chat_id"] if row[2] else row[3]["source"]["chat_id"] for row in items
                     if row[2] or row[3] and is_source_row(row[3])), None)
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
                retry = retry_view(target)
                pending_retry = retry and not retry.get("exhausted") and target.get("status") not in ("unknown", "needs_reconciliation")
                heading = "⏳ **Technische Prüfung ausstehend**" if pending_retry else "⏳ **Börsenziel ungeklärt**" if target.get("status") in ("unknown", "pending", "planned", "opening", "protecting", "needs_reconciliation") else "⚠️ **Signal abgelehnt**"
                detail = observation_lines(target) if pending_retry else "**Grund:** " + literal(ERRORS.get(reason, reason))
                if retry and retry.get("exhausted"):
                    detail += "\n\n" + observation_lines(target)
                blocks.append(f"{heading} · {title}\n\n{context(data, plan)}\n\n{detail}")
        if not by_trade and len(blocks) == 1:
            notice = items[0][1]
            blocks.append("ℹ️ **Systemmeldung**\n" + literal(notice.get("error") or "Gespeichertes Ereignis ohne vollständige Trade-Zuordnung"))
        messages.append({"notification_ids": ids, "severity": "normal", "texts": split_blocks("\n\n".join(blocks))})
    for message in messages:
        message["notification_versions"] = {identity: notification_versions[identity] if notification_versions is not None else digest(notifications[identity])
                                            for identity in message["notification_ids"]}
    return {"status": split_blocks(status_text(data, exchanges, sheets_enabled, inventory or {})) if include_status else [], "messages": messages,
            "remaining_notification_ids": [identity for identity in notifications if identity not in chosen]}
