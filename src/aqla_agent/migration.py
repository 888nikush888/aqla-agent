"""Checked configuration cutover; the financial database is never restored or rewritten here."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from aqla_hyperliquid_mcp.models import AqlaError, Settings, digest, now_ms

from .runtime_controls import atomic_yaml


def configuration_changes(root):
    root = Path(root).resolve()
    paths = (root / "config.yaml", root / "hermes-home/config.yaml")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise AqlaError("Migration requires regular existing Aqla configuration files")
    original = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]
    config, hermes = copy.deepcopy(original)
    config.pop("google_sheets", None)
    Settings.model_validate(config)
    hermes.get("mcp_servers", {}).pop("sheets", None)
    plugin = hermes.get("plugins", {}).get("entries", {}).get("aqla-events", {})
    if "mcp_allowlist" in plugin:
        plugin["mcp_allowlist"] = [key for key in plugin["mcp_allowlist"] if key != "sheets"]
    for key, values in hermes.get("platform_toolsets", {}).items():
        if isinstance(values, list):
            hermes["platform_toolsets"][key] = [value for value in values if value != "mcp-sheets"]
    display = hermes.setdefault("display", {})
    quiet = {"tool_progress": "off", "long_running_notifications": False, "interim_assistant_messages": False, "cleanup_progress": True, "busy_ack_detail": False}
    display.update(quiet)
    display.setdefault("platforms", {}).setdefault("telegram", {}).update(quiet)
    hermes.setdefault("platforms", {}).setdefault("telegram", {})["gateway_restart_notification"] = False
    return {"paths": paths, "before": original, "after": [config, hermes], "fingerprint": digest(original)}


def migrate(root, *, apply=False):
    root = Path(root).resolve()
    changes = configuration_changes(root)
    result = {"changed": [path.relative_to(root).as_posix() for path, before, after in zip(changes["paths"], changes["before"], changes["after"], strict=True) if before != after],
              "financial_database_modified": False, "configuration_fingerprint": changes["fingerprint"]}
    if not apply:
        return result
    if os.name != "posix":
        raise AqlaError("Apply targets the stopped Linux VPS service; use dry-run elsewhere")
    executable = shutil.which("systemctl")
    if executable is None:
        raise AqlaError("systemctl is required to verify that the Aqla service is stopped")
    service = subprocess.run([str(Path(executable).resolve()), "is-active", "aqla-agent.service"], capture_output=True, text=True, check=False)
    if service.returncode not in (3, 4) or service.stdout.strip() not in ("inactive", "failed", "unknown", ""):
        raise AqlaError("Stop aqla-agent.service before configuration migration")
    backup = root / ".updates" / ("telegram-migration-" + str(now_ms()))
    backup.mkdir(parents=True, mode=0o700)
    for index, path in enumerate(changes["paths"]):
        shutil.copy2(path, backup / (str(index) + ".yaml"))
        os.chmod(backup / (str(index) + ".yaml"), 0o600)
    latest = configuration_changes(root)
    if latest["fingerprint"] != changes["fingerprint"]:
        raise AqlaError("Configuration changed during migration; no update applied")
    for path, value in zip(changes["paths"], changes["after"], strict=True):
        atomic_yaml(path, value)
    result["backup"] = str(backup)
    return result


def main():
    parser = argparse.ArgumentParser(description="Inspect or apply the stopped Aqla configuration migration")
    parser.add_argument("--root", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, apply=args.apply)))


if __name__ == "__main__":
    main()
