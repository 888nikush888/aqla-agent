from pathlib import Path

import pytest

from aqla_agent.skill_review import ReviewCadence, master_content


def test_review_cooldown_survives_restart_and_expires_at_exact_boundary(tmp_path):
    import json

    cadence = ReviewCadence(tmp_path, clock=lambda: 1000)
    assert cadence.due(21600)
    assert cadence.due(21600, reserve=True)
    assert not cadence.due(21600)
    restarted = ReviewCadence(tmp_path, clock=lambda: 22599.999)
    assert not restarted.due(21600, reserve=True)
    assert ReviewCadence(tmp_path, clock=lambda: 22600).due(21600, reserve=True)
    assert json.loads(cadence.path.read_text()) == {"next_allowed_at": 44200}


def test_only_one_concurrent_automatic_review_can_reserve_the_interval(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cadence = ReviewCadence(tmp_path, clock=lambda: 1000)
    with ThreadPoolExecutor(max_workers=8) as executor:
        starts = list(executor.map(lambda _: cadence.due(21600, reserve=True), range(16)))
    assert sum(starts) == 1


@pytest.mark.parametrize("content", ["broken json", "[]", '{"next_allowed_at": "broken"}',
                                      '{"next_allowed_at": NaN}', '{"next_allowed_at": Infinity}'])
def test_invalid_review_cooldown_fails_closed(tmp_path, content):
    cadence = ReviewCadence(tmp_path, clock=lambda: 1000)
    cadence.path.parent.mkdir()
    cadence.path.write_text(content)
    assert not cadence.due(21600, reserve=True)
    assert cadence.path.read_text() == content


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
