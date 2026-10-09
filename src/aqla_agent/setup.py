"""Private setup-password verification; no password or verifier enters trading state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from aqla_hyperliquid_mcp.models import AqlaError

SCRYPT_N = 131072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
_VERIFY_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aqla-setup-verifier")


class PasswordHashRecord(TypedDict):
    version: Literal[1]
    algorithm: Literal["scrypt"]
    n: Literal[131072]
    r: Literal[8]
    p: Literal[1]
    salt: str
    digest: str


def create_password_hash(password: str) -> PasswordHashRecord:
    """Root-only opaque import helper; callers must not print the returned private record."""
    if not isinstance(password, str) or not password:
        raise AqlaError("Setup password must be a nonempty string")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=SCRYPT_MAXMEM, dklen=32)
    return {"version": 1, "algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": salt.hex(), "digest": digest.hex()}


def write_password_hash(path: Path, password: str) -> None:
    """Create a new private verifier; never overwrite an existing credential."""
    record = create_password_hash(password)
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        # The caller keeps the exclusive partial file for an explicit safe repair.
        raise


@dataclass(frozen=True, repr=False)
class SetupVerifier:
    _salt: bytes = field(repr=False)
    _digest: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "SetupVerifier(private)"

    @classmethod
    def from_record(cls, value) -> SetupVerifier:
        try:
            if not isinstance(value, dict) or set(value) != {"version", "algorithm", "n", "r", "p", "salt", "digest"}:
                raise ValueError
            expected = {"version": 1, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
            if any(type(value[key]) is not int or value[key] != item for key, item in expected.items()) or value["algorithm"] != "scrypt":
                raise ValueError
            if any(type(value[key]) is not str or len(value[key]) != size or any(char not in "0123456789abcdef" for char in value[key])
                   for key, size in (("salt", 32), ("digest", 64))):
                raise ValueError
            return cls(bytes.fromhex(value["salt"]), bytes.fromhex(value["digest"]))
        except (ValueError, TypeError, KeyError):
            raise AqlaError("Private setup verifier is invalid or unavailable") from None

    @classmethod
    def from_file(cls, path: Path | None) -> SetupVerifier:
        try:
            if path is None:
                raise ValueError
            path = Path(path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or os.name == "posix" and info.st_mode & 0o077:
                raise ValueError
            with path.open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError
            return cls.from_record(json.loads(raw))
        except (OSError, ValueError, TypeError, AqlaError):
            raise AqlaError("Private setup verifier is invalid or unavailable") from None

    def matches(self, password: str) -> bool:
        if not isinstance(password, str) or not password:
            return False
        try:
            actual = hashlib.scrypt(password.encode("utf-8"), salt=self._salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                                    maxmem=SCRYPT_MAXMEM, dklen=32)
            return secrets.compare_digest(actual, self._digest)
        except (ValueError, UnicodeError):
            return False

    async def verify(self, password: str) -> bool:
        return await asyncio.get_running_loop().run_in_executor(_VERIFY_WORKER, self.matches, password)
