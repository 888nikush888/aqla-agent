"""Verify shipping contents, source completeness and the VPS archive checksum."""
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from check_repository import forbidden


def verify_names(names):
    for name in names:
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise SystemExit("Unsafe artifact member path: " + name)
        relative = name.split("/", 1)[1] if "/" in name else name
        if forbidden(relative):
            raise SystemExit("Forbidden artifact member: " + name)


def main():
    dist = Path("dist")
    bundle = dist / "aqla-agent-vps.zip"
    expected = bundle.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != expected:
        raise SystemExit("VPS archive checksum mismatch")
    with zipfile.ZipFile(bundle) as archive:
        if archive.testzip() is not None:
            raise SystemExit("VPS archive integrity failed")
        names = archive.namelist()
        verify_names(names)
        for required in ("README.md", "uv.lock", "config.example.yaml", "hermes-runtime-overrides.txt", "src/aqla_agent/install.py", "src/aqla_agent/session_history.py", "src/aqla_agent/sources.py", "src/aqla_hyperliquid_mcp/storage.py", "src/aqla_sheets_mcp/server.py", "src/aqla_sheets_mcp/parameters.py", "docs/google-sheets.md", "docs/hermes-tools-and-storage.md", "docs/gold-backup-and-names.md", "skills/aqla-master/SKILL.md", "hermes_plugin/aqla-events/plugin.yaml", "scripts/verify_artifacts.py", "tests/native_pipeline_smoke.py", "tests/native_lifecycle_smoke.py", "tests/native_interaction_smoke.py"):
            if "aqla-agent/" + required not in names:
                raise SystemExit("Missing VPS asset: " + required)
        if len(archive.read("aqla-agent/skills/aqla-master/SKILL.md")) > 8192:
            raise SystemExit("Master skill exceeds its size budget")
        for required in ("src/aqla_agent/messages.py", "tests/native_message_smoke.py", "docs/telegram-messages.md"):
            if "aqla-agent/" + required not in names:
                raise SystemExit("Missing Telegram view asset: " + required)
        if len(json.loads(archive.read("aqla-agent/tests/fixtures/context_cases.json"))) != 30:
            raise SystemExit("Context evaluation fixtures are missing")
        with TemporaryDirectory() as target:
            archive.extractall(target)
            root = Path(target) / "aqla-agent"
            from aqla_agent.install import prepare
            prepare(root, Path(target) / "deployment", "12345", "ci-only", "https://example.invalid/v1")
            import sys
            from subprocess import run
            run([sys.executable, "-m", "ruff", "check", "src", "tests", "hermes_plugin", "scripts"], cwd=root, check=True)
    wheels = list(dist.glob("*.whl"))
    sources = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        verify_names(archive.namelist())
        for asset in ("aqla_hyperliquid_mcp/info_specs.json", "aqla_hyperliquid_mcp/api_manifest.json", "aqla_sheets_mcp/server.py", "aqla_sheets_mcp/parameters.py"):
            if asset not in archive.namelist():
                raise SystemExit("Wheel is missing API contracts")
    with tarfile.open(sources[0]) as archive:
        verify_names(archive.getnames())
    print("Wheel, source distribution, VPS contents and SHA-256 passed")


if __name__ == "__main__":
    main()
