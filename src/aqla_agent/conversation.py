"""Small file-backed native Hermes transcript adapter, without a database."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class Conversations:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, session_id):
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise ValueError("Invalid native session identity")
        return self.directory / (session_id + ".jsonl")

    def load(self, session_id):
        with self.lock:
            path = self.path(session_id)
            if not path.exists():
                return []
            with path.open(encoding="utf-8") as source:
                return [json.loads(line) for line in source if line.strip()]

    def append(self, session_id, message):
        with self.lock:
            record = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
            with self.path(session_id).open("a", encoding="utf-8") as target:
                target.write(record + "\n")
                target.flush()
                os.fsync(target.fileno())

    def rewrite(self, session_id, messages):
        with self.lock:
            path = self.path(session_id)
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as target:
                for message in messages:
                    target.write(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False) + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return True

    def has_owner(self, session_id, owner):
        return any((row.get("display_metadata") or {}).get("gateway_input_owner") == owner for row in self.load(session_id))

    def has_message(self, session_id, message_id):
        return any(str(row.get("platform_message_id", row.get("message_id", ""))) == str(message_id) for row in self.load(session_id))
