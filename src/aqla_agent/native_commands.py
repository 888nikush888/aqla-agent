"""Deterministic private Telegram controls over the existing authenticated MCP gates.

Only the earliest native PTB admission handler may execute these commands. Registry
handlers are discovery/fail-closed fallbacks, and dialogs contain no persisted state.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
import shlex
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from aqla_hyperliquid_mcp.models import AqlaError

from .native_setup import addressed_to_bot
from .pipelines import AccountLimits, Parameters, TradingProfile
from .ranking import TIERS

_CATALOG_NATIVE = ContextVar("aqla_native_catalog", default=False)

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


def routine_query(text):
    value = " ".join(text.casefold().strip().rstrip("?!. ").split())
    aliases = {
        "aqla_kanaele": {"alle aktuellen kanäle", "zeig mir alle kanäle", "zeige mir alle kanäle", "alle kanäle", "welche kanäle sind aktiv", "liste alle kanäle", "kanalliste"},
        "aqla_status": {"wie ist status", "wie ist der status", "status", "aktueller status", "wie ist unser status", "wie ist der aktuelle status"},
        "aqla_profile": {"alle profile", "zeige mir alle profile", "welche profile gibt es", "profile"},
        "aqla_funktionen": {"was kannst du", "welche funktionen gibt es", "welche befehle gibt es", "funktionen"},
    }
    compact = value.replace(" ", "")
    return next((command for command, values in aliases.items() if compact in {item.replace(" ", "") for item in values}), None)


def parameter_unit(field, parameters):
    if field == "fallback_stop_loss_percent":
        return "% Marginverlust" if parameters.get("fallback_stop_loss_unit", "course_percent") == "roe_percent" else "% Kursbewegung"
    if field == "fallback_take_profit_percentages":
        return ("% ROE" if parameters.get("fallback_take_profit_unit", "course_percent") == "roe_percent" else "% Kursbewegung") + " (JSON-Liste)"
    return UNITS.get(field, "Wert")


def register_commands(ctx):
    def unavailable(_raw_args):
        return "Native aqla-Zulassung ist nicht verfügbar. Es wurde nichts geändert."
    for name, (description, hint) in COMMANDS.items():
        # Hermes normalizes Telegram underscores to registry hyphens at dispatch.
        ctx.register_command(name.replace("_", "-"), unavailable, description=description,
                             args_hint=hint, argument_mode="text")


def system_catalog(allowed=None):
    return "\n".join(f"`/{name}{(' ' + hint) if hint else ''}` — {description}"
                     for name, (description, hint) in COMMANDS.items()
                     if allowed is None or name in allowed or name.replace("_", "-") in allowed)


def install_catalog(replace):
    import hermes_cli.commands as native_commands
    from gateway.slash_commands import GatewaySlashCommandsMixin

    native_lines = native_commands.gateway_help_lines

    def native_only(*args, **kwargs):
        lines = native_lines(*args, **kwargs)
        if not _CATALOG_NATIVE.get():
            return lines
        # Filter only catalog entries with an exact registered command name.
        # The native renderer still owns ACL/config gates, aliases and paging.
        result = []
        for line in lines:
            match = re.match(r"^`/([^\s`]+)", line)
            if not match or match[1].replace("-", "_") not in COMMANDS:
                result.append(line)
        return result

    replace(native_commands, "gateway_help_lines", native_only)

    for name in ("_handle_help_command", "_handle_commands_command"):
        original = getattr(GatewaySlashCommandsMixin, name)

        def wrap(native):
            async def catalog(self, event):
                from .events import gateway_extension
                extension = gateway_extension(self)
                source = event.source
                if (extension is None or getattr(source.platform, "value", source.platform) != "telegram"
                        or source.chat_type != "dm" or str(source.chat_id) != extension.owner
                        or str(source.user_id) != extension.owner):
                    return await native(self, event)
                token = _CATALOG_NATIVE.set(True)
                try:
                    text = await native(self, event)
                finally:
                    _CATALOG_NATIVE.reset(token)
                allowed = self._catalog_options(event).get("allowed_commands")
                return "**Hermes Agent**\n" + text + "\n\n**aqla Agent-System**\n" + system_catalog(allowed)
            return catalog

        replace(GatewaySlashCommandsMixin, name, wrap(original))


def install_menu(adapter, replace):
    native_menu = adapter._register_command_menu
    native_rows = adapter._bot_commands

    def prefixed(self, rows):
        return native_rows([(name, ("aqla: " if name in COMMANDS else "Hermes: ") + str(description))
                            for name, description in rows])

    async def owner_menu(self):
        await native_menu()
        from telegram import BotCommandScopeChat

        from .events import EXTENSIONS
        # The owner-specific scope reserves room for EVERY system command while
        # retaining native ordering/cap and all other scopes' native selection.
        rows = [(name, COMMANDS[name][0]) for name in ("aqla", "aqla_pause", "setup")]
        for extension in tuple(EXTENSIONS):
            if extension.gateway is None or extension.gateway is getattr(self, "gateway_runner", None):
                await self._bot.set_my_commands(self._bot_commands(rows), scope=BotCommandScopeChat(chat_id=int(extension.owner)))

    replace("_bot_commands", prefixed)
    replace("_register_command_menu", owner_menu)


@dataclass
class Dialog:
    command: str
    binding: object
    deadline: float
    snapshot: dict
    target: str | None = None
    field: str | None = None
    tokens: dict = dataclass_field(default_factory=dict)


def split_direct(raw, count):
    """Quoted exact targets/field labels plus an untouched JSON/numeric value."""
    parts = []
    rest = raw.strip()
    for _ in range(count):
        match = re.match(r'''(?:"([^"\n]*)"|'([^'\n]*)'|(\S+))(?:\s+|$)''', rest)
        if not match:
            raise ValueError("Direkte Syntax ist unvollständig.")
        parts.append(next(value for value in match.groups() if value is not None))
        rest = rest[match.end():].strip()
    return parts, rest


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


class NativeCommands:
    def __init__(self, extension):
        self.extension = extension
        self.dialog = None
        self.serial = asyncio.Lock()
        self.expiry_handle = None
        from .telegram_menu import TelegramMenu
        self.menu = TelegramMenu(self) if hasattr(extension, "config_path") else None

    def cancel(self):
        self.dialog = None
        if self.expiry_handle:
            self.expiry_handle.cancel()
            self.expiry_handle = None

    def live(self):
        dialog = self.dialog
        if dialog:
            try:
                valid = (self.extension.setup.now() < dialog.deadline
                         and self.extension.setup_binding() == dialog.binding and self.extension.setup.claims())
            except AqlaError:
                valid = False
            if not valid:
                self.cancel()
        return self.dialog

    def auth(self, *, protected=False):
        claims = self.extension.setup.claims()
        if protected and not claims:
            raise AqlaError("Setup ist aus oder abgelaufen. Starte /setup.")
        return self.extension.authority.sign("operator", "native-command:" + secrets.token_hex(16), setup=claims)

    async def status(self):
        return await self.extension.mcp_call(self.extension.server, "status", {"context_auth": self.auth(),
            **({"view": "configuration"} if self.menu else {})})

    async def control(self, **arguments):
        # Read authority immediately before RPC. MCP rechecks again at its write.
        protected = arguments.get("action") not in ("pause", "set_source_name", "close_trade", "clear_notification") and not (
            arguments.get("action") in ("set_account_enabled", "set_exchange_enabled") and arguments.get("enabled") is False)
        return await self.extension.mcp_call(self.extension.server, "control", {**arguments, "context_auth": self.auth(protected=protected)})

    async def reply(self, bot, text, buttons=None, *, markdown=False):
        if self.menu and not markdown:
            await self.menu.display(bot, text, buttons or (), raw=True)
            return
        if markdown:
            adapter = self.extension.binding[0]
            result = await adapter.send(self.extension.owner, text)
            if not result.success:
                raise AqlaError("Native Telegram-Antwort konnte nicht zugestellt werden.")
            return
        # Plain replies treat every untrusted label/value as literal text. Native
        # trade/status formatters already escape labels before Markdown conversion.
        rows = [[{"text": label[:64], "callback_data": token}] for label, token in (buttons or [])]
        chunks, chunk, length = [], [], 0
        for character in text:
            width = 2 if ord(character) > 0xffff else 1
            if length + width > 3500:
                chunks.append("".join(chunk))
                chunk, length = [], 0
            chunk.append(character)
            length += width
        chunks.append("".join(chunk) or "—")
        for index, chunk in enumerate(chunks):
            await asyncio.wait_for(bot.send_message(chat_id=self.extension.owner, text=chunk,
                parse_mode=None, reply_markup={"inline_keyboard": rows} if rows and index == 0 else None), 10)

    def choices(self, rows, action, page=0):
        dialog = self.dialog
        dialog.tokens.clear()
        page = max(0, min(page, max(0, (len(rows) - 1) // 8)))
        buttons = []
        for label, value in rows[page * 8:(page + 1) * 8]:
            token = CALLBACK_PREFIX + secrets.token_urlsafe(18)
            dialog.tokens[token] = (action, value)
            buttons.append((label, token))
        for label, value in (("← Zurück", page - 1), ("Weiter →", page + 1)):
            if 0 <= value * 8 < len(rows):
                token = CALLBACK_PREFIX + secrets.token_urlsafe(18)
                dialog.tokens[token] = ("page", (rows, action, value))
                buttons.append((label, token))
        token = CALLBACK_PREFIX + secrets.token_urlsafe(18)
        dialog.tokens[token] = ("cancel", None)
        buttons.append(("Abbrechen", token))
        return buttons

    def targets(self, dialog):
        snapshot = dialog.snapshot
        if dialog.command == "aqla_rang":
            from .ranking import active_sources, source_key
            selected = active_sources(snapshot)
            return [(snapshot.get("source_names", {}).get(identity, identity) + " · " + identity, identity)
                    for identity in snapshot["source_ranks"] if source_key(snapshot, identity) in selected]
        if dialog.command == "aqla_limits":
            return [(identity, identity) for identity, row in snapshot["execution_accounts"].items() if row.get("configured")]
        return [(row["name"] + " · " + identity, identity) for identity, row in snapshot["profiles"].items() if row.get("parameters")]

    def resolve(self, selector):
        dialog = self.dialog
        targets = self.targets(dialog)
        exact = [identity for _, identity in targets if identity == selector]
        if exact:
            return exact[0]
        if dialog.command == "aqla_rang":
            names = dialog.snapshot.get("source_names", {})
        elif dialog.command == "aqla_limits":
            names = {}
        else:
            names = {identity: row["name"] for identity, row in dialog.snapshot["profiles"].items()}
        matches = [identity for _, identity in targets if names.get(identity, "").casefold() == selector.casefold()]
        return matches[0] if len(matches) == 1 else None

    def fields(self):
        if self.dialog.command == "aqla_maker":
            return [(value, value) for value in ("entry", "tp", "beide")]
        if self.dialog.command == "aqla_limits":
            return [("Offene Trades", "max_open_trades"), ("Gesamtrisiko", "max_total_risk_percent")]
        return [(name, name) for name in Parameters.model_fields]

    def schema(self):
        dialog = self.dialog
        if dialog.command in ("aqla_rang", "aqla_maker"):
            return {"enum": list(TIERS) if dialog.command == "aqla_rang" else ["standard", "maker_only"]}
        model = AccountLimits if dialog.command == "aqla_limits" else Parameters
        return model.model_json_schema()["properties"][dialog.field]

    def current(self):
        dialog = self.dialog
        if dialog.command == "aqla_rang":
            return dialog.snapshot["source_ranks"][dialog.target]["tier"]
        if dialog.command == "aqla_limits":
            return dialog.snapshot["execution_accounts"][dialog.target]["limits"][dialog.field]
        values = Parameters.model_validate(dialog.snapshot["profiles"][dialog.target]["parameters"])
        if dialog.command == "aqla_maker":
            return {"entry": values.entry_execution, "tp": values.take_profit_execution} if dialog.field == "beide" else getattr(values, "entry_execution" if dialog.field == "entry" else "take_profit_execution")
        return getattr(values, dialog.field)

    async def show_target(self, bot, prefix=""):
        rows = self.targets(self.dialog)
        if not rows:
            self.cancel()
            await self.reply(bot, prefix + "Keine konfigurierten Ziele verfügbar.")
        else:
            await self.reply(bot, prefix + "Ziel wählen:", self.choices(rows, "target"))

    async def show_field(self, bot):
        if self.dialog.command == "aqla_rang":
            self.dialog.field = "tier"
            await self.show_value(bot)
        else:
            await self.reply(bot, "Feld wählen:", self.choices(self.fields(), "field"))

    async def show_value(self, bot, prefix=""):
        dialog = self.dialog
        schema = self.schema()
        variants = schema.get("anyOf", [schema])
        selected = next((value for value in variants if value.get("type") != "null"), schema)
        options = selected.get("enum")
        if selected.get("type") == "boolean":
            options = [True, False]
        current = self.current()
        parameters = dialog.snapshot["profiles"][dialog.target]["parameters"] if dialog.command == "aqla_parameter" else {}
        unit = parameter_unit(dialog.field, parameters)
        text = prefix + f"{dialog.target} · {dialog.field}\nAktuell: {current}\nEinheit: {unit}"
        if dialog.command == "aqla_parameter" and dialog.field.startswith("fallback_") and "roe_percent" in parameters.values():
            text += "\nROE-Basis: ursprüngliche Positionsmargin vor Gebühren und Funding."
        if options is not None:
            buttons = self.choices([(BACKUP_UNIT_LABELS.get(value, str(value)) if dialog.field in ("fallback_stop_loss_unit", "fallback_take_profit_unit") else {"standard": "Standard", "waverider": "Waverider"}.get(value, str(value)) if dialog.field == "management_mode" else str(value), value) for value in options], "value")
        else:
            example = '["move_stop", "close"]' if dialog.field == "allowed_updates" else "[1, 2]" if selected.get("type") == "array" else "5" if selected.get("type") == "integer" else "0,5"
            if dialog.field == "waverider_until_tp":
                example = "1 oder last"
            text += "\nSende den neuen Wert. Beispiel: " + example
            if any(row.get("type") == "null" for row in variants):
                text += "; null = aus"
            buttons = self.choices([], "value")
        await self.reply(bot, text, buttons)

    def revision(self, snapshot):
        dialog = self.dialog
        if dialog.command == "aqla_rang":
            return snapshot["source_ranks"].get(dialog.target, {}).get("revision")
        return snapshot["configuration_revision"]

    async def commit(self, bot, value):
        dialog = self.live()
        if dialog is None:
            await self.reply(bot, "Dialog ist abgelaufen. Starte den Befehl neu.")
            return
        fresh = await self.status()
        if self.live() is not dialog:
            await self.reply(bot, "Setup oder Dialog ist abgelaufen.")
            return
        if self.revision(fresh) != self.revision(dialog.snapshot):
            dialog.snapshot = fresh
            if dialog.target not in {identity for _, identity in self.targets(dialog)}:
                dialog.target = dialog.field = None
                await self.show_target(bot, "Ziel wurde geändert. ")
            else:
                await self.show_value(bot, "Einstellungen wurden geändert. Aktuellen Wert neu wählen.\n")
            return
        old = self.current()
        arguments = {"expected_revision": self.revision(fresh)}
        if dialog.command == "aqla_rang":
            if value not in TIERS:
                raise ValueError("Wähle einen Rang S, A, B, C, D, E oder F.")
            arguments.update(action="set_source_rank", source_id=dialog.target, tier=value)
        elif dialog.command == "aqla_limits":
            values = {**fresh["execution_accounts"][dialog.target]["limits"], dialog.field: value}
            limits = AccountLimits.model_validate(values)
            arguments.update(action="set_account_limits", account_key=dialog.target, limits=limits.model_dump(mode="json"))
            value = getattr(limits, dialog.field)
        else:
            profile = copy.deepcopy(fresh["profiles"][dialog.target])
            values = dict(profile["parameters"])
            if dialog.command == "aqla_maker":
                if dialog.field not in ("entry", "tp", "beide"):
                    raise ValueError("Wähle entry, tp oder beide.")
                for key in ({"entry": ["entry_execution"], "tp": ["take_profit_execution"], "beide": ["entry_execution", "take_profit_execution"]}[dialog.field]):
                    values[key] = value
            else:
                values[dialog.field] = value
            validated = TradingProfile(id=profile["id"], name=profile["name"], parameters=Parameters.model_validate(values))
            arguments.update(action="set_profile", profile=validated.model_dump(mode="json"))
            if dialog.command != "aqla_maker":
                value = getattr(validated.parameters, dialog.field)
        # No additional confirmation: a valid final value is the authorization.
        await self.control(**arguments)
        self.cancel()
        suffix = " · " + parameter_unit(dialog.field, values) if dialog.command == "aqla_parameter" and dialog.field in ("fallback_stop_loss_percent", "fallback_take_profit_percentages") else ""
        await self.reply(bot, f"Gespeichert: {dialog.target} · {dialog.field}: {old} → {value}" + suffix)

    async def start(self, bot, command, raw):
        self.cancel()
        self.auth(protected=True)
        binding = self.extension.setup_binding()
        deadline = min(self.extension.setup.now() + 300, self.extension.setup.lease[2])
        snapshot = await self.status()
        self.auth(protected=True)
        if self.extension.setup_binding() != binding or self.extension.setup.now() >= deadline:
            raise AqlaError("Setup oder Sitzung ist abgelaufen.")
        self.dialog = Dialog(command, binding, deadline, snapshot)
        self.expiry_handle = asyncio.get_running_loop().call_later(max(0, deadline - self.extension.setup.now()), self.cancel)
        if not raw:
            await self.show_target(bot)
            return
        parts, value = split_direct(raw, 1 if command == "aqla_rang" else 2)
        target = self.resolve(parts[0])
        if target is None:
            await self.show_target(bot, "Ziel fehlt oder Name ist mehrdeutig. Wähle die genaue ID. ")
            return
        self.dialog.target = target
        field_name = "tier" if command == "aqla_rang" else parts[1].lower()
        field_name = LIMIT_FIELDS.get(field_name, field_name)
        if command != "aqla_rang" and field_name not in {value for _, value in self.fields()}:
            await self.show_field(bot)
            return
        self.dialog.field = field_name
        if not value:
            await self.show_value(bot)
            return
        await self.commit(bot, parse_value(value, self.schema()))

    async def read(self, bot, command):
        snapshot = await self.status()
        if command == "aqla_status":
            for block in snapshot.get("telegram_view", {}).get("status", []):
                await self.reply(bot, block, markdown=True)
            # Use the same established trade formatter as notifications.
            from .messages import trade_block
            data = {**snapshot, "trades": {trade["trade_id"]: trade for trade in snapshot.get("trades", [])}}
            for trade in snapshot.get("trades", []):
                await self.reply(bot, trade_block(data, trade, []), markdown=True)
            setup = snapshot.get("setup", {})
            journal = snapshot.get("journal", {})
            accounts = "\n".join(f"{identity}: {'aktiv' if row.get('enabled') else 'aus'} · {'konfiguriert' if row.get('configured') else 'nicht konfiguriert'} · Limits: {row.get('limits') or '—'}"
                                 for identity, row in snapshot.get("execution_accounts", {}).items())
            await self.reply(bot, "Konten:\n" + (accounts or "—"))
            await self.reply(bot, f"Setup: {setup.get('remaining_seconds', 0)} Sekunden übrig\nJournal: {journal.get('pending', 0)} ausstehend; letzter Sync: {journal.get('last_synced_at') or '—'}")
            queue = snapshot.get("source_queue", {})
            processing = snapshot.get("source_processing", {})
            jobs = processing.get("jobs", processing.get("states", {}))
            decisions = processing.get("decision", processing.get("decision_status", {}))
            permissions = decisions.get("permissions", {}) if processing.get("mode") == "hybrid" and decisions.get("validated") else {}
            if processing.get("mode") == "legacy":
                processing_text = "Verarbeitung: Hermes + DeepSeek + Tools\nHistorische Quellenjobs: " + json.dumps(
                    processing.get("historical_jobs", jobs), ensure_ascii=False)
            else:
                processing_text = "Verarbeitung: " + json.dumps(jobs, ensure_ascii=False) + "\n"
                processing_text += "Decision-Modell: " + str(decisions.get("model", "nicht gemeldet")) + " · kalibriert: " + ("ja" if decisions.get("validated") else "nein") + "\n"
                processing_text += "Aktive Decision-Freigabe: " + json.dumps(permissions, ensure_ascii=False)
            await self.reply(bot, f"Quellenwarteschlange: bereit {queue.get('ready_total', 0)} · blockiert {queue.get('blocked_total', 0)} · Recovery {queue.get('recovery_total', 0)}\n"
                + "Verarbeitungsmodus: " + str(processing.get("mode", "nicht gemeldet")) + "\n" + processing_text + "\n"
                + "Eigentumskonflikte: " + str(sum(row.get("status") == "conflicted" for row in snapshot.get("trades", []))) + "\n"
                + "\nBenachrichtigungen und abgeschlossene Verarbeitung sind getrennte Zähler.")
        elif command == "aqla_kanaele":
            ranking = await self.extension.mcp_call(self.extension.server, "ranking_status", {})
            lines = [f"{row.get('name', identity)} · {identity}: {row['tier']} · Nachweise {row.get('proof_total', 0)} · ausgeschlossen {row.get('excluded_count', 0)}\nMetriken: {json.dumps(row.get('metrics', {}), ensure_ascii=False)}\nNachweise: {json.dumps(row.get('evidence', []), ensure_ascii=False)}\nAusschlüsse: {json.dumps(row.get('exclusions', []), ensure_ascii=False)}" for identity, row in ranking["sources"].items()]
            selected = snapshot.get("sources", list(ranking["sources"]))
            title = "Aktuell ausgewählte Kanäle · Konfigurationsrevision " + str(snapshot.get("configuration_revision", "—"))
            selection = [snapshot.get("source_names", {}).get(identity, identity) + " · " + identity for identity in selected]
            await self.reply(bot, title + "\n" + ("\n".join(selection) or "Keine Kanäle konfiguriert.") + "\n\nRanghistorie/Nachweise:\n" + ("\n\n".join(lines) or "—"))
        elif command == "aqla_profile":
            lines = [f"{row['name']} · {identity}\n" + "\n".join(f"{name}: {getattr(Parameters.model_validate(row['parameters']), name)}" + (" · " + parameter_unit(name, row['parameters']) if name in ("fallback_stop_loss_percent", "fallback_take_profit_percentages") else "") for name in Parameters.model_fields)
                     for identity, row in snapshot["profiles"].items() if row.get("parameters")]
            await self.reply(bot, "\n\n".join(lines) or "Keine Profile konfiguriert.")
        elif command == "aqla_funktionen":
            await self.reply(bot, "aqla Agent-System\n" + system_catalog())
        else:
            await self.reply(bot, "Journal: lokal in SQLite gespeichert. /aqla → Journal & Berichte")

    async def source_change(self, bot, command, raw):
        self.cancel()
        self.auth(protected=command != "aqla_kanalname")
        if not raw:
            await self.reply(bot, f"/{command} " + COMMANDS[command][1] + "\nIDs, @usernames oder registrierte Namen verwenden. Namen mit Leerzeichen in Anführungszeichen setzen.")
            return
        snapshot = await self.status()
        parts = shlex.split(raw)
        if command == "aqla_kanalname":
            if len(parts) != 2:
                raise ValueError("Supply ID and exact name")
            result = await self.control(action="set_source_name", source_id=parts[0], source_name=parts[1], expected_revision=snapshot.get("source_names_revision", 0))
            await self.reply(bot, "Kanalname gespeichert · Revision " + str(result["source_names_revision"]))
        else:
            result = await self.control(action="add_sources" if command == "aqla_hinzufuegen" else "remove_sources",
                sources=parts, expected_revision=snapshot["configuration_revision"])
            await self.reply(bot, "Kanalauswahl gespeichert · Revision " + str(result["configuration_revision"]) + "\n" + "\n".join(result["sources"]))

    async def ownership(self, bot, command, raw):
        parts = raw.split()
        if command == "aqla_konflikt":
            if len(parts) != 1:
                snapshot = await self.status()
                rows = [row for row in snapshot.get("trades", []) if row.get("status") == "conflicted"]
                await self.reply(bot, "Eigentumskonflikte:\n" + ("\n".join(row["plan"]["market"]["symbol"] + " · " + row["trade_id"] for row in rows) or "Keine.") + "\n/aqla_konflikt <Trade-ID>")
                return
            result = await self.extension.mcp_call(self.extension.server, "conflict_review", {"trade_id": parts[0], "context_auth": self.auth()})
            labels = {"position_not_flat": "Position noch offen", "owned_orders_not_terminal": "Eigene Orders noch offen/ungeklärt", "unknown_operation": "Börsenoperation ungeklärt", "foreign_or_unproved_fill": "Fremde oder unbelegte Ausführung", "foreign_exposure_in_history": "Fremde Position im Verlauf", "own_closure_not_proved": "Eigene Schließung nicht vollständig belegt", "fill_history_incomplete": "Ausführungsverlauf unvollständig", "immutable_fill_changed": "Gespeicherter Beleg widersprüchlich", "unsent_close_intention": "Gespeicherte Schließabsicht noch nicht ausgeführt", "entry_quantity_not_reconciled": "Neue Entry-Fills zuerst über den bestehenden Belegabgleich bestätigen; gespeicherte Entry-Menge weicht ab"}
            text = f"Eigentumsprüfung · {result['account_key']} · {result['market']}\nPosition: {result['position_size']}\n"
            text += "\n".join(labels.get(code, code) for code in result["reasons"]) or "Eigene vollständige Schließung belegt."
            if result["resolvable"]:
                text += "\nFür lokale Bestätigung innerhalb 90 Sekunden mit aktivem Setup:\n/aqla_konflikt_loesen " + parts[0] + " " + result["fingerprint"]
            await self.reply(bot, text + "\nPnL bleibt unvollständig. Es wird keine Order erstellt oder storniert.")
        else:
            self.auth(protected=True)
            if len(parts) != 2:
                raise ValueError("Trade ID and fresh fingerprint required")
            result = await self.control(action="resolve_conflict", trade_id=parts[0], review_fingerprint=parts[1])
            await self.reply(bot, "Eigene Schließung bestätigt; Marktreservierung freigegeben. PnL unvollständig." if result["resolved"] else "Schließung nicht belegt; Reservierung bleibt bestehen. Neue Eigentumsprüfung lesen.")

    async def intercept(self, update, bot):
        if self.menu and await self.menu.intercept(update, bot):
            return True
        query = getattr(update, "callback_query", None)
        if query and str(getattr(query, "data", "")).startswith(CALLBACK_PREFIX):
            user, chat = query.from_user, getattr(query.message, "chat", None)
            authentic = bool(user and chat and not user.is_bot and str(user.id) == self.extension.owner
                             and str(chat.id) == self.extension.owner and chat.type == "private")
            await query.answer()
            if not authentic:
                return True
            async with self.serial:
                dialog = self.live()
                selection = dialog.tokens.pop(query.data, None) if dialog else None
                if selection is None:
                    return True
                # Every displayed button is a one-use capability. Changing stage
                # invalidates its siblings, and data is never parsed as a value.
                dialog.tokens.clear()
                action, value = selection
                try:
                    if action == "cancel":
                        self.cancel()
                        await self.reply(bot, "Dialog abgebrochen.")
                    elif action == "page":
                        rows, stage, page = value
                        await self.reply(bot, "Auswahl:", self.choices(rows, stage, page))
                    elif action == "target":
                        dialog.target = value
                        await self.show_field(bot)
                    elif action == "field":
                        dialog.field = value
                        await self.show_value(bot)
                    elif action == "value":
                        await self.commit(bot, value)
                except (ValueError, AqlaError):
                    if self.live():
                        await self.show_value(bot, "Wert oder Änderung nicht zulässig. Prüfe die vollständigen Profilregeln.\n")
                    else:
                        await self.reply(bot, "Setup oder Dialog ist abgelaufen.")
            return True
        message = getattr(update, "effective_message", None)
        text = getattr(message, "text", None) or ""
        head, _, raw = text.partition(" ")
        command = head.split("@", 1)[0].lstrip("/").lower() if head.startswith("/") else ""
        if not command and self.dialog is None:
            command = routine_query(text) or ""
        if not addressed_to_bot(head, bot):
            return command in COMMANDS
        authentic = self.extension.setup.private_owner(message) and not any(getattr(update, key, None) for key in ("edited_message", "edited_channel_post"))
        if command in RESET_COMMANDS or command in ("setup", "cancel", "abbrechen"):
            if authentic:
                self.cancel()
            if command in ("cancel", "abbrechen") and authentic:
                await self.reply(bot, "Dialog abgebrochen.")
                return True
            return False
        had_dialog = self.dialog is not None
        dialog = self.live()
        if command not in COMMANDS and dialog is None:
            if had_dialog:
                if authentic:
                    await self.reply(bot, "Dialog ist abgelaufen. Starte den Befehl neu.")
                return True
            return False
        if not authentic:
            return command in COMMANDS or dialog is not None
        # Pause is an ordinary owner action even while a dialog is pending.
        if command == "aqla_pause":
            self.cancel()
            result = await self.control(action="pause")
            await self.reply(bot, f"Entries: {'pausiert' if result['previous_paused'] else 'aktiv'} → pausiert. Positionsschutz läuft weiter.")
            return True
        async with self.serial:
            try:
                if command in PROTECTED or command == "aqla_kanalname":
                    if command == "aqla_fortsetzen":
                        self.cancel()
                        result = await self.control(action="resume")
                        await self.reply(bot, f"Entries: {'pausiert' if result['previous_paused'] else 'aktiv'} → freigegeben.")
                    elif command in ("aqla_hinzufuegen", "aqla_entfernen", "aqla_kanalname"):
                        await self.source_change(bot, command, raw.strip())
                    elif command == "aqla_konflikt_loesen":
                        await self.ownership(bot, command, raw.strip())
                    else:
                        await self.start(bot, command, raw.strip())
                elif command == "aqla_konflikt":
                    await self.ownership(bot, command, raw.strip())
                elif command in COMMANDS:
                    await self.read(bot, command)
                elif dialog is not None:
                    if command:
                        self.cancel()
                        return False  # Preserve legitimate typed Hermes commands.
                    if dialog.field is None:
                        await self.reply(bot, "Wähle zuerst die Schaltflächen oder /cancel.")
                    else:
                        await self.commit(bot, parse_value(text, self.schema()))
            except (ValueError, AqlaError):
                if self.live() and self.dialog.field is not None:
                    await self.show_value(bot, "Wert oder Änderung nicht zulässig. Prüfe die vollständigen Profilregeln.\n")
                else:
                    await self.reply(bot, "Befehl nicht verfügbar oder ungültig. Für Änderungen /setup starten; genaue Ziele und vollständige Werte verwenden.")
            except Exception:
                # Do not log Update, values, callback actors or exception payloads.
                self.cancel()
                await self.reply(bot, "Native aqla-Steuerung ist vorübergehend nicht verfügbar.")
        return True
