"""Prepare an isolated VPS deployment. Never modifies an existing Hermes home."""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

HERMES_COMMIT = "1a4508e2aff2db5f50409893a2115be777bd5643"


def hermes_dependency_command(uv, source, python, overrides):
    """One tested minimal runtime recipe for the VPS installer and CI."""
    return [str(uv), "pip", "install", "--python", str(python), "--overrides", str(overrides),
            "-e", str(source) + "[telegram,mcp]", "Telethon==1.45.0", "PyYAML==6.0.3"]


def prepare(project: Path, destination: Path, owner: str, model: str, model_url: str):
    project, destination = project.resolve(), destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Deployment destination must be new and empty; existing Hermes installations are never overwritten")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("src", "hermes_plugin", "skills", "pyproject.toml", "uv.lock", "config.example.yaml", "hermes-runtime-overrides.txt"):
        source, target = project / name, destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)
    secret_dir = destination / "secrets"
    secret_dir.mkdir(mode=0o700)
    integration = secret_dir / "integration.secret"
    integration.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    os.chmod(integration, 0o600)
    config = yaml.safe_load((destination / "config.example.yaml").read_text(encoding="utf-8"))
    config["private_key_file"] = None
    config["telegram_owner_id"] = owner
    (destination / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    home = destination / "hermes-home"
    home.mkdir(mode=0o700)
    shutil.copytree(project / "skills" / "aqla-master", home / "skills" / "aqla-master")
    shutil.copytree(project / "hermes_plugin" / "aqla-events", home / "plugins" / "aqla-events")
    python = str(destination / ".venv" / "bin" / "python")
    hermes_config = {
        "model": {"default": model, "provider": "custom", "base_url": model_url, "api_key": "${HERMES_MODEL_API_KEY}"},
        "skills": {"auto_load": ["aqla-master"]},
        "agent": {"max_turns": 30},
        "display": {"busy_input_mode": "queue", "interim_assistant_messages": False, "tool_progress": "log"},
        "platform_toolsets": {"telegram": ["skills", "mcp-aqla", "mcp-sheets"], "cli": ["skills", "no_mcp"], "cron": ["skills", "no_mcp"]},
        "mcp_servers": {"aqla": {"command": python, "args": ["-m", "aqla_hyperliquid_mcp", "--config", str(destination / "config.yaml")],
                                  "cwd": str(destination), "trust": "full", "supports_parallel_tool_calls": False,
                                  "tools": {"exclude": ["integration"]}},
                        "sheets": {"command": python, "args": ["-m", "aqla_sheets_mcp", "--config", str(destination / "config.yaml")],
                                   "cwd": str(destination), "trust": "full", "supports_parallel_tool_calls": False,
                                   "tools": {"exclude": ["sync_internal"]}}},
        "plugins": {"enabled": ["aqla-events"], "entries": {"aqla-events": {"allow_gateway_injection": True,
                    "mcp_allowlist": ["aqla", "sheets"], "settings": {"owner_id": owner, "config_path": str(destination / "config.yaml"), "mcp_server": "aqla", "master_path": str(home / "skills" / "aqla-master" / "SKILL.md")}}}},
        "platforms": {"telegram": {"enabled": True, "unauthorized_dm_behavior": "ignore", "extra": {"allow_from": [owner], "group_allow_from": []}}},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(hermes_config, sort_keys=False), encoding="utf-8")
    env = "\n".join([f"HERMES_HOME={home}", f"PYTHONPATH={destination / 'src'}",
                       f"TELEGRAM_ALLOWED_USERS={owner}", f"TELEGRAM_HOME_CHANNEL={owner}",
                       "TELEGRAM_BOT_TOKEN=", "HERMES_MODEL_API_KEY=", ""])
    env_path = destination / "vps.env"
    env_path.write_text(env, encoding="utf-8")
    os.chmod(env_path, 0o600)
    unit = f"""[Unit]
Description=aqla Agent (native Hermes Telegram gateway)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aqla
WorkingDirectory={destination}
EnvironmentFile={env_path}
ExecStart={destination}/hermes-venv/bin/python -m aqla_agent.hermes_runtime
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""
    (destination / "aqla-agent.service").write_text(unit, encoding="utf-8")
    return destination


def install_runtime(destination: Path):
    if os.name == "nt":
        raise SystemExit("Runtime installation targets a Linux VPS. Use --prepare-only on Windows.")
    uv = shutil.which("uv")
    git = shutil.which("git")
    if not uv or not git:
        raise SystemExit("Install Python 3.12+, uv and git on the VPS first")
    def run(args):
        environment = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(destination / ".python"))
        return subprocess.run(args, cwd=destination, check=True, env=environment)
    run([uv, "sync", "--frozen", "--extra", "sources", "--extra", "sheets", "--python", "3.12", "--python-preference", "only-managed"])
    source = destination / "hermes-source"
    run([git, "clone", "--filter=blob:none", "https://github.com/NousResearch/hermes-agent.git", str(source)])
    run([git, "-C", str(source), "checkout", "--detach", HERMES_COMMIT])
    venv = destination / "hermes-venv"
    # The pinned Hermes release actually runs on Python 3.14; older interpreter
    # support in its metadata exists only for the upstream upgrade path.
    run([uv, "venv", "--python", "3.14", "--python-preference", "only-managed", str(venv)])
    python = str(venv / "bin" / "python")
    run(hermes_dependency_command(uv, source, python, destination / "hermes-runtime-overrides.txt"))
    # The plugin imports only source/authority modules from Aqla; its MCP runs in its own venv.
    return venv


def main():
    parser = argparse.ArgumentParser(description="Prepare a separate aqla VPS deployment")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-url", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not args.owner_id.isdigit() or "\n" in args.model or "\n" in args.model_url:
        raise SystemExit("Invalid owner/model configuration")
    project = Path(__file__).resolve().parents[2]
    if not (project / "config.example.yaml").is_file():
        project = Path.cwd()
    if not (project / "config.example.yaml").is_file():
        raise SystemExit("Run the installer from the extracted aqla VPS source folder")
    destination = prepare(project, Path(args.destination), args.owner_id, args.model, args.model_url)
    if not args.prepare_only:
        install_runtime(destination)
    print(f"Prepared isolated deployment: {destination}")
    print("Fill config.yaml and vps.env with your account and credentials, then install the generated systemd unit.")


if __name__ == "__main__":
    main()
