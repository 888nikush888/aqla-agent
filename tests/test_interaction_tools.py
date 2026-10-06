import sys
from types import SimpleNamespace

import pytest
from test_hermes_integration import extension  # noqa: F401 -- fixture

from aqla_agent.events import EXTENSIONS, record_clarify_outcome


@pytest.mark.parametrize('tool',['clarify','todo_list','session_search'])
def test_tools_require_live_operator_origin(extension,tool):  # noqa: F811 -- fixture
    ext,_=extension
    assert ext.before_tool(tool,{})['action']=='block'
    with ext.claim_scope(('source','signal')):
        assert ext.before_tool(tool,{})['action']=='block'
    with ext.claim_scope(('operator','owner-message')):
        assert ext.before_tool(tool,{}) is None


def test_session_search_cannot_switch_profile(extension):  # noqa: F811 -- fixture
    ext,_=extension
    with ext.claim_scope(('operator','owner')):
        assert ext.before_tool('session_search',{'query':'Risk'}) is None
        assert ext.before_tool('session_search',{'profile':'default'})['action']=='block'
        assert ext.before_tool('session_search',{'profile':'other'})['action']=='block'


@pytest.mark.parametrize('result',[{'outcome':'timed_out','answers':{}}, {'outcome':'cancelled','answers':{}},
    {'outcome':'undelivered','answers':{}},{'outcome':'submitted','answers':{'q':None}}])
def test_unanswered_clarification_never_authorizes_mutation(extension,result):  # noqa: F811 -- fixture
    ext,_=extension
    EXTENSIONS.add(ext)
    try:
        with ext.claim_scope(('operator','owner')):
            record_clarify_outcome(result)
            assert ext.before_tool('mcp__aqla__control',{'action':'pause'})['action']=='block'
            assert ext.before_tool('mcp__aqla__trade_execute',{'plan_id':'example'})['action']=='block'
            assert ext.before_tool('skill_manage',{})['action']=='block'
        with ext.claim_scope(('operator','fresh-owner-request')):
            assert ext.before_tool('mcp__aqla__control',{'action':'pause'})['action']=='modify'
    finally:
        EXTENSIONS.discard(ext)


def test_submitted_clarification_preserves_origin(extension):  # noqa: F811 -- fixture
    ext,_=extension
    EXTENSIONS.add(ext)
    try:
        with ext.claim_scope(('operator','owner')):
            record_clarify_outcome({'outcome':'submitted','answers':{'q':'Profile A'}})
            allowed=ext.before_tool('mcp__aqla__control',{'action':'pause'})
            assert ext.authority.verify(allowed['args']['context_auth'])['request_id']=='owner'
    finally:
        EXTENSIONS.discard(ext)


def test_pending_trading_work_can_cancel_a_question_without_touching_trade_state(extension,monkeypatch):  # noqa: F811 -- fixture
    ext,_=extension
    calls=[]
    gateway=SimpleNamespace(get_pending_for_session=lambda key,**kwargs:object(),clear_session=lambda key:calls.append(key))
    monkeypatch.setitem(sys.modules,'tools.clarify_gateway',gateway)
    ext.session_key='owner-session'
    ext.yield_clarification()
    assert calls==['owner-session']
