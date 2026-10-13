"""Safe source diagnostics. Provider exception text never enters durable state."""
from __future__ import annotations

import re

import httpx
from pydantic import ValidationError

from .decisions import DecisionFailure

FIELDS = {"kind", "source_refs", "symbol", "side", "entry", "entry_timing", "entry_prices",
          "stop_loss", "leverage", "take_profits", "take_profit_unit", "update_action",
          "reduce_percent", "evidence", "reason", "reason_codes", "bound_trade_id"}


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else "processing_failed"


def sanitize_detail(value, code="processing_failed"):
    value = value if isinstance(value, dict) else {}
    phase = value.get("phase")
    name = value.get("exception_type")
    paths = value.get("field_paths", [])
    paths = paths if isinstance(paths, list) else []
    return {"phase": phase if phase in ("prepare", "decision", "interpretation_fallback", "validate", "apply") else "validate",
            "code": safe_code(code),
            "exception_type": name if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,59}", name) else "UnknownError",
            "retryable": value.get("retryable") is True and safe_code(code) in ("provider_timeout", "provider_transport", "provider_rate_limited", "provider_unavailable"),
            "field_paths": sorted({path for path in paths if isinstance(path, str) and path in FIELDS})[:20]}


def classify_failure(error, phase="validate"):
    """Classify only known types/statuses; never log repr/str of foreign errors."""
    cause, seen = error, set()
    code, retryable = "source_processing_failed", False
    fields = []
    exception_type = type(error).__name__
    if isinstance(error, DecisionFailure):
        code = safe_code(str(error))
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        name = type(cause).__name__
        response = getattr(cause, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)) or name in ("APITimeoutError",):
            code, retryable = "provider_timeout", True
            exception_type = name
            break
        if isinstance(cause, (ConnectionError, httpx.TransportError)) or name in ("APIConnectionError",):
            code, retryable = "provider_transport", True
            exception_type = name
            break
        if status == 429 or name == "RateLimitError":
            code, retryable = "provider_rate_limited", True
            exception_type = name
            break
        if isinstance(status, int) and 500 <= status <= 599:
            code, retryable = "provider_unavailable", True
            exception_type = name
            break
        if isinstance(cause, ValidationError):
            exception_type = name
            code = "fallback_invalid_schema" if phase == "interpretation_fallback" else "interpretation_invalid_schema"
            fields = [item["loc"][0] for item in cause.errors(include_url=False, include_context=False, include_input=False)
                      if item["loc"] and item["loc"][0] in FIELDS]
        cause = cause.__cause__
    return sanitize_detail({"phase": phase, "exception_type": exception_type,
                            "retryable": retryable, "field_paths": fields}, code)


class SourceFailure(DecisionFailure):
    def __init__(self, detail):
        self.detail = sanitize_detail(detail, detail.get("code"))
        super().__init__(self.detail["code"])
