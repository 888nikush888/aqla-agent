import os
import shutil
import stat
import subprocess
from pathlib import Path
from sys import argv
from types import SimpleNamespace

import pytest
import yaml

from aqla_agent import install
from aqla_agent.install import hermes_dependency_command, prepare


def test_deployment_is_separate_and_has_native_hermes_transport(tmp_path):
    project = Path(__file__).resolve().parents[1]
    target = prepare(project, tmp_path / "new-vps", "12345", "test-model", "https://example.invalid/v1")
    config = yaml.safe_load((target / "hermes-home" / "config.yaml").read_text())
    assert config["model"]["default"] == "test-model"
    assert config["model"]["base_url"] == "https://example.invalid/v1"
    assert config["agent"]["reasoning_effort"] == "high"
    assert config['agent']['clarify_timeout'] == 60
    assert {'clarify','todo','session_search'} <= set(config['platform_toolsets']['telegram'])
    assert config["skills"]["write_approval"] is True
    assert config["skills"]["auto_load"] == ["caveman", "aqla-master"]
    assert (target / "hermes-home" / "skills" / "caveman" / "SKILL.md").read_bytes() == (project / "skills" / "caveman" / "SKILL.md").read_bytes()
    assert config["skills"]["creation_nudge_interval"] == 100
    assert config["auxiliary"]["background_review"] == {
        "enabled": True, "min_interval_seconds": 21600, "max_input_tokens": 256000}
    assert config["compression"]["threshold_tokens"] == 128000
    assert config["compression"]["protect_last_n"] == 20
    assert config["compression"]["proactive_prune_tokens"] == 48000
    assert config["plugins"]["entries"]["aqla-events"]["settings"]["owner_id"] == "12345"
    assert config["mcp_servers"]["aqla"]["command"].startswith(str(target))
    assert config["mcp_servers"]["sheets"]["args"][1] == "aqla_sheets_mcp"
    assert config["mcp_servers"]["sheets"]["tools"]["exclude"] == [
        "sync_internal", "publish_monthly_internal", "sync_presentation_internal", "presentation_selection_internal"]
    assert (target / "hermes-home" / "skills" / "aqla-master" / "SKILL.md").exists()
    assert "aqla_agent.hermes_runtime" in (target / "aqla-agent.service").read_text()
    assert "TimeoutStopSec=90" in (target / "aqla-agent.service").read_text()
    assert "KillMode=mixed" in (target / "aqla-agent.service").read_text()
    assert not list(target.rglob("*.db"))
    with pytest.raises(ValueError, match="new and empty"):
        prepare(project, target, "12345", "test-model", "https://example.invalid/v1")


def test_cli_new_install_defaults_to_deepseek_flash_high(tmp_path, monkeypatch):
    target = tmp_path / "default-vps"
    monkeypatch.setattr("sys.argv", [argv[0], "--destination", str(target), "--owner-id", "12345", "--prepare-only"])
    install.main()
    config = yaml.safe_load((target / "hermes-home" / "config.yaml").read_text())
    assert config["model"]["default"] == "deepseek-v4.1-flash"
    assert config["model"]["base_url"] == "https://opencode.ai/zen/go/v1"
    assert config["agent"]["reasoning_effort"] == "high"
    assert config["model"]["api_key"] == "${HERMES_MODEL_API_KEY}"
    assert config["fallback_providers"] == []
    assert config["auxiliary"]["source_interpretation"]["fallback_chain"] == []
    assert config["plugins"]["entries"]["aqla-events"]["settings"]["source_processing"] == {"mode": "legacy"}
    assert "openrouter" not in yaml.safe_dump(config).lower()
    assert "OPENROUTER" not in (target / "vps.env").read_text()
    assert (target / "src" / "aqla_sheets_mcp" / "parameters.py").is_file()


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
    assert {"PyJWT==2.15.1", "httpx2==2.13.1", "httpcore2==2.13.1", "mcp==2.2.0",
            "certifi==2026.10.19", "Pillow==13.0.0"} <= set(contents.splitlines())


@pytest.mark.parametrize("character", [chr(value) for value in range(32)] + [chr(127), "'", '"'])
def test_destination_rejects_control_characters_and_quotes_before_filesystem_use(tmp_path, monkeypatch, character):
    target = tmp_path / ("invalid" + character + "destination")
    project = Path(__file__).parents[1]
    with monkeypatch.context() as local:
        local.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("resolve before destination validation"))
        local.setattr(Path, "mkdir", lambda *args, **kwargs: pytest.fail("mkdir before destination validation"))
        with pytest.raises(ValueError, match="control character|unsupported in systemd"):
            prepare(project, target, "12345")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Backslash is a normal Windows path separator")
def test_destination_rejects_literal_posix_backslash_before_filesystem_use(tmp_path, monkeypatch):
    with monkeypatch.context() as local:
        local.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("resolve before destination validation"))
        with pytest.raises(ValueError, match="unsupported in systemd"):
            prepare(Path(__file__).parents[1], tmp_path / "literal\\backslash", "12345")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["normal", "with spaces", "percent%H%", "dollar${HOME}$HOME{literal}", "mixed space%H${HOME}"])
def test_prepare_supported_paths_keep_configuration_and_unit_paths(tmp_path, name):
    target = prepare(Path(__file__).resolve().parents[1], tmp_path / name, "12345")
    unit = (target / "aqla-agent.service").read_text(encoding="utf-8")
    settings = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    # The two path settings are raw systemd values. Percent is the sole specifier escape.
    assert settings["WorkingDirectory"].removesuffix("/").replace("%%", "%") == target.as_posix()
    assert settings["EnvironmentFile"].replace("%%", "%") == (target / "vps.env").as_posix()
    # Colon disables environment expansion; the full quoted executable is one token.
    executable, arguments = settings["ExecStart"].split('" ', 1)
    assert executable.startswith(':"')
    assert executable[2:].replace("%%", "%") == (target / "hermes-venv/bin/python").as_posix()
    assert arguments == "-m aqla_agent.hermes_runtime"
    config = yaml.safe_load((target / "hermes-home/config.yaml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["aqla"]["cwd"] == str(target)
    assert config["mcp_servers"]["aqla"]["command"] == str(target / ".venv/bin/python")


@pytest.mark.parametrize("character", ["\0", "\n", chr(127), '"'])
def test_cli_rejects_invalid_destination_before_resolve(tmp_path, monkeypatch, character):
    monkeypatch.setattr("sys.argv", [argv[0], "--destination", str(tmp_path / ("invalid" + character)), "--owner-id", "12345", "--prepare-only"])
    with monkeypatch.context() as local:
        local.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("CLI resolve before destination validation"))
        with pytest.raises(ValueError, match="control character|unsupported in systemd"):
            install.main()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt" or shutil.which("systemd-analyze") is None, reason="Native Linux systemd parser required")
@pytest.mark.parametrize("name", ["normal", "with spaces", "percent%H%", "dollar${HOME}$HOME{literal}", "mixed space%H${HOME}", "trailing space "])
def test_native_systemd_finds_actual_full_executable_and_preserves_modes(tmp_path, name):
    previous_mask = os.umask(0o022)
    try:
        target = prepare(Path(__file__).resolve().parents[1], tmp_path / name, "12345")
    finally:
        os.umask(previous_mask)
    for relative, expected in (("secrets", 0o700), ("hermes-home", 0o700), ("secrets/integration.secret", 0o600), ("vps.env", 0o600)):
        assert stat.S_IMODE((target / relative).stat().st_mode) == expected
    executable = target / "hermes-venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    executable.chmod(0o700)
    command = ["systemd-analyze", "verify", "--generators=no", "--man=no", str(target / "aqla-agent.service")]
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0 and not result.stderr, result.stderr
    # A passing syntax check alone is insufficient: removing only this executable
    # must identify its full real path and fail, proving the parser selected it.
    executable.unlink()
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode != 0
    assert str(executable) in result.stderr and "is not executable" in result.stderr
