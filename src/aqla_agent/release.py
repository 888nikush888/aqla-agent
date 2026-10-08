"""Build a checked VPS bundle and publish its ZIP/checksum pair with recovery."""
import argparse
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from aqla_agent.artifact_rules import validate_member, validate_zip_members

SHIPPING_DIRS = ("src", "hermes_plugin", "skills", "tests", "docs", "scripts")
SHIPPING_FILES = (
    "README.md", "IMPLEMENTATION_STATUS.md", "pyproject.toml", "uv.lock",
    "config.example.yaml", "hermes-runtime-overrides.txt",
)


def _lexical_path(path: Path) -> Path:
    if ".." in path.parts or any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        raise ValueError(f"Unsafe filesystem path: {str(path)!r}")
    # abspath is lexical: unlike resolve(), it never follows a link.
    return Path(os.path.abspath(path))


def _checked_stat(path: Path, *, directory: bool = False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Linked/reparse filesystem path: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError(f"Non-regular {'directory' if directory else 'file'}: {path}")
    return info


def _check_chain(path: Path, *, create: bool = False) -> None:
    for component in (*reversed(path.parents), path):
        try:
            _checked_stat(component, directory=True)
        except FileNotFoundError:
            if not create:
                raise
            component.mkdir()
            _checked_stat(component, directory=True)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_source(path: Path, expected) -> bytes:
    _check_chain(path.parent)
    if _identity(_checked_stat(path)) != _identity(expected):
        raise ValueError(f"Source changed before reading: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as source:
        if _identity(os.fstat(source.fileno())) != _identity(expected):
            raise ValueError(f"Source changed while opening: {path}")
        data = source.read()
        if _identity(os.fstat(source.fileno())) != _identity(expected):
            raise ValueError(f"Source changed while reading: {path}")
        return data


def _collect_sources(project: Path):
    files = []

    def walk(directory, skip_cache=False):
        _check_chain(directory)
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                info = path.lstat()
                # Reject links before the cache exception, without following them.
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError(f"Linked/reparse source: {path}")
                cached = skip_cache or path.name.casefold() == "__pycache__" or path.suffix.casefold() == ".pyc"
                if cached:
                    if stat.S_ISDIR(info.st_mode):
                        walk(path, skip_cache=True)
                    continue
                relative = path.relative_to(project).as_posix()
                validate_member(relative)
                if stat.S_ISDIR(info.st_mode):
                    walk(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append((path, info, "aqla-agent/" + relative))
                else:
                    raise ValueError(f"Non-regular source: {path}")

    # Check every allowlist root before traversing any of them.
    for name in SHIPPING_DIRS:
        _checked_stat(project / name, directory=True)
    for name in SHIPPING_FILES:
        path = project / name
        info = _checked_stat(path)
        validate_member(name)
        files.append((path, info, "aqla-agent/" + name))
    for name in SHIPPING_DIRS:
        walk(project / name)
    return sorted(files, key=lambda item: item[2])


def _write_fsynced(path: Path, data: bytes) -> None:
    with path.open("xb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if os.name == "nt" and error.errno in (errno.EACCES, errno.EINVAL):
            return  # Windows does not expose directory fsync through this API.
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def _verify_pair(archive_path: Path, sha_path: Path, final_name: str, members=None) -> str:
    _check_chain(archive_path.parent)
    archive_info, sha_info = _checked_stat(archive_path), _checked_stat(sha_path)
    sha = _read_source(sha_path, sha_info).decode("utf-8")
    archive_bytes = _read_source(archive_path, archive_info)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    if sha != f"{digest}  {final_name}\n":
        raise ValueError(f"Invalid ZIP/checksum pair: {archive_path}")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        validate_zip_members(archive)
        if archive.testzip() is not None:
            raise ValueError(f"ZIP integrity failed: {archive_path}")
        if members is not None and archive.namelist() != members:
            raise ValueError(f"ZIP member set changed: {archive_path}")
    return digest


def _restore_pair(output, sha, recovery_zip, recovery_sha, staging):
    # Never consume the durable backups during a fallible restoration.
    for target, backup, name in ((output, recovery_zip, "restore.zip"), (sha, recovery_sha, "restore.sha256")):
        temporary = staging / name
        _write_fsynced(temporary, _read_source(backup, _checked_stat(backup)))
        _check_chain(target.parent)
        if target.exists() or target.is_symlink():
            _checked_stat(target)
        os.replace(temporary, target)
    _verify_pair(output, sha, output.name)
    _sync_directory(output.parent)


def build(project: Path, output: Path):
    project, output = _lexical_path(project), _lexical_path(output)
    sha = output.with_suffix(output.suffix + ".sha256")
    for target in (output, sha):
        if any(target.is_relative_to(project / name) for name in SHIPPING_DIRS) or target in (
            project / name for name in SHIPPING_FILES
        ):
            raise ValueError(f"Output overlaps a shipping path: {target}")
    if output.suffix.casefold() != ".zip":
        raise ValueError(f"Release output must have a .zip suffix: {output}")
    _check_chain(project)
    _check_chain(output.parent, create=True)
    previous = []
    for target in (output, sha):
        try:
            previous.append(_checked_stat(target))
        except FileNotFoundError:
            previous.append(None)
    if any(previous):
        if not all(previous):
            raise ValueError(f"Incomplete existing ZIP/checksum pair: {output}")
        _verify_pair(output, sha, output.name)
    files = _collect_sources(project)
    members = [item[2] for item in files]
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    staged_zip, staged_sha = staging / "new.zip", staging / "new.sha256"
    recovery_zip, recovery_sha = staging / "previous.zip", staging / "previous.sha256"
    published = {}
    publication_started = False
    keep_recovery = False
    try:
        with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, info, name in files:
                archive.writestr(name, _read_source(path, info))
        with staged_zip.open("r+b") as source:
            source.flush()
            os.fsync(source.fileno())
        digest = hashlib.sha256(staged_zip.read_bytes()).hexdigest()
        _write_fsynced(staged_sha, f"{digest}  {output.name}\n".encode())
        _verify_pair(staged_zip, staged_sha, output.name, members)
        if all(previous):
            _write_fsynced(recovery_zip, _read_source(output, previous[0]))
            _write_fsynced(recovery_sha, _read_source(sha, previous[1]))
            _verify_pair(recovery_zip, recovery_sha, output.name)
        _sync_directory(staging)
        _sync_directory(output.parent)
        for target, expected, staged in ((output, previous[0], staged_zip), (sha, previous[1], staged_sha)):
            _check_chain(target.parent)
            try:
                current = _checked_stat(target)
            except FileNotFoundError:
                current = None
            if (current is None) != (expected is None) or current and _identity(current) != _identity(expected):
                raise ValueError(f"Output changed before publication: {target}")
            staged_info = _checked_stat(staged)
            publication_started = True
            os.replace(staged, target)
            published[target] = _identity(staged_info)
        _verify_pair(output, sha, output.name, members)
        _sync_directory(output.parent)
    except BaseException as error:
        if not isinstance(error, Exception):
            # Interruptions can leave a mixed pair, including during os.replace().
            # Retain the durable old pair until a caller explicitly recovers it.
            keep_recovery = publication_started
            raise
        if published:
            try:
                if all(previous):
                    _restore_pair(output, sha, recovery_zip, recovery_sha, staging)
                else:
                    for target, identity in published.items():
                        _check_chain(target.parent)
                        if _identity(_checked_stat(target)) != identity:
                            raise ValueError(f"Published target changed during rollback: {target}")
                        target.unlink()
                    _sync_directory(output.parent)
            except BaseException as rollback_error:
                keep_recovery = True
                raise RuntimeError(f"Publication and rollback failed; recovery retained at {staging}") from rollback_error
        raise
    finally:
        if not keep_recovery:
            # A verified final or restored pair already exists; stale staging is harmless.
            shutil.rmtree(staging, ignore_errors=True)
    return {"archive": str(output), "sha256": digest, "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description="Build an aqla VPS source archive")
    parser.add_argument("--output", default="dist/aqla-agent-vps.zip")
    args = parser.parse_args()
    project = Path(__file__).absolute().parents[2]
    if not (project / "pyproject.toml").is_file():
        project = Path.cwd()
    print(json.dumps(build(project, Path(args.output)), indent=2))


if __name__ == "__main__":
    main()
