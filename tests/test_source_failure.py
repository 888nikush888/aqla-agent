import json

import httpx
import pytest

from aqla_agent.decisions import DecisionFailure
from aqla_agent.source_failure import classify_failure, sanitize_detail


@pytest.mark.parametrize("status,code,retry", [(429, "provider_rate_limited", True), (503, "provider_unavailable", True),
                                            (401, "decision_api_unavailable", False)])
def test_provider_failure_uses_status_and_type_without_secret_payload(status, code, retry):
    request = httpx.Request("POST", "https://example.test/private", headers={"Authorization": "SECRET"})
    response = httpx.Response(status, text="PROVIDER_SECRET", request=request)
    cause = httpx.HTTPStatusError("TOKEN_SECRET", request=request, response=response)
    error = DecisionFailure("decision_api_unavailable")
    error.__cause__ = cause
    detail = classify_failure(error, "decision")
    assert detail["code"] == code and detail["retryable"] is retry
    assert "SECRET" not in json.dumps(detail)


def test_timeout_retry_is_transport_only_and_unsafe_detail_is_removed():
    assert classify_failure(httpx.ReadTimeout("SECRET"), "interpretation_fallback")["code"] == "provider_timeout"
    value = sanitize_detail({"phase": "SECRET", "exception_type": "Error:SECRET", "retryable": True,
                             "field_paths": "SECRET", "content": "SECRET"}, "unknown_semantic")
    assert not value["retryable"] and value["field_paths"] == [] and "SECRET" not in json.dumps(value)
