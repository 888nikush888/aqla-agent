import hashlib
import io
import os
import runpy
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from aqla_agent import release
from aqla_agent.artifact_rules import forbidden, validate_member, validate_tar_members, validate_zip_members
from aqla_agent.release import build

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for directory in release.SHIPPING_DIRS:
        (root / directory).mkdir()
        (root / directory / "allowed.txt").write_text("synthetic public asset", encoding="utf-8")
    for name in release.SHIPPING_FILES:
        (root / name).write_text("synthetic public asset", encoding="utf-8")
    return root


@pytest.mark.parametrize("suffix", [".db", ".sqlite", ".sqlite3", ".session"])
@pytest.mark.parametrize("sidecar", ["", "-journal", "-wal", "-shm", "-mj123", ".lock", ".initialized", "-wal.lock", "-journal.initialized", "-shm.lock.initialized"])
def test_artifact_rules_reject_local_state_and_all_sqlite_sidecars(suffix, sidecar):
    assert forbidden("src/LOCAL" + suffix.upper() + sidecar.upper())


@pytest.mark.parametrize("name", [
    ".git/HEAD", "src/.ENV", "src/.env.local", "src/.envrc", "docs/name.env", "tests/name.env.production",
    "tests/state.json", "tests/state.json.lock", "src/start.initialized", "src/key.secret", "tests/local.pyc",
    "secrets/key", "reports/check.json", "dist/package.zip", ".cache/file", ".venv/file", "config.yaml",
])
def test_artifact_rules_reject_local_state_paths(name):
    assert forbidden(name)


@pytest.mark.parametrize("name", ["src/.env.example", "src/.ENV.EXAMPLE", "src/aqla_agent/release.py", "docs/guide.md", "config.example.yaml"])
def test_artifact_rules_allow_env_example_and_shipping_sources(name):
    assert not forbidden(name)
    validate_member(name)


@pytest.mark.parametrize("name", ["/etc/file", "C:/file", "C:file", "//server/file", r"folder\file", "pkg/../file", "./pkg/file", "pkg/./file", "pkg/\x00file", "pkg/\nfile", "pkg/\x7ffile", ".git/file"])
def test_artifact_rules_reject_original_archive_path_before_prefix_strip(name):
    verifier = runpy.run_path(str(ROOT / "scripts/verify_artifacts.py"))
    with pytest.raises(ValueError):
        verifier["verify_names"]([name])


def test_artifact_rules_reject_zip_links_and_tar_links_before_extract():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        link = zipfile.ZipInfo("pkg/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "synthetic-target")
    data.seek(0)
    with zipfile.ZipFile(data) as archive, pytest.raises(ValueError, match="Non-regular ZIP"):
        validate_zip_members(archive)
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            link = tarfile.TarInfo("pkg/link")
            link.type, link.linkname = kind, "synthetic-target"
            archive.addfile(link)
        data.seek(0)
        with tarfile.open(fileobj=data) as archive, pytest.raises(ValueError, match="Non-regular TAR"):
            validate_tar_members(archive)


def test_check_repository_runs_without_installed_dependencies():
    result = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "scripts/check_repository.py")], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Repository hygiene passed" in result.stdout


def test_verifier_rule_import_runs_without_installed_dependencies(tmp_path):
    code = f"import runpy; module=runpy.run_path({str(ROOT / 'scripts/verify_artifacts.py')!r}); assert module['forbidden']('src/local.sqlite3-wal'); assert not module['forbidden']('src/.env.example')"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _symlink(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"Platform cannot create symlinks: {error}")


@pytest.mark.parametrize("name", ["linked.txt", "linked.pyc", "__pycache__"])
def test_file_symlink_before_read(release_project, tmp_path, monkeypatch, name):
    target = tmp_path / "target.txt"
    target.write_text("synthetic untouched target", encoding="utf-8")
    _symlink(release_project / "src" / name, target)
    monkeypatch.setattr(release, "_read_source", lambda *args: pytest.fail("Source read before link validation"))
    with pytest.raises(ValueError, match="Linked/reparse"):
        build(release_project, tmp_path / "out.zip")
    assert target.read_text(encoding="utf-8") == "synthetic untouched target"


def test_allowlist_directory_symlink_before_traversal(release_project, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (release_project / "src/allowed.txt").unlink()
    (release_project / "src").rmdir()
    _symlink(release_project / "src", target, directory=True)
    monkeypatch.setattr(release.os, "scandir", lambda *args: pytest.fail("Traversal before directory link validation"))
    with pytest.raises(ValueError, match="Linked/reparse"):
        build(release_project, tmp_path / "out.zip")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction verification")
def test_windows_junction_before_traversal(release_project, tmp_path, monkeypatch):
    target = tmp_path / "junction-target"
    target.mkdir()
    link = release_project / "src"
    (link / "allowed.txt").unlink()
    link.rmdir()
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert link.parent.resolve().is_relative_to(tmp_path.resolve())
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if created.returncode:
        pytest.skip(f"Junction unavailable: {created.stderr}")
    try:
        assert link.lstat().st_file_attributes & 0x400
        monkeypatch.setattr(release.os, "scandir", lambda *args: pytest.fail("Traversal before junction validation"))
        with pytest.raises(ValueError, match="Linked/reparse"):
            build(release_project, tmp_path / "out.zip")
        assert list(target.iterdir()) == []
    finally:
        # Remove the checked junction itself before pytest's recursive temporary cleanup.
        assert link.parent.resolve().is_relative_to(tmp_path.resolve())
        assert link.lstat().st_file_attributes & 0x400
        link.rmdir()


def test_linked_output_parent_before_resolve(release_project, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-output"
    _symlink(link, target, directory=True)
    with monkeypatch.context() as local:
        local.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("resolve followed a link"))
        with pytest.raises(ValueError, match="Linked/reparse"):
            build(release_project, link / "out.zip")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction verification")
@pytest.mark.parametrize("location", ["output", "cache", "nested_cache"])
def test_windows_junction_output_and_cache_before_use(release_project, tmp_path, monkeypatch, location):
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "sentinel.txt").write_text("synthetic untouched target", encoding="utf-8")
    if location == "output":
        link = tmp_path / "linked-output"
        output = link / "out.zip"
    else:
        parent = release_project / "src"
        if location == "nested_cache":
            parent = parent / "__pycache__"
            parent.mkdir()
        link = parent / "__pycache__"
        output = tmp_path / "out.zip"
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert link.parent.resolve().is_relative_to(tmp_path.resolve())
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if result.returncode:
        pytest.skip(f"Junction unavailable: {result.stderr}")
    original = release.os.scandir
    traversed = []
    def scan(path):
        traversed.append(Path(path))
        return original(path)
    try:
        monkeypatch.setattr(release.os, "scandir", scan)
        monkeypatch.setattr(release, "_read_source", lambda *args: pytest.fail("Read before junction validation"))
        with pytest.raises(ValueError, match="Linked/reparse"):
            build(release_project, output)
        assert link not in traversed and target not in traversed
        assert (target / "sentinel.txt").read_text(encoding="utf-8") == "synthetic untouched target"
        assert list(target.iterdir()) == [target / "sentinel.txt"]
    finally:
        assert link.parent.resolve().is_relative_to(tmp_path.resolve())
        assert link.lstat().st_file_attributes & 0x400
        link.rmdir()


@pytest.mark.parametrize("name", ["src", "scripts", "README.md"])
def test_missing_or_nonregular_allowlist_before_traversal(release_project, tmp_path, monkeypatch, name):
    path = release_project / name
    if path.is_dir():
        (path / "allowed.txt").unlink()
        path.rmdir()
        path.write_text("synthetic non-directory", encoding="utf-8")
    else:
        path.unlink()
        path.mkdir()
    monkeypatch.setattr(release.os, "scandir", lambda *args: pytest.fail("Traversal before allowlist validation"))
    with pytest.raises(ValueError, match="Non-regular"):
        build(release_project, tmp_path / "out.zip")


def test_source_identity_change_before_read(release_project, tmp_path, monkeypatch):
    original = release._collect_sources
    def collect(root):
        files = original(root)
        (root / "src/allowed.txt").write_text("changed synthetic asset with different size", encoding="utf-8")
        return files
    monkeypatch.setattr(release, "_collect_sources", collect)
    with pytest.raises(ValueError, match="Source changed before reading"):
        build(release_project, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_regular_cache_is_skipped_and_env_example_is_shipped(release_project, tmp_path):
    cache = release_project / "src/__pycache__"
    cache.mkdir()
    (cache / "cache.pyc").write_bytes(b"synthetic ignored cache")
    (release_project / "src/module.pyc").write_bytes(b"synthetic ignored cache")
    (release_project / "src/.env.example").write_text("synthetic public example", encoding="utf-8")
    result = build(release_project, tmp_path / "out.zip")
    with zipfile.ZipFile(result["archive"]) as archive:
        assert "aqla-agent/src/.env.example" in archive.namelist()
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in archive.namelist())


@pytest.mark.parametrize("where", ["project", "output"])
def test_lexical_parent_segments(release_project, tmp_path, where):
    root = release_project / ".." / "project" if where == "project" else release_project
    output = tmp_path / ".." / "out.zip" if where == "output" else tmp_path / "out.zip"
    with pytest.raises(ValueError, match="Unsafe filesystem path"):
        build(root, output)


@pytest.mark.parametrize("name", ["src/out.zip", "tests/nested/out.zip", "README.md"])
def test_output_inside_shipping_tree(release_project, name):
    with pytest.raises(ValueError, match="Output overlaps"):
        build(release_project, release_project / name)


def test_non_zip_output_before_filesystem_use(release_project, tmp_path, monkeypatch):
    monkeypatch.setattr(release, "_check_chain", lambda *args, **kwargs: pytest.fail("Filesystem access before ZIP suffix validation"))
    with pytest.raises(ValueError, match=".zip suffix"):
        build(release_project, tmp_path / "out.tar")


@pytest.mark.parametrize("name", ["src/.env", "tests/local.sqlite3", "docs/local.db", "scripts/state.json.lock", "skills/setup.initialized"])
def test_synthetic_local_state_before_archive_open(release_project, tmp_path, monkeypatch, name):
    (release_project / name).write_text("synthetic forbidden sentinel", encoding="utf-8")
    monkeypatch.setattr(release.zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("Archive opened before all members were validated"))
    monkeypatch.setattr(release, "_read_source", lambda *args: pytest.fail("Read before all members were validated"))
    with pytest.raises(ValueError, match="Unsafe artifact member"):
        build(release_project, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_synthetic_local_state_with_previous_pair_before_archive_open(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    (release_project / "tests/local.sqlite3").write_text("synthetic forbidden sentinel", encoding="utf-8")
    monkeypatch.setattr(release.zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("Existing archive opened before source validation"))
    monkeypatch.setattr(release, "_read_source", lambda *args: pytest.fail("Existing pair read before source validation"))
    with pytest.raises(ValueError, match="Unsafe artifact member"):
        build(release_project, output)
    assert (output.read_bytes(), sha.read_bytes()) == previous


def test_cli_returns_nonzero_without_success_json_for_forbidden_source(release_project, tmp_path):
    package = release_project / "src/aqla_agent"
    package.mkdir()
    for name in ("__init__.py", "release.py", "artifact_rules.py"):
        (package / name).write_bytes((ROOT / "src/aqla_agent" / name).read_bytes())
    (release_project / "tests/local.sqlite3").write_text("synthetic forbidden sentinel", encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(os.environ["PYTHONPATH"].split(os.pathsep)[0])), str(release_project / "src")]))
    result = subprocess.run([sys.executable, "-m", "aqla_agent.release", "--output", str(tmp_path / "out.zip")], cwd=release_project, env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "local.sqlite3" in result.stderr
    assert "synthetic forbidden sentinel" not in result.stderr


def test_preserves_all_allowed_source_assets(tmp_path):
    result = build(ROOT, tmp_path / "out.zip")
    tracked = subprocess.run(["git", "ls-files", "-z", "--", *release.SHIPPING_DIRS, *release.SHIPPING_FILES], cwd=ROOT, check=True, capture_output=True).stdout.decode().split("\0")
    expected = {"aqla-agent/" + name for name in tracked if name}
    expected.add("aqla-agent/src/aqla_agent/artifact_rules.py")
    with zipfile.ZipFile(result["archive"]) as archive:
        assert set(archive.namelist()) == expected
    assert result["files"] == len(expected)


def _previous_pair(root, tmp_path, name="out.zip"):
    output = tmp_path / name
    build(root, output)
    sha = output.with_suffix(".zip.sha256")
    previous = output.read_bytes(), sha.read_bytes()
    (root / "src/allowed.txt").write_text("different synthetic public asset", encoding="utf-8")
    return output, sha, previous


def _assert_pair(output, sha, previous):
    assert (output.read_bytes(), sha.read_bytes()) == previous
    assert release._verify_pair(output, sha, output.name) == hashlib.sha256(previous[0]).hexdigest()


def test_zip_write_failure_preserves_previous_pair(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    def fail(*args, **kwargs):
        raise OSError("synthetic ZIP write failure")
    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(OSError, match="ZIP write"):
        build(release_project, output)
    _assert_pair(output, sha, previous)


def test_sha_write_failure_preserves_previous_pair(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release._write_fsynced
    def write(path, data):
        if path.name == "new.sha256":
            raise OSError("synthetic SHA write failure")
        original(path, data)
    monkeypatch.setattr(release, "_write_fsynced", write)
    with pytest.raises(OSError, match="SHA write"):
        build(release_project, output)
    _assert_pair(output, sha, previous)


@pytest.mark.parametrize("stage", ["new.zip", "new.sha256"])
def test_zip_replace_failure_preserves_previous_pair(release_project, tmp_path, monkeypatch, stage):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release.os.replace
    def replace(source, target):
        if Path(source).name == stage:
            raise OSError("synthetic replace failure")
        original(source, target)
    monkeypatch.setattr(release.os, "replace", replace)
    with pytest.raises(OSError, match="replace failure"):
        build(release_project, output)
    _assert_pair(output, sha, previous)


def test_sha_replace_failure_rolls_back_previous_pair(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release.os.replace
    def replace(source, target):
        if Path(source).name == "new.sha256":
            assert output.read_bytes() != previous[0]
            assert sha.read_bytes() == previous[1]
            raise OSError("synthetic SHA replace failure")
        original(source, target)
    monkeypatch.setattr(release.os, "replace", replace)
    with pytest.raises(OSError, match="SHA replace"):
        build(release_project, output)
    _assert_pair(output, sha, previous)


def test_post_publish_verification_failure_rolls_back_pair(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release._verify_pair
    def verify(archive, checksum, final_name, members=None):
        if archive == output and members is not None:
            raise OSError("synthetic post-publication failure")
        return original(archive, checksum, final_name, members)
    monkeypatch.setattr(release, "_verify_pair", verify)
    with pytest.raises(OSError, match="post-publication"):
        build(release_project, output)
    _assert_pair(output, sha, previous)


@pytest.mark.parametrize("phase", ["staging", "final"])
def test_directory_sync_failure_preserves_previous_pair(release_project, tmp_path, monkeypatch, phase):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release._sync_directory
    failed = False
    def sync(path):
        nonlocal failed
        should_fail = path != output.parent if phase == "staging" else path == output.parent and output.read_bytes() != previous[0]
        if should_fail and not failed:
            failed = True
            raise OSError("synthetic directory sync failure")
        original(path)
    monkeypatch.setattr(release, "_sync_directory", sync)
    with pytest.raises(OSError, match="directory sync"):
        build(release_project, output)
    assert failed
    _assert_pair(output, sha, previous)


@pytest.mark.parametrize("phase", ["sha_replace", "post_verify"])
def test_without_previous_pair_cleans_failed_publication(release_project, tmp_path, monkeypatch, phase):
    output = tmp_path / "out.zip"
    if phase == "sha_replace":
        original = release.os.replace
        def replace(source, target):
            if Path(source).name == "new.sha256":
                raise OSError("synthetic publication failure")
            original(source, target)
        monkeypatch.setattr(release.os, "replace", replace)
    else:
        original = release._verify_pair
        def verify(archive, checksum, final_name, members=None):
            if archive == output:
                raise OSError("synthetic publication failure")
            return original(archive, checksum, final_name, members)
        monkeypatch.setattr(release, "_verify_pair", verify)
    with pytest.raises(OSError, match="publication failure"):
        build(release_project, output)
    assert not output.exists()
    assert not output.with_suffix(".zip.sha256").exists()
    assert list(tmp_path.glob(".out.zip-*")) == []


def test_rollback_failure_retains_recovery_pair(release_project, tmp_path, monkeypatch):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    original = release.os.replace
    def replace(source, target):
        if Path(source).name in ("new.sha256", "restore.sha256"):
            raise OSError("synthetic double filesystem failure")
        original(source, target)
    monkeypatch.setattr(release.os, "replace", replace)
    with pytest.raises(RuntimeError, match="recovery retained at") as failure:
        build(release_project, output)
    staging, = tmp_path.glob(".out.zip-*")
    assert str(staging) in str(failure.value)
    assert ((staging / "previous.zip").read_bytes(), (staging / "previous.sha256").read_bytes()) == previous
    assert release._verify_pair(staging / "previous.zip", staging / "previous.sha256", output.name)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_retains_recovery_pair_and_verifier_rejects_mixed_pair(release_project, tmp_path, monkeypatch, interruption):
    output, sha, previous = _previous_pair(release_project, tmp_path / "dist", "aqla-agent-vps.zip")
    original = release.os.replace
    def replace(source, target):
        if Path(source).name == "new.sha256":
            raise interruption("synthetic interruption between replacements")
        original(source, target)
    monkeypatch.setattr(release.os, "replace", replace)
    with pytest.raises(interruption):
        build(release_project, output)
    staging, = output.parent.glob(".aqla-agent-vps.zip-*")
    assert ((staging / "previous.zip").read_bytes(), (staging / "previous.sha256").read_bytes()) == previous
    assert release._verify_pair(staging / "previous.zip", staging / "previous.sha256", output.name)
    assert output.read_bytes() != previous[0] and sha.read_bytes() == previous[1]
    with pytest.raises(ValueError, match="Invalid ZIP/checksum pair"):
        release._verify_pair(output, sha, output.name)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_artifacts.py")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "VPS archive checksum mismatch" in result.stderr
    assert "passed" not in result.stdout


@pytest.mark.parametrize("kind", ["missing_sha", "bad_sha", "bad_zip"])
def test_invalid_previous_pair_is_preserved(release_project, tmp_path, kind):
    output, sha, previous = _previous_pair(release_project, tmp_path)
    if kind == "missing_sha":
        sha.unlink()
    elif kind == "bad_sha":
        sha.write_text("synthetic invalid checksum", encoding="utf-8")
    else:
        output.write_bytes(b"synthetic invalid ZIP")
    before = output.read_bytes(), sha.read_bytes() if sha.exists() else None
    with pytest.raises(ValueError):
        build(release_project, output)
    assert (output.read_bytes(), sha.read_bytes() if sha.exists() else None) == before


def test_release_archive_has_source_and_deployment_assets_but_no_local_state_or_credentials(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = build(root, tmp_path / "aqla-vps.zip")
    with zipfile.ZipFile(result["archive"]) as archive:
        names = archive.namelist()
        assert "aqla-agent/README.md" in names
        assert "aqla-agent/skills/aqla-master/SKILL.md" in names
        assert "aqla-agent/docs/api-coverage.md" in names
        assert "aqla-agent/scripts/verify_artifacts.py" in names
        assert "aqla-agent/hermes-runtime-overrides.txt" in names
        assert not any(".cache/" in name or ".venv/" in name or "data/state" in name or name.endswith((".secret", ".session", ".env")) for name in names)
    assert len(result["sha256"]) == 64
