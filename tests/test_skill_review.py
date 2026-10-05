from pathlib import Path

import pytest

from aqla_agent.skill_review import master_content


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
