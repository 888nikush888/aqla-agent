"""Pinned Telegram transport/escaping/splitting and native session persistence, offline."""
import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', required=True)
    parser.add_argument('--home', required=True)
    args = parser.parse_args()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_HOME'] = str(home)
    os.environ['HERMES_SKIP_CONFIG_MIGRATION'] = '1'
    os.environ['HERMES_SKIP_UPDATE_CHECK'] = '1'
    sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(args.hermes_source).resolve())]
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run_turn import GatewayTurnMixin
    from gateway.session import SessionSource
    from hermes_state import SessionDB
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram.constants import ParseMode

    from aqla_agent.messages import lifecycle_block, monthly_message, telegram_view

    trade = {'trade_id': 'trade', 'event_id': 'event', 'chat_id': '-100123', 'status': 'open',
             'filled_quantity': '2', 'remaining_quantity': '2', 'entry_vwap': '100', 'stop_price': '99',
             'targets': ['101', '102'], 'target_budgets': ['1', '1'],
             'plan': {'market': {'symbol': 'BTC', 'quote_token': 'USDC'}, 'side': 'long', 'source_refs': ['setup'],
                      'quantity': '2', 'effective_leverage': 12, 'signal_leverage': 12, 'signal_leverage_original': '12.78',
                      'network': 'testnet', 'exchange': 'aster', 'requested_exchange': 'hyperliquid', 'fallback_reason': 'Confirmed absent',
                      'rank_snapshot': {'tier': 'D', 'factor': '.75'}, 'policy': {'risk_percent': '3.75'},
                      'source_tp': {'unit': 'roi_percent', 'values': ['20', '40']}, 'profile_id': 'standard', 'backup_fields': []},
             'initial_sl_risk': {'amount': '2'}}
    trade['fill_events'] = {'entry': {'kind': 'entry', 'sz': '2', 'px': '100', 'time': 1000}}
    data = {'trades': {'trade': trade}, 'events': {}, 'operations': {}, 'paused': False,
            'source_names': {'-100123': '[VIP]* Gold_`'}, 'profiles': {'standard': {'name': 'Standard'}},
            'notifications': {'notice': {'trade_id': 'trade', 'change': {'status': 'open', 'filled_quantity': '2'},
                                         'previous': {'status': 'opening', 'filled_quantity': '0'}}}}
    view = telegram_view(data, {'hyperliquid': {'network': 'testnet', 'configured': True, 'enabled': True}})
    card = '\n\n'.join(view['messages'][0]['texts'])
    assert 'ROI vor Kosten' in card and 'Kanalrang' in card and 'Börsenersatz' in card and 'RPR-Budget' in card
    monthly = monthly_message({'month': '2026-10', 'summary': {'trades': 2, 'complete': 1, 'excluded': 1,
        'wins': 1, 'losses': 0, 'win_rate_percent': '100', 'roe_sum_percent': '12.5', 'net_by_currency': {'USDC': '3.75'}},
        'open_trades': [], 'undated_closed_trades': [], 'url': 'https://docs.google.com/spreadsheets/d/ci/edit#gid=1'})
    entry_card = lifecycle_block(data, {'kind': 'entry_fill', 'time': 1791397511739, 'facts': {**trade, 'entry_stop_confirmed': False}})
    closed_card = lifecycle_block(data, {'kind': 'closed', 'time': 1791397512420, 'facts': {**trade, 'status': 'closed', 'fill_events': {}}})
    retry_data = copy.deepcopy(data)
    retry_trade = retry_data['trades']['trade']
    retry_trade.update(status='preparing', filled_quantity='0', remaining_quantity='0', fill_events={},
                       entry_vwap=None, observation_retry={'attempts': 1, 'next_retry_at': 1791782100000,
                           'deadline_at': 1791790000000, 'code': 'stale_observation', 'stage': 'account',
                           'category': 'read', 'exhausted': False, 'authority': 'execute',
                           'error': 'PRIVATE_PROVIDER_TRACE_MUST_NOT_APPEAR'})
    retry_card = '\n\n'.join(telegram_view(retry_data, {})['messages'][0]['texts'])
    assert 'Technische Prüfung ausstehend' in retry_card and 'Nächste Prüfung' in retry_card
    assert 'Trade abgelehnt' not in retry_card and 'PRIVATE_PROVIDER_TRACE' not in retry_card
    adapter = TelegramAdapter(PlatformConfig(extra={'allow_from': ['12345']}))
    delivered = []
    class Bot:
        async def send_message(self, **kwargs):
            delivered.append(kwargs)
            assert kwargs['parse_mode'] == ParseMode.MARKDOWN_V2
            assert len(kwargs['text'].encode('utf-16-le')) // 2 <= 4096
            return SimpleNamespace(message_id=len(delivered))
        async def send_chat_action(self, **kwargs):
            return True
        async def send_document(self, **kwargs):
            raise AssertionError("Unexpected document from formatter label")
        async def send_photo(self, **kwargs):
            raise AssertionError("Unexpected image from formatter label")
    adapter._bot = Bot()
    async def verify():
        first = await adapter.send('12345', card, metadata={'notify': True})
        assert first.success and '*Trade eröffnet*' in delivered[0]['text']
        assert '\\[' not in delivered[0]['text'].split('**Quelle**')[0]  # Name is native inline code.
        sent = await adapter.send('12345', monthly, metadata={'notify': True})
        assert sent.success and '*Monatsbericht' in delivered[-1]['text'] and '/aqla' in delivered[-1]['text'] and 'gid=1' not in delivered[-1]['text']
        for message in (entry_card, closed_card):
            sent = await adapter.send('12345', message, metadata={'notify': True})
            assert sent.success
        assert '*Einstieg ausgeführt*' in delivered[-2]['text'] and '*Trade geschlossen*' in delivered[-1]['text']
        sent = await adapter.send('12345', retry_card, metadata={'notify': True})
        assert sent.success and '*Technische Prüfung ausstehend*' in delivered[-1]['text']
        assert 'PRIVATE_PROVIDER_TRACE' not in delivered[-1]['text']
        before = len(delivered)
        long = '\n\n'.join(card for _ in range(15))
        result = await adapter.send('12345', long, metadata={'notify': True})
        assert result.success and len(delivered) > before + 1
        return long
    long = asyncio.run(verify())
    async def normal_gateway_route():
        source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', user_id='12345')
        event = MessageEvent(text='Status', source=source, message_id='4242')
        runner = SimpleNamespace(_delivery_adapter_for=lambda _: adapter, _should_send_voice_reply=lambda *_args, **_kwargs: False)
        completed = []
        fixture = home / 'cache' / 'synthetic-report.txt'
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text('Benign native formatter fixture', encoding='utf-8')
        labels = ['[Daten](https://example.invalid/image.png)', 'MEDIA:/not-existing/synthetic.pdf',
                  'MEDIA:' + fixture.as_posix(), '[[audio_as_voice]]', '[[as_document]]',
                  '<img src="https://example.invalid/x.png">', 'VIP_`[] 🚀 Ä \\', 'Ä🚀[]`\\{}' * 150]
        for label in labels:
            data['source_names']['-100123'] = label
            view = telegram_view(data, {'hyperliquid': {'network':'testnet','configured':True,'enabled':True}})
            response = '\n\n'.join(view['messages'][0]['texts'])
            response = '\n\n'.join(response for _ in range(12))
            routed = await GatewayTurnMixin._hmwa_deliver_turn_response(
                runner, event, source, SimpleNamespace(session_id='synthetic-route'), 'synthetic-route', 1,
                {'final_response':response}, [], response, None, False)
            assert routed == response
            extracted = await adapter._extract_response_content(routed, event, 'synthetic-route', is_ephemeral_response=False)
            assert not extracted.images and not extracted.media_files and not extracted.local_files
            assert not extracted.force_document_attachments
            assert extracted.text_content == response.strip()
            before = len(delivered)
            await adapter._send_final_text(event, 'synthetic-route', extracted.text_content, {}, False, 0, completed.append)
            await adapter._deliver_attachments(event, extracted, {}, anything_sent=True, record_delivery=completed.append)
            assert len(delivered) > before and all(result.success for result in completed)
            assert all('](https://example.invalid' not in message['text'] or '\\[' in message['text'] or '`' in message['text'] for message in delivered[before:])
        assert 'zum Belegzeitpunkt nicht bestätigt' in entry_card
        assert 'PnL nicht vollständig bestätigt' in closed_card
        intended = await adapter._extract_response_content('Eigener Auftrag ![Owner](https://example.invalid/owner.png)', event,
                                                           'synthetic-owner-media', is_ephemeral_response=False)
        assert intended.images == [('https://example.invalid/owner.png', 'Owner')]
    asyncio.run(normal_gateway_route())
    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session('message-view', 'telegram')
        db.append_messages_batch('message-view', [{'role': 'user', 'content': 'Status'}, {'role': 'assistant', 'content': card}])
        assert db.get_messages_as_conversation('message-view')[-1]['content'] == card
        db.append_messages_batch('message-view', [{'role': 'assistant', 'content': monthly}])
        assert db.get_messages_as_conversation('message-view')[-1]['content'] == monthly
        db.append_messages_batch('message-view', [{'role': 'assistant', 'content': entry_card}, {'role': 'assistant', 'content': closed_card}])
        assert [row['content'] for row in db.get_messages_as_conversation('message-view')[-2:]] == [entry_card, closed_card]
        db.append_messages_batch('message-view', [{'role': 'assistant', 'content': retry_card}])
        assert db.get_messages_as_conversation('message-view')[-1]['content'] == retry_card
    finally:
        db.close()
    print(json.dumps({'native_markdown_send': True, 'native_split': True, 'saved_reply_matches': True,
                      'normal_gateway_response_router': True, 'no_unexpected_media_or_label_links': True, 'owner_media_extraction_preserved': True,
                      'unknown_fill_price_not_inferred': True,
                      'pending_read_retry_delivered_without_final_rejection': True,
                      'chunks': len(delivered), 'exchange_actions': 0, 'telegram_network_calls': 0, 'input_chars': len(long)}))


if __name__ == '__main__':
    main()
