import asyncio
import copy
from decimal import Decimal

import pytest
from test_adaptive_tps import setup_signal
from test_backup_and_names import configure as configure_backup
from test_backup_and_names import missing
from test_pipelines import routed  # noqa: F401

from aqla_agent.observations import TemporaryObservation
from aqla_agent.trading import EntryBudgetChanged, Trading
from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Market, Policy
from aqla_hyperliquid_mcp.rules import executable_targets, split_targets


@pytest.mark.parametrize('quantity,count', [('4',5),('1',4),('.6',3),('.3',2),('.1',1)])
def test_adaptive_uses_largest_feasible_prefix_without_increasing_quantity(quantity,count):
    market=Market(symbol='BTC',asset_id=0,product='perp',mid='100',size_decimals=2)
    targets=list(map(Decimal, ('110','120','130','140','150','160')))
    original=targets.copy()
    policy=Policy(take_profit_allocation='adaptive')
    prices,sizes=executable_targets(Decimal(quantity),targets,policy,market)
    assert prices==original[:count] and targets==original
    assert sum(sizes)==Decimal(quantity) and all(size*price>=10 for size,price in zip(sizes,prices,strict=True))
    assert sizes==split_targets(Decimal(quantity),count,policy,market)
    if count<5:
        larger=split_targets(Decimal(quantity),count+1,policy,market)
        assert any(size*price<10 for size,price in zip(larger,targets,strict=False))


@pytest.mark.parametrize('allocation',['equal','halving','custom'])
def test_explicit_distributions_and_single_target_minimum_remain_strict(allocation):
    market=Market(symbol='BTC',asset_id=0,product='perp',mid='100',size_decimals=2)
    policy=Policy(take_profit_allocation=allocation,take_profit_weights=[20]*5)
    with pytest.raises(AqlaError,match='TP leg'):
        executable_targets(Decimal('.3'),list(map(Decimal,('110','120','130','140','150'))),policy,market)
    with pytest.raises(AqlaError,match='TP leg'):
        executable_targets(Decimal('.1'),[Decimal(90)],Policy(take_profit_allocation='adaptive'),market)


@pytest.mark.asyncio
async def test_small_adaptive_trade_keeps_native_stop_and_frozen_count_for_updates_restart(setup):
    _,state,fake,exchange,engine=setup
    state.update(lambda data:data['policy'].update(take_profit_allocation='adaptive',stop_management='tp_ladder',
        sizing_mode='fixed_notional',fixed_notional_usdc='40'))
    source,signal=setup_signal(engine,count=6)
    original=signal.model_dump(mode='json')
    plan=await engine.preview(source.event_id,signal)
    assert len(plan['take_profits'])==3 and plan['source_take_profit_count']==6
    trade=await engine.execute(plan['plan_id'])
    assert trade['status']=='open' and len([row for row in trade['orders'] if row['kind']=='tp'])==3
    stop=next(row for row in trade['orders'] if row['kind']=='sl')
    assert fake.orders[stop['cloid']]['status']=='open'
    await engine.replace_targets(trade['trade_id'],list(map(Decimal,('115','125','135','145','155','165'))),'target-update')
    await Trading(exchange,state).recover()
    current=state.snapshot()['trades'][trade['trade_id']]
    assert current['targets']==['115.00','125.00','135.00']
    assert current['plan']==plan and signal.model_dump(mode='json')==original
    assert all(order['r'] for action in fake.actions if action['type']=='order' for order in action['orders'] if order['c']!=trade['entry_cloid'])


@pytest.mark.asyncio
@pytest.mark.parametrize('source_percentages',[False,True])
async def test_reduced_count_is_kept_during_backup_and_source_roe_finalization(setup,signal,source_percentages):
    _,state,_,_,engine=setup
    state.update(lambda data:data['policy'].update(take_profit_allocation='adaptive',sizing_mode='fixed_notional',fixed_notional_usdc='40'))
    configure_backup(state,stop='40',targets=['5','10','15','20','25'],unit='roe_percent')
    source,value=signal
    value=missing(value,'stop_loss','take_profits')
    if source_percentages:
        # The backend source conversion is separately covered with original quote validation.
        from aqla_hyperliquid_mcp.rules import make_plan
        market=await engine.exchange.market('BTC')
        from aqla_hyperliquid_mcp.models import BackupValues, Interpretation
        interpreted=Interpretation(kind='new_signal',source_refs=value.source_refs,symbol='BTC',side='long',entry='market',
            stop_loss='90',take_profit_unit='roe_percent',take_profits=[5,10,15,20,25])
        policy=Policy(take_profit_allocation='adaptive',sizing_mode='fixed_notional',fixed_notional_usdc='40')
        plan=make_plan(interpreted,market,AccountSnapshot(equity=10000,available_margin=10000),policy,1,source.sent_at,('testnet','account'),backup=BackupValues(leverage=2))
        assert len(plan.take_profits)==len(plan.source_tp['values'])==len(plan.source_tp['preview_prices'])==2
    else:
        plan=await engine.preview(source.event_id,value)
        assert len(plan['take_profits'])==2
        trade=await engine.execute(plan['plan_id'])
        assert trade['status']=='open' and len(trade['backup_finalization']['take_profits'])==2


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['equity','margin'])
async def test_unsent_budget_refresh_only_shrinks_and_original_plan_is_immutable_idempotent(setup,signal,monkeypatch,change):
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    frozen=copy.deepcopy(original)
    account=exchange.account_snapshot
    async def reduced(market):
        result=await account(market)
        return result.model_copy(update={'equity':Decimal(5000)} if change=='equity' else {'available_margin':Decimal(100)})
    monkeypatch.setattr(exchange,'account_snapshot',reduced)
    trade=await engine.execute(original['plan_id'])
    assert trade['status']=='open'
    fresh=trade['plan']
    assert fresh['plan_id']!=original['plan_id'] and Decimal(fresh['quantity'])<Decimal(original['quantity'])
    assert Decimal(fresh['risk_usdc'])<=Decimal(original['risk_usdc']) and fresh['deadline_at']==original['deadline_at']
    assert fresh['source_revisions']==original['source_revisions'] and fresh['effective_leverage']==original['effective_leverage']
    assert state.snapshot()['plans'][original['plan_id']]['plan']==frozen
    assert trade['admission_refresh_from']==original['plan_id']
    sends=len(fake.actions)
    assert (await engine.execute(original['plan_id']))['trade_id']==trade['trade_id']
    assert len(fake.actions)==sends


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_first',[False,True])
async def test_budget_drift_after_accepted_leverage_shrinks_unsent_entry_once(setup,signal,monkeypatch,retry_first):
    from aqla_agent import observations
    from aqla_hyperliquid_mcp.models import now_ms
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account=exchange.account_snapshot
    failing=retry_first
    async def changed(market):
        if failing and any(action['type']=='updateLeverage' for action in fake.actions):
            raise TemporaryObservation('read_unavailable','account','transport')
        result=await account(market)
        return result.model_copy(update={'equity':Decimal(5000)}) if fake.actions else result
    monkeypatch.setattr(exchange,'account_snapshot',changed)
    trade=await engine.execute(original['plan_id'])
    if retry_first:
        assert trade['observation_retry']['attempts']==1
        failing=False
        monkeypatch.setattr(observations,'now_ms',lambda:now_ms()+30001)
        await engine.recover()
        trade=state.snapshot()['trades'][original['trade_id']]
    assert trade['status']=='open'
    fresh=trade['plan']
    assert fresh['quantity']!=original['quantity'] and Decimal(fresh['risk_usdc'])<=Decimal(original['risk_usdc'])
    assert fresh['policy']==original['policy'] and fresh['deadline_at']==original['deadline_at']
    assert fresh['stop_loss']==original['stop_loss'] and fresh['take_profits']==original['take_profits']
    assert state.snapshot()['plans'][original['plan_id']]['plan']==original
    assert state.snapshot()['plans'][original['plan_id']]['refreshed_plan_id']==fresh['plan_id']
    assert len([a for a in fake.actions if a['type']=='updateLeverage'])==1
    assert len([a for a in fake.actions if a['type']=='order' and any(not o['r'] for o in a['orders'])])==1
    sent=copy.deepcopy(fake.actions)
    await engine.execute(original['plan_id'])
    assert fake.actions==sent


@pytest.mark.asyncio
async def test_second_budget_drop_after_owned_refresh_is_terminal_without_entry(setup,signal,monkeypatch):
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account=exchange.account_snapshot
    async def falling(market):
        result=await account(market)
        if not fake.actions:
            return result
        equity=2500 if state.snapshot()['plans'][original['plan_id']].get('refreshed_plan_id') else 5000
        return result.model_copy(update={'equity':Decimal(equity)})
    monkeypatch.setattr(exchange,'account_snapshot',falling)
    with pytest.raises(EntryBudgetChanged):
        await engine.execute(original['plan_id'])
    assert len(state.snapshot()['plans'])==2
    assert state.snapshot()['trades'][original['trade_id']]['status']=='rejected'
    assert not any(a['type']=='order' for a in fake.actions)


@pytest.mark.asyncio
async def test_manual_position_during_budget_refresh_stays_foreign_and_blocks_entry(setup,signal,monkeypatch):
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account=exchange.account_snapshot
    async def changed(market):
        result=await account(market)
        if fake.actions:
            fake.position=1.0
            return result.model_copy(update={'equity':Decimal(5000),'position_size':Decimal(1)})
        return result
    monkeypatch.setattr(exchange,'account_snapshot',changed)
    with pytest.raises(AqlaError,match='exposure'):
        await engine.execute(original['plan_id'])
    assert fake.position==1.0 and len(state.snapshot()['plans'])==1
    assert not any(a['type']=='order' for a in fake.actions)


@pytest.mark.asyncio
async def test_budget_refresh_is_one_durable_attempt_even_when_called_again(setup,signal,monkeypatch):
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account=exchange.account_snapshot
    calls=[]
    async def falling(market):
        calls.append(1)
        result=await account(market)
        return result.model_copy(update={'equity':Decimal(5000) if len(calls)<3 else Decimal(2500)})
    monkeypatch.setattr(exchange,'account_snapshot',falling)
    with pytest.raises(EntryBudgetChanged):
        await engine.execute(original['plan_id'])
    assert len(state.snapshot()['plans'])==2 and not fake.actions
    with pytest.raises(EntryBudgetChanged):
        await engine.execute(original['plan_id'])
    assert len(state.snapshot()['plans'])==2 and not fake.actions


@pytest.mark.asyncio
async def test_observation_retry_after_refresh_uses_only_the_smaller_plan(setup,signal,monkeypatch):
    _,state,_,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account,quote=exchange.account_snapshot,exchange.price_snapshot
    calls=[]
    async def reduced(market):
        return (await account(market)).model_copy(update={'equity':Decimal(5000)})
    async def prices(market):
        calls.append(1)
        if len(calls)==3:
            raise TemporaryObservation('price_stale','price')
        return await quote(market)
    monkeypatch.setattr(exchange,'account_snapshot',reduced)
    monkeypatch.setattr(exchange,'price_snapshot',prices)
    trade=await engine.execute(original['plan_id'])
    fresh_id=state.snapshot()['plans'][original['plan_id']]['refreshed_plan_id']
    assert trade['plan']['plan_id']==fresh_id and trade['observation_retry']['attempts']==1
    assert trade['reserved_risk']==trade['plan']['risk_usdc']
    assert 'observation_retry' not in state.snapshot()['plans'][original['plan_id']]


@pytest.mark.asyncio
async def test_committed_refresh_survives_interruption_before_dispatch_without_another_resize(setup,signal,monkeypatch):
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account,execute=exchange.account_snapshot,engine.execute_locked
    async def reduced(market):
        return (await account(market)).model_copy(update={'equity':Decimal(5000)})
    async def interrupted(plan_id,**kwargs):
        if kwargs.get('refresh_budget') is False:
            raise asyncio.CancelledError
        return await execute(plan_id,**kwargs)
    monkeypatch.setattr(exchange,'account_snapshot',reduced)
    monkeypatch.setattr(engine,'execute_locked',interrupted)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(original['plan_id'])
    fresh_id=state.snapshot()['plans'][original['plan_id']]['refreshed_plan_id']
    assert state.snapshot()['plans'][fresh_id]['refreshed_from_plan_id']==original['plan_id'] and not fake.actions
    resumed=await Trading(exchange,state).execute(original['plan_id'])
    assert resumed['status']=='open' and resumed['plan']['plan_id']==fresh_id
    assert len(state.snapshot()['plans'])==2


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['quantity','market'])
async def test_refreshed_plan_is_rejected_before_save_if_it_changes_original_intent(setup,signal,monkeypatch,damage):
    from aqla_agent import trading as module
    _,state,fake,exchange,engine=setup
    state.update(lambda data:data['policy'].update(max_notional_usdc='5000'))
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account,make=exchange.account_snapshot,module.make_plan
    async def reduced(market):
        return (await account(market)).model_copy(update={'equity':Decimal(5000)})
    def enlarged(*args,**kwargs):
        plan=make(*args,**kwargs)
        if damage=='quantity':
            plan.quantity=Decimal(original['quantity'])+1
            plan.risk_usdc=Decimal(original['risk_usdc'])*2
        else:
            plan.market=plan.market.model_copy(update={'asset_id':plan.market.asset_id+1})
        return plan
    monkeypatch.setattr(exchange,'account_snapshot',reduced)
    monkeypatch.setattr(module,'make_plan',enlarged)
    with pytest.raises(EntryBudgetChanged,match='original quantity' if damage=='quantity' else 'original market'):
        await engine.execute(original['plan_id'])
    assert len(state.snapshot()['plans'])==1 and not fake.actions and not state.snapshot()['trades']


@pytest.mark.asyncio
async def test_ranked_budget_refresh_preserves_profile_network_and_reason(routed,setup,signal,monkeypatch):  # noqa: F811
    router,_=routed
    state=setup[1]
    state.update(lambda data:data['ranking_policy'].update(enabled=True))
    state.update(lambda data:data['source_ranks']['channel'].update(tier='D'))
    state.update(lambda data:data['pipelines']['source-one'].update(targets=data['pipelines']['source-one']['targets'][:1]))
    source,value=signal
    target=(await router.preview(source.event_id,value))['targets'][0]
    original=target['plan']
    engine=router.engine_for(original['exchange'],original['network'])
    account=engine.exchange.account_snapshot
    async def reduced(market):
        return (await account(market)).model_copy(update={'equity':Decimal(5000)})
    monkeypatch.setattr(engine.exchange,'account_snapshot',reduced)
    result=await router.execute(target['plan_id'])
    assert result['status']=='open', result
    for field in ('exchange','requested_exchange','profile_id','pipeline_id','routing_leg_id','rank_snapshot'):
        assert result['plan'][field]==original[field]
    assert not result['plan'].get('fallback_reason')
    assert Decimal(result['plan']['quantity'])<Decimal(original['quantity'])
    await router.close()


@pytest.mark.asyncio
async def test_configuration_change_before_refresh_cannot_reuse_the_old_execution_request(setup,signal,monkeypatch):
    from aqla_agent.pipelines import ConfigurationChanged
    _,state,fake,exchange,engine=setup
    source,value=signal
    original=await engine.preview(source.event_id,value)
    account,preview=exchange.account_snapshot,engine.preview
    async def reduced(market):
        return (await account(market)).model_copy(update={'equity':Decimal(5000)})
    async def changed(*args,**kwargs):
        if kwargs.get('_refresh_from'):
            def configure(data):
                data['policy']['risk_percent']='0.25'
                data['policy_revision']+=1
            state.update(configure)
        return await preview(*args,**kwargs)
    monkeypatch.setattr(exchange,'account_snapshot',reduced)
    monkeypatch.setattr(engine,'preview',changed)
    with pytest.raises(ConfigurationChanged):
        await engine.execute(original['plan_id'])
    assert not fake.actions and not state.snapshot()['trades'] and len(state.snapshot()['plans'])==1
