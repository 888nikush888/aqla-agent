from decimal import Decimal

import pytest
from test_exchange_fallback import ranked

from aqla_agent.entry import PriceSnapshot
from aqla_agent.liquidity import reroute_waiting
from aqla_hyperliquid_mcp.models import AqlaError, Evidence, Interpretation, SourceEvent, now_ms

pytest_plugins = ["test_pipelines"]


def quote(market, quantity, price=100):
    stamp = now_ms()
    return PriceSnapshot(market=market.symbol, bid=price, ask=price, mid=price, stop_reference=price,
        stop_reference_kind="mark", exchange_time=stamp, started_at=stamp, observed_at=stamp,
        bids=[(price, quantity)], asks=[(price, quantity)])


@pytest.mark.asyncio
async def test_executable_depth_selects_one_venue_and_blocks_other_candidate(routed, setup, signal, monkeypatch):
    router, boundaries = routed
    ranked(setup[1], replacements=["aster"])
    async def thin(market):
        return quote(market, Decimal(".01"))
    async def deep(market):
        return quote(market, Decimal(100))
    monkeypatch.setattr(router.engines["hyperliquid"].exchange, "depth_snapshot", thin, raising=False)
    monkeypatch.setattr(router.engines["aster"].exchange, "depth_snapshot", deep, raising=False)
    source, value = signal
    result = await router.preview(source.event_id, value)
    assert result["targets"][0]["exchange"] == "aster"
    saved = setup[1].snapshot()["plans"]
    losers = [identity for identity, row in saved.items() if row.get("liquidity_candidate") and not row.get("liquidity_selected")]
    assert losers
    for identity in losers:
        with pytest.raises(AqlaError, match="candidate plans cannot execute"):
            await router.execute(identity)
    assert not any(boundary.actions for boundary in boundaries.values())
    await router.close()


def limit_signal(router):
    source = SourceEvent(chat_id="channel", message_id=50, sent_at=now_ms(), text="BTC LONG\nLIMIT Entry 95\nSL 90\nTP1 110\nTP2 120")
    router.ingest(source)
    value = Interpretation(kind="new_signal", source_refs=[source.source_id], symbol="BTC", side="long", entry="limit", entry_prices=[95],
        stop_loss=90, take_profits=[110, 120], evidence=[Evidence(field=field, quote=value, source_id=source.source_id) for field, value in (
            ("symbol", "BTC"), ("side", "LONG"), ("entry_prices.0", "95"), ("stop_loss", "90"),
            ("take_profits.0", "110"), ("take_profits.1", "120"))])
    return source, value


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_during_cancel", [False, True])
async def test_zero_fill_handoff_releases_previous_venue_before_one_new_entry(routed, setup, monkeypatch, fill_during_cancel):
    router, boundaries = routed
    ranked(setup[1], replacements=["aster"])
    source, value = limit_signal(router)
    result = await router.preview(source.event_id, value)
    plan = result["targets"][0]["plan"]
    assert plan["exchange"] == "hyperliquid"
    trade = await router.execute(plan["plan_id"])
    assert trade["status"] == "opening" and not Decimal(trade["filled_quantity"])
    await router.recover()
    boundaries["aster"].mid = "94"
    if fill_during_cancel:
        exchange = router.engines["hyperliquid"].exchange
        original = exchange.mutate
        filled = False
        async def race(identity, action, **kwargs):
            nonlocal filled
            if not filled and action["type"] == "cancelByCloid" and any(row["cloid"] == trade["entry_cloid"] for row in action["cancels"]):
                filled = True
                boundaries["hyperliquid"].fill(trade["entry_cloid"], plan["quantity"], 95)
            return await original(identity, action, **kwargs)
        monkeypatch.setattr(exchange, "mutate", race)
    await reroute_waiting(router)
    snapshot = setup[1].snapshot()
    old = snapshot["trades"][trade["trade_id"]]
    if fill_during_cancel:
        assert old["venue_switch"]["phase"] == "abandoned"
        assert len(snapshot["trades"]) == 1
        assert boundaries["hyperliquid"].position > 0
        assert not boundaries["aster"].actions
    else:
        assert old["status"] == "cancelled" and old["cleanup_complete"]
        assert old["venue_switch"]["phase"] == "done"
        current = snapshot["trades"][snapshot["plans"][old["rerouted_to_plan_id"]]["plan"]["trade_id"]]
        assert current["plan"]["exchange"] == "aster"
        assert current["venue_switch_count"] == 1
        assert current["plan"]["deadline_at"] == plan["deadline_at"]
        assert Decimal(current["plan"]["quantity"]) <= Decimal(plan["quantity"])
        assert len([row for row in boundaries["aster"].orders.values() if not row["order"]["reduceOnly"]]) == 1
        before = sum(len(boundary.actions) for boundary in boundaries.values())
        await reroute_waiting(router)
        assert sum(len(boundary.actions) for boundary in boundaries.values()) == before
    await router.close()
