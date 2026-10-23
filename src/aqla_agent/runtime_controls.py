"""Allowlisted technical controls; native Hermes still owns model and Telegram execution."""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading

import yaml
from pydantic import Field

from aqla_hyperliquid_mcp.models import AqlaError, Model, ReplaySettings, Settings, digest

from .decisions import SourceProcessingConfig
from .install import DEFAULT_MODEL


class TechnicalSettings(Model):
    model: str = DEFAULT_MODEL
    reasoning: str = "high"
    caveman: bool = True
    express: str = Field(default="off", pattern=r"^(off|shadow|live)$")
    source_processing: str = Field(default="legacy", pattern=r"^(legacy|shadow|hybrid)$")
    replay_enabled: bool = True
    replay_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024, le=16 * 1024 * 1024 * 1024)
    replay_retention_days: int | None = Field(default=None, ge=1, le=3650)


def atomic_yaml(path, data):
    previous = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if previous is not None and hasattr(os, "fchown"):
                os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            stream.write(yaml.safe_dump(data, sort_keys=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        from .release import _sync_directory
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RuntimeControls:
    def __init__(self, extension):
        self.extension = extension
        self.config = extension.config_path
        self.hermes = extension.master_path.parents[2] / "config.yaml"
        self.lock = threading.RLock()

    def read(self):
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        hermes = yaml.safe_load(self.hermes.read_text(encoding="utf-8"))
        plugin = hermes.get("plugins", {}).get("entries", {}).get("aqla-events", {}).get("settings", {})
        replay = ReplaySettings.model_validate(config.get("waverider_replay", {}))
        values = TechnicalSettings(model=hermes.get("model", {}).get("default", DEFAULT_MODEL),
            reasoning=hermes.get("agent", {}).get("reasoning_effort", "high"),
            caveman="caveman" in hermes.get("skills", {}).get("auto_load", []),
            express=config.get("express", {}).get("mode", "off"), source_processing=plugin.get("source_processing", {}).get("mode", "legacy"),
            replay_enabled=replay.enabled, replay_max_bytes=replay.max_bytes, replay_retention_days=replay.retention_days)
        return {"values": values.model_dump(mode="json"), "revision": digest([config, hermes]),
                "restart_required": False, "apply_status": getattr(self.extension, "technical_apply_status", "confirmed")}

    async def reconcile(self):
        """Retry only a persisted technical setting; never repeat a financial action."""
        if getattr(self.extension, "technical_apply_status", "confirmed") != "pending":
            return
        loaded = Settings.load(self.config)
        await self.extension.mcp_call(self.extension.server, "integration", {"action": "runtime_reload",
            "token": self.extension.authority.key.decode(), "technical": {
                "express": loaded.express.model_dump(mode="json"), "waverider_replay": loaded.waverider_replay.model_dump(mode="json")}})
        fresh = Settings.load(self.config)
        if fresh.express != loaded.express or fresh.waverider_replay != loaded.waverider_replay:
            raise AqlaError("Technische Konfiguration wurde während der Übernahme geändert")
        self.extension.settings = loaded
        await self.extension.source_worker.bind()
        self.extension.technical_apply_status = "confirmed"

    def validated_change(self, field, value, expected_revision, before):
        if before["revision"] != expected_revision:
            raise AqlaError("Technische Konfiguration wurde geändert; aktuelle Werte neu lesen")
        if not self.extension.setup.claims():
            raise AqlaError("Setup ist abgelaufen")
        values = TechnicalSettings.model_validate({**before["values"], field: value})
        if values.model != DEFAULT_MODEL or values.reasoning != "high":
            raise AqlaError("Für diese Modell-/Reasoning-Kombination fehlt der freigegebene Qualitätsnachweis")
        if not values.caveman:
            raise AqlaError("Die aktuelle Freigabe verwendet high mit Caveman; für Änderungen zuerst passenden Qualitätsnachweis hinterlegen")
        return values

    def change_documents(self, values):
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        hermes = yaml.safe_load(self.hermes.read_text(encoding="utf-8"))
        plugin = hermes["plugins"]["entries"]["aqla-events"]["settings"]
        updated_config, updated_hermes = copy.deepcopy(config), copy.deepcopy(hermes)
        selected_plugin = updated_hermes["plugins"]["entries"]["aqla-events"]["settings"]
        if values.express == "live":
            from .express import QUALIFIED_MASTER
            if hashlib.sha256(self.extension.master_path.read_bytes()).hexdigest() != QUALIFIED_MASTER:
                raise AqlaError("Express-Master ist nicht qualifiziert")
        if values.source_processing != plugin.get("source_processing", {}).get("mode", "legacy") and values.source_processing in ("shadow", "hybrid"):
            raise AqlaError("Für einen zusätzlichen Quellenverarbeiter fehlt eine eingerichtete native Modellfreigabe; der freigegebene Hermes-Ablauf bleibt aktiv")
        updated_config.setdefault("express", {})["mode"] = values.express
        updated_config["waverider_replay"] = {"enabled": values.replay_enabled, "max_bytes": values.replay_max_bytes,
                                              "retention_days": values.replay_retention_days}
        selected_plugin.setdefault("source_processing", {})["mode"] = values.source_processing
        updated_hermes.setdefault("model", {})["default"] = values.model
        updated_hermes.setdefault("agent", {})["reasoning_effort"] = values.reasoning
        Settings.model_validate(updated_config)
        SourceProcessingConfig.model_validate(selected_plugin["source_processing"])
        return updated_config, updated_hermes, selected_plugin

    async def apply_desired_change(self, field, loaded, technical, selected_plugin):
        if field in ("express", "replay_enabled", "replay_max_bytes", "replay_retention_days"):
            self.extension.technical_apply_status = "pending"
            try:
                await self.extension.mcp_call(self.extension.server, "integration", {"action": "runtime_reload",
                    "token": self.extension.authority.key.decode(), "technical": technical})
                fresh = Settings.load(self.config)
                if fresh.express != loaded.express or fresh.waverider_replay != loaded.waverider_replay:
                    raise AqlaError("Technische Konfiguration wurde während der Übernahme geändert")
                self.extension.settings = loaded
                self.extension.technical_apply_status = "confirmed"
            except AqlaError:
                pass  # Persisted desired state remains visible and is retried by the native worker.
        elif field == "source_processing":
            self.extension.source_worker.config = SourceProcessingConfig.model_validate(selected_plugin["source_processing"])
            self.extension.source_worker.bound_owner = None
            self.extension.technical_apply_status = "pending"
            try:
                await self.extension.source_worker.bind()
                self.extension.technical_apply_status = "confirmed"
            except AqlaError:
                pass

    async def change(self, field, value, expected_revision):
        if field not in TechnicalSettings.model_fields:
            raise AqlaError("Unbekannte technische Einstellung")
        # One plugin writer owns both these allowlisted configurations; no arbitrary file RPC.
        with self.lock:
            before = self.read()
            values = self.validated_change(field, value, expected_revision, before)
            updated_config, updated_hermes, selected_plugin = self.change_documents(values)
            if not self.extension.setup.claims():
                raise AqlaError("Setup ist abgelaufen")
            # Each field belongs to ONE file, so a crash cannot leave a mixed two-file update.
            if field in ("model", "reasoning", "caveman", "source_processing"):
                atomic_yaml(self.hermes, updated_hermes)
            else:
                atomic_yaml(self.config, updated_config)
            loaded = Settings.load(self.config)
            technical = {"express": loaded.express.model_dump(mode="json"), "waverider_replay": loaded.waverider_replay.model_dump(mode="json")}
            await self.apply_desired_change(field, loaded, technical, selected_plugin)
            return self.read()
