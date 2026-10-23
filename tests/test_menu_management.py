
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeExchange
from test_native_setup import fixture_controller
from test_telegram_panel import callback, choose

from aqla_agent.native_commands import NativeCommands
from aqla_agent.pipelines import VenueState
from aqla_agent.trading import Trading
from aqla_hyperliquid_mcp.api import Hyperliquid
from aqla_hyperliquid_mcp.models import require_first
from aqla_hyperliquid_mcp.server import close_server, create_server
from aqla_hyperliquid_mcp.state import State


# Sequential safety assertions; control flow (including test doubles): 13.
# skipcq: PY-R1000
@pytest.mark.asyncio
@pytest.mark.parametrize("root_network", ["mainnet", "testnet"])
@pytest.mark.parametrize("secondary", [False, True])
@pytest.mark.parametrize("changed_account", [False, True])
async def test_actual_menu_close_uses_original_network_and_one_identity(setup, signal, tmp_path, root_network, secondary, changed_account):
    settings, _, original, _, _ = setup
    selected=settings.model_copy(update={"network":root_network,"state_path":tmp_path/"menu-state.sqlite3"})
    state=State(selected)
    primary=FakeExchange(original.signer)
    client=httpx.AsyncClient(base_url=selected.base_url,transport=httpx.MockTransport(primary.handle))
    exchange=Hyperliquid(selected,state,client)
    server=create_server(selected,state=state,exchange=exchange)
    router=server.aqla_trading
    target=router.engines['hyperliquid']
    boundary=primary
    if secondary:
        target_settings=selected.model_copy(update={"network":"testnet" if root_network=="mainnet" else "mainnet"})
        view=VenueState(state,"hyperliquid",target_settings,legacy=False)
        boundary=FakeExchange(original.signer)
        adapter=Hyperliquid(target_settings,view,httpx.AsyncClient(base_url=target_settings.base_url,transport=httpx.MockTransport(boundary.handle)))
        target=Trading(adapter,view)
        router.register_account(view,target_settings)
        router.engines[view.account_key]=target
        state.update(lambda data:data['account_enabled'].__setitem__(view.account_key,True))
    native,bot,_,_,binding,*_=fixture_controller()
    bot.edits=[]
    async def edit(**values):
        bot.edits.append(values)
    bot.edit_message_text=edit
    async def rpc(_,tool,args):
        result=await server.call_tool(tool,args)
        return result.structured_content or json.loads(result.content[0].text)
    commands=NativeCommands(SimpleNamespace(owner="12345",setup=native,setup_binding=lambda:binding[0],
        authority=server.aqla_authority,server="aqla",mcp_call=rpc,config_path=tmp_path/"config.yaml"))
    try:
        source,value=signal
        router.ingest(source)
        plan=await target.preview(source.event_id,value)
        trade=await target.execute(plan['plan_id'])
        if secondary:
            primary.position=2
        before=list(boundary.actions)
        if changed_account:
            state.update(lambda data:data['execution_accounts'][target.state.account_key].update(account='0x'+'1'*40))
        menu=commands.menu
        await menu.positions(bot)
        label='BTC LONG · '+target.exchange.settings.network+' · open'
        await menu.intercept(callback(menu,choose(bot,label)),bot)
        await menu.intercept(callback(menu,choose(bot,'Position schließen')),bot)
        token=choose(bot,'Jetzt close ausführen')
        await menu.intercept(callback(menu,token),bot)
        snapshot=state.snapshot()
        if changed_account:
            assert not snapshot.get('operator_requests') and boundary.position!=0 and boundary.actions==before
            return
        assert snapshot['operator_requests'], (bot.edits or bot.sent)[-1]['text']
        request=require_first(iter(snapshot['operator_requests'].values()))
        assert request['status']=='done' and request['account_key']==target.state.account_key
        assert boundary.position==0
        if secondary:
            assert primary.position==2 and not primary.actions
        sent=list(boundary.actions)
        await menu.intercept(callback(menu,token),bot)
        assert boundary.actions==sent
        await router.recover()
        closes=[row for action in boundary.actions if action['type']=='order' for row in action['orders']
            if row['r'] and row['t'].get('limit',{}).get('tif')=='Ioc']
        assert len(closes)==1
        assert state.snapshot()['trades'][trade['trade_id']]['status']=='closed'
    finally:
        await close_server(server)
