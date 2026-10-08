import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from aqla_agent.aster import Aster
from aqla_agent.funding import aster_funding, hyperliquid_funding, lighter_funding
from aqla_agent.lighter import Lighter
from aqla_agent.read_budget import (
    ReadBudget,
    ReadBudgetExceeded,
    observation_auth,
    observed_request,
    read_limits,
)
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import Market


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["hyperliquid", "aster", "lighter"])
async def test_budget_counts_actual_history_pagination_transport_calls(venue):
    calls = []
    def transport(request):
        calls.append(request)
        if venue == "hyperliquid":
            body = json.loads(request.content)
            rows = [{"hash": str(index), "time": index, "delta": {"coin": "BTC", "type": "funding", "usdc": "1", "szi": "1"}}
                    for index in range(body["startTime"], body["endTime"] + 1)]
            return httpx.Response(200, json=rows * 500 if len(rows) > 1 else rows)
        if venue == "aster":
            start, end = int(request.url.params["startTime"]), int(request.url.params["endTime"])
            rows = [{"tranId": str(index), "time": index, "incomeType": "FUNDING_FEE", "symbol": "BTCUSDT", "income": "1", "asset": "USDC"}
                    for index in range(start, end + 1)]
            return httpx.Response(200, json=rows * 1000 if len(rows) > 1 else rows)
        return httpx.Response(200, json={"position_fundings": [], "next_cursor": str(len(calls))})
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)) as client:
        adapter = object.__new__({"hyperliquid": Hyperliquid, "aster": Aster, "lighter": Lighter}[venue])
        adapter.client = client
        adapter.settings = SimpleNamespace(account="owner", vault_address=None)
        if venue != "hyperliquid":
            adapter.account = "owner"
        if venue == "aster":
            adapter.signed = lambda method, path, params: {"method": method, "path": path, "params": params}
        elif venue == "lighter":
            adapter.auth = lambda: "synthetic-token"
        budget = ReadBudget("tick", 3, 100, clock=lambda: 0)
        market = Market(symbol="BTC", native_symbol="BTCUSDT", asset_id=1, product="perp", size_decimals=2, max_leverage=20, mid="100")
        fetch = {"hyperliquid": hyperliquid_funding, "aster": aster_funding, "lighter": lighter_funding}[venue]
        with read_limits(budget), pytest.raises(ReadBudgetExceeded, match="tick_request_limit"):
            await fetch(adapter, market, 0, 3)
        assert budget.requests == len(calls) == 3
        assert all(request.method == ("POST" if venue == "hyperliquid" else "GET") for request in calls)


@pytest.mark.asyncio
async def test_redirect_roundtrips_are_counted_and_denied_before_next_transport_call():
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "/redirect-" + str(len(calls))})
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        limit = ReadBudget("tick", 2, 100, clock=lambda: 0)
        with read_limits(limit), pytest.raises(ReadBudgetExceeded, match="tick_request_limit"), observed_request(client, "GET", "/read"):
            await client.get("/read")
        assert len(calls) == limit.requests == 2


@pytest.mark.asyncio
async def test_private_auth_retries_and_transport_failures_consume_actual_requests():
    class Auth(httpx.Auth):
        def auth_flow(self, request):
            response = yield request
            if response.status_code == 401:
                yield request
    calls = []
    def transport(request):
        calls.append(request)
        if len(calls) == 3:
            raise httpx.ConnectError("synthetic transport failure")
        return httpx.Response(401 if len(calls) == 1 else 200, json={})
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport), auth=Auth()) as client:
        limit = ReadBudget("tick", 3, 100, clock=lambda: 0)
        with read_limits(limit):
            with observed_request(client, "GET", "/read"):
                await client.get("/read")
            with pytest.raises(httpx.ConnectError), observed_request(client, "GET", "/read"):
                await client.get("/read")
        assert len(calls) == limit.requests == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_fresh_auth_followup_without_extensions_and_new_url_cannot_escape_budget(method):
    class Auth(httpx.Auth):
        def auth_flow(self, request):
            response = yield request
            if response.status_code == 401:
                yield httpx.Request(method, "https://synthetic.invalid/auth-read")
    calls = []
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", auth=Auth(), transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(401))) as client:
        limit = ReadBudget("tick", 1, 100, clock=lambda: 0)
        with read_limits(limit), pytest.raises(ReadBudgetExceeded, match="tick_request_limit"), observed_request(client, "GET", "/read"):
            await client.get("/read", auth=observation_auth(client))
        assert limit.requests == len(calls) == 1 and calls[0].url.path == "/read"


@pytest.mark.asyncio
async def test_reused_request_marker_after_read_scope_does_not_count_an_unrelated_send():
    calls = []
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))) as client:
        request = client.build_request("GET", "/read")
        limit = ReadBudget("tick", 1, 100, clock=lambda: 0)
        with read_limits(limit), observed_request(client, "GET", "/read"):
            await client.send(request)
        await client.send(request)
        assert limit.requests == 1 and len(calls) == 2


@pytest.mark.asyncio
async def test_job_limit_leaves_tick_capacity_and_observation_context_does_not_count_writes():
    calls = []
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={}))) as client:
        tick, job = ReadBudget("tick", 4, 100, clock=lambda: 0), ReadBudget("job", 1, 100, clock=lambda: 0)
        with read_limits(tick, job):
            with observed_request(client, "POST", "/info"):
                await client.post("/info", json={"type": "userFunding"})
                await client.post("/exchange", json={"synthetic": True})
            with pytest.raises(ReadBudgetExceeded, match="job_request_limit"), observed_request(client, "POST", "/info"):
                await client.post("/info", json={"type": "userFunding"})
        await client.post("/info", json={"type": "userFunding"})
        assert tick.requests == job.requests == 1 and len(calls) == 3


@pytest.mark.asyncio
async def test_parallel_read_contexts_are_isolated_and_time_is_checked_after_slow_response():
    clock = [0.0]
    def transport(request):
        if request.url.path == "/slow":
            clock[0] = 2.0
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)) as client:
        first, second = ReadBudget("tick", 1, 100, clock=lambda: 0), ReadBudget("tick", 2, 100, clock=lambda: 0)
        async def run(limit, count):
            with read_limits(limit):
                for _ in range(count):
                    with observed_request(client, "GET", "/read"):
                        await client.get("/read")
        await asyncio.gather(run(first, 1), run(second, 2))
        assert first.requests == 1 and second.requests == 2
        slow = ReadBudget("job", 3, 1.0, clock=lambda: clock[0])
        with read_limits(slow), pytest.raises(ReadBudgetExceeded, match="job_time_limit"), observed_request(client, "GET", "/slow"):
            await client.get("/slow")
        assert slow.requests == 1
