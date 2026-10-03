from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import time
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, Settings, SourceEvent


class SourceAccessError(AqlaError):
    def __init__(self, code, sources=()):
        self.code, self.sources = code, list(sources)
        super().__init__(code)

    @classmethod
    def from_error(cls, error):
        if isinstance(error, cls):
            return error
        # Use only known exception types, never RPC text or credential values.
        codes = {"UnauthorizedError": "reader_session_unauthorized", "AuthKeyError": "reader_session_unauthorized",
                 "ApiIdInvalidError": "reader_api_credentials_invalid", "FloodError": "reader_rate_limited",
                 "ChannelPrivateError": "sources_access_denied", "ChatAdminRequiredError": "sources_access_denied"}
        code = next((codes[base.__name__] for base in type(error).__mro__ if base.__name__ in codes), "reader_unavailable")
        return cls(code)


class TelegramReader:
    """Read-only Telegram user session. Operator messages stay entirely in Hermes."""

    def __init__(self, settings: Settings, deliver, snapshot, *, resolve=None, bootstrap=None, client_factory=None):
        self.settings, self.deliver, self.snapshot = settings, deliver, snapshot
        self.client_factory = client_factory
        self.client = None
        self.closed = False
        self.resolve = resolve
        self.bootstrap = bootstrap
        self.aliases = {}
        self.selected = None
        self.entities = {}
        self.intake_lock = asyncio.Lock()
        self.last_backfill = 0.0
        self.unavailable_sources = []

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

    async def _sync_sources(self, *, force=False, chat_id=None):
        configuration = await self.snapshot()
        selected = configuration["sources"]
        if selected == self.selected and not force and time.monotonic() - self.last_backfill < 30:
            return configuration
        if selected != self.selected or self.unavailable_sources:
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
            self.unavailable_sources = unavailable
        configuration["source_aliases"] = self.aliases
        for chat, entity in self.entities.items():
            if chat_id is not None and chat != chat_id:
                continue
            cursor = configuration["cursors"].get(chat)
            cutoff = configuration.get("context_cutoffs", {}).get(chat, 0)
            context_ids = configuration.get("context_ids", {}).get(chat)
            if cursor is None and chat not in configuration.get("context_cutoffs", {}):
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
                    await self.deliver(self.source(message, historical=True))
                cursor = cutoff
            else:
                cursor = cursor if cursor is not None else cutoff
                if context_ids is None and cutoff:
                    # Bounded recovery of an older state created before ID selection.
                    history = [message async for message in self.client.iter_messages(entity, limit=self.settings.max_context_messages, max_id=cutoff + 1)]
                    context_ids = sorted(message.id for message in history)
                    if self.bootstrap:
                        await self.bootstrap({chat: cutoff}, {chat: context_ids})
                ids = sorted(set([*(context_ids or []), *configuration.get("tracked_ids", {}).get(chat, [])]))
                if ids:
                    messages = await self.client.get_messages(entity, ids=ids)
                    for message in sorted((message for message in messages if message is not None), key=lambda item: item.id):
                        await self.deliver(self.source(message, historical=message.id <= cutoff))
            async for message in self.client.iter_messages(entity, min_id=max(cursor, cutoff), reverse=True):
                await self.deliver(self.source(message))
        if chat_id is None:
            self.last_backfill = time.monotonic()
        return configuration

    async def received(self, event):
        chat = str(event.message.chat_id)
        known = {*self.aliases.values(), *(name for name in self.selected or [] if name.lstrip("-").isdigit())}
        if chat not in known:
            return
        async with self.intake_lock:
            # Backfill ordered IDs first. A failed low ID prevents a later cursor advance.
            configuration = await self._sync_sources(force=True, chat_id=chat)
            message = self.source(event.message)
            message.historical = message.message_id <= configuration.get("context_cutoffs", {}).get(message.chat_id, 0)
            if message.chat_id in {*configuration["sources"], *self.aliases.values()}:
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
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession
        except ImportError:
            raise SourceAccessError("reader_dependency_missing") from None
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
