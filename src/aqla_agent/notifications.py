"""Versioned, read-only notification pages and lossless evidence fragments."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Literal

from pydantic_core import to_json
from typing_extensions import TypedDict

from aqla_hyperliquid_mcp.models import AqlaError, digest

NATIVE_BYTE_BUDGET = 600_000
SortKey = tuple[int, int, int, str]
Notice = dict[str, object]


class NotificationMessage(TypedDict):
    notification_ids: list[str]
    notification_versions: dict[str, str]
    severity: str
    texts: list[str]


class NotificationView(TypedDict):
    status: list[str]
    messages: list[NotificationMessage]
    remaining_notification_ids: list[str]


class NotificationReadResult(TypedDict):
    notifications: dict[str, Notice]
    notification_versions: dict[str, str]
    telegram_view: NotificationView
    pending: int
    next_cursor: str | None


class NotificationPageMetadata(TypedDict):
    pending: int
    next_cursor: str | None
    pagination_available: bool


class NotificationDetailsResult(TypedDict):
    notification_id: str
    version: str
    format: Literal["canonical_json_fragment"]
    fragment: str
    offset_bytes: int
    total_bytes: int
    complete: bool
    next_cursor: str | None


class NotificationAckResult(TypedDict):
    processed: list[str]
    stale: list[str]
    missing: list[str]
    delivery_owned_by: Literal["Hermes"]


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def native_response_bytes(payload) -> int:
    """MCP 2.2 text block plus the pinned renderer's native outer envelope."""
    text = to_json(payload, fallback=str, indent=2).decode("utf-8")
    return len(json.dumps({"result": text}, ensure_ascii=False).encode("utf-8"))


def notification_sort_key(identity: str, notice) -> SortKey:
    event = notice.get("lifecycle", {})
    timestamp = event.get("time", notice.get("time", notice.get("observed_at", 0)))
    return (0 if identity.startswith("protection:") else 1, timestamp if type(timestamp) is int else 0,
            int(event.get("kind", notice.get("kind")) == "closed"), identity)


def _scope(snapshot) -> str:
    return digest({name: snapshot[name] for name in ("account", "trading_account", "network")})


def _require_key(key: bytes | None) -> bytes:
    if key is None:
        raise AqlaError("Trusted Hermes integration key is not configured for notification pagination")
    return key


def _cursor(snapshot, key: bytes, purpose: str, **position) -> str:
    body = {"v": 1, "purpose": purpose, "scope": _scope(snapshot), **position}
    signature = hmac.new(key, b"aqla-notifications:v1:" + purpose.encode() + b"\0" + canonical_json(body), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(canonical_json({"body": body, "signature": signature})).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, snapshot, key: bytes, purpose: str, fields: set[str]):
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
            raise ValueError("Invalid cursor spelling")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
            raise ValueError("Noncanonical cursor")
        value = json.loads(raw)
        if set(value) != {"body", "signature"} or not isinstance(value["signature"], str):
            raise ValueError("Invalid envelope")
        body = value["body"]
        if set(body) != {"v", "purpose", "scope", *fields} or type(body["v"]) is not int or body["v"] != 1:
            raise ValueError("Invalid version/fields")
        expected = hmac.new(key, b"aqla-notifications:v1:" + purpose.encode() + b"\0" + canonical_json(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, value["signature"]) or body["purpose"] != purpose or body["scope"] != _scope(snapshot):
            raise ValueError("Invalid signature, purpose or scope")
        return body
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError) as error:
        raise AqlaError("Notification cursor is invalid, has another purpose or belongs to another account/network") from error


def _summary(identity: str, notice) -> Notice:
    event = notice.get("lifecycle", {})
    facts = event.get("facts", {})
    plan = facts.get("plan", {})
    result = {"detail_level": "summary", "details_available": True,
              "kind": event.get("kind", notice.get("kind", "protection" if identity.startswith("protection:") else "system"))}
    values = {"time": event.get("time"), "observed_at": notice.get("observed_at"), "trade_id": notice.get("trade_id"),
              "account_key": notice.get("account_key", plan.get("execution_account_key")), "account": plan.get("account"),
              "exchange": notice.get("exchange", plan.get("exchange")), "network": notice.get("network", plan.get("network")),
              "market": plan.get("market", {}).get("symbol"), "direction": plan.get("side"), "status": facts.get("status")}
    result.update({name: value for name, value in values.items() if value is not None})
    reason = notice.get("error") or notice.get("reason") or facts.get("close_reason", {}).get("label")
    if reason is not None:
        reason = str(reason)
        result["reason_shortened"] = len(reason) > 500
        result["reason"] = reason[:499] + "…" if len(reason) > 500 else reason
    return result


def notification_page(snapshot, key: bytes | None, exchanges, *, cursor: str | None = None,
                      limit: int = 100, require_key: bool = True) -> NotificationReadResult:
    from .messages import telegram_view
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AqlaError("Notification page limit must be between 1 and 100")
    if require_key or cursor is not None:
        _require_key(key)
    anchor = None
    if cursor is not None:
        body = _decode_cursor(cursor, snapshot, key, "notifications_read", {"last"})
        last = body["last"]
        if not isinstance(last, list) or len(last) != 4 or any(type(value) is not int for value in last[:3]) or not isinstance(last[3], str):
            raise AqlaError("Invalid notification cursor sort key")
        anchor = tuple(last)
    source = snapshot.get("notifications", {})
    ordered = sorted((notification_sort_key(identity, notice), identity) for identity, notice in source.items())
    if anchor is not None:
        ordered = [item for item in ordered if item[0] > anchor]
    selected = ordered[:limit]
    all_versions = {identity: digest(source[identity]) for _, identity in selected}

    def response(count, summary=False) -> NotificationReadResult:
        chosen = selected[:count]
        notices = {identity: _summary(identity, source[identity]) if summary else source[identity] for _, identity in chosen}
        versions = {identity: all_versions[identity] for _, identity in chosen}
        more = count < len(ordered)
        return {"notifications": dict(notices), "notification_versions": dict(versions),
                "telegram_view": telegram_view({**snapshot, "notifications": notices}, exchanges,
                                               notification_versions=versions, include_status=False),
                "pending": len(source), "next_cursor": _cursor(snapshot, key, "notifications_read", last=chosen[-1][0]) if chosen and more and key is not None else None}

    count = len(selected)
    result = response(count)
    while count > 1 and native_response_bytes(result) > NATIVE_BYTE_BUDGET:
        count = max(1, count // 2)
        result = response(count)
    if count == 1 and native_response_bytes(result) > NATIVE_BYTE_BUDGET:
        result = response(1, summary=True)
    if native_response_bytes(result) > NATIVE_BYTE_BUDGET:
        raise AqlaError("Notification response exceeds the native output budget")
    return result


def acknowledge_notifications(data, notification_ids: list[str], versions: dict[str, str] | None) -> NotificationAckResult:
    identities = list(dict.fromkeys(notification_ids))
    notices = data["notifications"]
    if versions is not None and any(identity not in versions for identity in identities):
        raise AqlaError("Supply the read notification version for every requested ID before acknowledgement")
    if versions is None and any(identity.startswith("protection:") and identity in notices for identity in identities):
        raise AqlaError("Protection notifications require a reread and versioned acknowledgement")
    result: NotificationAckResult = {"processed": [], "stale": [], "missing": [], "delivery_owned_by": "Hermes"}
    for identity in identities:
        if identity not in notices:
            result["missing"].append(identity)
        elif versions is not None and digest(notices[identity]) != versions[identity]:
            result["stale"].append(identity)
        else:
            del notices[identity]
            result["processed"].append(identity)
    return result


def _original_notice(snapshot, identity: str, version: str):
    notice = snapshot["notifications"].get(identity)
    if notice is None and identity.startswith("lifecycle:"):
        try:
            trade_id, kind = identity.removeprefix("lifecycle:").rsplit(":", 1)
            event = snapshot["trades"][trade_id]["lifecycle_events"][kind]
            notice = {"trade_id": trade_id, "lifecycle": event, "observed_at": event["observed_at"]}
        except (KeyError, ValueError):
            pass
    if notice is None:
        raise AqlaError("Notification details are no longer available")
    if digest(notice) != version:
        raise AqlaError("Notification version changed; reread the current notification before reading details")
    return notice


def notification_details(snapshot, key: bytes | None, identity: str, version: str, *,
                         cursor: str | None = None, limit: int = 16384) -> NotificationDetailsResult:
    key = _require_key(key)
    if type(limit) is not int or not 1 <= limit <= 65536:
        raise AqlaError("Notification detail limit must be between 1 and 65536 original UTF-8 bytes")
    original = canonical_json(_original_notice(snapshot, identity, version))
    offset = 0
    if cursor is not None:
        body = _decode_cursor(cursor, snapshot, key, "notifications_details", {"id", "version", "offset"})
        if body["id"] != identity or body["version"] != version or type(body["offset"]) is not int or not 0 <= body["offset"] < len(original):
            raise AqlaError("Notification detail cursor has another ID/version or an invalid byte position")
        offset = body["offset"]
    end = min(len(original), offset + limit)
    while True:
        try:
            fragment = original[offset:end].decode("utf-8")
            break
        except UnicodeDecodeError as error:
            if error.start == 0:
                raise AqlaError("Detail limit is too small for the next UTF-8 character; increase the limit") from error
            end = offset + error.start
    complete = end == len(original)
    result: NotificationDetailsResult = {"notification_id": identity, "version": version, "format": "canonical_json_fragment",
                                        "fragment": fragment, "offset_bytes": offset, "total_bytes": len(original), "complete": complete,
                                        "next_cursor": None if complete else _cursor(snapshot, key, "notifications_details", id=identity, version=version, offset=end)}
    if native_response_bytes(result) > NATIVE_BYTE_BUDGET:
        raise AqlaError("Notification detail response exceeds the native output budget")
    return result
