"""Explicit network cutover, preserving old execution identities in their archive."""
import copy

from aqla_hyperliquid_mcp.models import AqlaError


def mainnet_state(old, account, stamp):
    if old['network'] != 'testnet':
        raise AqlaError('Cutover source must be the existing Testnet state')
    if any(row['plan'].get('exchange','hyperliquid')=='hyperliquid' and row['status'] in
           ('preparing','opening','open','protecting','unknown','closing','cleanup','conflicted') for row in old['trades'].values()):
        raise AqlaError('Finish and reconcile Testnet exposure before Mainnet cutover')
    if any(row.get('exchange','hyperliquid')=='hyperliquid' and row['phase'] in ('prepared','sending','unknown') for row in old['operations'].values()):
        raise AqlaError('Testnet operations are unresolved')
    data=copy.deepcopy(old)
    data.update(network='mainnet',account=account.lower(),trading_account=account.lower(),paused=False,
                sources_live_after=stamp,notifications={},bindings={},nonces={},sequence=0,journal_pending={},journal_history={})
    data.pop('journal_synced_at',None)
    for collection in ('trades','plans'):
        data[collection]={key:row for key,row in data[collection].items() if row['plan'].get('exchange','hyperliquid')!='hyperliquid'}
    data['operations']={key:row for key,row in data['operations'].items() if row.get('exchange','hyperliquid')!='hyperliquid'}
    data['venue_accounts']['hyperliquid']={'network':'mainnet','account':account.lower()}
    data['venue_enabled']['hyperliquid']=True
    data['venue_events'].pop('hyperliquid',None)
    data['venue_bindings'].pop('hyperliquid',None)
    data.get('venue_nonces',{}).pop('hyperliquid',None)
    for event in data['events'].values():
        event['source']['historical']=True
        event['status']='context'
        for key in ('targets','last_error','interpretation'):
            event.pop(key,None)
        event['interpretation']=None
        chat=event['source']['chat_id']
        data['cursors'][chat]=max(data['cursors'].get(chat,0),event['source']['message_id'])
    data['context_cutoffs']={chat:max(cursor,data.get('context_cutoffs',{}).get(chat,0)) for chat,cursor in data['cursors'].items()}
    return data


def confirm_manual_testnet_close(data, trade_id, *, net_size, order_statuses, stamp):
    """Operator-authorized closure acknowledgement; never creates an order or PnL."""
    trade=data['trades'][trade_id]
    if trade['plan']['network']!='testnet' or trade['plan'].get('exchange','hyperliquid')!='hyperliquid':
        raise AqlaError('Only the original Testnet trade can be archived here')
    identities={row['cloid'] for row in [*trade['orders'],*trade.get('exit_orders',[])]}|{trade['entry_cloid']}
    from aqla_hyperliquid_mcp.models import TERMINAL_ORDER
    if net_size!=0 or set(order_statuses)!=identities or any(value not in TERMINAL_ORDER for value in order_statuses.values()):
        raise AqlaError('Manual Testnet closure/cancellation is not fully proved')
    trade.update(status='closed',remaining_quantity='0',reserved_risk='0',cleanup_complete=True,
                 manual_closure={'confirmed_at':stamp,'net_size':'0','order_statuses':order_statuses,'pnl_complete':False})
