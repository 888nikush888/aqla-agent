"""Pinned native skill loading: voice first, exact trading contract last; no APIs."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(Path(args.hermes_source).absolute()), str(root / "src")]
    from aqla_agent.install import prepare
    destination = prepare(root, Path(args.destination).absolute(), "12345")
    home = destination / "hermes-home"
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SKIP_CONFIG_MIGRATION"] = "1"
    import yaml
    from agent.skill_commands import build_auto_load_prompt
    config = yaml.safe_load((home / "config.yaml").read_text())
    prompt, loaded, missing = build_auto_load_prompt(user_config=config, home_override=home)
    assert loaded == ["caveman", "aqla-master"] and missing == []
    assert prompt.index('"caveman" skill is auto-loaded') < prompt.index('"aqla-master" skill is auto-loaded')
    voice = (home / "skills/caveman/SKILL.md").read_bytes()
    assert voice == (root / "skills/caveman/SKILL.md").read_bytes()
    assert (home / "skills/aqla-master/SKILL.md").read_bytes() == (root / "skills/aqla-master/SKILL.md").read_bytes()
    assert b"Never drop not/never/no/only/except" in voice and b"Numbers and units exact" in voice
    assert "telegram_view.messages unverändert" in prompt
    assert config["agent"]["reasoning_effort"] == "ultra"
    assert config["auxiliary"]["background_review"]["enabled"] is False
    print(json.dumps({"native_caveman_loaded": True, "trade_master_last": True, "master_unchanged": True,
                      "upstream_sha256": hashlib.sha256(voice).hexdigest(), "model_calls": 0, "exchange_actions": 0}))


if __name__ == "__main__":
    main()
