import json
import os
import shutil

import pytest

from aqla_agent.tool_outputs import PAGE_BYTES, OutputStore, capture_scope, encoded


def test_lossless_escaped_unicode_pages_restart_and_reused_call_ids(tmp_path):
    store = OutputStore(tmp_path)
    body = '😺ä"\\\n' * 18000 + "long-line:" + "Z" * 30000
    first = store.save(body, "session-a", "same-call-id")
    second = store.save("different output", "session-a", "same-call-id")
    assert first != second
    reader = OutputStore(tmp_path)
    combined, offset = "", 0
    while True:
        page = reader.read(first, "session-a", offset, 4096)
        assert len(json.dumps(encoded(page).decode(), ensure_ascii=True).encode()) <= PAGE_BYTES
        combined += page["content"]
        if page["eof"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert combined == body
    assert reader.read(second, "session-a")["content"] == "different output"
    with pytest.raises(ValueError):
        reader.read(first, "session-b")
    with pytest.raises(ValueError):
        OutputStore(tmp_path / "another-profile").read(first, "session-a")


@pytest.mark.parametrize("change", ["content", "session", "expires", "sha256"])
def test_modified_receipt_is_rejected(tmp_path, change):
    store = OutputStore(tmp_path)
    output_id = store.save("own result", "session", "call")
    path = store.root / (output_id + ".json")
    document = json.loads(path.read_bytes())
    document["receipt"][change] = "forged"
    path.write_bytes(encoded(document))
    with pytest.raises(ValueError):
        store.read(output_id, "session")


def test_expiry_cleanup_only_removes_expired_receipts(tmp_path):
    now = [1000]
    store = OutputStore(tmp_path, clock=lambda: now[0], ttl=10)
    expired = store.save("old", "s", "id")
    now[0] = 1005
    live = store.save("live", "s", "id")
    now[0] = 1011
    with pytest.raises(ValueError, match="expired"):
        store.read(expired, "s")
    assert store.cleanup() == 1
    assert store.read(live, "s")["content"] == "live"


@pytest.mark.parametrize("identity", ["../secret", "C:\\secrets", "/etc/passwd", "f" * 64, "", None])
def test_no_paths_or_unknown_refs_can_be_read(tmp_path, identity):
    store = OutputStore(tmp_path)
    store.save("own", "s", "id")
    with pytest.raises(ValueError):
        store.read(identity, "s")


def test_hard_links_and_directory_swapping_are_rejected(tmp_path):
    store = OutputStore(tmp_path)
    identity = store.save("own", "s", "id")
    path = store.root / (identity + ".json")
    alias = tmp_path / "hardlink"
    os.link(path, alias)
    with pytest.raises(ValueError, match="regular"):
        store.read(identity, "s")
    alias.unlink()
    original = store.root.with_name("retained-original")
    store.root.rename(original)
    shutil.copytree(original, store.root)
    with pytest.raises(ValueError, match="changed"):
        store.read(identity, "s")


def test_symlinks_are_rejected_without_reading_the_target(tmp_path):
    store = OutputStore(tmp_path)
    identity = store.save("own", "s", "id")
    path = store.root / (identity + ".json")
    real = tmp_path / "own-data"
    path.rename(real)
    try:
        path.symlink_to(real)
    except OSError:
        pytest.skip("Windows requires symlink privilege")
    with pytest.raises(ValueError, match="links"):
        store.read(identity, "s")


def test_capture_binds_trusted_home_and_model_arguments_do_not_select_identity(tmp_path):
    with capture_scope("native-session", "native-call", tmp_path):
        from aqla_agent.tool_outputs import CAPTURE
        assert CAPTURE.get() == ("native-session", "native-call", tmp_path.absolute())
    assert CAPTURE.get() is None


@pytest.mark.parametrize("offset,limit", [(True, 1), (0, False), (-1, 1), (0, 4097), (0, 0)])
def test_bad_page_inputs_are_not_guessed(tmp_path, offset, limit):
    store = OutputStore(tmp_path)
    identity = store.save("own", "s", "id")
    with pytest.raises(ValueError):
        store.read(identity, "s", offset, limit)
