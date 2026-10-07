"""Pinned Telegram transport/escaping/splitting and native session persistence, offline."""
import argparse
import asyncio
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
    from gateway.config import PlatformConfig
    from hermes_state import SessionDB
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram.constants import ParseMode

    from aqla_agent.messages import monthly_message, telegram_view

    trade = {'trade_id': 'trade', 'event_id': 'event', 'chat_id': '-100123', 'status': 'open',
             'filled_quantity': '2', 'remaining_quantity': '2', 'entry_vwap': '100', 'stop_price': '99',
             'targets': ['101', '102'], 'target_budgets': ['1', '1'],
             'plan': {'market': {'symbol': 'BTC', 'quote_token': 'USDC'}, 'side': 'long', 'source_refs': ['setup'],
                      'quantity': '2', 'effective_leverage': 12, 'signal_leverage': 12, 'signal_leverage_original': '12.78',
                      'network': 'testnet', 'exchange': 'hyperliquid', 'profile_id': 'standard', 'backup_fields': []}}
    data = {'trades': {'trade': trade}, 'events': {}, 'operations': {}, 'paused': False,
            'source_names': {'-100123': '[VIP]* Gold_`'}, 'profiles': {'standard': {'name': 'Standard'}},
            'notifications': {'notice': {'trade_id': 'trade', 'change': {'status': 'open', 'filled_quantity': '2'},
                                         'previous': {'status': 'opening', 'filled_quantity': '0'}}}}
    view = telegram_view(data, {'hyperliquid': {'network': 'testnet', 'configured': True, 'enabled': True}})
    card = '\n\n'.join(view['messages'][0]['texts'])
    monthly = monthly_message({'month': '2026-10', 'summary': {'trades': 2, 'complete': 1, 'excluded': 1,
        'wins': 1, 'losses': 0, 'win_rate_percent': '100', 'roe_sum_percent': '12.5', 'net_by_currency': {'USDC': '3.75'}},
        'open_trades': [], 'undated_closed_trades': [], 'url': 'https://docs.google.com/spreadsheets/d/ci/edit#gid=1'})
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
    adapter._bot = Bot()
    async def verify():
        first = await adapter.send('12345', card, metadata={'notify': True})
        assert first.success and '*Trade eröffnet*' in delivered[0]['text']
        assert '\\[' not in delivered[0]['text'].split('**Quelle**')[0]  # Name is native inline code.
        sent = await adapter.send('12345', monthly, metadata={'notify': True})
        assert sent.success and '*Monatsbericht' in delivered[-1]['text'] and 'gid=1' in delivered[-1]['text']
        before = len(delivered)
        long = '\n\n'.join(card for _ in range(15))
        result = await adapter.send('12345', long, metadata={'notify': True})
        assert result.success and len(delivered) > before + 1
        return long
    long = asyncio.run(verify())
    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session('message-view', 'telegram')
        db.append_messages_batch('message-view', [{'role': 'user', 'content': 'Status'}, {'role': 'assistant', 'content': card}])
        assert db.get_messages_as_conversation('message-view')[-1]['content'] == card
        db.append_messages_batch('message-view', [{'role': 'assistant', 'content': monthly}])
        assert db.get_messages_as_conversation('message-view')[-1]['content'] == monthly
    finally:
        db.close()
    print(json.dumps({'native_markdown_send': True, 'native_split': True, 'saved_reply_matches': True,
                      'chunks': len(delivered), 'exchange_actions': 0, 'telegram_network_calls': 0, 'input_chars': len(long)}))


if __name__ == '__main__':
    main()
