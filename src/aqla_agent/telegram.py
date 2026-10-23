from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import os
import random
import time
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from aqla_hyperliquid_mcp.models import AqlaError, Settings, SourceEvent, now_ms

ReaderErrorCode = Literal["reader_session_unauthorized", "reader_api_credentials_invalid", "reader_rate_limited", "sources_access_denied",
                          "reader_unavailable", "reader_configuration_missing", "reader_dependency_missing", "reader_session_missing",
                          "reader_configuration_unreadable", "reader_session_invalid", "sources_unavailable"]


class ReaderRetryStatus(TypedDict):
    code: ReaderErrorCode
    sources: list[str]
    attempt: int
    next_retry_at: int


class ReaderRetry:
    """Private monotonic deadlines; public diagnostics contain only safe fields."""
    def __init__(self, *, clock=None, wall_clock=now_ms, jitter=random.random):
        self.clock, self.wall_clock, self.jitter = clock or time.monotonic, wall_clock, jitter
        self.attempts = 0
        self.deadline = 0.0
        self.status: ReaderRetryStatus | None = None

    def reset(self):
        self.attempts, self.deadline, self.status = 0, 0.0, None

    def ready(self):
        return self.clock() >= self.deadline

    def failed(self, failure) -> ReaderRetryStatus:
        self.attempts += 1
        durable = failure.code in {"reader_session_unauthorized", "reader_api_credentials_invalid", "reader_configuration_missing",
                                   "reader_dependency_missing", "reader_session_missing", "reader_configuration_unreadable", "reader_session_invalid"}
        base, cap = (30, 300) if durable else (5, 120)
        delay = min(cap, base * 2 ** min(self.attempts - 1, 10))
        delay = min(cap, delay * (0.8 + 0.4 * self.jitter()))
        if failure.retry_after is not None:
            delay = max(delay, failure.retry_after + min(5, failure.retry_after * 0.1) * self.jitter())
        self.deadline = self.clock() + delay
        self.status = {"code": failure.code, "sources": list(failure.sources), "attempt": self.attempts,
                       "next_retry_at": self.wall_clock() + math.ceil(delay * 1000)}
        return self.status


def reader_metadata_token(settings: Settings, configuration: Path):
    """Internal-only metadata observation, never reads credential contents."""
    result: list[tuple[str, int, int, int] | tuple[str, Literal["unavailable"]] | None] = []
    for path in (configuration, settings.telegram_api_hash_file, settings.telegram_session_file):
        if path is None:
            result.append(None)
            continue
        try:
            value = Path(path).stat()
            result.append((str(path), value.st_ino, value.st_size, value.st_mtime_ns))
        except OSError:
            result.append((str(path), "unavailable"))
    return tuple(result)


class SourceAccessError(AqlaError):
    def __init__(self, code: ReaderErrorCode, sources=(), *, retry_after=None):
        self.code, self.sources = code, list(sources)
        self.retry_after = retry_after
        super().__init__(code)

    @classmethod
    def from_error(cls, error):
        if isinstance(error, cls):
            return error
        # Use only known exception types, never RPC text or credential values.
        codes = {"UnauthorizedError": "reader_session_unauthorized", "AuthKeyError": "reader_session_unauthorized",
                 "ApiIdInvalidError": "reader_api_credentials_invalid", "FloodError": "reader_rate_limited",
                 "ChannelPrivateError": "sources_access_denied", "ChatAdminRequiredError": "sources_access_denied",
                 "ChannelInvalidError": "sources_access_denied", "PeerIdInvalidError": "sources_access_denied",
                 "ChatIdInvalidError": "sources_access_denied", "UserBannedInChannelError": "sources_access_denied"}
        code = next((codes[base.__name__] for base in type(error).__mro__ if base.__name__ in codes), "reader_unavailable")
        seconds = None
        if code == "reader_rate_limited":
            value = getattr(error, "seconds", None)
            if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 2**31 - 1:
                seconds = float(value)
        return cls(code, retry_after=seconds)


class TelegramReader:
    """Read-only Telegram user session. Operator messages stay entirely in Hermes."""

    def __init__(self, settings: Settings, deliver, snapshot, *, resolve=None, bootstrap=None, client_factory=None,
                 clock=None, history_interval=30.0, dialog_interval=30.0):
        self.settings, self.deliver, self.snapshot = settings, deliver, snapshot
        self.client_factory = client_factory
        self.client = None
        self.closed = False
        self.resolve = resolve
        self.bootstrap = bootstrap
        self.aliases: dict[str, str] = {}
        self.selected = None
        self.entities: dict[str, Any] = {}
        self.intake_lock = asyncio.Lock()
        self.last_backfill = 0.0
        self.unavailable_sources: list[str] = []
        self.missing_sources: list[str] = []
        self.failed_sources: dict[str, list[str]] = {}
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 86400
               for value in (history_interval, dialog_interval)):
            raise ValueError("Reader history/dialog intervals must be finite seconds between 1 and 86400")
        self.clock = clock or time.monotonic
        self.history_interval, self.dialog_interval = history_interval, dialog_interval
        self.last_dialog_scan = None
        self.history_reads: dict[str, float] = {}
        self.last_delivered_versions: set[tuple[str, int, int | None]] = set()

    def status_error(self):
        return {"code": "sources_unavailable", "sources": list(self.unavailable_sources)} if self.unavailable_sources else None

    @staticmethod
    def source_name(value):
        value = value.strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if value.startswith(prefix):
                return "@" + value[len(prefix):].split("/", 1)[0].lower()
        return value.lower() if value.startswith("@") else value

    async def sync_sources(self, *, force=False):
        async with self.intake_lock:
            return await self._sync_sources(force=force)

    async def _resolve_sources(self, selected, now, changed):
        dialogs = {}
        async for dialog in self.client.iter_dialogs():
            dialogs[str(dialog.id)] = (str(dialog.id), dialog.entity)
            username = getattr(dialog.entity, "username", None)
            if username:
                dialogs["@" + username.lower()] = (str(dialog.id), dialog.entity)
        resolved = {}
        unavailable = []
        for name in selected:
            match = dialogs.get(self.source_name(name))
            if match is None:
                unavailable.append(name)
                continue
            resolved[name] = match[0]
        if self.resolve:
            await self.resolve(resolved)
        self.aliases = resolved
        self.entities = {chat: dialogs[chat][1] for chat in resolved.values()}
        self.selected = list(selected)
        self.missing_sources = unavailable
        self.failed_sources = {chat: names for chat, names in self.failed_sources.items() if chat in self.entities}
        self.update_source_status()
        self.last_dialog_scan = now
        if changed:
            self.history_reads.clear()

    def _history_due(self, chat, now, force):
        return force or chat not in self.history_reads or now - self.history_reads[chat] >= self.history_interval

    async def _sync_entities(self, configuration, now, force, chat_id, through):
        delivered = set()
        for chat, entity in self.entities.items():
            if chat_id is not None and chat != chat_id:
                continue
            try:
                reread = self._history_due(chat, now, force)
                if chat in self.failed_sources and not reread:
                    continue
                if chat_id is None and not reread:
                    continue
                delivered.update(await self.sync_source(chat, entity, configuration, reread=reread, through=through))
                if reread:
                    self.history_reads[chat] = now
            except Exception as error:
                if SourceAccessError.from_error(error).code != "sources_access_denied":
                    raise
                self.history_reads[chat] = now
                self.failed_sources[chat] = [name for name, resolved_chat in self.aliases.items() if resolved_chat == chat]
            else:
                self.failed_sources.pop(chat, None)
            self.update_source_status()
        return delivered

    async def _bootstrap_source(self, chat, entity, configuration, deliver):
        history = [message async for message in self.client.iter_messages(entity, limit=self.settings.max_context_messages)]
        cutoff = max((message.id for message in history), default=0)
        context_ids = sorted(message.id for message in history)
        if self.bootstrap:
            await self.bootstrap({chat: cutoff}, {chat: context_ids})
        configuration.setdefault("context_cutoffs", {})[chat] = cutoff
        configuration.setdefault("context_ids", {})[chat] = context_ids
        # Persist the bounded selection before the first delivery. A retry
        # must not replace this with an unbounded min_id=0 history scan.
        for message in reversed(history):
            await deliver(message, historical=True)
        return cutoff

    async def _reread_source(self, chat, entity, configuration, context_ids, cutoff, reread, deliver):
        if context_ids is None and cutoff:
            # Bounded recovery of an older state created before ID selection.
            history = [message async for message in self.client.iter_messages(entity, limit=self.settings.max_context_messages, max_id=cutoff + 1)]
            context_ids = sorted(message.id for message in history)
            if self.bootstrap:
                await self.bootstrap({chat: cutoff}, {chat: context_ids})
        ids = sorted({*(context_ids or []), *configuration.get("tracked_ids", {}).get(chat, [])})
        if ids and reread:
            messages = await self.client.get_messages(entity, ids=ids)
            for message in sorted((message for message in messages if message is not None), key=lambda item: item.id):
                await deliver(message, historical=message.id <= cutoff)

    async def _sync_sources(self, *, force=False, chat_id=None, configuration=None, through=None):
        configuration = await self.snapshot() if configuration is None else configuration
        selected = configuration["sources"]
        now = self.clock()
        changed = selected != self.selected
        resolve_due = changed or force or (self.missing_sources and (self.last_dialog_scan is None or now - self.last_dialog_scan >= self.dialog_interval))
        if resolve_due:
            await self._resolve_sources(selected, now, changed)
        configuration["source_aliases"] = self.aliases
        delivered = await self._sync_entities(configuration, now, force, chat_id, through)
        if chat_id is None:
            self.last_backfill = now
        self.last_delivered_versions = delivered
        return configuration

    def update_source_status(self):
        failed = set(self.missing_sources)
        failed.update(name for names in self.failed_sources.values() for name in names)
        self.unavailable_sources = [name for name in self.selected or [] if name in failed]

    async def sync_source(self, chat, entity, configuration, *, reread=True, through=None):
        delivered = set()
        async def deliver(message, *, historical=False):
            source = self.source(message, historical=historical)
            await self.deliver(source)
            delivered.add((source.chat_id, source.message_id, source.edited_at))
        cursor = configuration["cursors"].get(chat)
        cutoff = configuration.get("context_cutoffs", {}).get(chat, 0)
        context_ids = configuration.get("context_ids", {}).get(chat)
        if cursor is None and chat not in configuration.get("context_cutoffs", {}):
            cutoff = await self._bootstrap_source(chat, entity, configuration, deliver)
            cursor = cutoff
        else:
            cursor = cursor if cursor is not None else cutoff
            await self._reread_source(chat, entity, configuration, context_ids, cutoff, reread, deliver)
        options = {"min_id": max(cursor, cutoff), "reverse": True}
        if through is not None:
            options["max_id"] = through + 1
        async for message in self.client.iter_messages(entity, **options):
            if through is None or message.id <= through:
                await deliver(message)
        return delivered

    async def received(self, event):
        chat = str(event.message.chat_id)
        known = {*self.aliases.values(), *(name for name in self.selected or [] if name.lstrip("-").isdigit())}
        if chat not in known:
            return
        async with self.intake_lock:
            # Backfill ordered IDs first. A failed low ID prevents a later cursor advance.
            configuration = await self._sync_sources(chat_id=chat, through=event.message.id)
            if chat in self.failed_sources:
                return
            message = self.source(event.message)
            message.historical = message.message_id <= configuration.get("context_cutoffs", {}).get(message.chat_id, 0)
            if (message.chat_id in {*configuration["sources"], *self.aliases.values()}
                    and (message.chat_id, message.message_id, message.edited_at) not in self.last_delivered_versions):
                await self.deliver(message)

    @staticmethod
    def source(message, *, historical=False):
        caption = bool(getattr(message, "media", None)) and bool(message.message)
        reply = getattr(message, "reply_to", None)
        return SourceEvent(chat_id=str(message.chat_id), message_id=message.id,
                           sent_at=int(message.date.timestamp() * 1000),
                           edited_at=int(message.edit_date.timestamp() * 1000) if message.edit_date else None,
                           reply_to_id=getattr(reply, "reply_to_msg_id", None),
                           thread_id=getattr(reply, "reply_to_top_id", None),
                           album_id=str(message.grouped_id) if message.grouped_id else None,
                           text=message.message or "", forward_from=str(message.fwd_from.from_id) if message.fwd_from else None,
                           media="caption" if caption else "photo" if message.photo else "text", historical=historical)

    async def start(self):
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash_file or not self.settings.telegram_session_file:
            raise SourceAccessError("reader_configuration_missing")
        try:
            session = self.settings.telegram_session_file.read_text().strip()
        except FileNotFoundError:
            raise SourceAccessError("reader_session_missing") from None
        except (OSError, UnicodeError):
            raise SourceAccessError("reader_configuration_unreadable") from None
        try:
            api_hash = self.settings.telegram_api_hash_file.read_text().strip()
        except FileNotFoundError:
            raise SourceAccessError("reader_configuration_missing") from None
        except (OSError, UnicodeError):
            raise SourceAccessError("reader_configuration_unreadable") from None
        if not api_hash:
            raise SourceAccessError("reader_configuration_missing")
        try:
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            raise SourceAccessError("reader_dependency_missing") from None
        try:
            session = StringSession(session)
        except (ValueError, TypeError):
            raise SourceAccessError("reader_session_invalid") from None
        factory = self.client_factory or TelegramClient
        self.client = factory(session, self.settings.telegram_api_id, api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SourceAccessError("reader_session_unauthorized")

        self.client.add_event_handler(self.received, events.NewMessage())
        self.client.add_event_handler(self.received, events.MessageEdited())
        await self.sync_sources()

    async def close(self):
        self.closed = True
        if self.client:
            await self.client.disconnect()


def login_main():
    parser = argparse.ArgumentParser(description="Log in the read-only aqla Telegram source session")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    settings = Settings.load(args.config)

    async def login():
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if not settings.telegram_api_id or not settings.telegram_api_hash_file or not settings.telegram_session_file:
            raise SystemExit("Fill in telegram_api_id and its secret file paths first")
        client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash_file.read_text().strip())
        await client.start(phone=lambda: input("Telegram phone: "), password=lambda: getpass.getpass("Telegram 2FA: "))
        path = Path(settings.telegram_session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(client.session.save())
        await client.disconnect()
        print("Reader session saved to its secret file. No SQLite session was created.")

    asyncio.run(login())
