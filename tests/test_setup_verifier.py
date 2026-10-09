import asyncio
import json

import pytest

from aqla_agent.setup import (
    SCRYPT_MAXMEM,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SetupVerifier,
    create_password_hash,
    write_password_hash,
)
from aqla_hyperliquid_mcp.models import AqlaError, Settings


@pytest.fixture(scope="module")
def record():
    return create_password_hash("synthetic setup marker")


def test_exact_private_schema_real_scrypt_and_no_verifier_repr(record):
    assert set(record) == {"version", "algorithm", "n", "r", "p", "salt", "digest"}
    assert (record["n"], record["r"], record["p"]) == (SCRYPT_N, SCRYPT_R, SCRYPT_P) == (131072, 8, 1)
    assert SCRYPT_MAXMEM == 268435456 and len(record["salt"]) == 32 and len(record["digest"]) == 64
    verifier = SetupVerifier.from_record(record)
    assert verifier.matches("synthetic setup marker") and not verifier.matches("synthetic wrong marker")
    assert "synthetic setup marker" not in repr(verifier)
    assert record["salt"] not in repr(verifier) and record["digest"] not in repr(verifier)


@pytest.mark.parametrize("change", [{"version": True}, {"algorithm": "other"}, {"n": 1}, {"r": 1}, {"p": 2},
    {"salt": "sensitive malformed salt"}, {"digest": "sensitive malformed digest"}, {"extra": "sensitive value"}])
def test_malformed_verifier_is_denied_without_values(record, change):
    with pytest.raises(AqlaError) as failure:
        SetupVerifier.from_record({**record, **change})
    assert "sensitive" not in str(failure.value) and record["salt"] not in str(failure.value)


def test_private_file_writer_and_parser_never_overwrite(tmp_path):
    path = tmp_path / "setup-verifier.json"
    write_password_hash(path, "synthetic private password")
    before = path.read_bytes()
    assert SetupVerifier.from_file(path).matches("synthetic private password")
    with pytest.raises(FileExistsError):
        write_password_hash(path, "different synthetic password")
    assert path.read_bytes() == before and b"synthetic private password" not in before
    for invalid in (None, tmp_path / "missing"):
        with pytest.raises(AqlaError, match="invalid or unavailable"):
            SetupVerifier.from_file(invalid)


@pytest.mark.asyncio
async def test_expensive_verification_runs_outside_the_event_loop(record, monkeypatch):
    verifier = SetupVerifier.from_record(record)
    original = verifier.matches
    calls = []
    def check(password):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(True)
        return original(password)
    monkeypatch.setattr(SetupVerifier, "matches", lambda self, password: check(password))
    assert await verifier.verify("synthetic setup marker") is True and calls == [True]


def test_file_errors_and_oversize_never_echo_contents(tmp_path):
    path = tmp_path / "setup.json"
    for raw in ("invalid-private-value", json.dumps({"salt": "private marker"}), "private" * 1000):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(AqlaError) as failure:
            SetupVerifier.from_file(path)
        assert "private marker" not in str(failure.value) and "invalid-private-value" not in str(failure.value)


def test_setup_setting_resolves_only_the_private_hash_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('network: testnet\naccount: "0x' + '0' * 40 + '"\nsetup_password_hash_file: secrets/setup-password.json\n', encoding="utf-8")
    assert Settings.load(path).setup_password_hash_file == tmp_path / "secrets/setup-password.json"
