"""Readable trading settings; display only, never a second configuration source."""
from decimal import Decimal

from aqla_hyperliquid_mcp.models import MAX_SIGNAL_AGE_SECONDS, STOP_LADDERS, Policy

# Field -> category, label, unit, allowed values, explanation.
PARAMETERS = {
    "entry_execution": ("Ausführung", "Entry-Ausführung", "", "standard / maker_only", "Maker-only platziert ausschließlich Post-only-Limits; kein Taker-Fallback."),
    "take_profit_execution": ("Ausführung", "TP-Ausführung", "", "standard / maker_only", "Maker-only nutzt Post-only-Limits für Take Profits. Schutz-Stops bleiben unverändert."),
    "fallback_stop_loss_percent": ("Backup", "SL bei fehlender Angabe", "% Kursbewegung", "> 0 bis < 100 oder nicht gesetzt", "Nur fehlende SLs; Einheit laut SL-Einstellung. ROE: Verlust der ursprünglichen Positionsmargin vor Gebühren/Funding; wirksamer Tradehebel, tatsächlicher Entry-VWAP."),
    "fallback_stop_loss_unit": ("Backup", "Einheit des Backup-SL", "", "course_percent / roe_percent", "Kursverlust oder Verlust der ursprünglichen Positionsmargin vor Gebühren/Funding. Standard: Kursprozent."),
    "fallback_take_profit_percentages": ("Backup", "TPs bei fehlender Angabe", "% Kursbewegung", "1–20 eindeutige aufsteigende Werte zwischen 0 und 100", "Nur vollständig fehlende TP-Liste; Einheit laut TP-Einstellung. ROE: nominaler Gewinn auf ursprüngliche Positionsmargin vor Gebühren/Funding mit wirksamem Tradehebel. Signalziele bleiben erhalten."),
    "fallback_take_profit_unit": ("Backup", "Einheit der Backup-TPs", "", "course_percent / roe_percent", "Kursbewegung oder nominale ROE auf ursprüngliche Positionsmargin vor Gebühren/Funding. Standard: Kursprozent."),
    "product": ("Markt", "Handelsprodukt", "", "USDC-Perpetuals", "Die automatische Signalstrecke handelt USDC-Perpetuals; Spot wird dort nicht ausgeführt."),
    "sizing_mode": ("Positionsgröße", "Größenberechnung", "", "Nach Risiko / Fester Positionswert", "Bestimmt die Positionsgröße. Risiko-, Margin- und Positionswertgrenzen gelten in beiden Modi."),
    "risk_percent": ("Risiko", "Risiko je Trade", "% Kontoequity", "> 0 bis 100", "Obergrenze für das berechnete SL-Risiko einschließlich Kostenreserve; auch bei festem Positionswert."),
    "fixed_notional_usdc": ("Positionsgröße", "Fester Positionswert", "USDC", "> 0 oder nicht gesetzt", "Nur bei festem Positionswert erforderlich. Risiko- und Margin-Grenzen können die Menge weiter begrenzen."),
    "max_notional_usdc": ("Positionsgröße", "Maximaler Positionswert", "USDC", "> 0", "Obergrenze für den Positionswert eines neuen Trades, unabhängig vom Hebel."),
    "leverage_mode": ("Margin", "Hebelmodus", "", "Fest / Aus Signal, am Börsenmaximum gedeckelt", "Aus Signal: übernimmt den belegten Signalhebel bis zum aktuellen Marktmaximum. Ohne eindeutigen Signalhebel kein neuer Trade."),
    "leverage": ("Margin", "Fester Hebel", "fach", "1 bis 100; zusätzlich Börsenlimit", "Nur im festen Modus verwendet. Übersteigt dieser Wert das Marktlimit, wird der Trade abgelehnt. Im Signalmodus ist er kein Ersatzwert."),
    "margin_mode": ("Margin", "Margin-Modus", "", "Isolated / Cross", "Legt fest, ob die Börse isolierte oder gemeinsame Margin für den Markt verwendet."),
    "max_open_trades": ("Risiko", "Maximal offene Trades", "Trades", "1 bis 100", "Begrenzt gleichzeitig aktive Aqla-Trades einschließlich noch ausstehender Entries."),
    "max_total_risk_percent": ("Risiko", "Maximales Gesamtrisiko", "% Kontoequity", "> 0 bis 100", "Begrenzt die Summe des reservierten berechneten Risikos aller aktiven Aqla-Trades."),
    "entry_mode": ("Einstieg", "Entry bei fehlendem Preis", "", "Signalpreis erforderlich / Sonst Marktpreis", "Bestimmt, ob ein Signal ohne Entry-Preis zum Marktpreis umgesetzt werden darf."),
    "entry_range_choice": ("Einstieg", "Entry-Zone", "", "Zwei Entries / Nah / Fern / Mitte", "split: zwei Limits an den Grenzen mit gemeinsamem Budget. Bestehende Nah-/Fern-/Mitte-Pläne bleiben lesbar."),
    "management_mode": ("Verwaltung", "Verwaltungsmodus", "", "Standard / Waverider", "Waverider verwaltet Kern und Flex in Signalrichtung. Ohne passende Qualitätsfreigabe nur Shadow."),
    "waverider_flex_percent": ("Verwaltung", "Waverider Flex-Anteil", "%", "Größer 0, kleiner 100", "Anteil jedes ursprünglichen Fills; die übrige Menge bleibt Kern. Gemeinsames Risikobudget."),
    "waverider_until_tp": ("Verwaltung", "Waverider-Ende", "TP", "1–20 / last", "Am gewählten oder letzten vorhandenen TP enden neue Wellen; Laufzeit laut Waverider-Einstellung."),
    "waverider_lifetime": ("Verwaltung", "Waverider-Laufzeit", "", "signal_deadline / until_target", "Bis zur Entry-Frist oder unabhängig davon bis zum gewählten TP. Ursprüngliche Entries behalten immer ihre Signalfrist; SL und Risikobudget bleiben verbindlich."),
    "max_slippage_percent": ("Ausführung", "Maximale Slippage", "% Referenzpreis", "> 0 bis 10", "Preisgrenze für Marktorders relativ zum Referenzpreis. Eine vollständige Ausführung wird dadurch nicht garantiert."),
    "max_signal_age_seconds": ("Einstieg", "Maximales Signalalter", "Sekunden", f"1 bis {MAX_SIGNAL_AGE_SECONDS}", "Gültigkeitsdauer ab ursprünglichem Signalzeitpunkt. Die Frist wird durch Wiederholung nicht verlängert."),
    "take_profit_allocation": ("Take Profit", "TP-Verteilung", "", "Gleichmäßig / Halbieren / Eigene Gewichte / Adaptiv / 100 % an TP1", "Adaptiv: maximal fünf erste Signalziele, 50/25/12,5/7,5/5 %. Falls Mindestgrößen nicht reichen, weniger Ziele ohne größere Position. Das letzte erhält den Rest. 100 % an TP1 nutzt nur das erste Ziel."),
    "take_profit_weights": ("Take Profit", "Eigene TP-Gewichte", "% je TP", "Bis 20 Werte > 0; Summe 100", "Nur für eigene Gewichte. Anzahl muss den TPs des Signals entsprechen; minimale Börsengrößen gelten weiterhin."),
    "break_even_after_tp": ("Stop Loss", "Einstand nach TP", "TP-Nummer", "1 bis 20 oder deaktiviert", "Verschiebt den Stop nach vollständiger Ausführung des ausgewählten TP auf den tatsächlichen Einstand."),
    "stop_management": ("Stop Loss", "Stop-Verwaltung", "", "Einstand nach gewähltem TP / TP-Leiter / nach TP2 auf TP1", "TP-Leiter: TP1→Einstand, TP3→TP1, TP4→TP2. Nach TP2 auf TP1: erst nach vollständig bestätigten eigenen TP1-/TP2-Fills den Reststop auf den festgehaltenen TP1 setzen. Kein Nachziehen bei TP1; engere Stops bleiben erhalten."),
    "allowed_updates": ("Updates", "Erlaubte Signal-Updates", "", "Stop ändern / Einstand / TPs ersetzen / Reduzieren / Schließen / Entry stornieren", "Legt fest, welche Update-Arten aus Quellen automatisch umgesetzt werden dürfen."),
    "allow_stop_widening": ("Stop Loss", "Stop-Ausweitung erlauben", "", "Ja / Nein", "Erlaubt weiter entfernte Stops nur innerhalb der weiterhin geprüften Risikogrenzen."),
    "cost_reserve_bps": ("Risiko", "Kostenreserve", "Basispunkte", "0 bis 1000", "Puffer in der Risikoberechnung. 1 Basispunkt = 0,01 % des Preises; keine tatsächliche Gebührenabrechnung."),
}

VALUE_LABELS = {
    "course_percent": "Kursprozent", "roe_percent": "ROE auf ursprüngliche Margin",
    "standard": "Standard", "maker_only": "Nur Maker (Post-only)",
    "fixed": "Fest", "signal": "Aus Signal, am Börsenmaximum gedeckelt",
    "perp": "Perpetuals", "spot": "Spot (automatisch nicht unterstützt)",
    "risk_percent": "Nach Risiko", "fixed_notional": "Fester Positionswert",
    "cross": "Cross", "isolated": "Isolated", "source": "Signalpreis erforderlich",
    "source_or_market": "Sonst Marktpreis", "near": "Nah", "far": "Fern", "midpoint": "Mitte",
    "split": "Zwei Entries", "waverider": "Waverider", "last": "Letztes TP",
    "signal_deadline": "Bis Entry-Frist oder TP", "until_target": "Bis gewähltem TP",
    "equal": "Gleichmäßig", "halving": "Halbieren", "custom": "Eigene Gewichte",
    "adaptive": "Adaptiv (maximal fünf Ziele)", "first_target": "100 % an TP1", "tp_ladder": "TP-Leiter: TP1→Entry, TP3→TP1, TP4→TP2",
    "tp2_to_tp1": "Nach TP2 auf TP1",
    "move_stop": "Stop ändern", "break_even": "Einstand", "replace_targets": "TPs ersetzen",
    "reduce": "Reduzieren", "close": "Schließen", "cancel_entry": "Entry stornieren",
}

OPERATING_PARAMETERS = 6


def backup_unit(field, values, unit):
    if field == "fallback_stop_loss_percent" and values.get("fallback_stop_loss_unit") == "roe_percent":
        return "% Marginverlust"
    if field == "fallback_take_profit_percentages" and values.get("fallback_take_profit_unit") == "roe_percent":
        return "% ROE"
    return unit


def display(value, field=""):
    if value is None:
        return "Deaktiviert" if field == "break_even_after_tp" else "Nicht gesetzt"
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return ", ".join(str(display(item)) for item in value) if value else "Keine"
    return VALUE_LABELS.get(value, value) if isinstance(value, str) else value


def parameter_values(data):
    """Yield stable IDs and presentation cells from authoritative execution state."""
    selected = data.get("policy")
    current = Policy.model_validate(selected).model_dump() if selected is not None else None
    aliases = {value: name for name, value in data.get("source_aliases", {}).items()}
    sources = ", ".join(aliases.get(source, source) for source in data["sources"]) or "Keine freigegeben"
    yield "network", "Betrieb", "Handelsnetzwerk", data["network"].capitalize(), "", "Testnet / Mainnet", "VPS-Konfiguration", "Aktive Hyperliquid-Umgebung; ein Wechsel erfordert getrennten Ausführungszustand."
    yield "trading_account", "Betrieb", "Handelskonto", data.get("trading_account", data["account"]), "", "Konfiguriertes Konto oder Vault", "VPS-Konfiguration", "Konto, auf dem Orders ausgeführt werden. Keine Schlüssel oder Zugangsdaten."
    yield "paused", "Betrieb", "Neue Trades pausiert", display(data["paused"]), "", "Ja / Nein", "Nein", "Pause sperrt neue Trades; vorhandene Positionen und ihr Schutz werden weiter verwaltet."
    yield "sources", "Betrieb", "Freigegebene Quellen", sources, "", "Eigene Telegram-Quellen", "Keine", "Abonnierte Quellen; zusätzlich sind vom Betreiber an den Bot weitergeleitete Signale möglich."
    yield "policy_selected", "Betrieb", "Handelsregeln festgelegt", "Ja" if current is not None else "Nein", "", "Ja / Nein", "Nein", "Ohne festgelegte Handelsregeln werden keine neuen Trades eröffnet. Änderungen erfolgen über Telegram/Hermes."
    yield "policy_revision", "Betrieb", "Regelstand", data["policy_revision"], "Revision", "Automatisch", "0", "Version der aktiven Handelsregeln; wird beim Ändern der Regeln erhöht."
    yield from _policy_parameter_values(data, current)
    yield from _profile_parameter_values(data)
    yield from _venue_limit_values(data)
    yield from _account_limit_values(data)
    yield from _ranking_parameter_values(data)


def _policy_parameter_values(data, current):
    defaults = Policy().model_dump()
    for field, (category, label, unit, allowed, description) in PARAMETERS.items():
        default = "course_percent" if field in ("fallback_stop_loss_unit", "fallback_take_profit_unit") else "standard"
        value = display(current.get(field, default), field) if current is not None else "Nicht festgelegt"
        unit = backup_unit(field, current or {}, unit)
        if field == "leverage" and current is not None and current["leverage_mode"] == "signal":
            value = "Nicht verwendet (Signalmodus)"
        if field == "break_even_after_tp" and current is not None and current["stop_management"] in STOP_LADDERS:
            value = "Nicht verwendet (TP-Leiter)"
        if field == "take_profit_weights" and current is not None and current["take_profit_allocation"] != "custom":
            value = "Nicht verwendet"
        yield field, category, label, value, unit, allowed, display(defaults.get(field, default), field), description, "standard", "hyperliquid", "USDC", data.get("profiles", {}).get("standard", {}).get("revision", data["policy_revision"])


def _profile_parameter_values(data):
    for identity, profile in data.get("profiles", {}).items():
        if identity == "standard" or profile.get("parameters") is None:
            continue
        for field, value in profile["parameters"].items():
            legacy = {"fixed_notional_quote": "fixed_notional_usdc", "max_notional_quote": "max_notional_usdc"}.get(field, field)
            category, label, unit, allowed, description = PARAMETERS[legacy]
            unit = backup_unit(field, profile["parameters"], unit)
            yield "profile:" + identity + ":" + field, category, label, display(value, legacy), "Abrechnungswährung" if field.endswith("_quote") else unit, allowed, "", description, identity, "", "Je Börsenziel", profile["revision"]


def _venue_limit_values(data):
    for venue, limits in data.get("venue_limits", {}).items():
        for field in ("max_open_trades", "max_total_risk_percent"):
            _category, label, unit, allowed, _description = PARAMETERS[field]
            yield "limits:" + venue + ":" + field, "Börsenlimits", label, display(limits[field]) if limits else "Nicht festgelegt", unit, allowed, "", "Gemeinsam für alle Pipelines auf diesem Börsenkonto.", "", venue, "", data.get("configuration_revision", 0)


def _account_limit_values(data):
    for key, limits in data.get("account_limits", {}).items():
        for field in ("max_open_trades", "max_total_risk_percent"):
            _category, label, unit, allowed, _description = PARAMETERS[field]
            yield "account:" + key + ":" + field, "Kontolimits", label, display(limits[field]) if limits else "Nicht festgelegt", unit, allowed, "", "Gemeinsam je Börse/Netzwerk/Konto, einschließlich Reservierungen.", "", key, "", data.get("configuration_revision", 0)


def _ranking_parameter_values(data):
    ranking = data.get("ranking_policy")
    if ranking:
        for tier, factor in ranking["factors"].items():
            yield "rank:" + tier, "Ranking", "RPR-Faktor " + tier, display(factor), "× Profil-RPR", "> 0", "", "B/A/S erhöhen im Mainnet zusätzlich den Positionsdeckel; Kontolimits bleiben verbindlich.", "", "", "Je Konto", data["ranking_revision"]
        for field in ("window", "promotion_confirmations", "demotion_confirmations", "hard_drawdown_r"):
            yield "ranking:" + field, "Ranking", field, display(ranking[field]), "R" if field == "hard_drawdown_r" else "Setups", "Authentifizierte Betreiberkonfiguration", "", "Nur belegte Netto-R-Ergebnisse; ein ursprüngliches Setup zählt einmal.", "", "", "", data["ranking_revision"]
