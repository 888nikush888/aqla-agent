"""Installation-free path and archive-metadata rules for shipping artifacts."""
import re
import stat
from pathlib import PurePosixPath, PureWindowsPath

FORBIDDEN_PARTS = {
    ".git", ".cache", ".venv", "__pycache__", "secrets", "hermes-home", "hermes-source",
    "hermes-venv", ".python", "reports", "dist",
}
FORBIDDEN_NAMES = {"vps.env", "config.yaml", "state.json", "state.json.lock", ".coverage"}
LOCAL_DATABASE = re.compile(r"\.(?:db|sqlite|sqlite3|session)(?:-(?:journal|wal|shm|mj.*))?(?:\.lock|\.initialized)*$")


def unsafe_member_reason(name: str) -> str | None:
    """Check the original archive spelling before any package prefix is removed."""
    if not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        return "empty name or control character"
    if "\\" in name or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive:
        return "absolute, drive, UNC or backslash path"
    if any(part in ("", ".", "..") for part in name.removesuffix("/").split("/")):
        return "empty, current or parent path segment"
    return None


def forbidden_reason(name: str) -> str | None:
    for original in PurePosixPath(name.replace("\\", "/")).parts:
        part = original.casefold()
        if part in FORBIDDEN_PARTS or part in FORBIDDEN_NAMES:
            return "local state or credential path"
        if part != ".env.example" and ".env" in part:
            return "local environment path"
        if part.endswith((".secret", ".pyc", ".initialized")) or LOCAL_DATABASE.search(part):
            return "local state or credential suffix"
    return None


def forbidden(name: str) -> bool:
    return forbidden_reason(name) is not None


def validate_member(name: str) -> None:
    reason = unsafe_member_reason(name) or forbidden_reason(name)
    if reason:
        raise ValueError(f"Unsafe artifact member ({reason}): {name!r}")


def validate_zip_members(archive) -> None:
    for member in archive.infolist():
        validate_member(member.filename)
        mode = member.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"Non-regular ZIP member: {member.filename!r}")
        if member.external_attr & 0x400:
            raise ValueError(f"Reparse ZIP member: {member.filename!r}")


def validate_tar_members(archive) -> None:
    for member in archive.getmembers():
        validate_member(member.name)
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Non-regular TAR member: {member.name!r}")
