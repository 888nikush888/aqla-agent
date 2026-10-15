import json

import pytest

from aqla_agent.semantic_contract import supported_action_failure
from aqla_hyperliquid_mcp.models import Evidence, Interpretation, SourceEvent, now_ms
from aqla_hyperliquid_mcp.server import close_server, create_server

REPORTS = [
    'Обсуждаем рынок BTC\n00:00 - Начало\n02:12 - Entry limit orders at 100 and 95\n03:45 - Example short entry\n04:00 - risk limits',
    'BTC LONG. Entry 100. After TP1 close half and move stop to entry.\nВсё что выше написал - это пример агрессивной работы. Высочайший риск.',
    '#QNT – закрыл в небольшой плюс. Если открывались ниже входа, есть возможность выйти около безубытка.',
]


@pytest.mark.parametrize('text', REPORTS)
def test_original_report_framing_is_not_an_executable_structure(text):
    report = Interpretation(kind='report', source_refs=['anchor'], reason='Original commentary')
    assert supported_action_failure(text, report) is None


@pytest.mark.parametrize('text', REPORTS[:2])
def test_original_report_framing_does_not_relax_actionable_interpretations(text):
    action = Interpretation(kind='new_signal', source_refs=['anchor'], symbol='BTC', side='long')
    assert supported_action_failure(text, action) is not None


@pytest.mark.parametrize('tail', [
    '\nNow BTC reduce half and move stop to95.',
    '\nСейчас BTC: закрыть половину и стоп на вход.',
])
@pytest.mark.parametrize('report', REPORTS[:2])
def test_independent_current_instruction_after_report_framing_is_still_vetoed(report, tail):
    value = Interpretation(kind='report', source_refs=['anchor'], reason='Commentary')
    assert supported_action_failure(report + tail, value) is not None


@pytest.mark.parametrize('text', [
    'Earlier closed BTC. If price falls, close half and move stop to95.',
    'Earlier closed BTC. Now there is an opportunity: close half and move stop to95.',
])
def test_past_report_never_masks_an_independent_conditional_or_current_order(text):
    value = Interpretation(kind='report', source_refs=['anchor'], reason='Commentary')
    assert supported_action_failure(text, value) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('text', REPORTS)
async def test_report_receipt_and_original_evidence_complete_ready_work_without_financial_changes(setup, text):
    settings, state, fake, exchange, _ = setup
    server = create_server(settings, state=state, exchange=exchange)
    source = SourceEvent(chat_id='channel', message_id=901, sent_at=now_ms(), text=text)
    server.aqla_trading.ingest(source)
    before = state.snapshot()
    value = Interpretation(kind='report', source_refs=[source.source_id], reason='Original report framing, no current order',
        evidence=[Evidence(field='classification', source_id=source.source_id, quote=text)])
    receipt = server.aqla_trading.context(source.event_id)['source_read_receipt']
    try:
        result = await server.call_tool('interpret_source_batch', {'items': [{'event_id': source.event_id,
            'kind': 'report', 'source_refs': value.source_refs, 'reason': value.reason,
            'evidence': [row.model_dump(mode='json') for row in value.evidence], 'source_read_receipt': receipt}],
            'context_auth': server.aqla_authority.sign('source', 'report-regression')})
        payload = result.structured_content or json.loads(result.content[0].text)
        assert payload['succeeded'] == 1
        after = state.snapshot()
        assert after['events'][source.event_id]['status'] == 'report'
        for name in ('trades', 'plans', 'operations', 'policy', 'profiles', 'pipelines', 'account_enabled'):
            assert after.get(name) == before.get(name)
        assert not fake.actions
    finally:
        await close_server(server)
