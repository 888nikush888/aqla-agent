import zipfile
from pathlib import Path

from aqla_agent.release import build


def test_release_archive_has_source_and_deployment_assets_but_no_local_state_or_credentials(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = build(root, tmp_path / "aqla-vps.zip")
    with zipfile.ZipFile(result["archive"]) as archive:
        names = archive.namelist()
        assert "aqla-agent/README.md" in names
        assert "aqla-agent/skills/aqla-master/SKILL.md" in names
        assert "aqla-agent/docs/api-coverage.md" in names
        assert "aqla-agent/scripts/verify_artifacts.py" in names
        assert "aqla-agent/hermes-runtime-overrides.txt" in names
        assert not any(".cache/" in name or ".venv/" in name or "data/state" in name or name.endswith((".secret", ".session", ".env")) for name in names)
    assert len(result["sha256"]) == 64
