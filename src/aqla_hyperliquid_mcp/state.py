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
        self.journal_enabled = settings.google_sheets.enabled
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
                self.data = {"schema": 1, "network": settings.network, "account": settings.account.lower(), "trading_account": (settings.vault_address or settings.account).lower(), "sequence": 0,
                             "policy": settings.policy.model_dump(mode="json") if settings.policy else None,
                             "policy_revision": 1 if settings.policy else 0, "paused": False,
                             "sources": settings.sources, "source_aliases": {}, "events": {}, "trades": {}, "plans": {},
                             "bindings": {}, "operations": {}, "nonces": {}, "cursors": {}, "notifications": {}, "event_tombstones": {}}
                self._write(self.data)
            target = (settings.vault_address or settings.account).lower()
            legacy = "trading_account" not in self.data
            if legacy:
                # Legacy state can inherit only a target proved by its own execution records.
                targets = {row["plan"]["account"].lower() for row in self.data["plans"].values()}
                targets.update(row["plan"]["account"].lower() for row in self.data["trades"].values())
                targets.update(row["request"]["account"].lower() for row in self.data["operations"].values()
                               if row.get("request", {}).get("account") and not row["request"].get("leader"))
                inferred = next(iter(targets)) if len(targets) == 1 else settings.account.lower() if not targets else None
                if inferred != target:
                    raise AqlaError("Legacy execution state belongs to another or ambiguous trading target")
                self.data["trading_account"] = inferred
            self.assert_binding(settings)
            for row in [*self.data["plans"].values(), *self.data["trades"].values()]:
                plan = row["plan"]
                if plan["account"].lower() != target or plan["network"] != settings.network or plan.get("principal") not in (None, settings.account.lower()):
                    raise AqlaError("Saved plan belongs to another trading target or network")
            for row in self.data["operations"].values():
                self.assert_operation_binding(settings, row)
            if legacy:
                self._write(self.data)
            if self.journal_enabled:
                from aqla_sheets_mcp.journal import seed
                seed(self.data)
                self._write(self.data)
        except BaseException:
            self._lock.release()
            raise

    def assert_binding(self, settings):
        with self._thread_lock:
            if (self.data["network"] != settings.network or self.data["account"].lower() != settings.account.lower()
                    or self.data["trading_account"] != (settings.vault_address or settings.account).lower()):
                raise AqlaError("State principal, trading target or network differs; use a separate state")

    def assert_operation_binding(self, settings, record):
        self.assert_binding(settings)
        request = record["request"]
        if (request.get("network") != settings.network
                or not request.get("leader") and request.get("account", "").lower() != self.data["trading_account"]):
            raise AqlaError("Saved operation belongs to another trading target or network")
        if request.get("operation"):
            # Import locally: the API uses State, but replay validation needs the
            # same explicit SDK contract map without a second mutable registry.
            from .api import sdk_signing_scope
            scope = sdk_signing_scope(request["operation"])
            if "signing_scope" not in request:
                if scope == "principal" and settings.vault_address:
                    raise AqlaError("Legacy principal SDK operation has ambiguous vault signing scope")
                return
            expected_vault = settings.vault_address if scope == "trading" else None
            if (request["signing_scope"] != scope or request.get("principal") != settings.account.lower()
                    or "wire_vault_address" not in record or record["wire_vault_address"] != expected_vault):
                raise AqlaError("Saved SDK operation has mismatched principal or signing scope")

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
            if self.journal_enabled:
                from aqla_sheets_mcp.journal import enqueue_changes
                enqueue_changes(self.data, proposed)
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
