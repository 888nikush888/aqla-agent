"""One native command catalog and value parser shared with the Telegram panel."""
import json

COMMANDS = {
    "aqla": ("Dashboard und vollständige Steuerzentrale", ""),
    "setup": ("Private Setup-Freigabe", "[status|aus]"),
    "aqla_status": ("Trades, Konten, Sync und Setup-Zeit", ""),
    "aqla_kanaele": ("Kanalränge und Nachweise", ""),
    "aqla_profile": ("Profile und Einstellungen", ""),
    "aqla_journal": ("Lokales Journal und Monatsberichte", ""),
    "aqla_rang": ("Kanalrang ändern", '["Kanalname" C]'),
    "aqla_parameter": ("Profilparameter ändern", "[Profil Feld Wert]"),
    "aqla_maker": ("Entry/TP-Ausführung ändern", "[Profil entry|tp|beide standard|maker_only]"),
    "aqla_limits": ("Kontolimits ändern", "[Börse:Netzwerk Feld Wert]"),
    "aqla_pause": ("Neue Entries pausieren; Schutz läuft weiter", ""),
    "aqla_fortsetzen": ("Neue Entries freigeben", ""),
    "aqla_hinzufuegen": ("Kanäle ergänzen; bestehende bleiben erhalten", "[ID|@username|Name ...]"),
    "aqla_entfernen": ("Nur ausgewählte Kanäle entfernen", "[ID|@username|Name ...]"),
    "aqla_kanalname": ("Telegram-ID benennen", '[ID "Name"]'),
    "aqla_konflikt": ("Eigentumskonflikt anhand aktueller Belege prüfen", "[Trade-ID]"),
    "aqla_konflikt_loesen": ("Nur belegte Schließung lokal bestätigen", "[Trade-ID Fingerabdruck]"),
    "aqla_funktionen": ("Verfügbare native Funktionen", ""),
}


PROTECTED = {"aqla_rang", "aqla_parameter", "aqla_maker", "aqla_limits", "aqla_fortsetzen", "aqla_hinzufuegen", "aqla_entfernen", "aqla_konflikt_loesen"}


RESET_COMMANDS = {"stop", "new", "reset", "restart"}


CALLBACK_PREFIX = "aqla:"


UNITS = {
    "risk_percent": "%", "fixed_notional_quote": "Quote", "max_notional_quote": "Quote",
    "fallback_stop_loss_percent": "%", "fallback_take_profit_percentages": "% (JSON-Liste)",
    "max_slippage_percent": "%", "max_signal_age_seconds": "Sekunden", "cost_reserve_bps": "bps",
    "max_open_trades": "Trades", "max_total_risk_percent": "%", "leverage": "×",
    "take_profit_weights": "Gewichte (JSON-Liste)", "allowed_updates": "JSON-Liste",
    "break_even_after_tp": "TP-Nummer",
    "waverider_flex_percent": "% der eigenen Entry-Menge", "waverider_until_tp": "TP-Nummer 1–20 oder last",
    "waverider_lifetime": "signal_deadline = Entry-Frist; until_target = bis gewähltem TP",
}


BACKUP_UNIT_LABELS = {"course_percent": "% Kursbewegung", "roe_percent": "% ROE (ursprüngliche Margin)"}


LIMIT_FIELDS = {"offene_trades": "max_open_trades", "offene trades": "max_open_trades",
                "gesamtrisiko": "max_total_risk_percent"}


def parameter_unit(field, parameters):
    if field == "fallback_stop_loss_percent":
        return "% Marginverlust" if parameters.get("fallback_stop_loss_unit", "course_percent") == "roe_percent" else "% Kursbewegung"
    if field == "fallback_take_profit_percentages":
        return ("% ROE" if parameters.get("fallback_take_profit_unit", "course_percent") == "roe_percent" else "% Kursbewegung") + " (JSON-Liste)"
    return UNITS.get(field, "Wert")


def parse_value(text, schema):
    variants = schema.get("anyOf", [schema])
    if text.strip().lower() in ("null", "aus") and any(row.get("type") == "null" for row in variants):
        return None
    selected = next((row for row in variants if row.get("type") != "null"), schema)
    if selected.get("type") == "array":
        value = json.loads(text)
        if not isinstance(value, list):
            raise ValueError("Eine JSON-Liste ist erforderlich.")
        items = selected.get("items", {})
        if any(row.get("type") == "number" for row in items.get("anyOf", [items])):
            value = [item.replace(",", ".") if isinstance(item, str) else item for item in value]
        return value
    if selected.get("type") == "boolean":
        if text.lower() not in ("true", "false", "an", "aus"):
            raise ValueError("Wähle true oder false.")
        return text.lower() in ("true", "an")
    if "enum" in selected:
        return text.strip()
    return text.strip().replace(",", ".")


def system_catalog(allowed=None):
    return "\n".join(f"`/{name}{(' ' + hint) if hint else ''}` — {description}"
                     for name, (description, hint) in COMMANDS.items()
                     if allowed is None or name in allowed or name.replace("_", "-") in allowed)
