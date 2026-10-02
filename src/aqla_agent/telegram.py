from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import time
from pathlib import Path

from aqla_hyperliquid_mcp.models import AqlaError, Settings, SourceEvent


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

    async def _sync_sources(self, *, force=False):
        configuration = await self.snapshot()
        selected = configuration["sources"]
        if selected == self.selected and not force and time.monotonic() - self.last_backfill < 30:
            return configuration
        if selected != self.selected:
            dialogs = {}
            async for dialog in self.client.iter_dialogs():
                dialogs[str(dialog.id)] = (str(dialog.id), dialog.entity)
                username = getattr(dialog.entity, "username", None)
                if username:
                    dialogs["@" + username.lower()] = (str(dialog.id), dialog.entity)
            resolved = {}
            for name in selected:
                match = dialogs.get(self.source_name(name))
                if match is None:
                    raise AqlaError(f"Subscribed source {name} is not accessible to the reader account")
                resolved[name] = match[0]
            if self.resolve:
                await self.resolve(resolved)
            self.aliases = resolved
            self.entities = {chat: dialogs[chat][1] for chat in resolved.values()}
            self.selected = list(selected)
        configuration["source_aliases"] = self.aliases
        for chat, entity in self.entities.items():
            cursor = configuration["cursors"].get(chat)
            if cursor is None:
                history = [message async for message in self.client.iter_messages(entity, limit=self.settings.max_context_messages)]
                cutoff = max((message.id for message in history), default=0)
                if self.bootstrap:
                    await self.bootstrap({chat: cutoff})
                for message in reversed(history):
                    await self.deliver(self.source(message, historical=True))
            else:
                cutoff = configuration.get("context_cutoffs", {}).get(chat, 0)
                async for message in self.client.iter_messages(entity, min_id=cursor, reverse=True):
                    await self.deliver(self.source(message, historical=message.id <= cutoff))
                ids = configuration.get("tracked_ids", {}).get(chat, [])
                if ids:
                    for message in await self.client.get_messages(entity, ids=ids):
                        if message is not None:
                            await self.deliver(self.source(message, historical=message.id <= cutoff))
        self.last_backfill = time.monotonic()
        return configuration

    async def received(self, event):
        async with self.intake_lock:
            # Backfill ordered IDs first. A failed low ID prevents a later cursor advance.
            configuration = await self._sync_sources(force=True)
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
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash_file or not self.settings.telegram_session_file:
            raise AqlaError("Telegram source access is not configured; Hermes bot communication still works")
        factory = self.client_factory or TelegramClient
        self.client = factory(StringSession(self.settings.telegram_session_file.read_text().strip()),
                              self.settings.telegram_api_id, self.settings.telegram_api_hash_file.read_text().strip())
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise AqlaError("Telegram reader session is not authorized; run aqla-telegram-login on the VPS")

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
