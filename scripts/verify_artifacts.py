"""Verify shipping contents, source completeness and the VPS archive checksum."""
import hashlib
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aqla_agent.artifact_rules import (  # noqa: E402
    forbidden,
    validate_member,
    validate_tar_members,
    validate_zip_members,
)


def verify_names(names):
    for name in names:
        validate_member(name)
        relative = name.split("/", 1)[1] if "/" in name else name
        if forbidden(relative):
            raise SystemExit("Forbidden artifact member: " + name)


def main():
    dist = Path("dist")
    bundle = dist / "aqla-agent-vps.zip"
    expected = bundle.with_suffix(".zip.sha256").read_text(encoding="utf-8")
    if expected != f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n":
        raise SystemExit("VPS archive checksum mismatch")
    with zipfile.ZipFile(bundle) as archive:
        validate_zip_members(archive)
        if archive.testzip() is not None:
            raise SystemExit("VPS archive integrity failed")
        names = archive.namelist()
        verify_names(names)
        if "aqla-agent/src/aqla_agent/artifact_rules.py" not in names:
            raise SystemExit("Missing VPS artifact path rules")
        for required in ("README.md", "uv.lock", "config.example.yaml", "hermes-runtime-overrides.txt", "src/aqla_agent/install.py", "src/aqla_agent/session_history.py", "src/aqla_agent/sources.py", "src/aqla_hyperliquid_mcp/storage.py", "src/aqla_sheets_mcp/server.py", "src/aqla_sheets_mcp/parameters.py", "docs/google-sheets.md", "docs/hermes-tools-and-storage.md", "docs/gold-backup-and-names.md", "skills/aqla-master/SKILL.md", "hermes_plugin/aqla-events/plugin.yaml", "scripts/verify_artifacts.py", "tests/native_pipeline_smoke.py", "tests/native_lifecycle_smoke.py", "tests/native_interaction_smoke.py"):
            if "aqla-agent/" + required not in names:
                raise SystemExit("Missing VPS asset: " + required)
        if len(archive.read("aqla-agent/skills/aqla-master/SKILL.md")) > 8192:
            raise SystemExit("Master skill exceeds its size budget")
        for required in ("src/aqla_agent/messages.py", "tests/native_message_smoke.py", "docs/telegram-messages.md"):
            if "aqla-agent/" + required not in names:
                raise SystemExit("Missing Telegram view asset: " + required)
        for required in ("src/aqla_agent/accounting.py", "src/aqla_agent/network_transition.py", "docs/mainnet-shared-account.md", "tests/test_shared_account.py"):
            if "aqla-agent/" + required not in names:
                raise SystemExit("Missing shared-account asset: " + required)
        if len(json.loads(archive.read("aqla-agent/tests/fixtures/context_cases.json"))) != 30:
            raise SystemExit("Context evaluation fixtures are missing")
        with TemporaryDirectory() as target:
            archive.extractall(target)
            root = Path(target) / "aqla-agent"
            from aqla_agent.install import prepare
            prepare(root, Path(target) / "deployment", "12345", "ci-only", "https://example.invalid/v1")
            from subprocess import run
            run([sys.executable, "-m", "ruff", "check", "src", "tests", "hermes_plugin", "scripts"], cwd=root, check=True)
    wheels = list(dist.glob("*.whl"))
    sources = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        validate_zip_members(archive)
        verify_names(archive.namelist())
        if "aqla_agent/artifact_rules.py" not in archive.namelist():
            raise SystemExit("Wheel is missing artifact path rules")
        for asset in ("aqla_hyperliquid_mcp/info_specs.json", "aqla_hyperliquid_mcp/api_manifest.json", "aqla_sheets_mcp/server.py", "aqla_sheets_mcp/parameters.py"):
            if asset not in archive.namelist():
                raise SystemExit("Wheel is missing API contracts")
    with tarfile.open(sources[0]) as archive:
        validate_tar_members(archive)
        verify_names(archive.getnames())
        if not any(name.endswith("/src/aqla_agent/artifact_rules.py") for name in archive.getnames()):
            raise SystemExit("Source distribution is missing artifact path rules")
    print("Wheel, source distribution, VPS contents and SHA-256 passed")


if __name__ == "__main__":
    main()
