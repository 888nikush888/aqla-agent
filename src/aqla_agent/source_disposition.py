"""Stateless evidence receipts, separate from operator authorization."""
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import zlib

from aqla_hyperliquid_mcp.models import AqlaError, digest, now_ms

from .percent_targets import SOURCE_TP_PERCENT
from .source_queue import canonical_sources, content, is_source_row, recovery_ids, version

REREAD = "Source read receipt is missing, invalid, expired or stale; read source_context for the current anchor again"


def verify_percentage_classification(interpretation, original):
    """Reject only the fixed-policy uncertainty; never invent a trade decision."""
    if "%" not in original:
        return
    codes = set(interpretation.reason_codes)
    if codes - {"percentage_basis", "other"}:
        return  # An independently specified reason never authorizes a trade.
    explicit = "percentage_basis" in codes
    if not explicit and interpretation.kind not in ("ambiguous", "incomplete"):
        return  # Ordinary result/non-trade commentary can discuss uncertain ROI.
    # Compatibility for callers without structured codes. This checks only the
    # stated classification cause, not whether the source constitutes a signal.
    reason = interpretation.reason.casefold()
    target = re.search(r"\btps?\b|take.?profits?|targets?|ziele?|тейк|цел[ьи]", reason)
    source_target = SOURCE_TP_PERCENT.search(original)
    percentage = re.search(r"%|percent|prozent|процент", reason)
    basis = re.search(r"\bro[ei]\b|price movement|course|kurs|margin|equity|цены|ценов|марж|движени|доходност", reason)
    uncertain = re.search(r"ambig|unclear|unknown|unspecif|missing|not (?:clear|specified)|mehrdeutig|uneindeutig|unklar|unbekannt|nicht eindeutig|nicht .*belegbar|fehl|неяс|неоднознач|неизвест|непонят|не определ|не указан", reason)
    other = re.search(r"(?:missing|unknown|unclear|ambig|not (?:clear|specified))\s+(?:market|symbol|direction|reference|entry price)|(?:market|symbol|direction|reference|entry price)\s+(?:missing|unknown|unclear|ambig)|(?:markt|symbol|richtung|referenz|einstiegspreis).{0,30}(?:fehl|unbekannt|unklar|mehrdeutig)|(?:fehl|unbekannt|unklar|mehrdeutig).{0,30}(?:markt|symbol|richtung|referenz|einstiegspreis)|(?:рынок|тикер|направлен|ссылк|цен[аы] входа).{0,30}(?:неяс|неизвест|отсутств|не указан)|contradict|widerspr|противореч", reason)
    percentage_only = not other and (explicit or bool((target or source_target) and percentage and basis and uncertain))
    if percentage_only:
        raise AqlaError("Percentage basis alone cannot classify this source as non-actionable: source-roe-v1 defines source TP % as nominal ROE on own initial margin before costs. Re-read source_context and reinterpret with roe_percent; keep all other market, direction, evidence and execution guards.")


def fingerprint(events, event_id, refs=None):
    row = events.get(event_id)
    if not is_source_row(row):
        raise AqlaError(REREAD)
    latest = canonical_sources(events, [row["source_id"]])[row["source_id"]]
    if latest["event_id"] != event_id:
        raise AqlaError("Source anchor is superseded; read source_context for successor " + latest["event_id"])
    refs = set(refs or [row["source_id"]])
    if row["source_id"] not in refs:
        raise AqlaError(REREAD)
    return digest({"sources": sorted((item["event_id"], version(item), content(item)) for item in events.values()
                                      if is_source_row(item) and item["source_id"] in refs),
                   "anchor": {key: row.get(key) for key in ("status", "interpretation", "targets")}})


def issue_receipt(events, event_id, key, *, stamp=None, context_rows=None, compact=False):
    if not is_source_row(events.get(event_id)):
        raise AqlaError(REREAD)
    stamp = now_ms() if stamp is None else stamp
    refs = sorted({row["source_id"] for row in context_rows if is_source_row(row)}) if context_rows is not None else [events[event_id]["source_id"]]
    body = json.dumps({"kind": "source_read", "event_id": event_id, "refs": refs,
        "fingerprint": fingerprint(events, event_id, refs), "expires_at": stamp + 900000}, sort_keys=True, separators=(",", ":"))
    if compact:
        payload = zlib.compress(body.encode(), level=6)
        signature = hmac.new(key, b"aqla-source-read:v2:" + payload, hashlib.sha256).digest()
        return "sr2." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return json.dumps({"body": body, "signature": hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}, separators=(",", ":"))


def verify_disposition(snapshot, event_id, interpretation, token, key, *, stamp=None):
    stamp = now_ms() if stamp is None else stamp
    try:
        if not isinstance(token, str) or len(token) > 32768:
            raise ValueError(REREAD)
        if token.startswith("sr2."):
            _, body, signed = token.split(".")
            payload = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
            signature = base64.b64decode(signed + "=" * (-len(signed) % 4), altchars=b"-_", validate=True)
            expected = hmac.new(key, b"aqla-source-read:v2:" + payload, hashlib.sha256).digest()
            if not secrets.compare_digest(expected, signature):
                raise ValueError(REREAD)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(payload, 65536)
            if not decoder.eof or decoder.unused_data:
                raise ValueError(REREAD)
            claims = json.loads(raw)
        else:
            envelope = json.loads(token)
            expected = hmac.new(key, envelope["body"].encode(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(expected, envelope["signature"]):
                raise ValueError(REREAD)
            claims = json.loads(envelope["body"])
        if (claims["kind"] != "source_read"
                or claims["event_id"] != event_id or claims["expires_at"] <= stamp
                or claims["fingerprint"] != fingerprint(snapshot["events"], event_id, claims["refs"])):
            raise ValueError(REREAD)
    except (ValueError, KeyError, TypeError, binascii.Error, zlib.error, RecursionError) as error:
        raise AqlaError(REREAD) from error
    row = snapshot["events"][event_id]
    if row["status"] in ("executed", "updated") or event_id in recovery_ids(snapshot):
        raise AqlaError("Financial execution/recovery revision is immutable")
    if row["source_id"] not in interpretation.source_refs or not interpretation.reason.strip():
        raise AqlaError("Classification requires the current anchor reference and a reason")
    from aqla_hyperliquid_mcp.rules import verify_setup_references
    verify_setup_references(interpretation, row["source_id"])
    if not row["source"].get("text", "").strip() and interpretation.kind == "incomplete":
        return
    if not any(proof.field == "classification" and proof.source_id == row["source_id"]
               and proof.quote.strip() and proof.quote in row["source"]["text"] for proof in interpretation.evidence):
        raise AqlaError("Classification requires a classification evidence quote from the original anchor")
    verify_percentage_classification(interpretation, row["source"]["text"])
