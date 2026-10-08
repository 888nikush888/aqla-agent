import json

import pytest

from aqla_agent.session_history import HistoryImportError, diagnose_session_history, migrate_session_history


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
