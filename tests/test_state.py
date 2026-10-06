import json
import sqlite3

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Settings
from aqla_hyperliquid_mcp.state import State
from aqla_hyperliquid_mcp.storage import database_path, read_snapshot


def test_exclusive_writer_and_restart_keeps_rules(setup):
    settings, state, *_ = setup
    with pytest.raises(AqlaError, match="already owns"):
        State(settings)
    state.update(lambda row: row.update(paused=True, policy_revision=7))
    state.close()
    with State(settings) as restored:
        assert restored.snapshot()["paused"] is True
        assert restored.snapshot()["policy_revision"] == 7


def test_corrupt_state_is_not_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken")
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=path)
    with pytest.raises(json.JSONDecodeError):
        State(settings)
    assert path.read_text() == "{broken"


def test_write_failure_rolls_back_memory(setup, monkeypatch):
    _, state, *_ = setup
    old = state.snapshot()

    def fail(_):
        raise OSError("Disk full")

    monkeypatch.setattr(state, "_write", fail)
    with pytest.raises(OSError):
        state.update(lambda row: row.update(paused=True))
    assert state.snapshot() == old


def test_sqlite_transaction_failure_keeps_committed_state(setup):
    _, state, *_ = setup
    old = state.snapshot()
    # SQLite rejects the write within the real transaction, not a mocked save.
    state._store.connection.execute("CREATE TRIGGER deny_write BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'disk write failed'); END")
    with pytest.raises(AqlaError, match="Durable state write failed"):
        state.update(lambda data: data.update(paused=True))
    assert state.snapshot() == read_snapshot(state.path) == old


def test_json_migration_retains_all_identities_and_original_backup(setup, tmp_path):
    settings, state, *_ = setup
    data = state.snapshot()
    data['sequence'] = 88
    data['bindings'] = {'source:old': 'trade:existing'}
    data['nonces'] = {'existing-account': 1987654321}
    data['events'] = {'existing-message': {'event_id': 'existing-message', 'classification': 'report'}}
    path = tmp_path / 'import' / 'state.json'
    path.parent.mkdir()
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    settings = settings.model_copy(update={'state_path': path})
    with State(settings) as migrated:
        assert migrated.path == path.with_suffix('.sqlite3')
        assert migrated.snapshot() == data
        assert read_snapshot(path) == data
    assert path.read_bytes() == original
    # A stale backup must never override committed SQLite state.
    path.write_text('{invalid old backup')
    with State(settings) as resumed:
        assert resumed.snapshot() == data


@pytest.mark.asyncio
async def test_actual_trade_json_import_does_not_resend_orders(setup, signal, tmp_path):
    from aqla_hyperliquid_mcp.api import Hyperliquid
    from aqla_hyperliquid_mcp.trades import Trading

    settings, state, fake, exchange, trading = setup
    source, interpretation = signal
    plan = await trading.preview(source.event_id, interpretation)
    await trading.execute(plan['plan_id'])
    saved = state.snapshot()
    path = tmp_path / 'legacy-live-trade.json'
    path.write_text(json.dumps(saved), encoding='utf-8')
    settings = settings.model_copy(update={'state_path': path})
    before = len(fake.actions)
    with State(settings) as migrated:
        assert migrated.snapshot() == saved
        resumed = Trading(Hyperliquid(settings, migrated, exchange.client), migrated)
        await resumed.recover()
        assert len(fake.actions) == before
        assert migrated.snapshot()['operations'] == saved['operations']


def test_old_json_and_new_sqlite_configuration_share_one_writer(setup):
    settings, state, *_ = setup
    with pytest.raises(AqlaError, match='already owns'):
        State(settings.model_copy(update={'state_path': database_path(settings.state_path)}))


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'empty', 'newer_format'])
def test_sqlite_damage_never_resets_or_resurrects_old_json(setup, damage):
    settings, state, *_ = setup
    before = state.snapshot()
    path = state.path
    state.close()
    settings.state_path.write_text(json.dumps(before))  # stale legacy backup
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'broken sqlite')
    else:
        with sqlite3.connect(path) as connection:
            connection.execute('DELETE FROM state' if damage == 'empty' else 'PRAGMA user_version=99')
    with pytest.raises(AqlaError, match='missing|cannot be read|incomplete|Unsupported'):
        State(settings)


def test_migration_rejects_wrong_binding_before_creating_database(setup, tmp_path):
    settings, state, *_ = setup
    data = state.snapshot()
    data['network'] = 'mainnet'
    path = tmp_path / 'foreign.json'
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    with pytest.raises(AqlaError, match='differs'):
        State(settings.model_copy(update={'state_path': path}))
    assert path.read_bytes() == original
    assert not database_path(path).exists()


def test_sqlite_read_views_and_full_synchronous_mode(setup):
    _, state, *_ = setup
    state.update(lambda data: data['events'].update({'test-id': {'text': 'persisted'}}))
    with sqlite3.connect(state.path) as connection:
        assert connection.execute('SELECT id,json_extract(body,\'$.text\') FROM events').fetchall() == [('test-id', 'persisted')]
    assert state._store.connection.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert state._store.connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
