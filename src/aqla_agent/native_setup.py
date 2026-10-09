"""Private PTB ingress admission, before native debug logs, batches and model history."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import asdict, dataclass

from .setup import CHALLENGE_SECONDS, GRANT_SECONDS, LOCKOUT_SECONDS, SetupBinding, SetupVerifier


@dataclass(frozen=True)
class Challenge:
    binding: SetupBinding
    deadline: float
    prompt_id: int


class NativeSetup:
    def __init__(self, owner, verifier, request, binding, *, clock=time.monotonic):
        self.owner, self.verifier, self.request, self.binding = owner, verifier, request, binding
        self.clock, self._last_time = clock, clock()
        self.challenge = None
        self.old_prompts = set()
        self.failures, self.locked_until = 0, 0.0
        self.lease = None
        self.generation = None
        self.revision = 0
        self._serial = asyncio.Lock()

    def now(self):
        self._last_time = max(self._last_time, self.clock())
        return self._last_time

    def claims(self):
        if not self.lease:
            return None
        current = self.binding()
        if self.lease and (current != self.lease[1] or self.now() >= self.lease[2]):
            self.lease = None
        if not self.lease:
            return None
        return {"identity": self.lease[0], "binding": asdict(self.lease[1])}

    async def revoke(self):
        self.revision += 1
        self.lease = None
        if self.challenge:
            self.old_prompts.add(self.challenge.prompt_id)
        self.challenge = None
        await self.request("setup_revoke", setup_binding={"owner_id": self.owner})

    def private_owner(self, message):
        user, chat = getattr(message, "from_user", None), getattr(message, "chat", None)
        return bool(user and chat and str(user.id) == self.owner and str(chat.id) == self.owner
                    and chat.type == "private" and not user.is_bot and not getattr(message, "sender_chat", None)
                    and not any(getattr(message, key, None) for key in ("forward_origin", "forward_from", "forward_date", "via_bot")))

    async def reply(self, bot, text):
        # Never interpolate candidate text, exceptions or private verifier data.
        return await asyncio.wait_for(bot.send_message(chat_id=self.owner, text=text), timeout=10)

    async def intercept(self, update, bot):
        message = getattr(update, "effective_message", None)
        if not self.private_owner(message):
            return False
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command in ("/stop", "/new", "/reset", "/restart"):
            try:
                await self.revoke()
            except Exception:
                pass  # Local authority was already removed; MCP expiry still fails closed.
            return False
        old_reply = getattr(getattr(message, "reply_to_message", None), "message_id", None) in self.old_prompts
        candidate = bool(self.challenge and not command or old_reply)
        if command != "/setup" and not candidate:
            return False
        # Delete BEFORE waiting for serialized admission or the bounded hash worker.
        deleted = False
        suffix = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) == 2 else ""
        if candidate or command == "/setup" and suffix not in ("", "status", "aus"):
            try:
                deleted = await asyncio.wait_for(bot.delete_message(chat_id=self.owner, message_id=message.message_id), timeout=10) is True
            except Exception:
                pass
        try:
            async with self._serial:
                if command == "/setup":
                    suffix = text.split(maxsplit=1)[1].strip().lower() if len(text.split(maxsplit=1)) == 2 else ""
                    if suffix == "aus":
                        await self.revoke()
                        await self.reply(bot, "Setup ist aus.")
                    elif suffix == "status":
                        status = await self.request("setup_status", setup_binding=asdict(self.binding()), lease_id=(self.lease[0] if self.lease else None))
                        await self.reply(bot, f"Setup: {'aktiv' if status['active'] else 'aus'}; {status['remaining_seconds']} Sekunden übrig; Sperre: {status['locked_seconds']} Sekunden.")
                    elif suffix:
                        await self.reply(bot, "Nutze /setup, /setup status oder /setup aus. Das Passwort erst nach der Nachfrage senden.")
                    elif self.now() < self.locked_until:
                        await self.reply(bot, "Setup ist nach fünf Fehlversuchen für 15 Minuten gesperrt.")
                    elif self.verifier is None:
                        await self.reply(bot, "Der private Setup-Prüfhash ist nicht verfügbar.")
                    else:
                        await self.revoke()
                        binding = self.binding()
                        registered = await self.request("setup_bind", setup_binding=asdict(binding))
                        if registered.get("locked_seconds", 0):
                            self.locked_until = self.now() + registered["locked_seconds"]
                            await self.reply(bot, "Setup ist nach fünf Fehlversuchen für 15 Minuten gesperrt.")
                            return True
                        self.generation = registered["generation"]
                        deadline = self.now() + CHALLENGE_SECONDS
                        prompt = await self.reply(bot, "Sende das Setup-Passwort innerhalb von 120 Sekunden. Deine Antwort wird sofort gelöscht.")
                        if self.binding() == binding and self.now() < deadline:
                            self.challenge = Challenge(binding, deadline, prompt.message_id)
                            self.old_prompts.add(prompt.message_id)
                    return True
                challenge = self.challenge
                if old_reply and challenge and getattr(message.reply_to_message, "message_id", None) != challenge.prompt_id:
                    await self.reply(bot, "Diese alte Setup-Antwort ist abgelaufen.")
                    return True
                if (challenge is None or old_reply and getattr(message.reply_to_message, "message_id", None) != challenge.prompt_id
                        or self.binding() != challenge.binding or self.now() >= challenge.deadline or self.now() < self.locked_until):
                    self.challenge = None
                    await self.reply(bot, "Diese Setup-Nachfrage ist abgelaufen. Starte mit /setup neu.")
                    return True
                verified = bool(deleted and self.verifier and await self.verifier.verify(text))
                if self.challenge != challenge or self.binding() != challenge.binding or self.now() >= challenge.deadline:
                    self.challenge = None
                    return True
                if not verified:
                    self.failures += 1
                    await self.request("setup_failed", setup_binding=asdict(challenge.binding))
                    if self.failures >= 5:
                        self.locked_until = self.now() + LOCKOUT_SECONDS
                        self.failures = 0
                        self.challenge = None
                    await self.reply(bot, "Setup nicht freigegeben. Passwortprüfung oder bestätigtes Löschen fehlgeschlagen.")
                    return True
                identity, expiry = secrets.token_hex(32), self.now() + GRANT_SECONDS
                revision = self.revision
                self.challenge = None
                # MCP creates its own exact 300-second deadline. Local admission
                # expires from RPC start, conservatively earlier after any RTT.
                await self.request("setup_grant", setup_binding=asdict(challenge.binding), lease_id=identity,
                                   generation=self.generation)
                if self.revision != revision or self.binding() != challenge.binding or self.now() >= expiry:
                    await self.revoke()
                    return True
                self.lease = identity, challenge.binding, expiry
                self.failures = 0
                await self.reply(bot, "Setup ist für 300 Sekunden aktiv.")
        except asyncio.CancelledError:
            self.lease = None
            self.challenge = None
            raise
        except Exception as error:
            self.lease = None
            self.challenge = None
            # Do not expose exceptions carrying Update/candidate representations.
            logging.getLogger(__name__).warning("Private Setup admission unavailable (%s)", type(error).__name__)
        return True


class PrivateTransportLogs(logging.Filter):
    """PTB logs incoming API bodies before handler admission; omit those records."""
    def filter(self, record):
        if record.name.startswith("telegram"):
            if "update" in str(record.msg).lower() or "parameters" in str(record.msg).lower() or "return value" in str(record.msg).lower():
                record.msg, record.args, record.exc_info = "Telegram transport metadata omitted", (), None
        return True


def load_verifier(path):
    try:
        return SetupVerifier.from_file(path)
    except Exception:
        return None
