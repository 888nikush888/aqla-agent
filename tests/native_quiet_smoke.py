"""Pinned native output hooks with actual MCP notification reads and ACKs; offline."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


async def produce(home):
    import httpx
    import yaml

    from aqla_agent.recovery_notices import record_source_notice
    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.models import Settings
    from aqla_hyperliquid_mcp.server import close_server, create_server
    from aqla_hyperliquid_mcp.state import State

    key = home / 'integration.secret'
    key.write_text('synthetic-quiet-notification-key-' * 3, encoding='utf-8')
    settings = Settings(network='testnet', account='0x' + '0' * 40, state_path=home / 'state.sqlite3', integration_key_file=key)
    config = home / 'aqla.yaml'
    config.write_text(yaml.safe_dump(settings.model_dump(mode='json')), encoding='utf-8')
    state = State(settings)
    actions = []
    def transport(request):
        actions.append(request.url.path)
        raise AssertionError('Notification-only probe must make no exchange call')
    exchange = Hyperliquid(settings, state, httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(transport)))
    server = create_server(settings, state=state, exchange=exchange)
    try:
        state.update(lambda data: record_source_notice(data, 'reader_unavailable', 'new-outage'))
        result = await server.call_tool('notifications_read', {'view': 'rendered'})
        page = result.structured_content or json.loads(result.content[0].text)
        arguments = {'notification_ids': list(page['notification_versions']), 'versions': page['notification_versions'],
            'context_auth': server.aqla_authority.sign('source', 'native-quiet')}
        result = await server.call_tool('notifications_ack', arguments)
        ack = result.structured_content or json.loads(result.content[0].text)
        assert not actions and not state.snapshot()['notifications']
        arguments.pop('context_auth')
        (home / 'wire.json').write_text(json.dumps({'page': page, 'arguments': arguments, 'ack': ack}), encoding='utf-8')
    finally:
        await close_server(server)


async def verify(home, mcp_python):
    from concurrent.futures import ThreadPoolExecutor

    from agent.turn_finalizer import apply_llm_output_transform
    from hermes_cli import lifecycle
    from model_tools import _emit_post_tool_call_hook

    from aqla_agent.events import HermesExtension
    from aqla_hyperliquid_mcp.models import AqlaError

    subprocess.run([mcp_python, __file__, '--produce', '--home', str(home)], check=True, capture_output=True)
    wire = json.loads((home / 'wire.json').read_text(encoding='utf-8'))
    values = {'owner_id': '12345', 'config_path': str(home / 'aqla.yaml')}
    ext = HermesExtension(SimpleNamespace(get_config=lambda key, default=None: values.get(key, default)))
    old_hooks, old_has = lifecycle._plugin_hooks, lifecycle.has_hook
    def hooks(name, **args):
        if name == 'post_tool_call':
            ext.after_tool(**args)
        if name == 'transform_llm_output':
            return [ext.transform_output(**args)]
        return []
    lifecycle._plugin_hooks = hooks
    lifecycle.has_hook = lambda name: name in ('post_tool_call', 'transform_llm_output')
    try:
        with ext.claim_scope(('source', 'quiet-wake')):
            ext.turn_claim.get().automatic = True
            agent = SimpleNamespace(session_id='synthetic-native-quiet', model='synthetic', platform='telegram')
            text, changed, _ = apply_llm_output_transform(agent, 'Status-Runde: Unverändert', turn_id='quiet')
            assert text == 'NO_REPLY' and changed
        with ext.claim_scope(('source', 'event-wake')):
            ext.turn_claim.get().automatic = True
            page = wire['page']
            for name, arguments, result in (('notifications_read', {'view': 'rendered'}, page), ('notifications_ack', wire['arguments'], wire['ack'])):
                _emit_post_tool_call_hook(function_name='mcp__aqla__' + name, function_args=arguments,
                    result=json.dumps({'result': json.dumps(result)}), task_id='quiet-smoke')
            expected = '\n\n'.join(text for message in page['telegram_view']['messages'] for text in message['texts'])
            agent = SimpleNamespace(session_id='synthetic-native-event', model='synthetic', platform='telegram')
            text, changed, _ = apply_llm_output_transform(agent, 'A verbose invented status round', turn_id='event')
            assert text == expected and text != 'NO_REPLY' and changed
        from tools.interrupt import acting_for_tid, is_interrupted, set_interrupt
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        await asyncio.to_thread(set_interrupt, True)
        def probe(*_):
            return {'ok': not is_interrupted(), 'result': {'background_mcp_usable': True}}
        ext.ctx.call_mcp = probe
        assert await ext.mcp_call('aqla', 'integration', {}) == {'background_mcp_usable': True}
        await asyncio.to_thread(set_interrupt, True)
        try:
            with ext.claim_scope(('operator', 'still-active')):
                try:
                    await ext.mcp_call('aqla', 'integration', {})
                except AqlaError:
                    pass
                else:
                    raise AssertionError('Active operator interruption was cleared')
        finally:
            await asyncio.to_thread(set_interrupt, False)
        acting = acting_for_tid.set(123456789)
        set_interrupt(True, thread_id=123456789)
        try:
            assert await ext.mcp_call('aqla', 'integration', {}) == {'background_mcp_usable': True}
            assert is_interrupted(), 'Parent operator interrupt must remain set'
        finally:
            acting_for_tid.reset(acting)
            set_interrupt(False, thread_id=123456789)
        from hermes_cli.plugins import PluginContext
        from tools import mcp_tool_handlers
        handler, previous_context = mcp_tool_handlers._make_tool_handler, ext.ctx
        deadlines = []
        def delayed_handler(server, tool, timeout):
            deadlines.append((server, tool, timeout))
            return lambda _: json.dumps({'result': {'status': 'published'}}) if timeout >= 45 else json.dumps({'error': 'MCP call timed out'})
        native_context = SimpleNamespace(plugin_id='aqla-events', manifest=SimpleNamespace(name='aqla-events'),
            _mcp_allowlist=lambda _: ['sheets'], _mcp_envelope=PluginContext._mcp_envelope)
        native_context.call_mcp = lambda *args, **kwargs: PluginContext.call_mcp(native_context, *args, **kwargs)
        try:
            mcp_tool_handlers._make_tool_handler = delayed_handler
            ext.ctx = native_context
            assert await ext.mcp_call('sheets', 'sync_presentation_internal', {}) == {'status': 'published'}
            assert deadlines == [('sheets', 'sync_presentation_internal', 240.0)]
        finally:
            mcp_tool_handlers._make_tool_handler, ext.ctx = handler, previous_context
        print(json.dumps({'native_transform_before_persistence': True, 'actual_mcp_read_and_versioned_ack': True,
            'quiet_without_new_event': True, 'exact_rendered_event_delivered': True, 'exchange_calls': 0,
            'stale_pooled_interrupt_cleared_for_background_only': True, 'active_operator_interrupt_preserved': True,
            'native_sheets_deadline_covers_verified_publication': True}))
    finally:
        lifecycle._plugin_hooks, lifecycle.has_hook = old_hooks, old_has


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source')
    parser.add_argument('--mcp-python')
    parser.add_argument('--produce', action='store_true')
    parser.add_argument('--home', required=True)
    args = parser.parse_args()
    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_HOME'] = str(home)
    os.environ['HERMES_SKIP_CONFIG_MIGRATION'] = '1'
    os.environ['HERMES_SKIP_UPDATE_CHECK'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    if args.produce:
        asyncio.run(produce(home))
    else:
        sys.path.insert(0, str(Path(args.hermes_source).resolve()))
        asyncio.run(verify(home, args.mcp_python))


if __name__ == '__main__':
    main()
