"""One private native Telegram panel over the existing authenticated Aqla controls."""
from __future__ import annotations

import asyncio
import copy
import json
import secrets
from decimal import Decimal
from types import SimpleNamespace

from aqla_hyperliquid_mcp.models import AqlaError, require_first

from .messages import number
from .pipelines import AccountLimits, Parameters, Pipeline, TradingProfile
from .ranking import RankingPolicy, active_sources

PREFIX = "aqla-menu:"
HOME = [("📊 Dashboard", "dashboard"), ("📍 Positionen", "positions"), ("📒 Journal & Berichte", "journal"),
        ("⚙️ Profile & Handelsregeln", "profiles"), ("📡 Quellen & Routing", "sources"),
        ("🔐 Konten & Freigaben", "accounts"), ("🛠 System & KI", "system"), ("💬 Hermes & Hilfe", "help")]
GROUPS = {
    "Risiko & Hebel": ("sizing_mode", "risk_percent", "fixed_notional_quote", "max_notional_quote", "leverage_mode", "leverage", "margin_mode", "cost_reserve_bps"),
    "Entries & Ausführung": ("entry_mode", "entry_range_choice", "entry_execution", "take_profit_execution", "max_slippage_percent", "max_signal_age_seconds"),
    "Stop & Take Profit": ("take_profit_allocation", "take_profit_weights", "break_even_after_tp", "stop_management", "allowed_updates", "allow_stop_widening"),
    "Backup": ("fallback_stop_loss_percent", "fallback_stop_loss_unit", "fallback_take_profit_percentages", "fallback_take_profit_unit"),
    "Waverider": ("management_mode", "waverider_flex_percent", "waverider_until_tp", "waverider_lifetime"),
}
LABELS = {
    "sizing_mode": "Positionsgröße bestimmen", "risk_percent": "Risiko pro Trade (%)", "fixed_notional_quote": "Fester Positionswert",
    "max_notional_quote": "Maximaler Positionswert", "leverage_mode": "Hebelvorgabe", "leverage": "Hebel (×)", "margin_mode": "Margin-Modus",
    "cost_reserve_bps": "Kostenreserve (Basispunkte)", "entry_mode": "Fehlender Entry", "entry_range_choice": "Entry-Zone / Split",
    "entry_execution": "Entry-Ausführung", "take_profit_execution": "TP-Ausführung", "max_slippage_percent": "Maximale Slippage (%)",
    "max_signal_age_seconds": "Signalfrist (Sekunden)", "take_profit_allocation": "TP-Verteilung", "take_profit_weights": "TP-Gewichte",
    "break_even_after_tp": "Break-even nach TP", "stop_management": "Stop-Verwaltung", "allowed_updates": "Erlaubte Quellen-Updates",
    "allow_stop_widening": "Stop-Ausweitung erlauben", "fallback_stop_loss_percent": "Backup-SL (%)", "fallback_stop_loss_unit": "Backup-SL-Einheit",
    "fallback_take_profit_percentages": "Backup-TPs (%)", "fallback_take_profit_unit": "Backup-TP-Einheit", "management_mode": "Verwaltungsmodus",
    "waverider_flex_percent": "Waverider-Flexanteil (%)", "waverider_until_tp": "Waverider-Endziel", "waverider_lifetime": "Waverider-Laufzeit",
    "model": "KI-Modell", "reasoning": "Reasoning-Stufe", "caveman": "Caveman", "express": "Express-Erkennung",
    "source_processing": "Quellenverarbeitung", "replay_enabled": "Replay-Aufzeichnung", "replay_max_bytes": "Replay-Budget (Bytes)",
    "replay_retention_days": "Replay-Aufbewahrung (Tage)", "max_open_trades": "Maximale offene Trades", "max_total_risk_percent": "Gesamtrisiko (%)",
}


def input_value(text, schema):
    from .native_commands import parse_value
    selected = next((item for item in schema.get("anyOf", [schema]) if item.get("type") != "null"), schema)
    if selected.get("type") == "array" and not text.strip().startswith("["):
        text = json.dumps([part.strip() for part in text.split(";") if part.strip()], ensure_ascii=False)
    if selected.get("type") == "integer" and text.strip().lower() not in ("aus", "null") and not any(
            text.strip() == variant.get("const") for variant in schema.get("anyOf", [])):
        return int(text.strip())
    if any(item.get("type") == "integer" for item in schema.get("anyOf", [])) and text.strip().isdigit():
        return int(text.strip())
    return parse_value(text, schema)


def setting_value(value):
    if value is None:
        return "aus"
    if isinstance(value, bool):
        return "an" if value else "aus"
    if isinstance(value, list):
        return "; ".join(setting_value(item) for item in value) or "keine"
    return number(value) if isinstance(value, Decimal) else str(value)


class TelegramMenu:
    def __init__(self, commands):
        self.commands, self.extension = commands, commands.extension
        self.message_id = None
        self.fresh = False
        self.tokens, self.pending = {}, None
        self.recovered = False
        self.serial = asyncio.Lock()
        self.network, self.period, self.currency = "mainnet", "today", "USDC"
        self.receipt_file = self.extension.config_path.parent / "data" / "telegram-menu.json"
        try:
            value = json.loads(self.receipt_file.read_text(encoding="utf-8"))
            if value["owner"] == self.extension.owner:
                self.message_id = value["message_id"]
                self.recovered = True
        except (OSError, ValueError, KeyError):
            pass

    def button(self, label, action, *args):
        token = PREFIX + secrets.token_urlsafe(18)
        self.tokens[token] = (action, args)
        return {"text": str(label)[:64], "callback_data": token}

    def live(self):
        return self.message_id is not None

    async def rpc(self, tool, **args):
        return await self.extension.mcp_call(self.extension.server, tool, args)

    async def config(self):
        return await self.rpc("status", view="configuration", context_auth=self.commands.auth())

    async def display(self, bot, text, buttons=(), *, raw=False, page=0):
        self.tokens.clear()
        self.recovered = False
        rows = []
        if raw:
            rows = [[{"text": label[:64], "callback_data": token}] for label, token in buttons]
        else:
            rows = [[self.button(label, action, *args)] for label, action, *args in buttons]
        from .delivery import text_chunks
        pages = text_chunks(text, maximum=3200)
        page = max(0, min(page, len(pages) - 1))
        if len(pages) > 1:
            navigation = [self.button(label, "text_page", text, buttons, raw, value) for label, value in (("← Text", page - 1), ("Text →", page + 1)) if 0 <= value < len(pages)]
            rows.append(navigation)
            text = pages[page] + f"\n\nSeite {page + 1}/{len(pages)}"
        rows.append([self.button("⌂ Hauptmenü", "home"), self.button("Abbrechen", "cancel")])
        payload = {"chat_id": self.extension.owner, "text": text, "parse_mode": None, "reply_markup": {"inline_keyboard": rows}}
        previous = self.message_id
        if self.message_id is not None and not self.fresh:
            try:
                await asyncio.wait_for(bot.edit_message_text(message_id=self.message_id, **payload), 10)
                return
            except Exception as error:
                if "message is not modified" in str(error).lower():
                    return
                if not any(code in str(error).lower() for code in ("message to edit not found", "message can't be edited", "message_id_invalid")):
                    raise AqlaError("Telegram-Menü vorübergehend nicht aktualisierbar") from None
        sent = await asyncio.wait_for(bot.send_message(**payload), 10)
        self.message_id = sent.message_id
        self.fresh = False
        self.receipt_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"owner": self.extension.owner, "message_id": self.message_id}), encoding="utf-8")
        temporary.replace(self.receipt_file)
        if previous is not None and previous != self.message_id:
            try:
                await asyncio.wait_for(bot.delete_message(chat_id=self.extension.owner, message_id=previous), 10)
            except Exception:
                pass  # The new receipt already invalidates the old panel if Telegram cannot delete it.

    async def paged(self, bot, title, rows, action, args=(), page=0):
        page = max(0, min(page, max(0, (len(rows) - 1) // 8)))
        buttons = [(label, action, *args, value) for label, value in rows[page * 8:page * 8 + 8]]
        for label, target in (("← Zurückblättern", page - 1), ("Weiter →", page + 1)):
            if 0 <= target * 8 < len(rows):
                buttons.append((label, "page", title, rows, action, args, target))
        await self.display(bot, title + ("\nKeine Einträge." if not rows else ""), buttons)

    async def home(self, bot):
        self.pending = None
        self.commands.cancel()
        await self.display(bot, "Aqla · Steuerzentrale\nHandel, Journal und Einstellungen an einem Ort.",
                           [(label, action) for label, action in HOME])

    async def dashboard(self, bot, journal=False, offset=0):
        data = await self.rpc("dashboard", network=self.network, period=self.period, currency=self.currency, offset=offset)
        net = data["net_pnl"]
        lines = [f"{'Journal' if journal else 'Dashboard'} · {self.network.capitalize()} · {self.currency} · {data['resolved_period']}",
                 "Neue Entries: " + ("pausiert" if data["paused"] else "aktiv"),
                 "Netto belegt: " + number(net["value"], places=6) + f" {self.currency} · {net['known']}/{net['total']} abgeschlossen",
                 "Gebühren: " + number(data["fees"]["value"], places=6) + " · Funding: " + number(data["funding"]["value"], places=6),
                 f"Eigene offene Positionen: {data['open_trades']}"]
        for key, value in data.get("account_recovery", {}).items():
            if isinstance(value, dict):
                from .messages import _berlin_time
                verified = _berlin_time(value["last_success_at"]) + " Berlin" if value.get("last_success_at") else "unbekannt"
                lines.append(f"{key}: {value.get('category', 'unbekannt')} · letzter Erfolg {verified}")
        queue = data["source_queue"]
        lines.append(f"Quellen: {queue.get('ready_total', 0)} bereit · {queue.get('blocked_total', 0)} blockiert · {queue.get('recovery_total', 0)} Recovery")
        lines.append("Daten: zuletzt abgeglichene lokale Belege; unbekannte Werte sind nicht null.")
        buttons = [("Netzwerk: " + self.network, "network", journal), ("Zeitraum: " + self.period, "period", journal),
                   ("Währung: " + self.currency, "currency", journal), ("Aktualisieren", "journal" if journal else "dashboard")]
        if journal:
            buttons += [(row["symbol"] + " · " + row["side"].upper() + " · " + row["status"], "trade", row["trade_id"]) for row in data["journal"]]
            if offset:
                buttons.append(("← Vorherige", "journal", max(0, offset - 8)))
            if data.get("next_offset") is not None:
                buttons.append(("Weitere →", "journal", data["next_offset"]))
            buttons += [("Weitere Filter", "filters"), ("Monatsberichte", "months"), ("Verlauf & Ablehnungen", "activity", 0)]
        await self.display(bot, "\n".join(lines), buttons)

    async def positions(self, bot):
        data = await self.config()
        rows = [(row["plan"]["market"]["symbol"] + " " + row["plan"]["side"].upper() + " · " + row["plan"]["network"] + " · " + row["status"], row["trade_id"]) for row in data["trades"]]
        await self.paged(bot, "Eigene aktive Trades · Details aus den letzten bestätigten Belegen", rows, "trade")

    async def trade(self, bot, trade_id):
        # The established diagnostics own source, fill and protection interpretation.
        snapshot = await self.config()
        current = next((row for row in snapshot["trades"] if row["trade_id"] == trade_id), None)
        if current:
            from .messages import trade_block
            text = trade_block({**snapshot, "trades": {trade_id: current}}, current, [], detailed=True).replace("**", "")
            text += "\n\nEigene Menge: " + number(current.get("remaining_quantity"))
            text += "\nWaverider: " + ("pausiert" if current.get("waverider_paused") else str(current.get("waverider", {}).get("phase", "nicht aktiv")))
            from .operator_management import fingerprint
            base = fingerprint(current)
            actions = [("Position schließen", "manage", trade_id, base, "close"), ("Teil reduzieren", "manage", trade_id, base, "reduce"),
                       ("Stop anpassen", "manage", trade_id, base, "move_stop"), ("Stop auf Einstand", "manage", trade_id, base, "break_even"),
                       ("TPs anpassen", "manage", trade_id, base, "replace_targets"), ("Offene Entries stornieren", "manage", trade_id, base, "cancel_entry")]
            if current["plan"]["policy"].get("management_mode") == "waverider" and not current.get("waverider", {}).get("finished"):
                action = "resume_waverider" if current.get("waverider_paused") else "pause_waverider"
                actions.append(("Waverider fortsetzen" if current.get("waverider_paused") else "Waverider pausieren", "manage", trade_id, base, action))
        else:
            # No local state writer is opened in the gateway; fetch via the existing MCP.
            journal = await self.rpc("journal_read", table="Trades", filters={"ID": trade_id}, limit=1)
            row = journal["rows"][0] if journal["rows"] else {}
            accounting = row.get("accounting", {})
            text = "Historischer Trade · " + trade_id + "\n" + str(row.get("Symbol", "—")) + " · " + str(row.get("Status", "—"))
            text += "\nNetto: " + number(accounting.get("net_pnl"), places=6) + "\nGebühren: " + number(accounting.get("fees"), places=6) + "\nFunding: " + number(accounting.get("funding"), places=6)
            text += "\nAbrechnung: " + ("vollständig" if accounting.get("complete") else "; ".join(accounting.get("issues", ["unvollständig"])))
            actions = []
        actions += [("Orders & Fills", "trade_evidence", trade_id), ("Aktualisieren", "trade", trade_id)]
        await self.display(bot, text, actions)

    async def profiles(self, bot):
        data = await self.config()
        await self.paged(bot, "Profile · Änderungen wirken auf neue Planungen", [(row["name"], key) for key, row in data["profiles"].items()] + [("Neues Profil", "__new__")], "profile")

    async def profile(self, bot, identity):
        if identity == "__new__":
            return await self.run(bot, "prompt", "create_profile", "")
        data = await self.config()
        row = data["profiles"][identity]
        if row.get("parameters") is None:
            defaults = Parameters()
            text = row["name"] + " · noch nicht eingerichtet\nDieses Ausgangsprofil gilt für neue Planungen. Kontolimits und bestehende Freigaben bleiben verbindlich.\n\n"
            text += "\n".join(LABELS[name] + ": " + setting_value(getattr(defaults, name)) for name in Parameters.model_fields)
            return await self.display(bot, text, [("Dieses Ausgangsprofil übernehmen", "initialize_profile", identity, data["configuration_revision"])])
        await self.display(bot, row["name"] + " · Profil " + identity + "\nBestehende Trades behalten ihre gespeicherten Pläne.",
            [(name, "profile_group", identity, name) for name in GROUPS] + [("Profil umbenennen", "prompt", "rename_profile", identity), ("Profil löschen", "confirm_delete", "profile", identity)])

    async def profile_group(self, bot, identity, group):
        data = await self.config()
        parameters = Parameters.model_validate(data["profiles"][identity]["parameters"])
        rows = [(LABELS[name] + ": " + setting_value(getattr(parameters, name)), name) for name in GROUPS[group]]
        title = data["profiles"][identity]["name"] + " · " + group
        if group == "Waverider":
            permissions = data.get("waverider_permissions", {}).get(identity, {})
            title += "\nUSDC-Livefreigabe: " + "; ".join(key + ": " + (value or "fehlt / nur Beobachtung") for key, value in permissions.items())
            title += "\nProfilwahl ist keine neue historische Qualifikation."
        await self.paged(bot, title, rows, "profile_field", (identity,))

    async def field(self, bot, kind, identity, field):
        from .native_commands import parameter_unit
        data = await self.config()
        models = {"profile": Parameters, "ranking": RankingPolicy}
        if kind == "profile":
            values = Parameters.model_validate(data["profiles"][identity]["parameters"]).model_dump(mode="json")
            current = getattr(Parameters.model_validate(values), field)
            revision = data["configuration_revision"]
            schema = models[kind].model_json_schema()["properties"][field]
            unit = parameter_unit(field, values)
        elif kind == "ranking":
            field, _, subfield = field.partition(".")
            values, revision = data["ranking_policy"], data["ranking_revision"]
            current = values[field][subfield] if subfield else values[field]
            schema = RankingPolicy.model_json_schema()["properties"][field]
            if subfield:
                schema = schema["additionalProperties"]
            field += "." + subfield if subfield else ""
            unit = "Wert"
        elif kind == "limits":
            values, revision = data["execution_accounts"][identity]["limits"], data["configuration_revision"]
            if values is None:
                return await self.run(bot, "prompt", "initial_limits", identity)
            current = values[field]
            schema, unit = AccountLimits.model_json_schema()["properties"][field], "Trades" if field == "max_open_trades" else "%"
        else:
            from .runtime_controls import RuntimeControls, TechnicalSettings
            runtime = RuntimeControls(self.extension).read()
            current, revision = runtime["values"][field], runtime["revision"]
            schema, unit = TechnicalSettings.model_json_schema()["properties"][field], "Wert"
        self.pending = {"kind": kind, "identity": identity, "field": field, "schema": schema, "revision": revision}
        selected = next((row for row in schema.get("anyOf", [schema]) if row.get("type") != "null"), schema)
        options = selected.get("enum")
        if selected.get("type") == "boolean":
            options = [True, False]
        if kind == "technical" and field in ("express", "source_processing"):
            options = ["off", "shadow", "live"] if field == "express" else ["legacy", "shadow", "hybrid"]
        buttons = [(str(value), "input", value) for value in options] if options is not None else []
        text = LABELS.get(field, field) + f"\nAktuell: {setting_value(current)}\nEinheit: {unit}\nMit aktiver Setup-Freigabe wird ein gültiger neuer Wert sofort gespeichert."
        if not buttons:
            text += "\nNeuen Wert senden. Listen: Werte mit ; trennen. null = aus, falls zulässig."
        await self.display(bot, text, buttons)

    async def commit_field(self, bot, value):
        pending = self.pending
        if not pending or not self.live():
            raise AqlaError("Eingabe ist abgelaufen")
        self.commands.auth(protected=True)
        kind, identity, field = (pending[key] for key in ("kind", "identity", "field"))
        data = await self.config()
        suffix = ""
        if kind == "profile":
            row = data["profiles"][identity]
            profile = TradingProfile(id=row["id"], name=row["name"], parameters=Parameters.model_validate({**row["parameters"], field: value}))
            await self.commands.control(action="set_profile", profile=profile.model_dump(mode="json"), expected_revision=pending["revision"])
        elif kind == "ranking":
            values = copy.deepcopy(data["ranking_policy"])
            name, _, subfield = field.partition(".")
            if subfield:
                values[name][subfield] = value
            else:
                values[name] = value
            policy = RankingPolicy.model_validate(values)
            await self.commands.control(action="set_ranking_policy", ranking_policy=policy.model_dump(mode="json"), expected_revision=pending["revision"])
        elif kind == "limits":
            limits = AccountLimits.model_validate({**data["execution_accounts"][identity]["limits"], field: value})
            await self.commands.control(action="set_account_limits", account_key=identity, limits=limits.model_dump(mode="json"), expected_revision=pending["revision"])
        else:
            from .runtime_controls import RuntimeControls
            result = await RuntimeControls(self.extension).change(field, value, pending["revision"])
            suffix = "\nÜbernahme ausstehend; der native Dienst prüft die gespeicherte Einstellung erneut." if result["apply_status"] == "pending" else ""
        self.pending = None
        await self.display(bot, "Gespeichert · " + LABELS.get(field, field) + ": " + setting_value(value) + (suffix if kind == "technical" else ""),
            [("Weitere Einstellungen", "profile_group", identity, require_first(group for group, fields in GROUPS.items() if field in fields))] if kind == "profile" else [("Zur Übersicht", "system" if kind == "technical" else "accounts" if kind == "limits" else "ranking")])

    async def run(self, bot, action, *args):
        if action in ("home", "cancel"):
            if self.extension.setup.challenge is not None:
                await self.extension.setup.revoke()
            return await self.home(bot)
        if action == "dashboard":
            return await self.dashboard(bot)
        if action == "journal":
            return await self.dashboard(bot, True, args[0] if args else 0)
        if action in ("positions", "profiles"):
            return await getattr(self, action)(bot)
        if action in ("trade", "profile", "profile_group"):
            return await getattr(self, action)(bot, *args)
        if action == "page":
            return await self.paged(bot, *args)
        if action == "text_page":
            return await self.display(bot, args[0], args[1], raw=args[2], page=args[3])
        if action == "profile_field":
            return await self.field(bot, "profile", *args)
        if action == "initialize_profile":
            data = await self.config()
            row = data["profiles"][args[0]]
            if row.get("parameters") is not None:
                raise AqlaError("Profil ist inzwischen eingerichtet; aktuelle Werte verwenden")
            profile = TradingProfile(id=args[0], name=row["name"], parameters=Parameters())
            await self.commands.control(action="set_profile", profile=profile.model_dump(mode="json"), expected_revision=args[1])
            return await self.profile(bot, args[0])
        if action == "field":
            return await self.field(bot, *args)
        if action == "input":
            return await self.commit_field(bot, args[0])
        if action == "network":
            self.network = "testnet" if self.network == "mainnet" else "mainnet"
            return await self.dashboard(bot, args[0])
        if action in ("period", "currency"):
            data = await self.rpc("dashboard", network=self.network, period=self.period, currency=self.currency)
            rows = [(label, label) for label in data["available_periods"] if action == "period"] if action == "period" else [(label, label) for label in data["available_currencies"]]
            return await self.paged(bot, "Zeitraum wählen" if action == "period" else "Währung wählen", rows, "select_scope", (action, args[0]))
        if action == "select_scope":
            setattr(self, args[0], args[2])
            return await self.dashboard(bot, args[1])
        if action == "sources":
            data = await self.config()
            lines = [data.get("source_names", {}).get(key, key) + " · " + data.get("source_ranks", {}).get(key, {}).get("tier", "F") for key in sorted(active_sources(data))]
            return await self.display(bot, "Quellen & Routing\n" + "\n".join(lines), [("Kanäle & Nachweise", "source_list"), ("Kanalrang ändern", "command", "aqla_rang"),
                ("Quelle ergänzen", "prompt", "add_source", ""), ("Quelle entfernen", "prompt", "remove_source", ""),
                ("Quelle benennen", "prompt", "rename_source", ""), ("Ranking-Regeln", "ranking"), ("Routing", "pipelines")])
        if action == "source_list":
            data = await self.config()
            return await self.paged(bot, "Aktuelle Quellen · Rang, Metriken und Ausschlüsse", [(data.get("source_names", {}).get(key, key), key) for key in sorted(active_sources(data))], "source_details")
        if action == "source_details":
            result = await self.rpc("ranking_status", source=args[0], history_limit=8)
            if not result["sources"]:
                return await self.display(bot, "Quelle ist nicht mehr aktiv.\nHistorische Rangdaten und Handelsbelege bleiben erhalten; keine Bereinigung oder erneute Aufnahme nötig.", [("Aktuelle Quellen", "source_list")])
            text = []
            for key, row in result["sources"].items():
                text += [row.get("name", key) + " · " + row["tier"], "Belege: " + str(row.get("proof_total", 0)),
                         "Metriken: " + str(row.get("metrics", {})), "Nachweise: " + str(row.get("evidence", [])),
                         "Ausschlüsse: " + str(row.get("exclusions", []))]
            text.append("Letzte Rangwechsel: " + str(result.get("history", [])))
            return await self.display(bot, "\n\n".join(text), [("Rang ändern", "source_tier", args[0]), ("Quellen", "source_list")])
        if action == "source_tier":
            return await self.commands.start(bot, "aqla_rang", json.dumps(args[0]) + " ")
        if action == "ranking":
            data = await self.config()
            rows = []
            for field, value in data["ranking_policy"].items():
                rows += [(field + "." + key + ": " + str(item), field + "." + key) for key, item in value.items()] if isinstance(value, dict) else [(field + ": " + str(value), field)]
            return await self.paged(bot, "Ranking · vollständige validierte Rangregeln", rows, "ranking_field")
        if action == "ranking_field":
            return await self.field(bot, "ranking", "", args[0])
        if action == "accounts":
            data = await self.config()
            lines = [key + " · " + ("aktiv" if row.get("enabled") else "gesperrt") + " · max. " + str((row.get("limits") or {}).get("max_open_trades", "—")) + " Trades · Gesamtrisiko " + number((row.get("limits") or {}).get("max_total_risk_percent")) + " %" for key, row in data["execution_accounts"].items()]
            return await self.display(bot, "Konten & Freigaben\n" + "\n".join(lines), [("Entries sofort pausieren", "pause"), ("Entries fortsetzen", "resume"),
                ("Kontolimits", "limits_selection"), ("Konto aktivieren/sperren", "account_selection"), ("Eigentumskonflikte", "conflicts"),
                ("Setup-Freigabe", "setup"), ("Profile anlegen", "prompt", "create_profile", "")])
        if action in ("pause", "resume"):
            await self.commands.control(action=action)
            return await self.display(bot, "Neue Entries pausiert. Positionsschutz läuft weiter." if action == "pause" else "Neue Entries freigegeben.", [("Konten", "accounts")])
        if action == "account_selection":
            data = await self.config()
            return await self.paged(bot, "Konto wählen", [(key, key) for key in data["execution_accounts"]], "account")
        if action == "limits_selection":
            data = await self.config()
            return await self.paged(bot, "Limits je Ausführungskonto", [(key, key) for key, row in data["execution_accounts"].items() if row.get("configured")], "limit_fields")
        if action == "limit_fields":
            data = await self.config()
            if not data["execution_accounts"][args[0]].get("limits"):
                return await self.run(bot, "prompt", "initial_limits", args[0])
            return await self.display(bot, args[0], [(LABELS[field], "field", "limits", args[0], field) for field in AccountLimits.model_fields])
        if action == "account":
            data = await self.config()
            row = data["execution_accounts"][args[0]]
            return await self.display(bot, args[0] + " · " + ("aktiv" if row.get("enabled") else "gesperrt"), [("Sperren" if row.get("enabled") else "Aktivieren", "account_apply", args[0], not row.get("enabled"), data["configuration_revision"])])
        if action == "account_apply":
            await self.commands.control(action="set_account_enabled", account_key=args[0], enabled=args[1], expected_revision=args[2])
            return await self.run(bot, "accounts")
        if action == "conflicts":
            data = await self.config()
            return await self.paged(bot, "Eigentumskonflikte · ausschließlich belegte eigene Abschlüsse", [
                (row["plan"]["market"]["symbol"] + " · " + row["plan"]["network"], row["trade_id"]) for row in data["trades"] if row["status"] == "conflicted"], "conflict")
        if action == "conflict":
            review = await self.rpc("conflict_review", trade_id=args[0], context_auth=self.commands.auth())
            text = "Eigentumsprüfung · " + args[0][:8] + "\nEigene Position: " + str(review["position_size"]) + "\n" + "\n".join(review["reasons"])
            text += "\nEs werden keine fremden Positionen oder Orders übernommen. PnL bleibt bei fehlenden Belegen unvollständig."
            buttons = [("Belegten eigenen Abschluss bestätigen", "resolve_conflict", args[0], review["fingerprint"])] if review["resolvable"] else []
            return await self.display(bot, text, buttons)
        if action == "resolve_conflict":
            result = await self.commands.control(action="resolve_conflict", trade_id=args[0], review_fingerprint=args[1])
            return await self.display(bot, "Eigener Abschluss bestätigt." if result["resolved"] else "Belege haben sich geändert; erneut prüfen.", [("Konflikte", "conflicts")])
        if action == "system":
            from .runtime_controls import RuntimeControls
            technical = RuntimeControls(self.extension).read()
            values = technical["values"]
            from . import __version__
            lines = ["System & KI · Aqla " + __version__, "Freigaben und Grenzen werden bei jeder Wirkung erneut geprüft."]
            lines += [LABELS[key] + ": " + setting_value(value) for key, value in values.items()]
            lines.append("Technische Übernahme: " + technical["apply_status"])
            return await self.display(bot, "\n".join(lines), [("Technische Einstellungen", "technical"), ("Verlauf & Störungen", "activity", 0), ("Meldungsregeln", "notification_info"),
                ("Erschöpfte Zustellungen erneut prüfen", "retry_delivery"), ("Aktualisieren", "system")])
        if action == "retry_delivery":
            self.commands.auth()
            await self.extension.source_worker.bind()
            result = await self.extension.source_worker.internal("delivery_retry")
            return await self.display(bot, "Erneut vorgemerkt: " + str(result["requeued"]) + "\nBei früher verlorener Sendeantwort kann die Meldung bereits angekommen sein.")
        if action == "technical":
            from .runtime_controls import TechnicalSettings
            return await self.paged(bot, "Technische Einstellung wählen", [(LABELS[key], key) for key in TechnicalSettings.model_fields], "technical_field")
        if action == "technical_field":
            return await self.field(bot, "technical", "", args[0])
        if action == "notification_info":
            return await self.display(bot, "Automatische Meldungen\nEinstieg, tatsächliche TP-/Teilabschlüsse, vollständige Waverider-Wellen, Schließung und relevante Störungen.\nReine Lesestörungen: ab 120 Sekunden. Schutz-/Ausführungsprobleme sofort.\nRoutine, Ablehnungen und Beobachtungen: im Verlauf.\nMonatsbericht: Monatserster 09:00 Berlin.")
        if action == "activity":
            data = await self.extension.setup_request("activity", offset=args[0], limit=8)
            rows = [str(row.get("disposition", "—")) + " · " + str(row.get("reason") or row.get("notice", {}).get("error") or row.get("notice", {}).get("change") or row.get("notice", {}).get("source_notice") or row.get("notice", {}).get("recovery_reports") or "Handelsereignis")[:230] for row in data["rows"]]
            buttons = [("Weitere →", "activity", data["next_offset"])] if data.get("next_offset") is not None else []
            return await self.display(bot, "Verlauf · " + str(data["total"]) + " Ereignisse\n\n" + "\n\n".join(rows), buttons)
        if action == "setup":
            # The original private password ingress owns deletion, verification and lease issuance.
            update = SimpleNamespace(effective_message=SimpleNamespace(text="/setup", from_user=SimpleNamespace(id=self.extension.owner, is_bot=False),
                chat=SimpleNamespace(id=self.extension.owner, type="private")), edited_message=None, edited_channel_post=None)
            await self.extension.setup.intercept(update, bot)
            text = ("Die private Passwortnachfrage ist geöffnet. Passwort ausschließlich dort senden."
                    if self.extension.setup.challenge is not None else "Beachte die private Setup-Antwort im Chat.")
            return await self.display(bot, "Setup-Freigabe\n" + text + "\nAbbrechen oder Hauptmenü beendet eine offene Nachfrage.")
        if action == "command":
            command = args[0]
            if command in ("aqla_rang", "aqla_limits", "aqla_parameter", "aqla_maker"):
                if command == "aqla_limits":
                    return await self.run(bot, "limits_selection")
                return await self.commands.start(bot, command, "")
            return await self.commands.ownership(bot, command, "")
        if action == "help":
            return await self.display(bot, "Aqla + Hermes\nMenü und Einstellungen laufen direkt in Python. Für Erklärungen kannst du normal schreiben.\n/aqla öffnet dieses Menü; /aqla_pause pausiert neue Entries sofort.\nSetup: private Freigabe für geschützte Änderungen.\nVorhandene zugelassene Hermes-Befehle: /help und /commands.", [("Setup", "setup"), ("Befehle anzeigen", "help_commands")])
        if action == "help_commands":
            from .native_commands import system_catalog
            return await self.display(bot, system_catalog().replace("`", ""))
        if action == "months":
            data = await self.rpc("dashboard", network=self.network, period="all", currency=self.currency)
            rows = [(value, value) for value in data["available_periods"] if value not in ("all", "current", "today", "7d", "30d")]
            return await self.paged(bot, "Monatsberichte", rows, "month")
        if action == "month":
            data = await self.rpc("monthly_report", month=args[0], limit=8)
            summary = data["summary"]
            text = "Monatsbericht " + args[0] + " · Mainnet\n" + f"Abgeschlossen: {summary['trades']} · vollständig: {summary['complete']} · ausgeschlossen: {summary['excluded']}\n"
            text += "Netto je Währung: " + ("; ".join(number(value, places=6) + " " + currency for currency, value in sorted(summary["net_by_currency"].items())) or "nicht bestätigt")
            text += "\nTrefferquote: " + number(summary["win_rate_percent"], places=2) + " %\n" + summary["roe_sum_label"] + ": " + number(summary["roe_sum_percent"], places=2) + " %"
            return await self.display(bot, text, [("Trades im Journal", "select_scope", "period", True, args[0])])
        if action == "trade_evidence":
            self.pending = None
            return await self.paged(bot, "Belege wählen", [(label, label) for label in ("Orders", "Fills", "Vorgänge")], "evidence_table", (args[0],))
        if action == "evidence_table":
            trade_id, table = args[:2]
            offset = args[2] if len(args) > 2 else 0
            data = await self.rpc("journal_read", table=table, trade_id=trade_id, offset=offset, limit=8)
            rows = [str(row.get("Art", "Vorgang")) + " · " + str(row.get("Order-ID", row.get("ID"))) + " · Menge " + str(row.get("Menge", "—")) + " · Preis " + str(row.get("Preis", row.get("Preis/Ziel", "—"))) for row in data["rows"]]
            if table == "Orders":
                rows = [text + " · Status: " + str(row.get("Status Börse") or "nicht bestätigt") + (" · ersetzt" if row.get("Ersetzt") else "") for text, row in zip(rows, data["rows"], strict=True)]
            elif table == "Vorgänge":
                rows = [text + " · Phase: " + str(row.get("Phase") or "nicht bestätigt") for text, row in zip(rows, data["rows"], strict=True)]
            buttons = [("Weitere →", "evidence_table", trade_id, table, data["next_offset"])] if data.get("next_offset") is not None else []
            if offset:
                buttons.append(("← Vorherige", "evidence_table", trade_id, table, max(0, offset - 8)))
            return await self.display(bot, table + " · " + trade_id[:8] + "\n\n" + "\n".join(rows), buttons)
        if action == "filters":
            return await self.display(bot, "Journal filtern", [("Nach Symbol", "prompt", "filter_symbol", ""), ("Nach Quelle", "prompt", "filter_source", ""), ("Nach Status", "prompt", "filter_status", "")])
        if action == "filtered":
            column, value, offset = args
            result = await self.rpc("journal_read", table="Trades", filters={column: value}, account_key="hyperliquid:" + self.network, offset=offset, limit=8)
            buttons = [(row["Symbol"] + " · " + row["Status"], "trade", row["ID"]) for row in result["rows"]]
            if result.get("next_offset") is not None:
                buttons.append(("Weitere →", "filtered", column, value, result["next_offset"]))
            if offset:
                buttons.append(("← Vorherige", "filtered", column, value, max(0, offset - 8)))
            return await self.display(bot, "Journal · " + column + ": " + value + "\n" + str(result["total"]) + " Treffer", buttons)
        if action == "pipelines":
            data = await self.config()
            return await self.paged(bot, "Quellen → Profil · Routing", [(row["name"], key) for key, row in data["pipelines"].items()] + [("Routing anlegen", "__new__")], "pipeline")
        if action == "pipeline":
            if args[0] == "__new__":
                return await self.run(bot, "prompt", "create_pipeline", "")
            data = await self.config()
            row = data["pipelines"][args[0]]
            return await self.display(bot, row["name"] + "\nQuellen: " + "; ".join(row["sources"]) + "\nProfil: " + row["targets"][0]["profile_id"] + "\nAktiv: " + str(row["enabled"]),
                [("Aktiv umschalten", "pipeline_toggle", row["id"], data["configuration_revision"]), ("Profil ändern", "pipeline_profiles", row["id"]),
                 ("Quellen ändern", "prompt", "pipeline_sources", row["id"]), ("Umbenennen", "prompt", "pipeline_name", row["id"]), ("Löschen", "confirm_delete", "pipeline", row["id"])])
        if action == "pipeline_profiles":
            data = await self.config()
            return await self.paged(bot, "Profil für Routing", [(row["name"], key) for key, row in data["profiles"].items()], "pipeline_profile", (args[0], data["configuration_revision"]))
        if action in ("pipeline_profile", "pipeline_toggle"):
            data = await self.config()
            row = copy.deepcopy(data["pipelines"][args[0]])
            if action == "pipeline_toggle":
                row["enabled"] = not row["enabled"]
            else:
                row["targets"] = [{"exchange": "hyperliquid", "profile_id": args[2]}]
            selected = Pipeline.model_validate({key: value for key, value in row.items() if key in Pipeline.model_fields})
            await self.commands.control(action="set_pipeline", pipeline=selected.model_dump(mode="json"), expected_revision=args[1])
            return await self.run(bot, "pipeline", args[0])
        if action == "confirm_delete":
            data = await self.config()
            return await self.display(bot, args[0] + " " + args[1] + " löschen? Laufende Trades behalten ihre Pläne.", [("Dieses " + args[0] + " löschen", "delete", *args, data["configuration_revision"])])
        if action == "delete":
            await self.commands.control(action="delete_" + args[0], identity=args[1], expected_revision=args[2])
            return await self.run(bot, "profiles" if args[0] == "profile" else "pipelines")
        if action == "manage":
            trade_id, base, requested = args
            self.pending = {"kind": "manage", "trade_id": trade_id, "fingerprint": base, "action": requested}
            if requested in ("reduce", "move_stop", "replace_targets"):
                return await self.display(bot, "Trade " + trade_id[:8] + " · " + requested + "\n" + {"reduce": "Prozent der aktuell angezeigten eigenen Menge senden.", "move_stop": "Neuen Stop-Preis senden.", "replace_targets": "Alle TP-Preise in ursprünglicher Anzahl senden; mit ; trennen."}[requested])
            return await self.display(bot, "Trade " + trade_id[:8] + " · " + requested + "\nNur die zugeordnete eigene Position wird verwaltet.", [("Jetzt " + requested + " ausführen", "manage_apply", None)])
        if action == "manage_apply":
            pending = self.pending
            payload = {"trade_id": pending["trade_id"], "expected_fingerprint": pending["fingerprint"], "action": pending["action"]}
            if args[0] is not None:
                payload[{"reduce": "percent", "move_stop": "stop", "replace_targets": "targets"}[pending["action"]]] = args[0]
            protected = pending["action"] not in ("close", "reduce", "cancel_entry", "pause_waverider")
            from .operator_management import ManagementRequest
            payload = ManagementRequest.model_validate(payload).model_dump(mode="json")
            context_auth = self.commands.auth(protected=protected)
            try:
                result = await self.rpc("operator_manage", request=payload, context_auth=context_auth)
            except AqlaError:
                self.pending = None
                return await self.display(bot, "Auftragsantwort nicht bestätigt. Es kann bereits ein Auftrag vorliegen; zuerst den aktuellen Positionsstatus prüfen.", [("Position prüfen", "trade", payload["trade_id"])])
            self.pending = None
            label = {"done": "verarbeitet", "pending": "angenommen; Abgleich ausstehend", "rejected": "abgelehnt", "superseded": "durch neuere Positionsdaten überholt"}.get(result["status"], result["status"])
            return await self.display(bot, "Verwaltungsauftrag: " + label + "\nEin ungeklärter Auftrag wird mit derselben Identität abgeglichen.", [("Position anzeigen", "trade", payload["trade_id"])])
        if action == "prompt":
            data = await self.config()
            if args[0] == "create_profile" and data["profiles"]["standard"].get("parameters") is None:
                self.pending = None
                return await self.profile(bot, "standard")
            if not args[0].startswith("filter_") and args[0] != "rename_source":
                self.commands.auth(protected=True)
            self.pending = {"kind": "prompt", "action": args[0], "identity": args[1], "revision": data["configuration_revision"]}
            prompts = {"create_profile": "Name des neuen Profils senden. Es übernimmt die aktuellen Standardwerte und wird keinem Routing automatisch zugewiesen.", "rename_profile": "Neuen Profilnamen senden.",
                "initial_limits": "Noch keine Kontolimits hinterlegt. Beide ausdrücklich setzen: maximale offene Trades; Gesamtrisiko in Prozent. Beispiel: 3; 2. Es werden keine Limits stillschweigend ausgewählt.",
                "add_source": "Telegram-ID oder @username ergänzen. Bestehende Quellen bleiben erhalten.", "remove_source": "Genau eine vorhandene Telegram-ID oder @username entfernen.",
                "rename_source": 'Telegram-ID und "neuen Namen" senden.', "create_pipeline": "Namen des neuen Routings senden. Es wird zunächst deaktiviert mit dem Standardprofil angelegt.",
                "pipeline_sources": "Quellen mit ; getrennt senden. * bedeutet alle ausgewählten Quellen.", "pipeline_name": "Neuen Routingnamen senden.",
                "filter_symbol": "Exaktes Symbol senden, beispielsweise ETH.", "filter_source": "Exakte Telegram-Quellen-ID senden.", "filter_status": "Status senden, beispielsweise closed, rejected oder open."}
            return await self.display(bot, prompts[args[0]])
        raise AqlaError("Menüfunktion ist nicht verfügbar")

    async def text_input(self, bot, text):
        pending = self.pending
        if not pending or not self.live():
            raise AqlaError("Eingabe ist abgelaufen")
        if pending["kind"] in ("profile", "ranking", "technical", "limits"):
            return await self.commit_field(bot, input_value(text, pending["schema"]))
        if pending["kind"] == "manage":
            value = [part.strip().replace(",", ".") for part in text.split(";")] if pending["action"] == "replace_targets" else text.strip().replace(",", ".")
            return await self.run(bot, "manage_apply", value)
        action, identity = pending["action"], pending["identity"]
        data = await self.config()
        revision = pending["revision"]
        if action == "initial_limits":
            parts = [part.strip().replace(",", ".") for part in text.split(";")]
            if len(parts) != 2:
                raise ValueError("Beide Werte senden: maximale offene Trades; Gesamtrisiko (%)")
            limits = AccountLimits(max_open_trades=int(parts[0]), max_total_risk_percent=parts[1])
            await self.commands.control(action="set_account_limits", account_key=identity, limits=limits.model_dump(mode="json"), expected_revision=revision)
            self.pending = None
            return await self.display(bot, "Kontolimits gespeichert. Der bestehende Aktivierungsstatus bleibt unverändert.", [("Konten", "accounts")])
        if action.startswith("filter_"):
            column = {"filter_symbol": "Symbol", "filter_source": "Quelle", "filter_status": "Status"}[action]
            self.pending = None
            return await self.run(bot, "filtered", column, text.strip(), 0)
        if action in ("add_source", "remove_source", "rename_source"):
            await self.commands.source_change(bot, {"add_source": "aqla_hinzufuegen", "remove_source": "aqla_entfernen", "rename_source": "aqla_kanalname"}[action], text.strip())
        elif action in ("create_profile", "rename_profile"):
            if action == "create_profile":
                if data["profiles"]["standard"].get("parameters") is None:
                    self.pending = None
                    return await self.profile(bot, "standard")
                profile = TradingProfile(id="p_" + secrets.token_hex(6), name=text.strip(), parameters=Parameters.model_validate(data["profiles"]["standard"]["parameters"]))
            else:
                row = data["profiles"][identity]
                profile = TradingProfile(id=identity, name=text.strip(), parameters=Parameters.model_validate(row["parameters"]))
            await self.commands.control(action="set_profile", profile=profile.model_dump(mode="json"), expected_revision=revision)
        else:
            if action == "create_pipeline":
                pipeline = Pipeline(id="r_" + secrets.token_hex(6), name=text.strip(), sources=["*"], targets=[{"exchange": "hyperliquid", "profile_id": "standard"}], enabled=False)
            else:
                row = data["pipelines"][identity]
                values = {key: value for key, value in row.items() if key in Pipeline.model_fields}
                values["sources" if action == "pipeline_sources" else "name"] = [part.strip() for part in text.split(";") if part.strip()] if action == "pipeline_sources" else text.strip()
                pipeline = Pipeline.model_validate(values)
            await self.commands.control(action="set_pipeline", pipeline=pipeline.model_dump(mode="json"), expected_revision=revision)
        self.pending = None
        await self.display(bot, "Gespeichert.", [("Zur Übersicht", "sources" if "source" in action or "pipeline" in action else "profiles")])

    async def intercept(self, update, bot):
        query = getattr(update, "callback_query", None)
        if query and str(getattr(query, "data", "")).startswith(PREFIX):
            user, message = query.from_user, query.message
            chat = getattr(message, "chat", None)
            if not (user and chat and not user.is_bot and str(user.id) == self.extension.owner and str(chat.id) == self.extension.owner and chat.type == "private"):
                await query.answer()
                return True
            selection = self.tokens.pop(query.data, None) if self.live() and message.message_id == self.message_id else None
            if selection is None:
                if self.recovered and message.message_id == self.message_id:
                    await query.answer()
                    async with self.serial:
                        await self.home(bot)
                    return True
                await query.answer(text="Diese Schaltfläche wurde ersetzt. Bitte das aktuelle Menü verwenden.")
                return True
            await query.answer()
            action, args = selection
            self.tokens.clear()
            if action not in ("input", "manage_apply", "text_page"):
                self.pending = None
                self.commands.cancel()
            try:
                if action == "pause":
                    await self.run(bot, action, *args)
                else:
                    async with self.serial:
                        await self.run(bot, action, *args)
            except ValueError as error:
                await self.display(bot, "Änderung nicht ausgeführt.\n" + str(error)[:600] + "\nFür geschützte Änderungen Setup öffnen.", [("Setup", "setup")])
            except Exception:
                self.pending = None
                await self.display(bot, "Menü vorübergehend nicht verfügbar. Ein möglicherweise angenommener Handelsauftrag wird mit derselben Identität abgeglichen.")
            return True
        message = getattr(update, "effective_message", None)
        text = getattr(message, "text", "") or ""
        head = text.split(maxsplit=1)[0] if text else ""
        command = head.split("@", 1)[0].lower()
        aliases = {"/aqla": ("home",), "/aqla_status": ("dashboard",), "/aqla_journal": ("journal",), "/aqla_profile": ("profiles",), "/aqla_kanaele": ("sources",), "/aqla_funktionen": ("help",)}
        if len(text.split(maxsplit=1)) == 1:
            aliases.update({"/aqla_limits": ("limits_selection",), "/aqla_parameter": ("profiles",),
                "/aqla_hinzufuegen": ("prompt", "add_source", ""), "/aqla_entfernen": ("prompt", "remove_source", ""),
                "/aqla_kanalname": ("prompt", "rename_source", ""), "/aqla_konflikt": ("conflicts",), "/aqla_konflikt_loesen": ("conflicts",)})
        from .native_setup import addressed_to_bot
        if not addressed_to_bot(head, bot):
            return command in aliases
        from .native_commands import COMMANDS
        if (text.startswith("/") and command.lstrip("/") in COMMANDS and self.extension.setup.private_owner(message)
                and not any(getattr(update, key, None) for key in ("edited_message", "edited_channel_post"))):
            self.fresh = True
        relevant = command in aliases or self.pending is not None and not text.startswith("/")
        if not relevant:
            if text.startswith("/"):
                self.pending = None
            return False
        if not self.extension.setup.private_owner(message) or any(getattr(update, key, None) for key in ("edited_message", "edited_channel_post")):
            return True
        try:
            async with self.serial:
                if command in aliases:
                    self.pending = None
                    await self.run(bot, *aliases[command])
                else:
                    await self.text_input(bot, text)
        except ValueError as error:
            await self.display(bot, "Nicht gespeichert.\n" + str(error)[:600], [("Setup", "setup")])
        except Exception:
            self.pending = None
            await self.display(bot, "Menü vorübergehend nicht verfügbar. Ein möglicherweise angenommener Handelsauftrag wird mit derselben Identität abgeglichen.")
        return True
