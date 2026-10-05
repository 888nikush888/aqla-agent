import copy
from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import (
    AccountSnapshot,
    AqlaError,
    Evidence,
    Interpretation,
    Market,
    Policy,
    SourceEvent,
    TradePlan,
    now_ms,
)
from aqla_hyperliquid_mcp.rules import make_plan, selected_targets, split_targets


@pytest.mark.parametrize("count,weights", [(1, [100]), (2, [50, 50]), (3, [50, 25, 25]),
    (4, [50, 25, Decimal('12.5'), Decimal('12.5')]), (5, [50, 25, Decimal('12.5'), Decimal('7.5'), 5])])
def test_adaptive_weights_for_every_supported_count(count, weights):
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2)
    sizes = split_targets(Decimal(100), count, Policy(take_profit_allocation="adaptive"), market)
    assert sizes == weights and sum(sizes) == 100


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 6, 20])
@pytest.mark.parametrize("side", ["long", "short"])
def test_selection_and_preview_keep_original_signal(count, side):
    prices = [Decimal(100) + (index + 1) * (1 if side == "long" else -1) for index in range(count)]
    signal = Interpretation(kind="new_signal", source_refs=["source"], symbol="BTC", side=side,
                            stop_loss=90 if side == "long" else 110, entry="market", take_profits=prices)
    original = signal.model_dump()
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, max_leverage=20, size_decimals=2)
    plan = make_plan(signal, market, AccountSnapshot(equity=10000, available_margin=10000),
                     Policy(take_profit_allocation="adaptive", stop_management="tp_ladder"), 1, now_ms(), ("testnet", "account"))
    assert plan.source_take_profit_count == count and len(plan.take_profits) == min(5, count)
    assert sum(plan.tp_quantities) == plan.quantity and signal.model_dump() == original
    assert all(size * price >= market.min_notional for size, price in zip(plan.tp_quantities, plan.take_profits, strict=True))


def test_first_target_and_minimums():
    policy = Policy(take_profit_allocation="first_target")
    assert selected_targets([110, 120, 130], policy) == [110]
    market = Market(symbol="BTC", asset_id=0, product="perp", mid=100, size_decimals=2)
    assert split_targets(Decimal('1.23'), 1, policy, market) == [Decimal('1.23')]
    with pytest.raises(AqlaError):
        split_targets(Decimal('.01'), 5, Policy(take_profit_allocation="adaptive"), market)
    with pytest.raises(AqlaError):
        split_targets(Decimal(10), 6, Policy(take_profit_allocation="adaptive"), market)


def configure(state):
    state.update(lambda data: data['policy'].update(take_profit_allocation='adaptive', stop_management='tp_ladder'))


def setup_signal(trading, side='long', count=5):
    targets = [Decimal(100) + (i + 1) * 10 * (1 if side == 'long' else -1) for i in range(count)]
    stop = 90 if side == 'long' else 110
    text = f'BTC {side} SL {stop} Ziele ' + ' '.join(str(value) for value in targets)
    source = SourceEvent(chat_id='channel', message_id=10, sent_at=now_ms(), text=text)
    trading.ingest(source)
    signal = Interpretation(kind='new_signal', source_refs=[source.source_id], symbol='BTC', side=side, entry='market', stop_loss=stop, take_profits=targets,
        evidence=[Evidence(field=field, source_id=source.source_id, quote=quote) for field, quote in [
            ('symbol','BTC'),('side',side),('stop_loss',str(stop)),*[(f'take_profits.{i}',str(price)) for i,price in enumerate(targets)]]])
    return source, signal


@pytest.mark.asyncio
@pytest.mark.parametrize('side',['long','short'])
async def test_ladder_waits_for_full_own_fills_and_moves_in_order(setup, side):
    _,state,fake,_,trading=setup
    configure(state)
    source,signal=setup_signal(trading,side)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    assert all(row['r'] for action in fake.actions if action['type']=='order' for row in action['orders'] if row['c']!=trade['entry_cloid'])
    for index in range(4):
        target=next(row for row in trade['orders'] if row['kind']=='tp' and row['index']==index)
        fake.mid=str(Decimal(target['price'])+(1 if side=='long' else -1))
        amount=Decimal(target['quantity'])
        if index==0:
            fake.fill(target['cloid'],amount/2,target['price'])
            await trading.refresh_trade(trade['trade_id'])
            assert Decimal(state.snapshot()['trades'][trade['trade_id']]['stop_price'])==signal.stop_loss
            amount/=2
        fake.fill(target['cloid'],amount,target['price'])
        await trading.refresh_trade(trade['trade_id'])
        current=state.snapshot()['trades'][trade['trade_id']]
        expected=100 if index<2 else Decimal(plan['take_profits'][0 if index==2 else 1])
        assert Decimal(current['stop_price'])==expected
    before=len(fake.actions)
    await trading.recover()
    await trading.recover()
    assert len(fake.actions)==before


@pytest.mark.asyncio
async def test_unknown_stop_result_retains_old_stop_and_reuses_intent(setup, monkeypatch):
    _,state,fake,exchange,trading=setup
    configure(state)
    source,signal=setup_signal(trading)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    target=next(row for row in trade['orders'] if row['kind']=='tp' and row['index']==0)
    fake.mid='115'
    fake.fill(target['cloid'],target['quantity'],'110')
    original_status=exchange.order_status
    original_mutate=exchange.mutate
    blocked={'value':True}
    op='tp-ladder:'+trade['trade_id']+':1'
    async def lost(identity,action,**kwargs):
        if identity==op:
            fake.lost_response=True
        return await original_mutate(identity,action,**kwargs)
    async def invisible(identity):
        if identity==exchange.cloid(op) and blocked['value']:
            return {'status':'unknownOid'}
        return await original_status(identity)
    monkeypatch.setattr(exchange,'mutate',lost)
    monkeypatch.setattr(exchange,'order_status',invisible)
    await trading.refresh_trade(trade['trade_id'])
    current=state.snapshot()['trades'][trade['trade_id']]
    assert Decimal(current['stop_price'])==90
    assert fake.orders[next(row['cloid'] for row in trade['orders'] if row['kind']=='sl')]['status']=='open'
    frozen=copy.deepcopy(current['stop_ladder_intents'])
    count=len([action for action in fake.actions if action['type']=='order'])
    blocked['value']=False
    await trading.recover()
    assert len([action for action in fake.actions if action['type']=='order'])==count
    assert state.snapshot()['trades'][trade['trade_id']]['stop_ladder_intents']==frozen
    assert Decimal(state.snapshot()['trades'][trade['trade_id']]['stop_price'])==100


@pytest.mark.asyncio
async def test_target_update_cannot_rewrite_completed_ladder_reference(setup):
    _,state,fake,_,trading=setup
    configure(state)
    source,signal=setup_signal(trading,count=6)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    fake.mid='115'
    target=next(row for row in trade['orders'] if row['kind']=='tp' and row['index']==0)
    fake.fill(target['cloid'],target['quantity'],'110')
    await trading.refresh_trade(trade['trade_id'])
    await trading.replace_targets(trade['trade_id'],[Decimal(value) for value in (115,125,135,145,155,165)],'new-targets')
    for index in (1,2):
        current=state.snapshot()['trades'][trade['trade_id']]
        target=next(row for row in current['orders'] if row['kind']=='tp' and row['index']==index and not row.get('retired'))
        fake.mid=str(Decimal(target['price'])+1)
        fake.fill(target['cloid'],target['quantity'],target['price'])
        await trading.refresh_trade(trade['trade_id'])
    current=state.snapshot()['trades'][trade['trade_id']]
    assert Decimal(current['stop_price'])==110 and Decimal(current['completed_target_prices']['0'])==110


def test_old_trade_and_profile_defaults_do_not_enable_ladder():
    assert Policy().stop_management=='break_even'
    assert Policy().take_profit_allocation=='equal'
    # Old plans accept the new optional count without rehashing existing IDs.
    market=Market(symbol='BTC',asset_id=0,product='perp',mid=100,max_leverage=20,size_decimals=2)
    signal=Interpretation(kind='new_signal',source_refs=['source'],symbol='BTC',side='long',stop_loss=90,take_profits=[110])
    plan=make_plan(signal,market,AccountSnapshot(equity=10000,available_margin=10000),Policy(),1,now_ms(),('testnet','account')).model_dump(mode='json')
    plan.pop('source_take_profit_count')
    plan['policy'].pop('stop_management')
    assert TradePlan.model_validate(plan).plan_id==plan['plan_id']


@pytest.mark.asyncio
async def test_first_target_closes_entire_trade_with_stop_kept_until_fill(setup):
    _,state,fake,_,trading=setup
    state.update(lambda data:data['policy'].update(take_profit_allocation='first_target',stop_management='tp_ladder'))
    source,signal=setup_signal(trading,count=6)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    targets=[row for row in trade['orders'] if row['kind']=='tp']
    assert len(targets)==1 and Decimal(targets[0]['quantity'])==Decimal(trade['filled_quantity'])
    stop=next(row for row in trade['orders'] if row['kind']=='sl')
    assert fake.orders[stop['cloid']]['status']=='open'
    fake.fill(targets[0]['cloid'],targets[0]['quantity'],targets[0]['price'])
    await trading.refresh_trade(trade['trade_id'])
    assert state.snapshot()['trades'][trade['trade_id']]['status']=='closed'
    assert fake.orders[stop['cloid']]['status']=='canceled'


@pytest.mark.asyncio
async def test_gap_to_tp4_uses_highest_confirmed_stage_once(setup):
    _,state,fake,_,trading=setup
    configure(state)
    source,signal=setup_signal(trading)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    fake.mid='145'
    for row in trade['orders']:
        if row['kind']=='tp' and row['index']<4:
            fake.fill(row['cloid'],row['quantity'],row['price'])
    await trading.refresh_trade(trade['trade_id'])
    current=state.snapshot()['trades'][trade['trade_id']]
    assert Decimal(current['stop_price'])==120 and current['stop_ladder_stage']==4
    assert set(current['stop_ladder_intents'])=={'4'}


@pytest.mark.asyncio
async def test_ladder_never_widens_a_manually_tighter_stop(setup):
    _,state,fake,_,trading=setup
    configure(state)
    source,signal=setup_signal(trading)
    plan=await trading.preview(source.event_id,signal)
    trade=await trading.execute(plan['plan_id'])
    fake.mid='135'
    await trading.replace_stop(trade['trade_id'],Decimal(125),'manual-tight-stop')
    for row in trade['orders']:
        if row['kind']=='tp' and row['index']<3:
            fake.fill(row['cloid'],row['quantity'],row['price'])
    await trading.refresh_trade(trade['trade_id'])
    current=state.snapshot()['trades'][trade['trade_id']]
    assert Decimal(current['stop_price'])==125 and current['stop_ladder_stage']==3
