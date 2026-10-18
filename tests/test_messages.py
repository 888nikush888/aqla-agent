import copy
from decimal import Decimal
from itertools import permutations

import pytest

from aqla_agent.lifecycle import close_reason, record_lifecycle
from aqla_agent.messages import MAX_NOTIFICATIONS, literal, number, split_blocks, telegram_view

READY = {'hyperliquid': {'configured': True, 'enabled': True, 'network': 'testnet'}}


def test_delayed_target_notification_uses_current_terminal_outcome(opened):
    trade=next(iter(opened['trades'].values()))
    trade.update(status='rejected',filled_quantity='0',remaining_quantity='0',fill_events={})
    marker={'authority':'preview','attempts':1,'next_retry_at':1000,'deadline_at':9999999999999,'exhausted':False,'code':'read_unavailable','stage':'market'}
    old={'pipeline_id':'legacy','exchange':'hyperliquid','profile_id':'standard','network':'testnet','status':'pending','observation_retry':marker}
    event=opened['events'][trade['event_id']]
    event['targets']={'legacy:hyperliquid':{**old,'status':'rejected','error':'Current equity/margin no longer permits this exact plan'}}
    opened['notifications']={'delayed':{'event_id':trade['event_id'],'targets':[old]}}
    rendered=text(telegram_view(opened,READY))
    assert 'Signal abgelehnt' in rendered and 'Nächste Prüfung' not in rendered
    assert 'Eigenkapital' in rendered and 'Prüfung ausstehend' not in rendered


@pytest.mark.parametrize('allocation',['adaptive','first_target'])
def test_only_adaptive_mode_reports_adaptive_selection_and_backup_labels_use_active_targets(opened,allocation):
    trade=next(iter(opened['trades'].values()))
    trade['status']='protecting'
    trade['plan']['policy']['take_profit_allocation']=allocation
    trade['plan']['source_take_profit_count']=8
    trade['targets']=trade['targets'][:1]
    trade['plan']['backup_fields']=['take_profits']
    trade['plan']['backup_values']={'fallback_take_profit_unit':'roe_percent','fallback_take_profit_percentages':['25','50']}
    result=text(telegram_view(opened,READY))
    assert ('Adaptive TP-Auswahl' in result)==(allocation=='adaptive')
    assert 'TPs: 25 % ROE' in result and 'TPs: 25, 50 % ROE' not in result


def exit_trade(reasons, times=None):
    mapping = {"stop_loss": "sl", "take_profit": "tp", "source_close": "close", "operator_close": "close"}
    fills = [{"kind": mapping[reason], "sz": "1", "px": str(90 + index * 10), "oid": index + 2,
              "time": times[index] if times else 2000} for index, reason in enumerate(reasons)]
    orders = [{"cloid": "exit" + str(index), "oid": index + 2, "reason": reason} for index, reason in enumerate(reasons) if mapping[reason] == "close"]
    return fills, {"fill_events": {"entry": {"kind": "entry", "sz": str(len(reasons)), "px": "100", "oid": 1, "time": 1000}}, "exit_orders": orders}


def test_tied_exit_reasons_are_permutation_invariant():
    exits, base = exit_trade(["stop_loss", "take_profit", "source_close", "operator_close"])
    expected = {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt", "last_exit_time": 2000}
    for ordered in permutations(exits):
        trade = {**base, "fill_events": {**base["fill_events"], **{str(row["oid"]): row for row in ordered}}}
        assert close_reason(trade) == expected


@pytest.mark.parametrize("reason", ["stop_loss", "take_profit", "source_close", "operator_close"])
def test_tied_exits_with_same_reason_remain_confirmed(reason):
    exits, base = exit_trade([reason, reason])
    results = []
    for ordered in permutations(exits):
        trade = {**base, "fill_events": {**base["fill_events"], **{str(row["oid"]): row for row in ordered}}}
        result = close_reason(trade)
        assert result["kind"] == reason and result["last_exit_time"] == 2000
        assert "last_exit_price" not in result and "last_exit_oid" not in result
        results.append(result)
    assert results[0] == results[1]


def test_unique_last_exit_retains_chronological_evidence():
    exits, base = exit_trade(["stop_loss", "take_profit", "source_close"], [1500, 2000, 2500])
    for ordered in permutations(exits):
        trade = {**base, "fill_events": {**base["fill_events"], **{str(row["oid"]): row for row in ordered}}}
        assert close_reason(trade) == {"kind": "source_close", "label": "Quellenauftrag", "last_exit_time": 2500,
                                       "last_exit_price": "110", "last_exit_oid": 4}


def test_existing_immutable_closed_lifecycle_is_not_rewritten(opened):
    identity, trade = next(iter(opened["trades"].items()))
    trade["status"] = "closed"
    record_lifecycle(opened, identity, "closed")
    frozen = copy.deepcopy(trade["lifecycle_events"]["closed"])
    original_reason = copy.deepcopy(trade["close_reason"])
    exits, base = exit_trade(["stop_loss", "take_profit"])
    trade.update(base, fill_events={**base["fill_events"], **{str(row["oid"]): row for row in exits}})
    record_lifecycle(opened, identity, "closed")
    assert trade["lifecycle_events"]["closed"] == frozen
    assert trade["close_reason"] == original_reason


def test_tied_exit_reason_is_honest_in_formatter_and_lifecycle(opened):
    identity, trade = next(iter(opened["trades"].items()))
    exits, base = exit_trade(["stop_loss", "take_profit"])
    trade.update(base, fill_events={**base["fill_events"], **{str(row["oid"]): row for row in exits}},
                 status="closed", remaining_quantity="0")
    opened["notifications"].clear()
    record_lifecycle(opened, identity, "closed")
    event = trade["lifecycle_events"]["closed"]
    assert event["time"] == 2000
    assert event["facts"]["close_reason"] == {"kind": "unconfirmed", "label": "Grund nicht vollständig belegt", "last_exit_time": 2000}
    rendered = text(telegram_view(opened, READY))
    assert "Trade geschlossen" in rendered and "Grund nicht vollständig belegt" in rendered


def text(view):
    return '\n\n'.join(chunk for row in view['messages'] for chunk in row['texts'])


@pytest.fixture
async def opened(setup, signal):
    _,state,_,_,trading=setup
    source,value=signal
    state.update(lambda data:data['source_names'].update({'channel':'VIP Gold'}))
    plan=await trading.preview(source.event_id,value)
    await trading.execute(plan['plan_id'])
    result = state.snapshot()
    # Existing status-card coverage uses legacy notifications; immutable lifecycle
    # cards have separate rapid-entry/exit tests.
    trade = next(iter(result['trades'].values()))
    result['notifications'] = {'legacy-open': {'trade_id': trade['trade_id'], 'change': {'status': 'open'}, 'previous': {'status': 'opening'}}}
    return result


def test_view_is_pure_and_opening_has_only_confirmed_values(opened):
    before=copy.deepcopy(opened)
    result=telegram_view(opened,READY)
    rendered=text(result)
    assert 'Trade eröffnet' in rendered and 'VIP Gold' in rendered
    assert 'Entry realisiert' in rendered and '100 USDC' in rendered
    assert 'Testnet' in rendered and 'Standard' in rendered
    assert '50 %' in rendered and 'Positionsanteil' in rendered
    assert 'channel' not in rendered
    assert opened==before
    assert set(result['messages'][0]['notification_ids'])==set(opened['notifications'])


def test_maker_modes_and_native_role_are_distinct_from_intent(opened):
    trade = next(iter(opened['trades'].values()))
    trade['plan'].update(entry_execution='maker_only', take_profit_execution='maker_only')
    trade['fill_execution_roles'] = {identity: 'taker' for identity in trade['fill_events']}
    rendered = text(telegram_view(opened, READY))
    assert 'Entry Maker only · TP Maker only' in rendered
    assert '**Fillrolle belegt:** Taker' in rendered
    trade['fill_execution_roles'] = {}
    rendered = text(telegram_view(opened, READY))
    assert '**Fillrolle belegt:** unbekannt' in rendered


def test_maker_target_rejection_remains_visible_with_live_stop(opened):
    trade = next(iter(opened['trades'].values()))
    trade.update(status='protecting', maker_target_rejection='Native maker TP rejected')
    rendered = text(telegram_view(opened, READY))
    assert '**TP abgelehnt:**' in rendered and 'vorhandener SL bleibt aktiv' in rendered


@pytest.mark.parametrize('stage', ['closed', 'closing', 'cleanup', 'cancelled', 'rejected'])
def test_historical_maker_rejection_never_claims_live_stop_during_exit(opened, stage):
    trade = next(iter(opened['trades'].values()))
    trade.update(status=stage, maker_target_rejection='Native maker TP rejected', execution_incident='Emergency protection close')
    rendered = text(telegram_view(opened, READY))
    assert '**TP abgelehnt:**' not in rendered and 'vorhandener SL bleibt aktiv' not in rendered


@pytest.mark.parametrize('missing', ['order_observation', 'coverage', 'price', 'remaining', 'emergency'])
def test_maker_rejection_does_not_invent_live_stop_protection(opened, missing):
    trade = next(iter(opened['trades'].values()))
    trade.update(status='protecting', maker_target_rejection='Native maker TP rejected')
    if missing == 'order_observation':
        trade['order_observations'] = {}
    elif missing == 'remaining':
        trade['remaining_quantity'] = '0'
    elif missing == 'emergency':
        trade['execution_incident'] = 'Protection could not be confirmed; closing own exposure'
    else:
        for order in trade['orders']:
            if order['kind'] == 'sl':
                order['quantity' if missing == 'coverage' else 'price'] = '0'
    rendered = text(telegram_view(opened, READY))
    assert 'vorhandener SL bleibt aktiv' not in rendered
    assert ('**TP abgelehnt:**' in rendered) is (missing != 'remaining')
    if missing != 'remaining':
        assert 'SL-Schutz wird abgeglichen' in rendered


def test_lifecycle_displays_role_enrichment_without_rewriting_frozen_facts(opened):
    from aqla_agent.messages import lifecycle_block

    trade = next(iter(opened['trades'].values()))
    event = trade['lifecycle_events']['entry_fill']
    before = copy.deepcopy(event)
    trade['fill_execution_roles'] = {identity: 'maker' for identity in trade['fill_events']}
    assert '**Fillrolle belegt:** Maker' in lifecycle_block(opened, event)
    assert event == before


@pytest.mark.parametrize('value,expected',[('86032.125','86.032,125'),('0.0000123','0,0000123'),('-1234.5','−1.234,5'),('1.000','1'),('NaN','nicht bestätigt')])
def test_german_numbers_keep_precision(value,expected):
    assert number(value)==expected


@pytest.mark.parametrize('phase,label',[('opening','Entry wartet'),('unknown','Ausführung ungeklärt'),('protecting','Schutz wird abgeglichen')])
def test_pending_and_unknown_entries_are_not_reported_as_open(opened,phase,label):
    trade=next(iter(opened['trades'].values()))
    trade['status']=phase
    if phase=='opening':
        trade.update(filled_quantity='0',remaining_quantity='0',fill_events={},entry_vwap=None)
    result=text(telegram_view(opened,READY))
    assert label in result and 'Trade eröffnet' not in result
    if phase=='opening':
        assert 'Entry geplant' in result and 'Entry realisiert' not in result


def test_partial_entry_is_explicit(opened):
    trade=next(iter(opened['trades'].values()))
    trade['filled_quantity']=str(Decimal(trade['plan']['quantity'])/2)
    trade['remaining_quantity']=trade['filled_quantity']
    for fill in trade['fill_events'].values():
        if fill['kind']=='entry':
            fill['sz']=trade['filled_quantity']
    result=text(telegram_view(opened,READY))
    assert 'Entry teilweise ausgeführt' in result and '50 % der geplanten Menge' in result


def test_sl_and_tp_updates_are_bundled_without_repeating_opening(opened):
    identity,trade=next(iter(opened['trades'].items()))
    trade.update(stop_price='100',tp_filled=['1','0'],target_budgets=['1','1'],remaining_quantity='1',filled_quantity='2')
    trade['fill_events']={'entry':{'kind':'entry','sz':'2','px':'100'},'tp':{'kind':'tp','sz':'1','px':'101'}}
    opened['notifications']={'sl':{'trade_id':identity,'change':{'stop_price':'100'},'previous':{'stop_price':'99'}},
                             'tp':{'trade_id':identity,'change':{'tp_filled':['1','0'],'remaining_quantity':'1'},'previous':{'tp_filled':['0','0']}}}
    result=text(telegram_view(opened,READY))
    assert 'Trade aktualisiert' in result and '99 → 100' in result and 'Einstand' in result
    assert 'TP1' in result and 'vollständig ausgeführt' in result and 'Position verbleibend' in result
    assert 'Entry realisiert' not in result


def test_mixed_venue_results_share_one_signal_message(opened):
    trade=next(iter(opened['trades'].values()))
    event=opened['events'][trade['event_id']]
    opened['notifications']['rejection']={'event_id':trade['event_id'],'targets':[{'exchange':'aster','profile_id':'standard','error':'Signal SL is missing and Backup SL percentage is not configured'}]}
    view=telegram_view(opened,READY)
    assert len(view['messages'])==1
    result=text(view)
    assert 'Trade eröffnet' in result and 'Signal abgelehnt' in result and 'Aster' in result
    assert 'SL fehlt' in result
    assert event['interpretation']['source_refs']==trade['plan']['source_refs']


def test_protection_alert_has_priority_and_never_waits_for_siblings(opened):
    opened['notifications']['protection:aster']={'exchange':'aster','error':'SL nicht bestätigt'}
    first=telegram_view(opened,READY)['messages'][0]
    assert first['severity']=='urgent' and first['notification_ids']==['protection:aster']
    assert 'Entries:** gesperrt' in first['texts'][0]


def test_closed_pnl_is_only_shown_with_known_fees(opened):
    identity,trade=next(iter(opened['trades'].items()))
    trade.update(status='closed',remaining_quantity='0',fill_events={'entry':{'kind':'entry','sz':'1','px':'100','fee':'0.1','feeToken':'USDC'},
                                                                  'exit':{'kind':'tp','sz':'1','px':'110','fee':'0.1','feeToken':'USDC'}})
    opened['notifications']={'closed':{'trade_id':identity,'change':{'status':'closed'}}}
    assert 'Netto vor Funding:** 9,8 USDC' in text(telegram_view(opened,READY))
    trade['fill_events']['exit']['fee']=None
    assert 'PnL nicht vollständig bestätigt' in text(telegram_view(opened,READY))


def test_leverage_and_backup_provenance(opened):
    trade=next(iter(opened['trades'].values()))
    trade['plan'].update(signal_leverage_original='12.78',signal_leverage=12,effective_leverage=10,backup_fields=['stop_loss'])
    result=text(telegram_view(opened,READY))
    assert '12,78× → abgerundet 12× → wirksam 10×' in result
    assert 'Backup verwendet:** SL' in result and 'vorläufig' in result


def test_backup_roe_intent_uses_original_margin_and_saved_values(opened):
    trade=next(iter(opened['trades'].values()))
    trade['plan'].update(backup_fields=['stop_loss','take_profits'],backup_values={
        'fallback_stop_loss_percent':'40','fallback_stop_loss_unit':'roe_percent',
        'fallback_take_profit_percentages':['25','50'],'fallback_take_profit_unit':'roe_percent'})
    result=text(telegram_view(opened,READY))
    assert 'SL: 40 % Marginverlust' in result and 'TPs: 25, 50 % ROE' in result
    assert 'ursprüngliche Positionsmargin vor Gebühren und Funding' in result


def test_unsafe_names_are_literal_not_links_or_instructions():
    assert literal('[VIP](https://invalid)')=='`[VIP](https://invalid)`'
    assert '\n' not in literal('VIP\n**test**')
    assert '`' not in literal('foo`bar')[1:-1]
    assert literal('MEDIA:/not-existing/synthetic.pdf') == '`MEDIA：/not-existing/synthetic.pdf`'
    assert literal('media:/not-existing/synthetic.pdf') == '`media：/not-existing/synthetic.pdf`'
    assert '[[audio_as_voice]]' not in literal('[[audio_as_voice]]')
    assert '[[as_document]]' not in literal('[[as_document]]')
    assert literal('<img src="https://example.invalid/x.png">') == '`‹img src="https://example.invalid/x.png"›`'


def test_long_messages_split_without_loss_or_broken_lines():
    blocks=['**Block '+str(index)+'**\n'+('Wert 123,45\n'*40).strip() for index in range(20)]
    parts=split_blocks('\n\n'.join(blocks))
    assert all(len(part.encode('utf-16-le'))//2<=3000 for part in parts)
    assert '\n\n'.join(parts)=='\n\n'.join(blocks)


def test_empty_background_has_no_message_and_disabled_sheets_is_explicit(setup):
    _,state,*_=setup
    view=telegram_view(state.snapshot(),READY,sheets_enabled=False)
    assert view['messages']==[]
    assert 'Sheets:** deaktiviert' in '\n'.join(view['status'])


def test_notification_limit_exposes_unprocessed_ids(setup):
    _,state,*_=setup
    data=state.snapshot()
    data['notifications']={str(index):{'error':'saved fact'} for index in range(MAX_NOTIFICATIONS+2)}
    view=telegram_view(data,READY)
    delivered={identity for item in view['messages'] for identity in item['notification_ids']}
    assert len(delivered)==MAX_NOTIFICATIONS
    assert delivered.isdisjoint(view['remaining_notification_ids'])
    assert delivered|set(view['remaining_notification_ids'])==set(data['notifications'])


def test_unknown_target_is_not_a_rejection(setup,signal):
    _,state,*_=setup
    source,_=signal
    data=state.snapshot()
    data['notifications']={'one':{'event_id':source.event_id,'targets':[{'exchange':'aster','profile_id':'standard','status':'unknown','error':'Antwort fehlt'}]}}
    result=text(telegram_view(data,READY))
    assert 'Börsenziel ungeklärt' in result and 'Signal abgelehnt' not in result


def test_incomplete_exit_fills_do_not_produce_pnl(opened):
    identity,trade=next(iter(opened['trades'].items()))
    trade.update(status='closed',fill_events={'entry':{'kind':'entry','sz':'1','px':'100','fee':'0.1'}})
    opened['notifications']={'closed':{'trade_id':identity,'change':{'status':'closed'}}}
    assert 'PnL nicht vollständig bestätigt' in text(telegram_view(opened,READY))
