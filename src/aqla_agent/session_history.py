"""One-time atomic import of legacy Aqla JSONL into native Hermes SessionDB."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .conversation import Conversations


def migrate_session_history(home: Path, *, db=None):
    home = Path(home).resolve()
    directory = home / "sessions" / "transcripts"
    if not directory.is_dir():
        return {"imported": 0, "skipped": 0, "messages": 0}
    own_db = db is None
    if own_db:
        from hermes_state import SessionDB
        db = SessionDB(db_path=home / "state.db")
    try:
        db._execute_write(lambda connection: connection.execute(
            "CREATE TABLE IF NOT EXISTS aqla_jsonl_imports (session_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, imported_messages INTEGER NOT NULL)"))
        routing_path = home / "sessions" / "sessions.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8")) if routing_path.is_file() else {}
        entries = {value["session_id"]: value for value in routing.values()
                   if isinstance(value, dict) and value.get("session_id")}
        reader = Conversations(directory)
        result = {"imported": 0, "skipped": 0, "messages": 0}
        for path in sorted(directory.glob("*.jsonl")):
            session_id = path.stem
            # The old adapter's filename contract also excludes path traversal.
            reader.path(session_id)
            if db._read_one("SELECT 1 FROM aqla_jsonl_imports WHERE session_id = ?", (session_id,)):
                result["skipped"] += 1
                continue
            rows = reader.load(session_id)
            for row in rows:
                if not isinstance(row, dict) or row.get("role") not in ("user", "assistant", "tool", "system"):
                    raise ValueError("Invalid legacy transcript; original file is preserved")
                if row.get("message_id") is not None and row.get("platform_message_id") is None:
                    row["platform_message_id"] = str(row["message_id"])
            entry = entries.get(session_id, {})
            source = entry.get("source") or {}
            started = entry.get("created_at")
            if isinstance(started, str):
                started = datetime.fromisoformat(started).timestamp()
            payload = {"id": session_id, "source": source.get("platform", "telegram"),
                       "started_at": started, "chat_id": str(source.get("chat_id", "")),
                       "user_id": str(source.get("user_id", "")), "title": "Aqla Telegram-Verlauf",
                       "messages": rows}
            normalized, errors = db._validate_import_payload([payload])
            if errors:
                raise ValueError("Legacy history failed native import validation; original file is preserved")
            item = normalized[0]
            fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            count = len(rows)

            def import_one(connection, session_id=session_id, item=item, fingerprint=fingerprint, count=count):
                if connection.execute("SELECT 1 FROM aqla_jsonl_imports WHERE session_id = ?", (session_id,)).fetchone():
                    return False
                exists = connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not exists:
                    db._import_session_row(connection, item["session"], item["messages"], session_id)
                # Same transaction as message import; also prevents resurrection after user deletion.
                connection.execute("INSERT INTO aqla_jsonl_imports VALUES (?, ?, ?)",
                                   (session_id, fingerprint, count if not exists else 0))
                return not exists

            imported = db._execute_write(import_one)
            result["imported" if imported else "skipped"] += 1
            if imported:
                result["messages"] += len(rows)
        return result
    finally:
        if own_db:
            db.close()
