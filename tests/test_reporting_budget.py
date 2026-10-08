import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_monthly import data, monthly_report, payment, trade

from aqla_agent.reporting import Reporting
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import Market, digest
from aqla_hyperliquid_mcp.state import State


class Clock:
    now = 0.0
    wall = 1800000000000
    def __call__(self):
        return self.now
    def millis(self):
        return self.wall


def fake_state(snapshot):
    return SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda callback: callback(snapshot))


def setup_http(snapshot, client):
    adapter = object.__new__(Hyperliquid)
    adapter.client = client
    adapter.settings = SimpleNamespace(account="owner", vault_address=None)
    state = fake_state(snapshot)
    engine = SimpleNamespace(exchange=adapter, state=state, assert_plan_binding=lambda *_: None)
    return state, SimpleNamespace(engines={"hyperliquid": engine})


@pytest.fixture
def plan_shape(monkeypatch):
    monkeypatch.setattr("aqla_agent.reporting.TradePlan.model_validate", lambda plan: SimpleNamespace(market=Market.model_validate(plan["market"]), created_at=plan["created_at"]))


def financial_transport(rows, calls, *, clock=None, fail=False):
    def respond(request):
        value = json.loads(request.content)
        calls.append(value["type"])
        assert request.url.path == "/info"
        if fail:
            return httpx.Response(503, json={})
        if value["type"] == "userFillsByTime":
            return httpx.Response(200, json=list(rows.values()))
        assert value["type"] == "userFunding"
        return httpx.Response(200, json=[])
    return httpx.MockTransport(respond)


@pytest.mark.asyncio
async def test_multiple_jobs_tick_uses_actual_requests_and_preserves_financial_reports(plan_shape):
    snapshot = data(trade("first"), trade("second"))
    before = monthly_report(snapshot, "2026-10")
    calls, clock = [], Clock()
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=financial_transport(snapshot["trades"]["first"]["fill_events"], calls)) as client:
        state, router = setup_http(snapshot, client)
        result = await Reporting(state, router, clock=clock, wall_clock=clock.millis).tick()
    assert [attempt["trade_id"] for attempt in result["attempts"]] == ["first", "second"]
    assert all(attempt["complete"] for attempt in result["attempts"])
    assert result["requests"] == len(calls) == 4 and all(attempt["requests"] == 2 for attempt in result["attempts"])
    assert before == monthly_report(snapshot, "2026-10")
    assert snapshot["trades"]["first"]["reporting_attempt_sequence"] < snapshot["trades"]["second"]["reporting_attempt_sequence"]


@pytest.mark.asyncio
async def test_request_exhaustion_preserves_verified_facts_and_next_restart_reaches_later_job(plan_shape):
    snapshot = data(trade("a"), trade("b"))
    before = copy.deepcopy(snapshot)
    calls, clock = [], Clock()
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=financial_transport(snapshot["trades"]["a"]["fill_events"], calls)) as client:
        state, router = setup_http(snapshot, client)
        first = await Reporting(state, router, max_requests=1, clock=clock, wall_clock=clock.millis).tick()
        second = await Reporting(state, router, max_requests=2, clock=clock, wall_clock=clock.millis).tick()
    assert [attempt["trade_id"] for attempt in first["attempts"]] == ["a"] and first["stop_reason"] == "tick_request_limit"
    assert [attempt["trade_id"] for attempt in second["attempts"]] == ["b"]
    assert first["requests"] + second["requests"] == len(calls) == 3
    for identity in before["trades"]:
        assert snapshot["trades"][identity]["fill_events"] == before["trades"][identity]["fill_events"]
        assert snapshot["trades"][identity]["funding_events"] == before["trades"][identity]["funding_events"]
        assert snapshot["trades"][identity]["funding_coverage"] == before["trades"][identity]["funding_coverage"]
    assert monthly_report(snapshot, "2026-10") == monthly_report(before, "2026-10")


@pytest.mark.asyncio
async def test_slow_fast_error_jobs_share_tick_fairly_with_fake_clock_and_transport_counts(plan_shape):
    snapshot = data(trade("a-slow"), trade("b-fast"), trade("c-error"), trade("d-later"))
    before = copy.deepcopy(snapshot)
    calls, clock, current = [], Clock(), [None]
    def transport(request):
        body = json.loads(request.content)
        calls.append((current[0], body["type"]))
        if current[0] == "a-slow":
            clock.now += 3.0
        elif current[0] == "c-error":
            return httpx.Response(503, json={})
        return httpx.Response(200, json=list(snapshot["trades"][current[0]]["fill_events"].values()) if body["type"] == "userFillsByTime" else [])
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)) as client:
        state, router = setup_http(snapshot, client)
        reporter = Reporting(state, router, max_requests=10, time_budget_seconds=10, job_time_seconds=2, clock=clock, wall_clock=clock.millis)
        original = reporter.refresh
        async def selected(identity):
            current[0] = identity
            return await original(identity)
        reporter.refresh = selected
        result = await reporter.tick()
    assert [attempt["trade_id"] for attempt in result["attempts"]] == list(snapshot["trades"])
    assert [attempt["reason"] for attempt in result["attempts"]] == ["job_time_limit", "complete", "unavailable", "complete"]
    assert result["requests"] == len(calls) == 6 and result["elapsed_seconds"] == 3.0
    for identity in snapshot["trades"]:
        for key in ("fill_events", "funding_events", "funding_coverage"):
            assert snapshot["trades"][identity][key] == before["trades"][identity][key]
    assert monthly_report(snapshot, "2026-10") == monthly_report(before, "2026-10")


@pytest.mark.asyncio
async def test_cancellation_propagates_after_durable_attempt_and_same_millisecond_restart_is_fair():
    snapshot = data(trade("a"), trade("b"))
    state, clock, starts = fake_state(snapshot), Clock(), []
    async def cancelled(identity):
        starts.append(identity)
        assert snapshot["trades"][identity]["reporting_attempt_sequence"] > 0
        raise asyncio.CancelledError
    first = Reporting(state, SimpleNamespace(), clock=clock, wall_clock=clock.millis)
    first.refresh = cancelled
    with pytest.raises(asyncio.CancelledError):
        await first.tick()
    second = Reporting(state, SimpleNamespace(), clock=clock, wall_clock=clock.millis)
    second.refresh = cancelled
    with pytest.raises(asyncio.CancelledError):
        await second.tick()
    assert starts == ["a", "b"]
    assert snapshot["trades"]["a"]["reporting_attempted_at"] == snapshot["trades"]["b"]["reporting_attempted_at"]


@pytest.mark.asyncio
async def test_actual_sqlite_restart_retains_attempt_order_and_reservation_failure_has_no_reads(setup, monkeypatch):
    settings, state, *_ = setup
    rows = data(trade("a"), trade("b"))["trades"]
    for row in rows.values():
        row["plan"].update(account=settings.account.lower(), network=settings.network, account_key="hyperliquid:" + settings.network)
    state.update(lambda snapshot: snapshot["trades"].update(rows))
    clock, starts = Clock(), []
    async def cancelled(identity):
        starts.append(identity)
        raise asyncio.CancelledError
    reporter = Reporting(state, SimpleNamespace(), clock=clock, wall_clock=clock.millis)
    reporter.refresh = cancelled
    with pytest.raises(asyncio.CancelledError):
        await reporter.tick()
    state.close()
    restarted = State(settings)
    try:
        reporter = Reporting(restarted, SimpleNamespace(), clock=clock, wall_clock=clock.millis)
        reporter.refresh = cancelled
        with pytest.raises(asyncio.CancelledError):
            await reporter.tick()
        assert starts == ["a", "b"]
        before = restarted.snapshot()
        def fail_write(*args):
            raise OSError("synthetic failed metadata persistence")
        monkeypatch.setattr(restarted, "_write", fail_write)
        with pytest.raises(OSError, match="metadata persistence"):
            await reporter.tick()
        assert starts == ["a", "b"] and restarted.snapshot() == before
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_time_slices_use_remaining_tick_time_without_real_sleep_and_keep_all_proofs():
    snapshot = data(*(trade(identity) for identity in ("a", "b", "c", "d")))
    before = copy.deepcopy(snapshot)
    clock, durations = Clock(), []
    async def fake_timeout(coroutine, *, timeout):
        coroutine.close()
        durations.append(timeout)
        clock.now += timeout
        raise TimeoutError
    reporter = Reporting(fake_state(snapshot), SimpleNamespace(), clock=clock, wall_clock=clock.millis,
                         time_budget_seconds=5, job_time_seconds=2, wait_for=fake_timeout)
    result = await reporter.tick()
    assert durations == [2, 2, 1] and result["elapsed_seconds"] == 5
    assert [attempt["trade_id"] for attempt in result["attempts"]] == ["a", "b", "c"]
    assert result["stop_reason"] == "tick_time_limit" and result["requests"] == 0
    assert "reporting_attempt_sequence" not in snapshot["trades"]["d"]
    for identity, row in snapshot["trades"].items():
        for key in ("fill_events", "funding_events", "funding_coverage"):
            assert row[key] == before["trades"][identity][key]
    assert monthly_report(snapshot, "2026-10") == monthly_report(before, "2026-10")


@pytest.mark.asyncio
async def test_job_request_limit_leaves_capacity_for_later_jobs(plan_shape):
    snapshot, calls, clock = data(trade("a"), trade("b"), trade("c")), [], Clock()
    original = copy.deepcopy(snapshot)
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=financial_transport(snapshot["trades"]["a"]["fill_events"], calls)) as client:
        state, router = setup_http(snapshot, client)
        result = await Reporting(state, router, max_requests=6, job_requests=1, clock=clock, wall_clock=clock.millis).tick()
    assert [attempt["trade_id"] for attempt in result["attempts"]] == ["a", "b", "c"]
    assert all(attempt["reason"] == "job_request_limit" for attempt in result["attempts"])
    assert result["requests"] == len(calls) == 3
    assert monthly_report(snapshot, "2026-10") == monthly_report(original, "2026-10")


@pytest.mark.asyncio
async def test_always_failing_job_does_not_starve_later_jobs_across_same_millisecond_restarts(plan_shape):
    snapshot, clock, calls, current = data(trade("a-error"), trade("b"), trade("c")), Clock(), [], [None]
    def transport(request):
        calls.append(current[0])
        body = json.loads(request.content)
        if current[0] == "a-error":
            return httpx.Response(503, json={})
        return httpx.Response(200, json=list(snapshot["trades"][current[0]]["fill_events"].values()) if body["type"] == "userFillsByTime" else [])
    async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)) as client:
        state, router = setup_http(snapshot, client)
        attempted = []
        for _ in range(3):
            reporter = Reporting(state, router, max_jobs=1, clock=clock, wall_clock=clock.millis)
            original = reporter.refresh
            async def selected(identity, original=original):
                current[0] = identity
                return await original(identity)
            reporter.refresh = selected
            result = await reporter.tick()
            attempted.extend(attempt["trade_id"] for attempt in result["attempts"])
    assert attempted == ["a-error", "b", "c"] and calls == ["a-error", "b", "b", "c", "c"]


@pytest.mark.parametrize("options", [{"max_jobs": True}, {"max_requests": 0}, {"job_requests": 2, "max_requests": 1},
                                      {"time_budget_seconds": float("nan")}, {"time_budget_seconds": 41},
                                      {"job_time_seconds": 3, "time_budget_seconds": 2}, {"job_time_seconds": True}])
def test_reporting_rejects_invalid_budget_configuration(options):
    with pytest.raises(ValueError):
        Reporting(None, None, **options)


def baseline_module(path=None):
    path = path or Path(__file__).resolve().parent / "oracles/reporting_b2cf5f5.py"
    spec = importlib.util.spec_from_file_location("reporting_b2_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_b2_oracle_loads_from_gitless_archive_without_subprocess(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parent / "oracles/reporting_b2cf5f5.py"
    target = tmp_path / original.name
    target.write_bytes(original.read_bytes())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    def forbidden(*args, **kwargs):
        pytest.fail("Frozen oracle must not execute Git or another subprocess")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert baseline_module(target).Reporting.__module__ == "reporting_b2_oracle"
    tree = ast.parse(target.read_text(encoding="utf-8"))
    functions = ast.Module(body=[node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))], type_ignores=[])
    assert hashlib.sha256(ast.dump(functions).encode()).hexdigest() == "225fbfa7611d4a3f6ad72f740ac770f7cc643d6a362a8e22fe097caae517b96f"


@pytest.mark.asyncio
async def test_counted_single_job_b2_baseline_and_new_tick_work_without_sla_claims(monkeypatch):
    baseline, clock, counts = baseline_module(), Clock(), []
    monkeypatch.setattr(baseline, "now_ms", clock.millis)
    for implementation in (baseline.Reporting, Reporting):
        snapshot, calls = data(trade("a"), trade("b")), []
        state = fake_state(snapshot)
        async with httpx.AsyncClient(base_url="https://synthetic.invalid", transport=financial_transport({}, calls)) as client:
            adapter = object.__new__(Hyperliquid)
            adapter.client, adapter.settings = client, SimpleNamespace(account="owner", vault_address=None)
            reporter = implementation(state, SimpleNamespace())
            async def refresh(identity, state=state, adapter=adapter):
                await adapter.info("userFunding", user="owner", startTime=1, endTime=2)
                state.update(lambda current: current["trades"][identity].update(reporting_checked_at=clock.millis()))
                return {"complete": True, "reason": "complete"}
            reporter.refresh = refresh
            result = await reporter.tick()
            counts.append(len(calls))
            if implementation is Reporting:
                assert result["requests"] == 2 and len(result["attempts"]) == 2
    assert counts == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "foreign_fill", "missing_fill", "foreign_market", "foreign_currency", "tied_time", "wrong_size",
                                  "changed_payment", "duplicate_payment", "changed_fee", "missing_fee", "foreign_fee_currency", "outage", "manual_closure"])
async def test_refresh_full_financial_coverage_fingerprint_report_and_json_types_match_b2(case, plan_shape, monkeypatch):
    base = baseline_module()
    monkeypatch.setattr(base, "now_ms", lambda: 1800000000000)
    monkeypatch.setattr("aqla_agent.reporting.now_ms", lambda: 1800000000000)
    results = []
    for implementation in (base.Reporting, Reporting):
        row = trade()
        snapshot = data(row)
        observed = copy.deepcopy(list(row["fill_events"].values()))
        facts = [payment("fund", row["entry_accounting"]["entry_at"] + 500, "-1", "USDC", "BTC", size="2")]
        if case == "foreign_fill":
            observed.append({**observed[0], "oid": 999, "tid": 999})
        elif case == "missing_fill":
            observed.pop()
        elif case == "foreign_market":
            facts[0]["market"] = "ETH"
        elif case == "foreign_currency":
            facts[0]["currency"] = "USDT"
        elif case == "tied_time":
            facts[0]["time"] = observed[1]["time"]
        elif case == "wrong_size":
            facts[0]["signed_size"] = "3"
        elif case == "changed_payment":
            row["funding_events"] = {"fund": {**facts[0], "amount": "-2"}}
        elif case == "duplicate_payment":
            facts.append({**facts[0], "amount": "-2"})
        elif case == "changed_fee":
            observed[0]["fee"] = "1"
        elif case == "missing_fee":
            observed[0]["fee"] = None
        elif case == "foreign_fee_currency":
            observed[0]["feeToken"] = "USDT"
        elif case == "manual_closure":
            row["manual_closure"] = True
        async def info(*args, observed=observed, **kwargs):
            if case == "outage":
                raise OSError("synthetic inaccessible transport")
            return copy.deepcopy(observed)
        async def payments(*args, facts=facts):
            return copy.deepcopy(facts)
        state = fake_state(snapshot)
        adapter = SimpleNamespace(info=info, funding_payments=payments, account="owner")
        engine = SimpleNamespace(exchange=adapter, state=state, assert_plan_binding=lambda *_: None)
        await implementation(state, SimpleNamespace(engines={"hyperliquid": engine})).refresh("trade")
        results.append(json.dumps({"trade": row, "report": monthly_report(snapshot, "2026-10")}, ensure_ascii=False, sort_keys=True, allow_nan=False))
    assert results[0] == results[1]
    assert isinstance(json.loads(results[1])["trade"]["funding_coverage"]["complete"], bool)
    assert digest(json.loads(results[0])) == digest(json.loads(results[1]))
