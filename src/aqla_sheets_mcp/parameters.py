"""Readable trading settings; display only, never a second configuration source."""
from decimal import Decimal

from aqla_hyperliquid_mcp.models import Policy

# Field -> category, label, unit, allowed values, explanation.
PARAMETERS = {
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
    "entry_range_choice": ("Einstieg", "Preis aus Entry-Zone", "", "Nah / Fern / Mitte", "Mitte: Zonenmittelpunkt. Nah: Long obere, Short untere Grenze. Fern: jeweils umgekehrt."),
    "max_slippage_percent": ("Ausführung", "Maximale Slippage", "% Referenzpreis", "> 0 bis 10", "Preisgrenze für Marktorders relativ zum Referenzpreis. Eine vollständige Ausführung wird dadurch nicht garantiert."),
    "max_signal_age_seconds": ("Einstieg", "Maximales Signalalter", "Sekunden", "1 bis 86400", "Gültigkeitsdauer ab ursprünglichem Signalzeitpunkt. Die Frist wird durch Wiederholung nicht verlängert."),
    "take_profit_allocation": ("Take Profit", "TP-Verteilung", "", "Gleichmäßig / Halbieren / Eigene Gewichte", "Gleichmäßig auf alle Ziele oder jeweils halbieren; beim Halbieren erhält das letzte Ziel den Rest."),
    "take_profit_weights": ("Take Profit", "Eigene TP-Gewichte", "% je TP", "Bis 20 Werte > 0; Summe 100", "Nur für eigene Gewichte. Anzahl muss den TPs des Signals entsprechen; minimale Börsengrößen gelten weiterhin."),
    "break_even_after_tp": ("Stop Loss", "Einstand nach TP", "TP-Nummer", "1 bis 20 oder deaktiviert", "Verschiebt den Stop nach vollständiger Ausführung des ausgewählten TP auf den tatsächlichen Einstand."),
    "allowed_updates": ("Updates", "Erlaubte Signal-Updates", "", "Stop ändern / Einstand / TPs ersetzen / Reduzieren / Schließen / Entry stornieren", "Legt fest, welche Update-Arten aus Quellen automatisch umgesetzt werden dürfen."),
    "allow_stop_widening": ("Stop Loss", "Stop-Ausweitung erlauben", "", "Ja / Nein", "Erlaubt weiter entfernte Stops nur innerhalb der weiterhin geprüften Risikogrenzen."),
    "cost_reserve_bps": ("Risiko", "Kostenreserve", "Basispunkte", "0 bis 1000", "Puffer in der Risikoberechnung. 1 Basispunkt = 0,01 % des Preises; keine tatsächliche Gebührenabrechnung."),
}

VALUE_LABELS = {
    "fixed": "Fest", "signal": "Aus Signal, am Börsenmaximum gedeckelt",
    "perp": "Perpetuals", "spot": "Spot (automatisch nicht unterstützt)",
    "risk_percent": "Nach Risiko", "fixed_notional": "Fester Positionswert",
    "cross": "Cross", "isolated": "Isolated", "source": "Signalpreis erforderlich",
    "source_or_market": "Sonst Marktpreis", "near": "Nah", "far": "Fern", "midpoint": "Mitte",
    "equal": "Gleichmäßig", "halving": "Halbieren", "custom": "Eigene Gewichte",
    "move_stop": "Stop ändern", "break_even": "Einstand", "replace_targets": "TPs ersetzen",
    "reduce": "Reduzieren", "close": "Schließen", "cancel_entry": "Entry stornieren",
}

OPERATING_PARAMETERS = 6


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
    defaults = Policy().model_dump()
    for field, (category, label, unit, allowed, description) in PARAMETERS.items():
        value = display(current[field], field) if current is not None else "Nicht festgelegt"
        if field == "leverage" and current is not None and current["leverage_mode"] == "signal":
            value = "Nicht verwendet (Signalmodus)"
        yield field, category, label, value, unit, allowed, display(defaults[field], field), description, "standard", "hyperliquid", "USDC", data.get("profiles", {}).get("standard", {}).get("revision", data["policy_revision"])
    for identity, profile in data.get("profiles", {}).items():
        if identity == "standard" or profile.get("parameters") is None:
            continue
        for field, value in profile["parameters"].items():
            legacy = {"fixed_notional_quote": "fixed_notional_usdc", "max_notional_quote": "max_notional_usdc"}.get(field, field)
            category, label, unit, allowed, description = PARAMETERS[legacy]
            yield "profile:" + identity + ":" + field, category, label, display(value, legacy), "Abrechnungswährung" if field.endswith("_quote") else unit, allowed, "", description, identity, "", "Je Börsenziel", profile["revision"]
    for venue, limits in data.get("venue_limits", {}).items():
        for field in ("max_open_trades", "max_total_risk_percent"):
            category, label, unit, allowed, description = PARAMETERS[field]
            yield "limits:" + venue + ":" + field, "Börsenlimits", label, display(limits[field]) if limits else "Nicht festgelegt", unit, allowed, "", "Gemeinsam für alle Pipelines auf diesem Börsenkonto.", "", venue, "", data.get("configuration_revision", 0)
