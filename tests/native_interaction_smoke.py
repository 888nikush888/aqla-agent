"""Pinned SQLite import/search, planning persistence and native Telegram question cards."""
import argparse
import asyncio
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MethodType, SimpleNamespace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--hermes-source',required=True)
    parser.add_argument('--home',required=True)
    args=parser.parse_args()
    home=Path(args.home).resolve()
    home.mkdir(parents=True,exist_ok=True)
    os.environ['HERMES_HOME']=str(home)
    os.environ['HERMES_SKIP_CONFIG_MIGRATION']='1'
    os.environ['HERMES_SKIP_UPDATE_CHECK']='1'
    sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(args.hermes_source).resolve())]
    import yaml
    from gateway.platforms.base import SendResult
    from gateway.run_turn_runner import TurnRunner
    from hermes_state import SessionDB
    from model_tools import get_tool_definitions
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from run_agent import AIAgent
    from tools import clarify_gateway
    from tools.clarify_tool import clarify_tool
    from tools.session_search_tool import session_search
    from tools.todo_tool import TodoStore, todo_tool

    from aqla_agent.conversation import Conversations
    from aqla_agent.events import EXTENSIONS, HermesExtension
    from aqla_agent.hermes_runtime import install_strict_profile
    from aqla_agent.session_history import migrate_session_history
    from aqla_hyperliquid_mcp.models import Settings

    legacy=Conversations(home/'sessions/transcripts')
    legacy.append('legacy_owner',{'role':'session_meta','model':'original-model','platform':'telegram','timestamp':'2026-10-03T18:15:00Z','tools':['todo_list']})
    legacy.append('legacy_owner',{'role':'user','content':'KriteriumCrossMargin war eine bewusste Einstellung.','message_id':'42','display_metadata':{'gateway_input_owner':'legacy-owner'}})
    original=legacy.path('legacy_owner').read_bytes()
    assert migrate_session_history(home)=={'imported':1,'skipped':0,'messages':1}
    assert migrate_session_history(home)['imported']==0
    assert legacy.path('legacy_owner').read_bytes()==original
    undo=install_strict_profile()
    db=SessionDB(db_path=home/'state.db')
    try:
        found=json.loads(session_search(query='KriteriumCrossMargin',db=db))
        assert 'legacy_owner' in json.dumps(found),found
        assert db.get_session('legacy_owner')['model']=='original-model'
        imported_header=db._read_one('SELECT source_metadata FROM aqla_jsonl_imports WHERE session_id=?',('legacy_owner',))
        assert json.loads(imported_header[0])['tools']==['todo_list']
        db.create_session('planning','telegram')
        todo=TodoStore()
        planned=todo_tool(todos=[{'id':'profile-a','content':'Profil A anlegen','status':'in_progress'},{'id':'pipeline','content':'Pipeline prüfen','status':'pending'}],store=todo)
        db.append_messages_batch('planning',[
            {'role':'user','content':'Erstelle Profil A und prüfe seine Pipeline.'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'todo-1','type':'function','function':{'name':'todo_list','arguments':'{}'}}]},
            {'role':'tool','content':planned,'tool_call_id':'todo-1','tool_name':'todo_list'},
            {'role':'assistant','content':'Die Aufgaben sind gespeichert.'}])
        assert 'Profil A anlegen' in json.dumps(db.get_messages_as_conversation('planning'))
        resumed=object.__new__(AIAgent)
        resumed._todo_store=TodoStore()
        resumed.quiet_mode=True
        resumed._hydrate_todo_store(db.get_messages_as_conversation('planning'))
        assert [item['id'] for item in resumed._todo_store.snapshot()['todos']]==['profile-a','pipeline']
        names={tool['function']['name'] for tool in get_tool_definitions(enabled_toolsets=['clarify','todo','session_search'],quiet_mode=True,skip_tool_search_assembly=True)}
        assert {'clarify','todo_list','session_search'} <= names,names

        configuration=home/'aqla-test.yaml'
        secret=home/'integration.secret'
        secret.write_text('local-interaction-test-only-'*3)
        settings=Settings(network='testnet',account='0x'+'0'*40,state_path=home/'unused-trading.json',integration_key_file=secret)
        configuration.write_text(yaml.safe_dump(settings.model_dump(mode='json')))
        ctx=SimpleNamespace(get_config=lambda key,default=None:{'owner_id':'12345','config_path':str(configuration)}.get(key,default))
        extension=HermesExtension(ctx)
        EXTENSIONS.add(extension)
        adapter=object.__new__(TelegramAdapter)
        adapter._clarify_state={}
        cards=[]
        async def send_prompt(self,name,chat_id,metadata,build,**kwargs):
            text,keyboard,remember=build()
            remember(SimpleNamespace(message_id=7))
            cards.append((text,keyboard))
            return SendResult(success=True)
        adapter._send_prompt=MethodType(send_prompt,adapter)
        runner=object.__new__(TurnRunner)
        def ask(question,choices,multi_select,rearm=True):
            clarify_gateway.register('question-1','owner',question,choices,multi_select=multi_select)
            asyncio.run(adapter.send_clarify('12345',question,choices,'question-1','owner'))
            assert clarify_gateway.resolve_gateway_clarify('question-1','Profil A')
            return clarify_gateway.wait_for_response('question-1',timeout=.1),True
        runner._ask_clarify_question=ask
        with extension.claim_scope(('operator','owner-question')):
            assert extension.before_tool('clarify',{}) is None
            reply=json.loads(clarify_tool([{'question':'Welches Profil?','choices':['Profil A','Profil B']}],callback=runner._clarify_callback_sync))
            assert 'Profil A' in json.dumps(reply),reply
            assert extension.turn_claim.get().can_change()
        assert cards and len(cards[0][1].inline_keyboard)==3
        with extension.claim_scope(('source','channel-message')):
            assert all(extension.before_tool(name,{})['action']=='block' for name in ('clarify','todo_list','session_search'))
        runner._ask_clarify_question=lambda *args,**kwargs:('[user did not respond]',False)
        with extension.claim_scope(('operator','timeout-question')):
            clarify_tool([{'question':'Welches Profil?','choices':['Profil A','Profil B']}],callback=runner._clarify_callback_sync)
            assert not extension.turn_claim.get().can_change()
        extension.session_key='owner'
        pending=clarify_gateway.register('question-2','owner','Risk?',None)
        started=threading.Event()
        original_wait=pending.event.wait
        def wait(timeout=None):
            started.set()
            return original_wait(timeout)
        pending.event.wait=wait
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiter=executor.submit(clarify_gateway.wait_for_response,'question-2',5)
            assert started.wait(3)
            extension.yield_clarification()
            assert waiter.result(3)==clarify_gateway.CANCELLED
        EXTENSIONS.discard(extension)

        # Deleting a migrated conversation does not resurrect it on the next boot.
        db.delete_session('legacy_owner')
        assert migrate_session_history(home,db=db)['imported']==0
        assert db.get_session('legacy_owner') is None
        legacy.append('rollback_owner',{'role':'user','content':'Atomic import retry'})
        original_import=db._import_session_row
        def failed_import(*args,**kwargs):
            original_import(*args,**kwargs)
            raise RuntimeError('simulated interruption before import receipt')
        db._import_session_row=failed_import
        try:
            migrate_session_history(home,db=db)
            raise AssertionError('Import fault was not raised')
        except RuntimeError:
            assert db.get_session('rollback_owner') is None
            assert not db._read_one('SELECT 1 FROM aqla_jsonl_imports WHERE session_id=?',('rollback_owner',))
        finally:
            db._import_session_row=original_import
        assert migrate_session_history(home,db=db)['imported']==1
        print(json.dumps({'native_sqlite_import_search':True,'migration_idempotent':True,'old_files_preserved':True,
                         'import_rollback':True,'planning_persisted':True,'native_telegram_question_card':True,'source_tools_blocked':True,
                         'timeout_not_authorization':True,'trading_work_cancels_question':True,'exchange_actions':0}))
    finally:
        db.close()
        undo()


if __name__=='__main__':
    main()
