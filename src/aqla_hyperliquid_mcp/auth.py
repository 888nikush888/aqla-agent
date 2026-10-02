"""Trusted native-session claims; source text never grants operator authority."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from .models import AqlaError, now_ms


class Authority:
    def __init__(self, key: str):
        if len(key) < 32:
            raise AqlaError("Integration key needs at least 32 characters")
        self.key = key.encode()

    @classmethod
    def from_file(cls, path: Path):
        return cls(path.read_text(encoding="utf-8").strip())

    def sign(self, kind, request_id, *, expires_at=None):
        if kind not in ("operator", "source") or not request_id:
            raise AqlaError("Invalid trusted context")
        body = json.dumps({"kind": kind, "request_id": request_id, "expires_at": expires_at or now_ms() + 60000}, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        return json.dumps({"body": body, "signature": signature}, separators=(",", ":"))

    def verify(self, token: str, *, operator=False):
        try:
            value = json.loads(token)
            expected = hmac.new(self.key, value["body"].encode(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(expected, value["signature"]):
                raise ValueError("Signature mismatch")
            claims = json.loads(value["body"])
            if claims["kind"] not in ("operator", "source") or claims["expires_at"] < now_ms() or not claims["request_id"]:
                raise ValueError("Expired or malformed claim")
            if operator and claims["kind"] != "operator":
                raise ValueError("Operator authority required")
            return claims
        except (ValueError, KeyError, TypeError) as error:
            raise AqlaError("Trusted Hermes context is missing, invalid, expired or lacks operator authority") from error

    def internal(self, token: str):
        if not secrets.compare_digest(token.encode(), self.key):
            raise AqlaError("Internal integration access denied")
