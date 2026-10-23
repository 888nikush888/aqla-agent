"""One-time atomic import of legacy Aqla JSONL into native Hermes SessionDB."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal

from typing_extensions import TypedDict

from .conversation import Conversations

HistoryCode = Literal["json_decode", "metadata", "role", "timestamp", "native_validation", "routing_metadata", "identity", "read_failed", "native_validation_unavailable"]


class HistoryIssue(TypedDict):
    path: str
    line: int
    code: HistoryCode
    repair_hint: str


class HistoryDiagnosis(TypedDict):
    files: int
    valid: int
    issues: list[HistoryIssue]


class HistoryImportError(ValueError):
    def __init__(self, issue: HistoryIssue):
        self.issue = issue
        super().__init__(f"Legacy history {issue['path']}:{issue['line']} ({issue['code']}); original file preserved")


def _issue(path: Path, line: int, code: HistoryCode) -> HistoryIssue:
    return {"path": str(path), "line": line, "code": code,
            "repair_hint": "Repair the indicated original JSON/metadata record offline, keep a backup, then repeat diagnosis; do not delete import receipts."}


def _routing_entries(home: Path):
    path = home / "sessions/sessions.json"
    if not path.is_file():
        return {}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise HistoryImportError(_issue(path, error.lineno, "routing_metadata")) from None
    except (OSError, UnicodeError):
        raise HistoryImportError(_issue(path, 1, "read_failed")) from None
    # The pinned SessionStore writes only this reserved informational sentinel.
    # Do not treat arbitrary underscore-prefixed data as valid session metadata.
    if isinstance(values, dict):
        if any(key.startswith("_") and (key != "_README" or not isinstance(value, str)) for key, value in values.items()):
            raise HistoryImportError(_issue(path, 1, "routing_metadata"))
        values = {key: value for key, value in values.items() if key != "_README"}
    if not isinstance(values, dict) or any(not isinstance(value, dict) or not isinstance(value.get("session_id"), str) or
                                          value.get("source") is not None and not isinstance(value["source"], dict) for value in values.values()):
        raise HistoryImportError(_issue(path, 1, "routing_metadata"))
    for value in values.values():
        _timestamp(value.get("created_at"), path, 1)
    return {value["session_id"]: value for value in values.values()}


def _timestamp(value, path: Path, line: int):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                value = datetime.fromisoformat(value).timestamp()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("timestamp")
        return value
    except (ValueError, OverflowError, OSError):
        raise HistoryImportError(_issue(path, line, "timestamp")) from None


def _history_payload(path: Path, entries):
    if not path.stem or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in path.stem):
        raise HistoryImportError(_issue(path, 1, "identity"))
    try:
        original = path.read_bytes()
        lines = original.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        raise HistoryImportError(_issue(path, 1, "read_failed")) from None
    rows, locations = [], []
    for number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            raise HistoryImportError(_issue(path, number, "json_decode")) from None
        rows.append(row)
        locations.append(number)
    header = {}
    if rows and isinstance(rows[0], dict) and rows[0].get("role") == "session_meta":
        header = rows.pop(0)
        line = locations.pop(0)
        if set(header) - {"role", "tools", "model", "platform", "timestamp"}:
            raise HistoryImportError(_issue(path, line, "metadata"))
        if any(header.get(name) is not None and not isinstance(header[name], str) for name in ("model", "platform")) or (
            header.get("tools") is not None and (not isinstance(header["tools"], list) or any(not isinstance(tool, str) for tool in header["tools"]))
        ):
            raise HistoryImportError(_issue(path, line, "metadata"))
        _timestamp(header.get("timestamp"), path, line)
    for row, line in zip(rows, locations, strict=True):
        if not isinstance(row, dict) or row.get("role") not in ("user", "assistant", "tool", "system"):
            raise HistoryImportError(_issue(path, line, "role"))
        _timestamp(row.get("timestamp"), path, line)
        if row.get("message_id") is not None and row.get("platform_message_id") is None:
            row["platform_message_id"] = str(row["message_id"])
    entry = entries.get(path.stem, {})
    source = entry.get("source") or {}
    started = _timestamp(entry.get("created_at") or header.get("timestamp"), path, 1)
    payload = {"id": path.stem, "source": source.get("platform") or header.get("platform") or "telegram",
               "started_at": started, "chat_id": str(source.get("chat_id", "")), "user_id": str(source.get("user_id", "")),
               "title": "Aqla Telegram-Verlauf", "model": header.get("model"), "messages": rows}
    return payload, header, original, locations


def _validate_native(payload, path: Path, locations: list[int], validator):
    normalized, errors = validator([payload])
    if errors:
        # Native errors may include message bodies or tool arguments. Use only
        # their presence and locate a rejected row by another read-only check.
        line = 1
        for row, location in zip(payload["messages"], locations, strict=True):
            _, row_errors = validator([{**payload, "messages": [row]}])
            if row_errors:
                line = location
                break
        raise HistoryImportError(_issue(path, line, "native_validation"))
    return normalized


def diagnose_session_history(home: Path, *, validator=None) -> HistoryDiagnosis:
    """Inspect every original file and routing record without opening/writing a DB."""
    home = Path(home).resolve()
    result: HistoryDiagnosis = {"files": 0, "valid": 0, "issues": []}
    try:
        entries = _routing_entries(home)
    except HistoryImportError as error:
        entries = {}
        result["issues"].append(error.issue)
    if validator is None:
        try:
            from hermes_state import SessionDB
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            validator = object.__new__(SessionDB)._validate_import_payload
        except ImportError:
            result["issues"].append(_issue(home, 0, "native_validation_unavailable"))
    for path in sorted((home / "sessions/transcripts").glob("*.jsonl")):
        result["files"] += 1
        try:
            payload, _, _, locations = _history_payload(path, entries)
            if validator is not None:
                _validate_native(payload, path, locations, validator)
            result["valid"] += 1
        except HistoryImportError as error:
            result["issues"].append(error.issue)
    return result


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
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        db._execute_write(lambda connection: connection.execute(
            "CREATE TABLE IF NOT EXISTS aqla_jsonl_imports (session_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, imported_messages INTEGER NOT NULL, source_metadata TEXT NOT NULL DEFAULT '{}')"))
        # Pinned native integration interface; covered by native contract and integration checks.
        # skipcq: PYL-W0212
        if "source_metadata" not in {row[1] for row in db._read_all("PRAGMA table_info(aqla_jsonl_imports)")}:
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            db._execute_write(lambda connection: connection.execute(
                "ALTER TABLE aqla_jsonl_imports ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}'"))
        entries = _routing_entries(home)
        reader = Conversations(directory)
        result = {"imported": 0, "skipped": 0, "messages": 0}
        for path in sorted(directory.glob("*.jsonl")):
            session_id = path.stem
            # The old adapter's filename contract also excludes path traversal.
            reader.path(session_id)
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            if db._read_one("SELECT 1 FROM aqla_jsonl_imports WHERE session_id = ?", (session_id,)):
                result["skipped"] += 1
                continue
            payload, header, original, locations = _history_payload(path, entries)
            rows = payload["messages"]
            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            normalized = _validate_native(payload, path, locations, db._validate_import_payload)
            item = normalized[0]
            fingerprint = hashlib.sha256(original).hexdigest()
            count = len(rows)
            metadata = json.dumps(header, ensure_ascii=False, allow_nan=False)

            def import_one(connection, session_id=session_id, item=item, fingerprint=fingerprint, count=count, metadata=metadata):
                if connection.execute("SELECT 1 FROM aqla_jsonl_imports WHERE session_id = ?", (session_id,)).fetchone():
                    return False
                exists = connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if not exists:
                    # Pinned native integration interface; covered by native contract and integration checks.
                    # skipcq: PYL-W0212
                    db._import_session_row(connection, item["session"], item["messages"], session_id)
                # Same transaction as message import; also prevents resurrection after user deletion.
                connection.execute("INSERT INTO aqla_jsonl_imports (session_id,source_sha256,imported_messages,source_metadata) VALUES (?, ?, ?, ?)",
                                   (session_id, fingerprint, count if not exists else 0, metadata))
                return not exists

            # Pinned native integration interface; covered by native contract and integration checks.
            # skipcq: PYL-W0212
            imported = db._execute_write(import_one)
            result["imported" if imported else "skipped"] += 1
            if imported:
                result["messages"] += len(rows)
        return result
    finally:
        if own_db:
            db.close()


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Read-only offline diagnosis of original legacy Hermes history")
    parser.add_argument("--home", required=True)
    parser.add_argument("--hermes-source")
    parser.add_argument("--diagnose", action="store_true", required=True)
    arguments = parser.parse_args()
    if arguments.hermes_source:
        sys.path.insert(0, str(Path(arguments.hermes_source).resolve()))
    result = diagnose_session_history(Path(arguments.home))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result["issues"] else 0)


if __name__ == "__main__":
    main()
