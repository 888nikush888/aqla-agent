from pathlib import Path

import pytest

from aqla_agent.skill_review import master_content


def test_installed_bundled_master_leaves_room_for_native_owner_proposal(tmp_path):
    from aqla_agent.install import prepare

    project = Path(__file__).resolve().parents[1]
    target = prepare(project, tmp_path / "master-profile", "12345")
    master = target / "hermes-home" / "skills" / "aqla-master" / "SKILL.md"
    original = master.read_bytes()
    assert original == (project / "skills" / "aqla-master" / "SKILL.md").read_bytes()
    assert len(original) <= 7800
    content = original.decode("utf-8").replace(
        "Du bist aqla Agent.", "Du bist aqla Agent. Prüfe Replies besonders sorgfältig.", 1)
    assert content != original.decode("utf-8")
    assert master_content({"action": "patch", "name": "aqla-master", "content": content}, master) == content


@pytest.mark.parametrize("args", [
    {"name": "another", "action": "edit", "content": "small"},
    {"name": "aqla-master", "action": "delete"},
    {"name": "aqla-master", "action": "edit", "content": ""},
    {"name": "aqla-master", "action": "edit", "content": "ü" * 4097},
    {"name": "aqla-master", "action": "edit", "content": "small", "file_path": ""},
    {"operations": []},
    {"operations": [{"action": "edit", "name": "aqla-master", "content": "small"}] * 2},
    {"operations": [{"action": "patch", "name": "aqla-master", "content": "small", "file_content": "outside"}]},
    {"operations": [{"action": "patch", "name": "aqla-master", "content": "small", "operations": []}]},
])
def test_master_proposal_rejects_unsafe_shapes(args):
    with pytest.raises(ValueError):
        master_content(args, Path("not-needed-for-full-rewrite"))


@pytest.mark.parametrize("args", [
    {"action": "edit", "name": "aqla-master", "content": "ü" * 4096},
    {"operations": [{"action": "patch", "name": "aqla-master", "content": "ü" * 4096}]},
])
def test_master_proposal_accepts_native_and_legacy_shape_at_byte_limit(args):
    assert master_content(args, Path("not-needed-for-full-rewrite")) == "ü" * 4096
