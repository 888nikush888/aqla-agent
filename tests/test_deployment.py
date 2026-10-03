from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from aqla_agent import install
from aqla_agent.install import hermes_dependency_command, prepare


def test_deployment_is_separate_and_has_native_hermes_transport(tmp_path):
    project = Path(__file__).resolve().parents[1]
    target = prepare(project, tmp_path / "new-vps", "12345", "test-model", "https://example.invalid/v1")
    config = yaml.safe_load((target / "hermes-home" / "config.yaml").read_text())
    assert config["plugins"]["entries"]["aqla-events"]["settings"]["owner_id"] == "12345"
    assert config["mcp_servers"]["aqla"]["command"].startswith(str(target))
    assert config["mcp_servers"]["sheets"]["args"][1] == "aqla_sheets_mcp"
    assert config["mcp_servers"]["sheets"]["tools"]["exclude"] == ["sync_internal"]
    assert (target / "hermes-home" / "skills" / "aqla-master" / "SKILL.md").exists()
    assert "aqla_agent.hermes_runtime" in (target / "aqla-agent.service").read_text()
    assert "TimeoutStopSec=90" in (target / "aqla-agent.service").read_text()
    assert "KillMode=mixed" in (target / "aqla-agent.service").read_text()
    assert not list(target.rglob("*.db"))
    with pytest.raises(ValueError, match="new and empty"):
        prepare(project, target, "12345", "test-model", "https://example.invalid/v1")


def test_linux_install_uses_separate_managed_interpreters_and_pinned_hermes(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(install, "os", SimpleNamespace(name="posix", environ={}))
    monkeypatch.setattr(install.shutil, "which", lambda name: name)
    def run(args, **kwargs):
        commands.append((args, kwargs))
    monkeypatch.setattr(install.subprocess, "run", run)
    install.install_runtime(tmp_path)
    assert commands[0][0] == ["uv", "sync", "--frozen", "--extra", "sources", "--extra", "sheets", "--python", "3.12", "--python-preference", "only-managed"]
    assert any("3.14" in args and "only-managed" in args for args, _ in commands)
    assert any(args[-1] == install.HERMES_COMMIT and "--detach" in args for args, _ in commands)
    assert all(options["env"]["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / ".python") for _, options in commands)
    dependency = commands[-1][0]
    assert "--overrides" in dependency
    assert any(value.endswith("[telegram,mcp]") for value in dependency)
    assert not any("messaging" in value for value in dependency)


def test_native_dependency_recipe_is_shared_and_security_versions_are_exact():
    project = Path(__file__).resolve().parents[1]
    override = project / "hermes-runtime-overrides.txt"
    recipe = hermes_dependency_command("uv", Path("source"), "python", override)
    assert recipe[recipe.index("--overrides") + 1] == str(override)
    contents = override.read_text()
    assert {"PyJWT==2.15.1", "httpx2==2.13.1", "httpcore2==2.13.1"} <= set(contents.splitlines())
