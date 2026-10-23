"""Typed read failures and bounded, durable retry facts, never provider bodies."""
from __future__ import annotations

import httpx

from aqla_hyperliquid_mcp.models import AqlaError, now_ms


class TemporaryObservation(AqlaError):
    def __init__(self, code, stage, category="freshness"):
        self.code, self.stage, self.category = code, stage, category
        super().__init__({"price_stale": "Entry price observation is stale or has an invalid timestamp",
                          "account_stale": "Account observation is stale",
                          "inventory_stale": "DEX position snapshot is stale or unclocked",
                          "wire_expired": "Unsent signed entry packet expired; original signal deadline remains unchanged",
                          "read_unavailable": "Fresh exchange observation is temporarily unavailable"}[code])

    def diagnostic(self):
        return {"code": self.code, "stage": self.stage, "category": self.category, "retryable": True, "exception_type": "TemporaryObservation"}


async def read_observation(call, stage):
    try:
        return await call()
    except (httpx.TransportError, OSError):
        raise TemporaryObservation("read_unavailable", stage, "transport") from None
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (408, 429, 500, 502, 503, 504):
            raise TemporaryObservation("read_unavailable", stage, "transport") from None
        raise


def diagnostic(error, stage):
    if isinstance(error, TemporaryObservation):
        return error.diagnostic()
    name = type(error).__name__
    safe_name = name if name in {"ValueError", "KeyError", "TypeError", "RuntimeError", "AttributeError", "OverflowError", "OSError", "TimeoutError", "ConnectionError", "CancelledError", "AqlaError", "ConfigurationChanged", "EntryBudgetChanged", "ExchangeMinimum", "SymbolUnavailable", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "RemoteProtocolError", "HTTPStatusError"} else "Exception"
    return {"code": "admission_rejected" if isinstance(error, AqlaError) else "unclassified_failure",
            "stage": stage, "category": "protection" if isinstance(error, AqlaError) else "unexpected", "retryable": False, "exception_type": safe_name}


def retry_marker(previous, error, deadline, authority):
    previous = previous or {}
    attempts = previous.get("attempts", 0) + (0 if previous.get("attempt_inflight") else 1)
    exhausted = attempts >= 5 or now_ms() >= deadline
    delay = (30, 60, 120, 300)[min(attempts - 1, 3)] * 1000
    return {**error.diagnostic(), "attempts": attempts, "next_retry_at": min(now_ms() + delay, deadline),
            "deadline_at": deadline, "exhausted": exhausted, "authority": authority, "attempt_inflight": False}


def retry_due(marker):
    return bool(marker and not marker.get("exhausted") and now_ms() >= marker["next_retry_at"])


def not_sent(error, stage):
    return {"error": str(error) if isinstance(error, AqlaError) else "Entry guard failed before any exchange send",
            "diagnostic": diagnostic(error, stage)}
