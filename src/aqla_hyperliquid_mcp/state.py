from __future__ import annotations

import copy
import json
import threading

import portalocker

from .models import AqlaError, Settings
from .storage import SQLiteStore, database_path, read_snapshot

METADATA_ROOTS = {"nonces": dict, "account_nonces": dict, "venue_nonces": dict, "cursors": dict,
                  "notifications": dict, "monthly_jobs": dict, "monthly_requests": list,
                  "monthly_request_generations": dict, "monthly_reports": dict,
                  "source_jobs": dict, "notification_outbox": dict, "source_status": dict}


class State:
    """One writer and one transactional SQLite state, with legacy JSON import."""

    def __init__(self, settings: Settings):
        self.path = database_path(settings.state_path)
        self._store = None
        self.journal_enabled = settings.google_sheets.enabled
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._lock = portalocker.Lock(str(self.path) + ".lock", timeout=0)
        try:
            self._lock.acquire()
        except portalocker.LockException as error:
            raise AqlaError("Another aqla process already owns this state") from error
        try:
            existing = self.path.exists()
            legacy_path = self.path.with_suffix(".json")
            if not existing and self.path.with_suffix(self.path.suffix + ".initialized").exists():
                raise AqlaError("Previously initialized SQLite state is missing; explicit recovery is required")
            if existing or legacy_path.exists():
                self.data = read_snapshot(self.path) if existing else json.loads(legacy_path.read_text(encoding="utf-8"))
                if not isinstance(self.data, dict) or self.data.get("schema") not in (1, 2, 3) or self.data.get("network") != settings.network or self.data.get("account", "").lower() != settings.account.lower():
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
                if plan.get("exchange", "hyperliquid") != "hyperliquid" or plan.get("account_key") not in (None, "hyperliquid:" + settings.network):
                    continue
                if plan["account"].lower() != target or plan["network"] != settings.network or plan.get("principal") not in (None, settings.account.lower()):
                    raise AqlaError("Saved plan belongs to another trading target or network")
            for row in self.data["operations"].values():
                if row.get("exchange", "hyperliquid") == "hyperliquid" and row.get("account_key") in (None, "hyperliquid:" + settings.network):
                    self.assert_operation_binding(settings, row)
            from aqla_agent.pipelines import migrate

            had_ranking = "ranking_policy" in self.data
            migrated = migrate(self.data)
            if settings.ranking_enabled and not had_ranking:
                self.data["ranking_policy"]["enabled"] = True
                migrated = True
            if not existing and not legacy_path.exists():
                for row in self.data["source_ranks"].values():
                    row.update(tier="F", bootstrap=False)
            if self.journal_enabled:
                from aqla_sheets_mcp.journal import seed
                seed(self.data)
            self._store = SQLiteStore(self.path, create=not existing)
            if not existing or migrated or legacy or self.journal_enabled:
                self._write(self.data)
            self._store.seal()
            from aqla_agent.source_queue import repair_queue
            self.update_if_changed(repair_queue)
        except BaseException:
            if self._store is not None:
                self._store.close()
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
        self._store.save(data)

    def _initialize_journal_roots(self, proposed):
        if self.journal_enabled:
            proposed.setdefault("journal_pending", {})
            proposed.setdefault("journal_history", {})

    def snapshot(self):
        with self._thread_lock:
            return copy.deepcopy(self.data)

    def presentation_snapshot(self, selection=None):
        """Fixed bounded projection; source text/history never leaves the state lock."""
        from aqla_agent.presentation import build_presentation
        with self._thread_lock:
            result = build_presentation(self.data, selection)
            return copy.deepcopy(result.model_dump(mode="json"))

    def read(self, *roots, entries=None):
        """Return detached selected roots/entries, never a callback over live state."""
        entries = entries or {}
        if any(not isinstance(root, str) for root in roots) or not set(entries) <= set(roots):
            raise AqlaError("State read requires named roots and matching entry selectors")
        with self._thread_lock:
            result = {}
            for root in roots:
                if root not in self.data:
                    continue
                value = self.data[root]
                if root in entries:
                    if not isinstance(value, dict):
                        raise AqlaError("Selected state entries require a dictionary root")
                    value = {key: value[key] for key in entries[root] if key in value}
                result[root] = value
            return copy.deepcopy(result)

    def has_entry(self, root, key):
        with self._thread_lock:
            value = self.data.get(root, {})
            if not isinstance(value, dict):
                raise AqlaError("State membership requires a dictionary root")
            return key in value

    def update_metadata(self, roots, callback):
        """Persist detached allowlisted metadata, preserving the generic journal path."""
        roots = tuple(roots)
        if not roots or len(set(roots)) != len(roots) or any(root not in METADATA_ROOTS for root in roots):
            raise AqlaError("Metadata transaction includes an unknown or financial state root")
        with self._thread_lock:
            part = {root: copy.deepcopy(self.data.get(root, METADATA_ROOTS[root]())) for root in roots}
            result = callback(part)
            if set(part) != set(roots) or any(not isinstance(part[root], METADATA_ROOTS[root]) for root in roots):
                raise AqlaError("Metadata transaction changed undeclared roots or root types")
            normalized = json.loads(json.dumps(part, allow_nan=False))
            proposed = {**self.data, **normalized, "sequence": self.data["sequence"] + 1}
            self._initialize_journal_roots(proposed)
            self._write(proposed)
            self.data = proposed
            return copy.deepcopy(result)

    def journal_batch(self, limit=100):
        from aqla_sheets_mcp.journal import batch

        with self._thread_lock:
            return copy.deepcopy(batch(self.data, limit))

    def notification_page(self, key, *, cursor=None, limit=100, view="full"):
        from aqla_agent.notifications import notification_page

        with self._thread_lock:
            return copy.deepcopy(notification_page(self.data, key, {}, cursor=cursor, limit=limit, view=view))

    def notification_details(self, key, identity, version, *, cursor=None, limit=16384):
        from aqla_agent.notifications import notification_details

        with self._thread_lock:
            return copy.deepcopy(notification_details(self.data, key, identity, version, cursor=cursor, limit=limit))

    def reader_snapshot(self, max_context_messages):
        """Fixed read-only projection; no full state/body snapshot leaves the lock."""
        from .projections import reader_overview

        with self._thread_lock:
            return copy.deepcopy(reader_overview(self.data, max_context_messages))

    def acknowledge_journal(self, accepted):
        """The only partial path allowed to release digest-confirmed history rows."""
        from aqla_sheets_mcp.journal import acknowledge

        if not isinstance(accepted, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in accepted.items()):
            raise AqlaError("Journal acknowledgements require record keys and fingerprint strings")
        with self._thread_lock:
            proposed = dict(self.data)
            for root in ("journal_pending", "journal_history"):
                if root in self.data:
                    proposed[root] = dict(self.data[root])
            result = acknowledge(proposed, accepted)
            proposed["sequence"] = self.data["sequence"] + 1
            self._initialize_journal_roots(proposed)
            self._write(proposed)
            self.data = proposed
            return copy.deepcopy(result)

    def monthly_transaction(self, action, **arguments):
        """Fixed job operations inspect current financial evidence under the lock."""
        from aqla_agent.monthly import (
            acknowledge_publication,
            fail_publication,
            monthly_page,
            next_job,
            request_publication,
        )

        functions = {"job": next_job, "page": monthly_page, "failure": fail_publication,
                     "request": request_publication, "ack": acknowledge_publication}
        if action not in functions:
            raise AqlaError("Unknown partial monthly job operation")
        roots = ("monthly_jobs", "monthly_requests", "monthly_request_generations")
        if action == "ack":
            roots += ("monthly_reports", "notifications")
        with self._thread_lock:
            proposed = dict(self.data)
            for root in roots:
                if root in proposed:
                    proposed[root] = copy.deepcopy(proposed[root])
            # These reviewed builtins only read financial roots and mutate job roots.
            # No arbitrary callback receives shared financial data through this API.
            result = functions[action](proposed, **copy.deepcopy(arguments))
            changed = {root: proposed[root] for root in roots if root in proposed}
            normalized = json.loads(json.dumps(changed, allow_nan=False))
            proposed.update(normalized)
            proposed["sequence"] = self.data["sequence"] + 1
            self._initialize_journal_roots(proposed)
            self._write(proposed)
            self.data = proposed
            return copy.deepcopy(result)

    def update(self, callback):
        return self._update(callback, conditional=False)

    def update_if_changed(self, callback):
        """Generic journal-safe update that preserves explicit revision/order changes."""
        return self._update(callback, conditional=True)

    def _update(self, callback, *, conditional):
        with self._thread_lock:
            proposed = copy.deepcopy(self.data)
            result = callback(proposed)
            if conditional and json.dumps(proposed, allow_nan=False) == json.dumps(self.data, allow_nan=False):
                return copy.deepcopy(result)
            proposed["sequence"] += 1
            if self.journal_enabled:
                from aqla_sheets_mcp.journal import enqueue_changes
                enqueue_changes(self.data, proposed)
            proposed = json.loads(json.dumps(proposed, allow_nan=False))
            self._write(proposed)
            self.data = proposed
            return copy.deepcopy(result)

    def close(self):
        with self._thread_lock:
            if self._store is not None:
                self._store.close()
                self._store = None
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
