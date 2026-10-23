"""Independent regressions for the seven confirmed full-program audit findings."""

import copy
import json
from decimal import ROUND_CEILING, Decimal

import httpx
import pytest
from test_source_worker import worker_boundary  # noqa: F401 -- real integration fixture
from test_updates import update

from aqla_agent.decisions import DecisionFailure, DecisionResult
from aqla_agent.messages import telegram_view
from aqla_agent.notifications import notification_page
from aqla_agent.observations import TemporaryObservation
from aqla_agent.router import Router
from aqla_agent.source_worker import SourceWorker
from aqla_agent.usage_metrics import source_efficiency
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Market, SourceEvent, now_ms, require_first
from aqla_hyperliquid_mcp.rules import backup_prices
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


@pytest.mark.asyncio
@pytest.mark.parametrize('venue', ['hyperliquid'])
async def test_slow_account_transport_cannot_authorize_entry(setup, signal, request, monkeypatch, venue):
    fixture = None
    adapter, engine = (setup[3], setup[4]) if fixture is None else (fixture[0], fixture[-1])
    clock = [now_ms()]
    monkeypatch.setattr('time.time_ns', lambda: clock[0] * 1000000)
    original = adapter.info

    async def read(kind, **kwargs):
        value = await original(kind, **kwargs)
        if kind == 'clearinghouseState':
            clock[0] += 11001
        return value
    monkeypatch.setattr(adapter, 'info', read)
    source, value = signal
    with pytest.raises(AqlaError, match='stale'):
        await engine.preview(source.event_id, value)
    assert not setup[2].actions and (not setup[1].snapshot()['operations'])
    if fixture:
        assert not fixture[1].posts
        await adapter.close()


@pytest.mark.parametrize("leverage", [3, 49])
@pytest.mark.parametrize("side", ["long", "short"])
def test_backup_roe_matches_independent_native_margin_oracle(leverage, side):
    market = Market(symbol="BTC", asset_id=1, product="perp", size_decimals=4, mid=100,
                    price_tick=".0001", quantity_step=".0001", margin_leverage_scale=10000, max_leverage=100)
    stop, targets = backup_prices(Decimal(100), side, market, Decimal(40), [Decimal(25), Decimal(50)],
        stop_unit="roe_percent", take_profit_unit="roe_percent", leverage=leverage)
    native_fraction = (Decimal(10000) / leverage).to_integral_value(rounding=ROUND_CEILING)
    margin = Decimal(100) * native_fraction / 10000
    tolerance = market.price_tick / margin * 100
    for price, expected in zip([stop, *targets], [40, 25, 50], strict=True):
        assert abs(abs(price - 100) / margin * 100 - expected) <= tolerance


@pytest.mark.asyncio
async def test_full_mcp_status_preserves_opaque_notice_and_legacy_event(setup):
    settings, state, fake, exchange, _ = setup
    opaque = {"event_id": "old-report", "classification": "report"}
    notice = {"event_id": "old-report", "error": "Synthetic retained notice"}
    state.update(lambda data: (data["events"].update({"old-report": opaque}),
                              data["notifications"].update({"legacy-notice": notice})))
    server = create_server(settings, state=state, exchange=exchange)
    before = copy.deepcopy(state.snapshot())
    try:
        page = notification_page(before, None, {}, require_key=False)
        assert "legacy-notice" in page["notifications"]
        view = telegram_view(before, {}, include_status=False)
        assert "Synthetic retained notice" in " ".join(view["messages"][0]["texts"])
        await server.call_tool("status", {"context_auth": server.aqla_authority.sign("operator", "opaque-notice")})
        assert state.snapshot()["events"]["old-report"] == opaque and state.snapshot()["notifications"]["legacy-notice"] == notice
        assert not fake.actions
    finally:
        await close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["source_record", "source_apply"])
async def test_model_validity_survives_only_failures_after_recording(worker_boundary, monkeypatch, phase):  # noqa: F811 -- async fixture
    extension, server, _state, fake, _, _ = worker_boundary
    source = SourceEvent(chat_id="channel", message_id=907, sent_at=now_ms(), text="Result report")
    server.aqla_trading.ingest(source)
    async def decision(*args, **kwargs):
        raise DecisionFailure("synthetic_unclear")
    from test_source_worker import report
    async def fallback(packet, code, usage):
        usage.append({"phase": "interpretation_fallback", "response_success": True, "contract_valid": True,
                      "interpretation_valid": True})
        return DecisionResult(interpretation=report(packet), model="synthetic", fallback=True)
    call = extension.mcp_call
    async def unavailable(server, tool, args):
        if args["action"] == phase:
            raise AqlaError("Synthetic integration failure")
        return await call(server, tool, args)
    monkeypatch.setattr(extension, "mcp_call", unavailable)
    worker = SourceWorker(extension, {"mode": "hybrid", "model_revision": "fixture"}, fallback=fallback,
                          adapter=type("Fixture", (), {"interpret": staticmethod(decision)})())
    await worker.tick()
    import sqlite3
    with sqlite3.connect(extension.master_path.parents[2] / "cache/aqla-usage.sqlite3") as db:
        metrics = json.loads(db.execute("SELECT metrics FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert metrics["failed"] and metrics["workflow"]["source_job_failed"]
    assert metrics["workflow"]["interpretation_valid"] is (phase == "source_apply")
    assert source_efficiency([metrics])["valid_interpretations"] == int(phase == "source_apply")
    assert not fake.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["before_cancel", "after_cancel", "after_stage"])
async def test_accepted_target_replacement_resumes_exactly_once_after_restart(setup, signal, monkeypatch, failure_at):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = signal
    trade = await router.execute((await router.preview(source.event_id, value))["plan_id"])
    msg, change = update(router, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=[115, 125])
    change.evidence = [Evidence(field=f"take_profits.{i}", source_id=msg.source_id, quote=str(n)) for i, n in enumerate([115, 125])]
    operation = "update:" + msg.event_id
    original = exchange.mutate
    async def interrupted(identity, action, **kwargs):
        if identity == "cancel-targets:" + operation and failure_at == "before_cancel":
            raise AqlaError("Synthetic preparation interruption")
        result = await original(identity, action, **kwargs) if identity != operation else None
        if identity == "cancel-targets:" + operation and failure_at == "after_cancel" or identity == operation:
            raise AqlaError("Synthetic checkpoint interruption")
        return result
    monkeypatch.setattr(exchange, "mutate", interrupted)
    await router.update_trade(msg.event_id, change)
    monkeypatch.setattr(exchange, "mutate", original)
    revision = state.snapshot()["trades"][trade["trade_id"]]["management_revision"]
    await router.close()
    state.close()
    state = State(settings)
    exchange = Hyperliquid(settings, state, httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(fake.handle)))
    restarted = Router(settings, state, exchange)
    await restarted.recover()
    after = copy.deepcopy(state.snapshot())
    assert after["trades"][trade["trade_id"]]["status"] == "open"
    saved = after["trades"][trade["trade_id"]]["target_updates"][operation]
    assert saved["completed"] and operation in after["operations"]
    assert revision == trade["management_revision"] + 1
    assert after["trades"][trade["trade_id"]]["management_revision"] == revision
    assert after["events"][msg.event_id]["status"] == "updated"
    live = [row for row in fake.orders.values() if row["status"] == "open" and row["order"]["reduceOnly"] and not row["order"]["isTrigger"]]
    assert len(live) == 2 and {row["order"]["limitPx"] for row in live} == {"115", "125"}
    assert {row["order"]["cloid"] for row in live} == {row["c"] for row in saved["action"]["orders"]}
    calls = copy.deepcopy(fake.actions)
    await restarted.recover()
    assert fake.actions == calls and state.snapshot()["trades"][trade["trade_id"]]["target_updates"] == after["trades"][trade["trade_id"]]["target_updates"]
    await restarted.close()
    state.close()



@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["terminal", "reduce", "stop", "legacy", "transient", "manual"])
async def test_pending_target_recovery_preserves_later_financial_facts(setup, signal, monkeypatch, later):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = signal
    trade = await router.execute((await router.preview(source.event_id, value))["plan_id"])
    first, change = update(router, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=[115, 125])
    change.evidence = [Evidence(field=f"take_profits.{i}", source_id=first.source_id, quote=str(n)) for i, n in enumerate([115, 125])]
    operation = "update:" + first.event_id
    original = exchange.mutate
    async def interrupted(identity, action, **kwargs):
        if identity == operation:
            raise AqlaError("Synthetic pre-operation interruption")
        return await original(identity, action, **kwargs)
    monkeypatch.setattr(exchange, "mutate", interrupted)
    await router.update_trade(first.event_id, change)
    monkeypatch.setattr(exchange, "mutate", original)
    quantity = Decimal(trade["remaining_quantity"])
    second = None
    if later == "terminal":
        stop = require_first(order for order in trade["orders"] if order["kind"] == "sl")
        fake.fill(stop["cloid"], quantity, "90")
    elif later == "reduce":
        second, change = update(router, 3, "reduce", "Reduce 50%", reduce_percent=50)
        await router.update_trade(second.event_id, change)
    elif later == "stop":
        second, change = update(router, 3, "move_stop", "SL 95", stop_loss=95)
        change.evidence = [Evidence(field="stop_loss", source_id=second.source_id, quote="95")]
        await router.update_trade(second.event_id, change)
    elif later == "legacy":
        def old_shape(data):
            row = data["trades"][trade["trade_id"]]
            row.pop("pending_target_update")
            for name in ("completed", "accepted_revision"):
                row["target_updates"][operation].pop(name)
            data["events"][first.event_id]["status"] = "rejected"
        state.update(old_shape)
        second, change = update(router, 3, "replace_targets", "Neue Ziele 120 und 130", take_profits=[120, 130])
        change.evidence = [Evidence(field=f"take_profits.{i}", source_id=second.source_id, quote=str(n)) for i, n in enumerate([120, 130])]
        await router.update_trade(second.event_id, change)
    elif later == "transient":
        async def unavailable(identity, action, **kwargs):
            if identity == operation:
                raise TemporaryObservation("read_unavailable", "signer", "transport")
            return await original(identity, action, **kwargs)
        monkeypatch.setattr(exchange, "mutate", unavailable)
        await router.recover()
        assert Decimal(str(fake.position)) == quantity and state.snapshot()["venue_enabled"]["hyperliquid"]
        monkeypatch.setattr(exchange, "mutate", original)
    else:
        monkeypatch.setattr(exchange.settings, "shared_account", True)
        fake.position += 1
    calls = copy.deepcopy(fake.actions)
    await router.recover()
    await router.recover()
    after = state.snapshot()
    saved = after["trades"][trade["trade_id"]]
    if later == "terminal":
        assert saved["status"] == "closed" and saved["target_updates"][operation]["closed_without_application"]
        assert operation not in after["operations"] and after["events"][first.event_id]["status"] == "rejected"
    elif later == "manual":
        assert saved["status"] == "conflicted" and fake.actions == calls and Decimal(str(fake.position)) == quantity + 1
    else:
        assert Decimal(str(fake.position)) == (quantity / 2 if later == "reduce" else quantity)
        assert after["venue_enabled"]["hyperliquid"] and not saved.get("execution_incident")
        active = [order for order in saved["orders"] if order["kind"] == "tp" and not order.get("retired")]
        assert sum(Decimal(order["quantity"]) for order in active) == Decimal(str(fake.position))
        assert {order["price"] for order in active} == ({"120", "130"} if later == "legacy" else {"115", "125"})
        if later == "stop":
            assert Decimal(saved["stop_price"]) == 95 and after["events"][second.event_id]["status"] == "updated"
        if later == "legacy":
            assert saved["target_updates"][operation]["superseded"] and after["events"][first.event_id]["status"] == "superseded"
    await router.close()


@pytest.mark.asyncio
async def test_later_target_generation_supersedes_unsent_one_without_resurrection(setup, signal, monkeypatch):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = signal
    trade = await router.execute((await router.preview(source.event_id, value))["plan_id"])
    first, change = update(router, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=[115, 125])
    change.evidence = [Evidence(field=f"take_profits.{i}", source_id=first.source_id, quote=str(n)) for i, n in enumerate([115, 125])]
    old = "update:" + first.event_id
    original = exchange.mutate
    async def interrupted(identity, action, **kwargs):
        if identity == old:
            raise AqlaError("Synthetic unsent replacement")
        return await original(identity, action, **kwargs)
    monkeypatch.setattr(exchange, "mutate", interrupted)
    await router.update_trade(first.event_id, change)
    monkeypatch.setattr(exchange, "mutate", original)
    latest, change = update(router, 3, "replace_targets", "Neue Ziele 120 und 130", take_profits=[120, 130])
    change.evidence = [Evidence(field=f"take_profits.{i}", source_id=latest.source_id, quote=str(n)) for i, n in enumerate([120, 130])]
    await router.update_trade(latest.event_id, change)
    calls = copy.deepcopy(fake.actions)
    await router.recover()
    after = state.snapshot()
    assert old not in after["operations"] and after["trades"][trade["trade_id"]]["target_updates"][old]["superseded"]
    assert after["events"][first.event_id]["status"] == "superseded" and after["events"][latest.event_id]["status"] == "updated"
    assert fake.actions == calls
    assert {row["order"]["limitPx"] for row in fake.orders.values() if row["status"] == "open" and row["order"]["reduceOnly"] and not row["order"]["isTrigger"]} == {"120", "130"}
    await router.close()


@pytest.mark.asyncio
async def test_unknown_target_operation_is_reconciled_without_a_second_send(setup, signal, monkeypatch):
    settings, state, fake, exchange, _ = setup
    router = Router(settings, state, exchange)
    source, value = signal
    trade = await router.execute((await router.preview(source.event_id, value))["plan_id"])
    msg, change = update(router, 2, "replace_targets", "Neue Ziele 115 und 125", take_profits=[115, 125])
    change.evidence = [Evidence(field=f"take_profits.{i}", source_id=msg.source_id, quote=str(n)) for i, n in enumerate([115, 125])]
    original = exchange.mutate
    async def lost(identity, action, **kwargs):
        if identity == "update:" + msg.event_id:
            fake.lost_response = True
        return await original(identity, action, **kwargs)
    monkeypatch.setattr(exchange, "mutate", lost)
    await router.update_trade(msg.event_id, change)
    monkeypatch.setattr(exchange, "mutate", original)
    operation = "update:" + msg.event_id
    assert state.snapshot()["operations"][operation]["phase"] == "unknown"
    calls = copy.deepcopy(fake.actions)
    await router.recover()
    await router.recover()
    after = state.snapshot()
    assert fake.actions == calls and after["operations"][operation]["phase"] == "reconciled"
    assert after["trades"][trade["trade_id"]]["target_updates"][operation]["completed"]
    assert after["events"][msg.event_id]["status"] == "updated"
    await router.close()
