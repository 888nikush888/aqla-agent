import json

import pytest

from aqla_agent.session_history import HistoryImportError, diagnose_session_history, migrate_session_history


def routing_home(tmp_path, values):
    directory = tmp_path / "sessions/transcripts"
    directory.mkdir(parents=True)
    (directory / "synthetic_owner.jsonl").write_text('{"role":"user","content":"synthetic native history"}\n', encoding="utf-8")
    path = tmp_path / "sessions/sessions.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_known_native_readme_string_is_not_a_session_or_a_routing_error(tmp_path):
    path = routing_home(tmp_path, {"_README": "synthetic native mirror metadata", "synthetic_route": {
        "session_id": "synthetic_owner", "created_at": "2026-10-09T00:00:00Z", "origin": {"platform": "telegram"}}})
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    result = diagnose_session_history(tmp_path, validator=lambda values: (values, []))
    assert result == {"files": 1, "valid": 1, "issues": []}
    assert path.read_bytes() == before[path]
    assert before == {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("key,value", [("_README", None), ("_README", 1), ("_README", True), ("_README", []),
    ("_README", {"session_id": "synthetic_owner"}), ("_UNKNOWN", "synthetic private metadata"),
    ("_readme", "synthetic private metadata"), ("_UNKNOWN", {"session_id": "synthetic_owner"})])
def test_only_known_native_reserved_metadata_with_string_type_is_accepted(tmp_path, key, value):
    path = routing_home(tmp_path, {key: value})
    before = path.read_bytes()
    result = diagnose_session_history(tmp_path, validator=lambda values: (values, []))
    assert len(result["issues"]) == 1
    assert result["issues"][0]["path"] == str(path) and result["issues"][0]["line"] == 1
    assert result["issues"][0]["code"] == "routing_metadata"
    assert "synthetic private metadata" not in json.dumps(result)
    assert path.read_bytes() == before and not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("entry,code", [("invalid-session", "routing_metadata"), ({"session_id": None}, "routing_metadata"),
    ({"session_id": "synthetic_owner", "source": []}, "routing_metadata"),
    ({"session_id": "synthetic_owner", "created_at": "invalid-time"}, "timestamp")])
def test_native_readme_does_not_hide_invalid_session_records(tmp_path, entry, code):
    path = routing_home(tmp_path, {"_README": "synthetic native metadata", "synthetic_route": entry})
    before = path.read_bytes()
    result = diagnose_session_history(tmp_path, validator=lambda values: (values, []))
    assert result["issues"][0]["path"] == str(path) and result["issues"][0]["line"] == 1
    assert result["issues"][0]["code"] == code
    assert "invalid-session" not in json.dumps(result) and "invalid-time" not in json.dumps(result)
    assert path.read_bytes() == before and not (tmp_path / "state.db").exists()


@pytest.mark.parametrize("record,code", [("{secret-value", "json_decode"), ('{"role":"bad","content":"secret-value"}', "role"),
                                        ('{"role":"session_meta","secret-value":"content"}', "metadata"),
                                        ('{"role":"user","timestamp":"secret-value","content":"hidden"}', "timestamp")])
def test_history_diagnosis_reports_all_files_exact_line_without_contents_or_dbwrite(tmp_path, record, code):
    directory = tmp_path / "sessions/transcripts"
    directory.mkdir(parents=True)
    good = directory / "good.jsonl"
    bad = directory / "bad.jsonl"
    good.write_text('{"role":"user","content":"safe synthetic body"}\n', encoding="utf-8")
    bad.write_text('\n' + record + '\n', encoding="utf-8")
    before = {path: path.read_bytes() for path in directory.iterdir()}
    result = diagnose_session_history(tmp_path, validator=lambda values: (values, []))
    assert result["files"] == 2 and result["valid"] == 1
    assert result["issues"][0]["path"] == str(bad) and result["issues"][0]["line"] == 2 and result["issues"][0]["code"] == code
    assert "secret-value" not in json.dumps(result) and "safe synthetic body" not in json.dumps(result)
    assert before == {path: path.read_bytes() for path in directory.iterdir()}
    assert not (tmp_path / "state.db").exists()


def test_history_diagnosis_reports_routing_and_native_validation_failures(tmp_path):
    directory = tmp_path / "sessions/transcripts"
    directory.mkdir(parents=True)
    (tmp_path / "sessions/sessions.json").write_text('{invalid secret', encoding="utf-8")
    (directory / "session.jsonl").write_text('{"role":"user","content":"secret"}\n', encoding="utf-8")
    result = diagnose_session_history(tmp_path, validator=lambda values: ([], [{"error": "secret details"}]))
    assert [issue["code"] for issue in result["issues"]] == ["routing_metadata", "native_validation"]
    assert "secret" not in json.dumps(result)


def test_invalid_session_is_not_partially_imported_or_silently_skipped(tmp_path):
    directory = tmp_path / "sessions/transcripts"
    directory.mkdir(parents=True)
    path = directory / "session.jsonl"
    path.write_text('{"role":"user","content":"first"}\n{invalid secret\n', encoding="utf-8")
    before = path.read_bytes()
    class DB:
        def _execute_write(self, callback):
            class Connection:
                def execute(self, *args):
                    return None
            return callback(Connection())
        def _read_all(self, *args):
            return [(0, "source_metadata")]
        def _read_one(self, *args):
            return None
        def _validate_import_payload(self, *args):
            pytest.fail("Invalid file reached native import validation")
    with pytest.raises(HistoryImportError) as failure:
        migrate_session_history(tmp_path, db=DB())
    assert failure.value.issue["line"] == 2 and failure.value.issue["code"] == "json_decode"
    assert "secret" not in str(failure.value) and path.read_bytes() == before


def test_native_validation_diagnosis_locates_message_line_without_echoing_error(tmp_path):
    directory = tmp_path / "sessions/transcripts"
    directory.mkdir(parents=True)
    (directory / "session.jsonl").write_text('\n{"role":"user","content":"valid","timestamp":"1791397511"}\n\n{"role":"assistant","content":7}\n', encoding="utf-8")
    def validate(payloads):
        return ([], [{"error": "synthetic-private-body"}]) if any(type(row["content"]) is int for row in payloads[0]["messages"]) else (payloads, [])
    result = diagnose_session_history(tmp_path, validator=validate)
    assert result["issues"][0]["line"] == 4 and result["issues"][0]["code"] == "native_validation"
    assert "synthetic-private-body" not in json.dumps(result)
