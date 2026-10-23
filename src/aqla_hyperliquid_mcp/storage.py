"""Small transactional SQLite store; JSON is the stable application data contract."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import AqlaError


def database_path(path: Path) -> Path:
    path = Path(path).resolve()
    return path.with_suffix(".sqlite3") if path.suffix.lower() == ".json" else path


def read_snapshot(path: Path) -> dict:
    """Read without acquiring the writer lease or modifying the database."""
    path = database_path(path)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise AqlaError("Unsupported SQLite state format; execution remains blocked")
            row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
            if row is None:
                raise AqlaError("SQLite state is incomplete; do not restore an old snapshot blindly")
            data = json.loads(row[0])
            if not isinstance(data, dict):
                raise AqlaError("SQLite state is not an object")
            return data
    except (sqlite3.Error, json.JSONDecodeError) as error:
        raise AqlaError("SQLite state cannot be read; execution remains blocked") from error


class SQLiteStore:
    def __init__(self, path: Path, *, create: bool):
        self.path = path
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        try:
            # A single writer needs no WAL. DELETE also avoids the WAL-reset bug
            # in SQLite versions bundled with some supported Python releases.
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            if create:
                with self.connection:
                    self.connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL CHECK(json_valid(body)))")
                    self.connection.execute("PRAGMA user_version=1")
                    for collection in ("trades", "operations", "events", "profiles", "pipelines"):
                        self.connection.execute(f"CREATE VIEW {collection} AS SELECT key AS id, value AS body FROM state, json_each(state.body, '$.{collection}')")
        except BaseException:
            self.connection.close()
            raise

    def save(self, data: dict):
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            with self.connection:
                self.connection.execute("INSERT INTO state(id,body) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (payload,))
        except sqlite3.Error as error:
            raise AqlaError("Durable state write failed; no exchange action may be sent") from error

    def save_roots(self, changed: dict):
        """Patch metadata in the same FULL transaction without serializing financial history."""
        arguments: list[str] = []
        values: list[str] = []
        for name, value in changed.items():
            arguments.append("?,json(?)")
            values.extend(("$." + json.dumps(name), json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)))
        try:
            with self.connection:
                # Only placeholder pairs vary; all JSON paths and values are bound.
                updated = self.connection.execute("UPDATE state SET body=json_set(body," + ",".join(arguments) + ") WHERE id=1", values)  # skipcq: BAN-B608
                if updated.rowcount != 1:
                    raise sqlite3.DatabaseError("Authoritative state row is missing")
        except sqlite3.Error as error:
            raise AqlaError("Durable state write failed; no exchange action may be sent") from error

    def seal(self):
        # Survives accidental DB deletion. Never resurrect stale orders from the
        # retained JSON import or create an empty trading account on restart.
        marker = self.path.with_suffix(self.path.suffix + ".initialized")
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("SQLite is authoritative. Missing database requires explicit recovery.\n")
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def close(self):
        self.connection.close()
