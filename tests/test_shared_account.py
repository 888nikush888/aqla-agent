import copy
import json

import httpx
import pytest

from aqla_agent.accounting import occupied_markets
from aqla_agent.network_transition import confirm_manual_testnet_close, mainnet_state
from aqla_hyperliquid_mcp.models import AqlaError


@pytest.fixture
def shared(setup,monkeypatch):
    from aqla_agent import inventory_stream
    settings,state,fake,exchange,trading=setup
    settings.shared_account=True
    manual={'ETH':'1','SOL':'2'}
    orders=[]
    config={'mode':'unifiedAccount','total':'10000','hold':'100','ceiling':'9000'}
    original=fake.handle
    def handle(request):
        query=json.loads(request.content)
        if request.url.path=='/info':
            kind=query.get('type')
            if kind=='userAbstraction':
                return httpx.Response(200,json=config['mode'])
            if kind=='perpDexs':
                return httpx.Response(200,json=[None,{'name':'xyz'}])
            if kind=='allPerpMetas':
                return httpx.Response(200,json=[{'universe':[{'name':symbol} for symbol in ('BTC','ETH','SOL','ZEC','HYPE')]},{'universe':[{'name':'xyz:GOLD'}]}])
            if kind=='clearinghouseState':
                values={} if query.get('dex') else {**manual,**({'BTC':str(fake.position)} if fake.position else {})}
                return httpx.Response(200,json={'assetPositions':[{'position':{'coin':coin,'szi':size,'entryPx':'100'}} for coin,size in values.items()],
                    'marginSummary':{'accountValue':'1'},'withdrawable':'0'})
            if kind=='frontendOpenOrders':
                return httpx.Response(200,json=[] if query.get('dex') else orders)
            if kind=='spotClearinghouseState':
                return httpx.Response(200,json={'balances':[{'coin':'USDC','token':0,'total':config['total'],'hold':config['hold']}],
                                               'tokenToAvailableAfterMaintenance':[[0,config['ceiling']]]})
        return original(request)
    exchange.client=httpx.AsyncClient(base_url=settings.base_url,transport=httpx.MockTransport(handle))
    async def observe(adapter,names):
        return [(dex,await adapter.info('clearinghouseState',user=adapter.account,**({'dex':dex} if dex else {})),
                 await adapter.info('frontendOpenOrders',user=adapter.account,**({'dex':dex} if dex else {}))) for dex in names]
    monkeypatch.setattr(inventory_stream,'observe',observe)
    return manual,orders,config,exchange,trading


@pytest.mark.asyncio
async def test_manual_other_markets_count_but_allow_free_market(shared,setup,signal):
    manual,orders,config,exchange,trading=shared
    _,state,fake,*_=setup
    source,value=signal
    snapshot=await exchange.account_inventory()
    assert snapshot.equity==10000 and snapshot.available_margin==9000
    assert occupied_markets(state.snapshot(),snapshot)=={'ETH','SOL'}
    plan=await trading.preview(source.event_id,value)
    trade=await trading.execute(plan['plan_id'])
    assert trade['status']=='open'
    assert manual=={'ETH':'1','SOL':'2'} and orders==[]
    assert all(order['a']==0 for action in fake.actions if action.get('type')=='order' for order in action['orders'])


@pytest.mark.asyncio
async def test_combined_capacity_and_same_market_reject_before_mutation(shared,setup,signal):
    manual,orders,config,exchange,trading=shared
    _,state,fake,*_=setup
    source,value=signal
    manual['ZEC']='1'
    with pytest.raises(AqlaError,match='Combined'):
        await trading.preview(source.event_id,value)
    manual.pop('ZEC')
    manual['BTC']='1'
    with pytest.raises(AqlaError,match='exposure'):
        await trading.preview(source.event_id,value)
    assert fake.actions==[]


@pytest.mark.asyncio
async def test_reduce_only_orders_do_not_reserve_slots(shared,setup,signal):
    manual,orders,config,exchange,trading=shared
    source,value=signal
    orders.append({'coin':'BTC','reduceOnly':True,'oid':999,'sz':'0'})
    assert (await trading.preview(source.event_id,value))['plan_id']
    orders[0]['reduceOnly']=False
    with pytest.raises(AqlaError,match='Combined|reserve'):
        await trading.preview(source.event_id,value)


@pytest.mark.parametrize('change',['position','reservation','hold'])
@pytest.mark.asyncio
async def test_manual_change_after_signer_await_prevents_entry(shared,setup,signal,monkeypatch,change):
    manual,orders,config,exchange,trading=shared
    _,state,fake,*_=setup
    source,value=signal
    plan=await trading.preview(source.event_id,value)
    original=exchange.verify_signer
    calls=0
    async def changed():
        nonlocal calls
        await original()
        calls+=1
        if calls==3:
            if change=='position':
                manual['BTC']='1'
            elif change=='reservation':
                orders.append({'coin':'BTC','reduceOnly':False,'oid':999,'sz':'1'})
            else:
                config['hold']='9999.99'
    monkeypatch.setattr(exchange,'verify_signer',changed)
    with pytest.raises(AqlaError):
        await trading.execute(plan['plan_id'])
    assert all(action['type']=='updateLeverage' for action in fake.actions)
    assert 'entry:'+plan['trade_id'] not in state.snapshot()['operations']


@pytest.mark.parametrize('mode',['portfolioMargin','dexAbstraction','unknown'])
@pytest.mark.asyncio
async def test_unsupported_account_modes_never_use_classic_budget(shared,mode):
    _,_,config,exchange,_=shared
    config['mode']=mode
    with pytest.raises(AqlaError,match='unsupported'):
        await exchange.account_inventory()


@pytest.mark.asyncio
async def test_external_intervention_is_local_and_does_not_close_mixed_position(shared,setup,signal):
    manual,orders,config,exchange,trading=shared
    _,state,fake,*_=setup
    state.update(lambda data:data['policy'].update(max_open_trades=8))
    source,value=signal
    plan=await trading.preview(source.event_id,value)
    trade=await trading.execute(plan['plan_id'])
    before=copy.deepcopy(fake.actions)
    fake.position+=1
    reports=await trading.recover()
    assert any(row.get('market_conflict') for row in reports)
    assert state.snapshot()['trades'][trade['trade_id']]['status']=='conflicted'
    assert trading.recovery_checked and fake.actions==before
    with pytest.raises(AqlaError,match='mixed'):
        await trading.close_position(trade['trade_id'])


@pytest.mark.asyncio
async def test_network_cutover_keeps_ids_and_prevents_old_signal_reentry(setup,signal):
    settings,state,fake,_,trading=setup
    source,value=signal
    plan=await trading.preview(source.event_id,value)
    trade=await trading.execute(plan['plan_id'])
    old=state.snapshot()
    original=copy.deepcopy(old)
    with pytest.raises(AqlaError,match='Finish'):
        mainnet_state(old,settings.account,source.sent_at+100)
    identity=trade['trade_id']
    clo={row['cloid'] for row in trade['orders']}|{trade['entry_cloid']}
    confirm_manual_testnet_close(old,identity,net_size=0,order_statuses={key:'canceled' for key in clo},stamp=source.sent_at+1)
    assert old['trades'][identity]['manual_closure']['pnl_complete'] is False
    transitioned=mainnet_state(old,settings.account,source.sent_at+100)
    assert transitioned['network']=='mainnet' and transitioned['trades']=={} and transitioned['operations']=={} and transitioned['nonces']=={}
    assert transitioned['profiles']==original['profiles']
    assert transitioned['events'][source.event_id]['source']['historical']
    assert transitioned['context_cutoffs']['channel']>=source.message_id
    assert old['trades'][identity]['plan']==original['trades'][identity]['plan']


@pytest.mark.asyncio
async def test_namespace_journal_retains_testnet_tables_and_binding():
    from types import SimpleNamespace

    from test_sheets import Google

    from aqla_sheets_mcp.client import PREFIX, SheetsClient
    from aqla_sheets_mcp.journal import record

    google=Google()
    legacy=SheetsClient(SimpleNamespace(spreadsheet_id='unit-test'),session=google,scope='testnet:account')
    legacy.sync([record('Trades','historic','channel','BTC','long','closed','now',100,100,1,1,0,90,'[]',10,0,1,0,1,'[]')])
    old=copy.deepcopy(google.tables)
    main=SheetsClient(SimpleNamespace(spreadsheet_id='unit-test',tab_namespace='mainnet'),session=google,scope='mainnet:account')
    main.layout()
    assert all(google.tables[key]==value for key,value in old.items())
    assert PREFIX+'Mainnet Trades' in google.tables
    assert {row['metadataKey'] for row in google.metadata}=={'aqla.scope','aqla.scope.mainnet'}
    wrong=SheetsClient(SimpleNamespace(spreadsheet_id='unit-test',tab_namespace='mainnet'),session=google,scope='mainnet:other')
    with pytest.raises(AqlaError,match='different'):
        wrong.layout()
