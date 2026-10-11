"""Stateless evidence receipts, separate from operator authorization."""
import hashlib
import hmac
import json
import secrets

from aqla_hyperliquid_mcp.models import AqlaError, digest, now_ms

from .source_queue import canonical_sources, content, recovery_ids, version

REREAD = "Source read receipt is missing, invalid, expired or stale; read source_context for the current anchor again"


def fingerprint(events, event_id, refs=None):
    row = events.get(event_id)
    if not row:
        raise AqlaError(REREAD)
    latest = canonical_sources(events, [row["source_id"]])[row["source_id"]]
    if latest["event_id"] != event_id:
        raise AqlaError("Source anchor is superseded; read source_context for successor " + latest["event_id"])
    refs = set(refs or [row["source_id"]])
    if row["source_id"] not in refs:
        raise AqlaError(REREAD)
    return digest({"sources": sorted((item["event_id"], version(item), content(item)) for item in events.values()
                                      if item["source_id"] in refs),
                   "anchor": {key: row.get(key) for key in ("status", "interpretation", "targets")}})


def issue_receipt(events, event_id, key, *, stamp=None, context_rows=None):
    stamp = now_ms() if stamp is None else stamp
    refs = sorted({row["source_id"] for row in context_rows}) if context_rows is not None else [events[event_id]["source_id"]]
    body = json.dumps({"kind": "source_read", "event_id": event_id, "refs": refs,
        "fingerprint": fingerprint(events, event_id, refs), "expires_at": stamp + 900000}, sort_keys=True, separators=(",", ":"))
    return json.dumps({"body": body, "signature": hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}, separators=(",", ":"))


def verify_disposition(snapshot, event_id, interpretation, token, key, *, stamp=None):
    stamp = now_ms() if stamp is None else stamp
    try:
        envelope = json.loads(token)
        expected = hmac.new(key, envelope["body"].encode(), hashlib.sha256).hexdigest()
        claims = json.loads(envelope["body"])
        if (not secrets.compare_digest(expected, envelope["signature"]) or claims["kind"] != "source_read"
                or claims["event_id"] != event_id or claims["expires_at"] <= stamp
                or claims["fingerprint"] != fingerprint(snapshot["events"], event_id, claims["refs"])):
            raise ValueError(REREAD)
    except (ValueError, KeyError, TypeError) as error:
        raise AqlaError(REREAD) from error
    row = snapshot["events"][event_id]
    if row["status"] in ("executed", "updated") or event_id in recovery_ids(snapshot):
        raise AqlaError("Financial execution/recovery revision is immutable")
    if row["source_id"] not in interpretation.source_refs or not interpretation.reason.strip():
        raise AqlaError("Classification requires the current anchor reference and a reason")
    if not row["source"].get("text", "").strip() and interpretation.kind == "incomplete":
        return
    if not any(proof.field == "classification" and proof.source_id == row["source_id"]
               and proof.quote.strip() and proof.quote in row["source"]["text"] for proof in interpretation.evidence):
        raise AqlaError("Classification requires a classification evidence quote from the original anchor")
