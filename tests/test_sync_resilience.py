from types import SimpleNamespace

import httpx
import pytest

from aqla_hyperliquid_mcp.models import AqlaError
from aqla_sheets_mcp.client import SheetsClient


@pytest.mark.parametrize("failure", [OSError("temporary connection loss"), 408, 429, 500, 502, 503, 504])
def test_sheet_reads_retry_transient_failures_without_exposing_payloads(monkeypatch, failure):
    attempts, delays = [], []
    monkeypatch.setattr("aqla_sheets_mcp.client.time.sleep", delays.append)
    def request(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, request=httpx.Request("GET", "https://synthetic.invalid"), json={"error":"private payload"})
        return httpx.Response(200, request=httpx.Request("GET", "https://synthetic.invalid"), json={"ok": True})
    client=SheetsClient(SimpleNamespace(spreadsheet_id="synthetic"), session=SimpleNamespace(request=request))
    assert client.request("GET") == {"ok": True}
    assert len(attempts) == 2 and len(delays) == 1 and 1 <= delays[0] <= 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_sheet_errors_are_not_retried(monkeypatch, status):
    calls=[]
    monkeypatch.setattr("aqla_sheets_mcp.client.time.sleep", lambda *_: pytest.fail("Permanent rejection was retried"))
    def request(*args, **kwargs):
        calls.append(1)
        return httpx.Response(status, request=httpx.Request("GET", "https://synthetic.invalid"), json={"error":"private payload"})
    client=SheetsClient(SimpleNamespace(spreadsheet_id="synthetic"), session=SimpleNamespace(request=request))
    with pytest.raises(AqlaError, match="remain pending") as caught:
        client.request("GET")
    assert len(calls)==1 and "private payload" not in str(caught.value)


@pytest.mark.parametrize("failure", [OSError("Reply lost after acceptance: private payload"), 429, 503])
def test_uncertain_sheet_write_is_not_replayed(monkeypatch, failure):
    calls=[]
    monkeypatch.setattr("aqla_sheets_mcp.client.time.sleep", lambda *_: pytest.fail("Uncertain write was retried"))
    def request(*args, **kwargs):
        calls.append(1)
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure, request=httpx.Request("POST", "https://synthetic.invalid"))
    client=SheetsClient(SimpleNamespace(spreadsheet_id="synthetic"), session=SimpleNamespace(request=request))
    with pytest.raises(AqlaError, match="remain pending") as caught:
        client.request("POST", json={"requests":[]})
    assert len(calls)==1 and "private payload" not in str(caught.value)


def test_sheet_read_retries_are_bounded_and_respect_retry_after(monkeypatch):
    calls,delays=[],[]
    monkeypatch.setattr("aqla_sheets_mcp.client.time.sleep", delays.append)
    def request(*args, **kwargs):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After":"4"}, request=httpx.Request("GET", "https://synthetic.invalid"))
    client=SheetsClient(SimpleNamespace(spreadsheet_id="synthetic"), session=SimpleNamespace(request=request))
    with pytest.raises(AqlaError, match="remain pending"):
        client.request("GET")
    assert len(calls)==3 and delays==[4,4]
