"""Verify shipping contents, source completeness and the VPS archive checksum."""
import argparse
import hashlib
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


def _verify_vps_bundle(bundle):
    with zipfile.ZipFile(bundle) as archive:
        validate_zip_members(archive)
        if archive.testzip() is not None:
            raise SystemExit("VPS archive integrity failed")
        names = archive.namelist()
        verify_names(names)
        if "aqla-agent/src/aqla_agent/artifact_rules.py" not in names:
            raise SystemExit("Missing VPS artifact path rules")
        required = ("README.md", "uv.lock", "config.example.yaml", "hermes-runtime-overrides.txt", "CONTENTS.sha256",
                    "src/aqla_agent/install.py", "src/aqla_agent/telegram_menu.py", "src/aqla_agent/journal.py",
                    "src/aqla_agent/operator_management.py", "src/aqla_hyperliquid_mcp/storage.py",
                    "docs/installation.md", "docs/telegram.md", "docs/trading.md", "docs/operations.md", "docs/quality.md",
                    "skills/aqla-master/SKILL.md", "hermes_plugin/aqla-events/plugin.yaml")
        if any("aqla-agent/" + name not in names for name in required):
            raise SystemExit("Missing standalone runtime asset")
        if any(name.startswith(("aqla-agent/tests/", "aqla-agent/scripts/", "aqla-agent/plans/", "aqla-agent/advisor-plans/", "aqla-agent/src/aqla_sheets_mcp/")) for name in names):
            raise SystemExit("Obsolete or development-only runtime assets")
        for line in archive.read("aqla-agent/CONTENTS.sha256").decode().splitlines():
            expected_hash, name = line.split("  ", 1)
            if hashlib.sha256(archive.read("aqla-agent/" + name)).hexdigest() != expected_hash:
                raise SystemExit("Runtime content manifest mismatch")
        if len(archive.read("aqla-agent/skills/aqla-master/SKILL.md")) > 8192:
            raise SystemExit("Master skill exceeds its size budget")
        with TemporaryDirectory() as target:
            archive.extractall(target)
            root = Path(target) / "aqla-agent"
            from aqla_agent.install import prepare
            prepare(root, Path(target) / "deployment", "12345", "ci-only", "https://example.invalid/v1")
            from subprocess import run
            run([sys.executable, "-m", "ruff", "check", "src", "hermes_plugin"], cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    dist = parser.parse_args().dist
    bundle = dist / "aqla-agent-vps.zip"
    expected = bundle.with_suffix(".zip.sha256").read_text(encoding="utf-8")
    if expected != f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n":
        raise SystemExit("VPS archive checksum mismatch")
    _verify_vps_bundle(bundle)
    wheels = list(dist.glob("*.whl"))
    sources = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        validate_zip_members(archive)
        verify_names(archive.namelist())
        if "aqla_agent/artifact_rules.py" not in archive.namelist():
            raise SystemExit("Wheel is missing artifact path rules")
        for asset in ("aqla_hyperliquid_mcp/info_specs.json", "aqla_hyperliquid_mcp/api_manifest.json", "aqla_agent/journal.py", "aqla_agent/telegram_menu.py"):
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
