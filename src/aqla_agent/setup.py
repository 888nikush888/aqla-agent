"""Private setup-password verification; no password or verifier enters trading state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from aqla_hyperliquid_mcp.models import AqlaError

SCRYPT_N: Literal[131072] = 131072
SCRYPT_R: Literal[8] = 8
SCRYPT_P: Literal[1] = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
_VERIFY_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aqla-setup-verifier")
_VERIFY_SLOT = threading.BoundedSemaphore(1)
CHALLENGE_SECONDS = 120
GRANT_SECONDS = 300
LOCKOUT_SECONDS = 900


@dataclass(frozen=True)
class SetupBinding:
    owner_id: str
    chat_id: str
    gateway_id: str
    session_id: str

    def valid(self) -> bool:
        return all(type(value) is str and value for value in (self.owner_id, self.chat_id, self.gateway_id, self.session_id)) and self.owner_id == self.chat_id


class SetupStatus(TypedDict):
    configured: bool
    active: bool
    remaining_seconds: int
    locked_seconds: int


@dataclass(frozen=True, repr=False)
class SetupLease:
    identity: str
    binding: SetupBinding
    expires_at: float


class SetupGate:
    """MCP-local registered capabilities; restart/rebinding invalidates old leases."""

    def __init__(self, configured: bool, *, clock: Callable[[], float] = time.monotonic):
        self.configured = configured
        self.clock = clock
        self._last_time = clock()
        self._lock = threading.RLock()
        self.generation = secrets.token_hex(32)
        self._bindings: dict[str, SetupBinding] = {}
        self._leases: dict[str, SetupLease] = {}
        self._seen: dict[str, SetupLease] = {}
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def _now(self) -> float:
        self._last_time = max(self._last_time, self.clock())
        return self._last_time

    def bind(self, binding: SetupBinding) -> str:
        if not binding.valid():
            raise AqlaError("Setup requires the actual private owner gateway session")
        with self._lock:
            old = self._bindings.get(binding.owner_id)
            if old != binding:
                self.revoke(binding.owner_id)
                self._bindings[binding.owner_id] = binding
            return self.generation

    @staticmethod
    def _valid_identity(identity):
        return type(identity) is str and len(identity) == 64 and all(char in "0123456789abcdef" for char in identity)

    def register(self, binding: SetupBinding, identity: str, expires_at: float | None, generation: str) -> None:
        with self._lock:
            now = self._now()
            if expires_at is None:
                expires_at = now + GRANT_SECONDS
            if (not self.configured or not binding.valid() or self._bindings.get(binding.owner_id) != binding
                    or now < self._locked_until.get(binding.owner_id, 0)
                    or generation != self.generation or not self._valid_identity(identity)
                    or type(expires_at) not in (int, float) or not now < expires_at <= now + GRANT_SECONDS):
                raise AqlaError("Setup registration is unavailable, expired or not bound to the current native session")
            existing = self._seen.get(identity)
            lease = SetupLease(identity, binding, expires_at)
            if existing is not None and (existing != lease or identity not in self._leases):
                raise AqlaError("An existing setup lease cannot be renewed or rebound")
            self._leases[identity] = lease
            self._seen[identity] = lease
            self._failures[binding.owner_id] = 0

    def failed(self, binding: SetupBinding) -> SetupStatus:
        with self._lock:
            if self._bindings.get(binding.owner_id) != binding:
                raise AqlaError("Setup failure requires the current private native binding")
            now = self._now()
            if now >= self._locked_until.get(binding.owner_id, 0):
                attempts = self._failures.get(binding.owner_id, 0) + 1
                self._failures[binding.owner_id] = attempts % 5
                if attempts >= 5:
                    self._locked_until[binding.owner_id] = now + LOCKOUT_SECONDS
            return self.status(binding)

    def require(self, identity: str | None, binding: SetupBinding | None) -> None:
        with self._lock:
            lease = self._leases.get(identity) if type(identity) is str else None
            if (not self.configured or lease is None or binding != lease.binding or self._bindings.get(binding.owner_id) != binding
                    or self._now() >= lease.expires_at):
                raise AqlaError("A current private owner Setup grant is required for this change")

    def revoke(self, owner_id: str) -> None:
        with self._lock:
            self._leases = {key: value for key, value in self._leases.items() if value.binding.owner_id != owner_id}
            self._bindings.pop(owner_id, None)

    def status(self, binding: SetupBinding | None = None, identity: str | None = None, *, owner_id: str | None = None) -> SetupStatus:
        with self._lock:
            now = self._now()
            lease = self._leases.get(identity) if type(identity) is str else None
            active = bool(lease and binding == lease.binding and self._bindings.get(binding.owner_id) == binding and now < lease.expires_at)
            return {"configured": self.configured, "active": active,
                    "remaining_seconds": max(0, int(lease.expires_at - now)) if active and lease is not None else 0,
                    "locked_seconds": max(0, int(self._locked_until.get(binding.owner_id if binding else owner_id or "", 0) - now))}


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
    # Keep the exclusive partial file for explicit repair if a durable write fails.
    with os.fdopen(descriptor, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


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
        except (OSError, ValueError, TypeError):
            raise AqlaError("Private setup verifier is invalid or unavailable") from None

    def matches(self, password: str) -> bool:
        if not isinstance(password, str) or not password:
            return False
        try:
            actual = hashlib.scrypt(password.encode("utf-8"), salt=self._salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                                    maxmem=SCRYPT_MAXMEM, dklen=32)
            return secrets.compare_digest(actual, self._digest)
        except ValueError:
            return False

    async def verify(self, password: str) -> bool:
        if not isinstance(password, str) or not 1 <= len(password) <= 4096 or not _VERIFY_SLOT.acquire(blocking=False):
            return False
        def check():
            try:
                return self.matches(password)
            finally:
                _VERIFY_SLOT.release()
        # The worker owns the slot until hashing finishes, even if admission is cancelled.
        return await asyncio.get_running_loop().run_in_executor(_VERIFY_WORKER, check)
