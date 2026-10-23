"""Optional offline scanner check; synthetic credentials never reach an external service."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_public_marker_exception_keeps_other_paths_and_api_keys_detectable(tmp_path):
    binary = os.environ.get("GITLEAKS_BINARY") or shutil.which("gitleaks")
    if not binary:
        pytest.skip("gitleaks executable is not configured")
    config = Path(__file__).resolve().parents[1] / ".gitleaks.toml"
    target = tmp_path / "source" / "src" / "aqla_sheets_mcp" / "presentation.py"
    target.parent.mkdir(parents=True)
    public_marker = ".".join(("aqla", "presentation", "v1"))
    marker_assignment = 'META_KEY = "' + public_marker + '"\n'
    target.write_text(marker_assignment, encoding="utf-8")
    report = tmp_path / "redacted-report.json"
    command = [binary, "dir", str(tmp_path / "source"), "--config", str(config), "--redact=100",
               "--report-format", "json", "--report-path", str(report), "--no-banner", "--no-color"]
    clean = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert clean.returncode == 0 and json.loads(report.read_text()) == []
    synthetic = base64.urlsafe_b64encode(hashlib.sha256(b"aqla scanner synthetic credential").digest()).decode()
    target.write_text(marker_assignment + '\nAPI_KEY = "' + synthetic + '"\n', encoding="utf-8")
    other = target.with_name("other.py")
    other.write_text(marker_assignment, encoding="utf-8")
    detected = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert detected.returncode == 1
    raw = report.read_text()
    if synthetic in raw + detected.stdout + detected.stderr:
        pytest.fail("Scanner output did not fully redact the synthetic credential")
    findings = json.loads(raw)
    assert any(Path(row["File"]) == target and row["RuleID"] == "generic-api-key" for row in findings)
    assert any(Path(row["File"]) == other for row in findings)
    assert all(row["Secret"] == "REDACTED" for row in findings)
