import copy
from decimal import Decimal

import pytest
from test_pipelines import routed  # noqa: F401 -- multi-target fixture

from aqla_agent.journal import HEADERS, project
from aqla_agent.pipelines import Parameters, Pipeline, TradingProfile, route_control
from aqla_agent.sources import set_source_name
from aqla_hyperliquid_mcp.models import AccountSnapshot, AqlaError, Interpretation, Market, Policy, now_ms
from aqla_hyperliquid_mcp.rules import make_plan, verify_evidence


def configure(state, *, stop=None, targets=(), leverage=2, unit="course_percent"):
    snapshot = state.snapshot()
    values = {**snapshot['profiles']['backup']['parameters'], 'leverage': leverage,
              'fallback_stop_loss_percent': stop, 'fallback_take_profit_percentages': list(targets),
              'fallback_stop_loss_unit': unit, 'fallback_take_profit_unit': unit}
    profile = TradingProfile(id='backup', name='Backup', parameters=Parameters(**values))
    state.update(lambda data: route_control(data, 'set_profile', expected_revision=data['configuration_revision'], profile=profile))


def missing(signal, *fields):
    return signal.model_copy(update={**{name: [] if name == 'take_profits' else None for name in fields},
                                    'evidence': [row for row in signal.evidence if not any(row.field.startswith(name) for name in fields)]})


@pytest.mark.parametrize('number,expected', [('12.78',12), ('12,78',12), ('20.99',20), ('1.99',1)])
def test_decimal_leverage_is_evidenced_before_rounding(setup,signal,number,expected):
    _,state,*_=setup
    source,original=signal
    source=source.model_copy(update={'text':source.text+' '+number+'x'})
    value=Interpretation.model_validate({**original.model_dump(mode='json'),'leverage':number,
        'evidence':[row.model_dump() for row in original.evidence]+[{'field':'leverage','source_id':source.source_id,'quote':number+'x'}]})
    events={source.event_id:{'event_id':source.event_id,'source_id':source.source_id,'source':source.model_dump(mode='json')}}
    verify_evidence(value,events)
    plan=make_plan(value,Market(symbol='BTC',asset_id=0,product='perp',size_decimals=2,mid='100',max_leverage=20),
                   AccountSnapshot(equity='10000',available_margin='10000'),Policy(leverage_mode='signal'),1,now_ms(),('testnet','owner'))
    assert plan.signal_leverage==expected and plan.effective_leverage==expected
    assert plan.signal_leverage_original==Decimal(number.replace(',','.'))
    with pytest.raises(AqlaError,match='evidence'):
        verify_evidence(value.model_copy(update={'leverage':Decimal(expected)}),events)


@pytest.mark.parametrize('symbol',['GOLD','gold','XAU','XAUUSD','XAUUSDT','XAUUSDC'])
@pytest.mark.parametrize('network',['testnet','mainnet'])
@pytest.mark.asyncio
async def test_gold_alias_uses_only_xyz_dex(setup,symbol,network):
    _,_,_,exchange,_=setup
    seen=[]
    async def info(kind,**params):
        seen.append((kind,params))
        if kind=='allMids':
            return {'xyz:GOLD':'4170'}
        if kind=='meta':
            return {'collateralToken':0,'universe':[{'name':'xyz:GOLD','szDecimals':4,'maxLeverage':25}]}
        if kind=='perpDexs':
            return [None,{'name':'other'},{'name':'xyz'}]
        raise AssertionError(kind)
    exchange.settings.network=network
    exchange.info=info
    market=await exchange.market(symbol)
    assert market.symbol=='xyz:GOLD' and market.dex=='xyz' and market.asset_id==120000
    assert market.quote_token=='USDC'
    assert all(params.get('dex')=='xyz' for kind,params in seen if kind in ('meta','allMids'))


@pytest.mark.parametrize('stop,targets',[('0',[]),('100',[]),('1',['2','1']),('1',['1','1']),('1',['100'])])
def test_invalid_backup_percentages_are_rejected(stop,targets):
    with pytest.raises(ValueError):
        Parameters(fallback_stop_loss_percent=stop,fallback_take_profit_percentages=targets)


@pytest.mark.asyncio
async def test_missing_fields_use_backup_without_changing_risk(setup,signal):
    _,state,fake,_,trading=setup
    source,interpretation=signal
    state.update(lambda data:data['policy'].update(leverage_mode='signal',entry_mode='source'))
    configure(state,stop='1',targets=['1','2','3'])
    interpretation=missing(interpretation,'stop_loss','take_profits','leverage').model_copy(update={'entry':'unspecified','entry_prices':[]})
    plan=await trading.preview(source.event_id,interpretation)
    assert set(plan['backup_fields'])=={'stop_loss','take_profits','leverage','entry'}
    assert plan['policy']['risk_percent']==state.snapshot()['policy']['risk_percent']
    assert plan['effective_leverage']==2 and Decimal(plan['stop_loss'])==99
    trade=await trading.execute(plan['plan_id'])
    assert trade['backup_finalization']['phase']=='applied'
    assert trade['backup_finalization']['reference_entry']=='100'
    assert [Decimal(value) for value in trade['targets']]==[Decimal('101'),Decimal('102'),Decimal('103')]
    before=copy.deepcopy(fake.actions)
    configure(state,stop='5',targets=['5','10'])
    await trading.recover()
    assert fake.actions==before
    assert state.snapshot()['trades'][trade['trade_id']]['targets']==trade['targets']


@pytest.mark.asyncio
async def test_blank_backup_fields_fail_before_exchange_action(setup,signal):
    _,state,fake,_,trading=setup
    source,value=signal
    with pytest.raises(AqlaError,match='Backup SL percentage'):
        await trading.preview(source.event_id,missing(value,'stop_loss'))
    with pytest.raises(AqlaError,match='Backup TP percentages'):
        await trading.preview(source.event_id,missing(value,'take_profits'))
    assert fake.actions==[]


@pytest.mark.asyncio
async def test_backup_revision_invalidates_unsent_plan(setup,signal):
    _,state,fake,_,trading=setup
    source,value=signal
    configure(state,targets=['1','2'])
    plan=await trading.preview(source.event_id,missing(value,'take_profits'))
    configure(state,targets=['2','3'])
    with pytest.raises(AqlaError,match='Backup profile changed'):
        await trading.execute(plan['plan_id'])
    assert fake.actions==[]


def test_names_are_unique_and_do_not_change_execution_or_subscription(setup,signal):
    _,state,*_=setup
    before=state.snapshot()
    state.update(lambda data:set_source_name(data,'-100123','  VIP Gold  ',0))
    named=state.snapshot()
    assert named['source_names']['-100123']=='VIP Gold'
    for key in ('policy','policy_revision','configuration_revision','sources','source_aliases','cursors','trades','plans','bindings'):
        assert named[key]==before[key]
    with pytest.raises(AqlaError,match='already belongs'):
        state.update(lambda data:set_source_name(data,'-100456','vip GOLD',1))
    with pytest.raises(AqlaError,match='source_names_revision'):
        state.update(lambda data:set_source_name(data,'-100123',None,0))
    state.update(lambda data:set_source_name(data,'-100123',None,1))
    assert state.snapshot()['source_names']=={}


def test_pipeline_name_is_stored_as_id(setup):
    _,state,*_=setup
    state.update(lambda data:data['sources'].append('-100123'))
    state.update(lambda data:set_source_name(data,'-100123','VIP Gold',0))
    pipeline=Pipeline(id='gold',name='Gold',sources=['vip gold'],targets=[{'exchange':'hyperliquid','profile_id':'standard'}])
    state.update(lambda data:route_control(data,'set_pipeline',expected_revision=data['configuration_revision'],pipeline=pipeline))
    assert state.snapshot()['pipelines']['gold']['sources']==['-100123']


def test_journal_keeps_ids_and_appends_name(setup,signal):
    _,state,*_=setup
    source,_=signal
    state.update(lambda data:data['source_names'].update({'channel':'VIP'}))
    row=project(state.snapshot())['Signale:'+source.event_id]
    assert row['values'][HEADERS['Signale'].index('Quelle')]=='channel'
    assert row['values'][HEADERS['Signale'].index('Quellenname')]=='VIP'


@pytest.mark.parametrize('side',['long','short'])
@pytest.mark.parametrize('fill_price',['99.5','100.2'])
@pytest.mark.asyncio
async def test_final_percent_prices_follow_real_fill_and_restart(setup,signal,side,fill_price):
    _,state,fake,_,trading=setup
    source,value=signal
    source=source.model_copy(update={'message_id':10,'text':source.text+' short'})
    value=value.model_copy(update={'source_refs':[source.source_id],
        'evidence':[row.model_copy(update={'source_id':source.source_id}) for row in value.evidence]})
    if side=='short':
        value=value.model_copy(update={'side':side,'evidence':[row.model_copy(update={'quote':'short'}) if row.field=='side' else row for row in value.evidence]})
    trading.ingest(source)
    configure(state,stop='1',targets=['1','2','3'])
    value=missing(value,'stop_loss','take_profits').model_copy(update={'entry':'limit','entry_prices':[Decimal('100')]})
    plan=await trading.preview(source.event_id,value)
    trade=await trading.execute(plan['plan_id'])
    assert 'backup_finalization' not in trade
    fake.fill(trade['entry_cloid'],Decimal(plan['quantity'])/2,fill_price)
    await trading.recover()
    trade=state.snapshot()['trades'][trade['trade_id']]
    assert fake.orders[trade['entry_cloid']]['status']=='canceled'
    assert trade['backup_finalization']['reference_entry']==fill_price
    assert trade['backup_finalization']['phase']=='applied'
    targets=[Decimal(value) for value in trade['targets']]
    reference=Decimal(fill_price)
    assert (targets[0]>=reference*Decimal('1.01') if side=='long' else targets[0]<=reference*Decimal('.99'))
    before=copy.deepcopy(fake.actions)
    await trading.recover()
    assert fake.actions==before


@pytest.mark.asyncio
@pytest.mark.parametrize('unit',['course_percent','roe_percent'])
async def test_rejected_backup_tp_closes_with_sl_retained_until_exit(setup,signal,unit):
    _,state,fake,exchange,trading=setup
    source,value=signal
    configure(state,targets=['25','50'] if unit=='roe_percent' else ['1','2'],unit=unit)
    plan=await trading.preview(source.event_id,missing(value,'take_profits'))
    original=fake.handle
    seen=[]
    def reject_targets(request):
        import json
        payload=json.loads(request.content)
        action=payload.get('action',{})
        if action.get('type')=='order' and all(order['r'] and 'trigger' not in order['t'] and order['t'].get('limit',{}).get('tif')=='Gtc' for order in action['orders']):
            fake.actions.append(action)
            import httpx
            return httpx.Response(200,json={'status':'err','response':'TP rejected'})
        if action.get('type')=='order' and all(order['r'] and order['t'].get('limit',{}).get('tif')=='Ioc' for order in action['orders']):
            seen.extend(row['status'] for row in fake.orders.values() if row['order']['isTrigger'])
        return original(request)
    import httpx
    exchange.client=httpx.AsyncClient(base_url=exchange.settings.base_url,transport=httpx.MockTransport(reject_targets))
    await trading.execute(plan['plan_id'])
    assert fake.position==0 and state.snapshot()['paused']
    assert 'open' in seen


@pytest.mark.asyncio
@pytest.mark.parametrize('unit',['course_percent','roe_percent'])
async def test_lost_backup_sl_reply_reconciles_exact_identity(setup,signal,monkeypatch,unit):
    _,state,fake,exchange,trading=setup
    source,value=signal
    configure(state,stop='40' if unit=='roe_percent' else '1',unit=unit)
    plan=await trading.preview(source.event_id,missing(value,'stop_loss'))
    original=exchange.mutate
    async def lose(operation,*args,**kwargs):
        if operation.startswith('backup-stop:'):
            fake.lost_response=True
        return await original(operation,*args,**kwargs)
    monkeypatch.setattr(exchange,'mutate',lose)
    trade=await trading.execute(plan['plan_id'])
    assert trade['backup_finalization']['phase']=='applied'
    before=copy.deepcopy(fake.actions)
    await trading.recover()
    assert fake.actions==before






@pytest.mark.asyncio
async def test_multi_target_backup_keeps_each_profile_and_shared_limits(routed,setup,signal):  # noqa: F811 -- fixture
    router,boundaries=routed
    _,state,*_=setup
    configure(state,stop='2',targets=['2','3'])
    source,value=signal
    previews=await router.preview(source.event_id,missing(value,'stop_loss','take_profits'))
    plans=[row['plan'] for row in previews['targets']]
    assert {plan['policy']['risk_percent'] for plan in plans}=={'0.25'}
    results=await router.execute_many([plan['plan_id'] for plan in plans])
    assert all(row.get('status')=='open' for row in results['targets'])
    assert all(row['backup_finalization']['phase']=='applied' for row in state.snapshot()['trades'].values())


@pytest.mark.asyncio
async def test_existing_invalid_targets_are_not_replaced_by_backup(setup,signal):
    _,state,fake,_,trading=setup
    source,value=signal
    configure(state,targets=['1','2'])
    value=value.model_copy(update={'take_profits':list(reversed(value.take_profits)),
        'evidence':[row.model_copy(update={'field':'take_profits.1' if row.field=='take_profits.0' else 'take_profits.0'}) if row.field.startswith('take_profits.') else row for row in value.evidence]})
    with pytest.raises(AqlaError,match='ordering'):
        await trading.preview(source.event_id,value)
    assert fake.actions==[]


@pytest.mark.asyncio
async def test_backup_keeps_custom_weight_validation(setup,signal):
    _,state,fake,_,trading=setup
    source,value=signal
    state.update(lambda data:data['policy'].update(take_profit_allocation='custom',take_profit_weights=['50','50']))
    configure(state,targets=['1','2','3'])
    with pytest.raises(AqlaError,match='TP weight count'):
        await trading.preview(source.event_id,missing(value,'take_profits'))
    assert fake.actions==[]


@pytest.mark.parametrize('kind',['delisted','other_currency'])
@pytest.mark.asyncio
async def test_gold_does_not_substitute_or_mix_currency(setup,kind):
    _,_,_,exchange,_=setup
    async def info(query,**params):
        if query=='allMids':
            return {'xyz:GOLD':'4170','PAXG':'4170'}
        if query=='meta':
            return {'collateralToken':0 if kind=='delisted' else 360,'universe':[{'name':'xyz:GOLD','szDecimals':4,'maxLeverage':25,'isDelisted':kind=='delisted'}]}
        if query=='perpDexs':
            return [None,{'name':'xyz'}]
        if query=='spotMeta':
            return {'tokens':[{'index':360,'name':'USDH'}]}
        raise AssertionError(query)
    exchange.info=info
    with pytest.raises(AqlaError,match='unavailable|USDC'):
        await exchange.market('XAU')


def test_integer_signal_json_stays_compatible():
    value=Interpretation(kind='new_signal',source_refs=['one'],leverage=12)
    assert value.model_dump(mode='json')['leverage']==12


@pytest.mark.asyncio
async def test_fault_after_fill_keeps_prepared_reference_before_order_intent(setup,signal,monkeypatch):
    _,state,fake,exchange,trading=setup
    source,value=signal
    configure(state,targets=['1','2'])
    plan=await trading.preview(source.event_id,missing(value,'take_profits'))
    original=exchange.mutate
    async def crash(operation,*args,**kwargs):
        if operation.startswith('targets:'):
            final=state.snapshot()['trades'][plan['trade_id']]['backup_finalization']
            assert final['phase']=='applied' and final['reference_entry']=='100'
            raise SystemExit('simulated process death before TP send')
        return await original(operation,*args,**kwargs)
    monkeypatch.setattr(exchange,'mutate',crash)
    with pytest.raises(SystemExit):
        await trading.execute(plan['plan_id'])
    monkeypatch.setattr(exchange,'mutate',original)
    configure(state,targets=['5','10'])
    await trading.recover()
    trade=state.snapshot()['trades'][plan['trade_id']]
    assert [Decimal(value) for value in trade['targets']]==[Decimal('101'),Decimal('102')]
    sent=copy.deepcopy(fake.actions)
    await trading.recover()
    assert fake.actions==sent
