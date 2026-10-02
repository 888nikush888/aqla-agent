from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

import portalocker

from .models import AqlaError, Settings


class State:
    """One writer, atomically replaced JSON. No database or second source of truth."""

    def __init__(self, settings: Settings):
        self.path = Path(settings.state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._lock = portalocker.Lock(str(self.path) + ".lock", timeout=0)
        try:
            self._lock.acquire()
        except portalocker.LockException as error:
            raise AqlaError("Another aqla process already owns this state") from error
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data.get("schema") != 1 or self.data.get("network") != settings.network or self.data.get("account", "").lower() != settings.account.lower():
                    raise AqlaError("State version, account or network differs; use a separate state")
                for key in ("events", "trades", "operations", "nonces", "plans", "bindings", "sources"):
                    if key not in self.data:
                        raise AqlaError("State is incomplete; do not restore an old snapshot blindly")
            else:
                self.data = {"schema": 1, "network": settings.network, "account": settings.account.lower(), "sequence": 0,
                             "policy": settings.policy.model_dump(mode="json") if settings.policy else None,
                             "policy_revision": 1 if settings.policy else 0, "paused": False,
                             "sources": settings.sources, "source_aliases": {}, "events": {}, "trades": {}, "plans": {},
                             "bindings": {}, "operations": {}, "nonces": {}, "cursors": {}, "notifications": {}, "event_tombstones": {}}
                self._write(self.data)
        except BaseException:
            self._lock.release()
            raise

    def _write(self, data):
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise AqlaError("Durable state write failed; no exchange action may be sent") from error

    def snapshot(self):
        with self._thread_lock:
            return copy.deepcopy(self.data)

    def update(self, callback):
        with self._thread_lock:
            proposed = copy.deepcopy(self.data)
            result = callback(proposed)
            proposed["sequence"] += 1
            proposed = json.loads(json.dumps(proposed, allow_nan=False))
            self._write(proposed)
            self.data = proposed
            return copy.deepcopy(result)

    def close(self):
        self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
