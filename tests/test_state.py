import json
import sqlite3
from decimal import Decimal

import pytest

from aqla_hyperliquid_mcp.models import AqlaError, Settings, SourceEvent, digest
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


def test_metadata_patch_never_serializes_financial_history_and_keeps_full_durability(setup, monkeypatch):
    _, state, *_ = setup
    before = state.snapshot()
    def forbidden(_):
        pytest.fail("Metadata update serialized the whole financial body")
    monkeypatch.setattr(state._store, "save", forbidden)
    state.update_metadata(("cursors",), lambda data: data["cursors"].update(stream=5))
    assert read_snapshot(state.path) == state.snapshot()
    assert state.snapshot()["trades"] == before["trades"]
    assert state._store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    committed = state.snapshot()
    state._store.connection.execute("CREATE TRIGGER deny_metadata BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'disk write failed'); END")
    with pytest.raises(AqlaError, match="Durable state write failed"):
        state.update_metadata(("cursors",), lambda data: data["cursors"].update(stream=6))
    assert state.snapshot() == read_snapshot(state.path) == committed


def test_empty_outbox_poll_has_no_persistent_write_or_sequence_change(setup, monkeypatch):
    from aqla_agent.delivery import NotificationOutbox
    _, state, *_ = setup
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("native")
    before = state.snapshot()
    monkeypatch.setattr(state, "_write", lambda _: pytest.fail("No work must not cause a durable write"))
    assert outbox.next_part("native")["delivery_id"] is None
    assert state.snapshot() == before


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
    settings, _state, *_ = setup
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


def test_metadata_transaction_matches_generic_state_and_detaches_callback_data(setup):
    _, state, *_ = setup
    before = state.snapshot()
    captured = []

    def change(part):
        captured.append(part)
        part["nonces"]["scope"] = 123
        return {"nonce": 123}

    assert state.update_metadata(("nonces",), change) == {"nonce": 123}
    expected = {**before, "nonces": {**before["nonces"], "scope": 123}, "sequence": before["sequence"] + 1}
    assert state.snapshot() == read_snapshot(state.path) == expected
    captured[0]["nonces"]["scope"] = 999
    assert state.snapshot() == expected


@pytest.mark.parametrize("root", ["trades", "policy", "operations", "account", "unknown", "sequence", "journal_history"])
def test_metadata_transaction_rejects_undeclared_or_financial_roots(setup, root):
    _, state, *_ = setup
    before = state.snapshot()
    with pytest.raises(AqlaError):
        state.update_metadata((root,), lambda part: part.update({root: {}}))
    with pytest.raises(AqlaError):
        state.update_metadata(("nonces",), lambda part: part.update({root: {}}))
    assert state.snapshot() == read_snapshot(state.path) == before


def test_metadata_transaction_real_sqlite_abort_keeps_ram_and_disk(setup):
    _, state, *_ = setup
    before = state.snapshot()
    state._store.connection.execute("CREATE TRIGGER deny_metadata BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'metadata rejected'); END")
    with pytest.raises(AqlaError, match="Durable state write failed"):
        state.update_metadata(("nonces",), lambda part: part["nonces"].update(scope=123))
    assert state.snapshot() == read_snapshot(state.path) == before


def test_metadata_transaction_json_failure_keeps_ram_and_disk(setup):
    _, state, *_ = setup
    before = state.snapshot()
    with pytest.raises((TypeError, ValueError)):
        state.update_metadata(("nonces",), lambda part: part["nonces"].update(scope=Decimal("1")))
    assert state.snapshot() == read_snapshot(state.path) == before


def test_selected_read_returns_only_detached_requested_entries(setup):
    _, state, *_ = setup
    state.update(lambda data: data["events"].update({"one": {"nested": [1]}, "two": {"nested": [2]}}))
    selected = state.read("events", "sequence", entries={"events": ["one"]})
    assert set(selected) == {"events", "sequence"} and set(selected["events"]) == {"one"}
    selected["events"]["one"]["nested"].append(999)
    assert state.snapshot()["events"]["one"] == {"nested": [1]}


def test_reader_projection_matches_full_snapshot_contract_and_detaches_returned_lists(setup):

    _, state, _, _, trading = setup
    for index in range(4):
        trading.ingest(SourceEvent(chat_id="channel", message_id=index + 1, sent_at=1_800_000_000_000 + index,
                                   received_at=1_800_000_000_000 + index, text="reader context", historical=True))
    before = state.snapshot()
    expected = {"sources": before["sources"], "cursors": before["cursors"], "context_cutoffs": {}, "context_ids": {},
                "source_aliases": before["source_aliases"], "tracked_ids": {"channel": [3, 4]}, "pending": [], "notification_pending": 0,
                "source_queue": {"ready_total": 0, "ready": [], "raw_unfinished_total": 0, "superseded_total": 0,
                    "blocked_total": 0, "blocked_revision_total": 0, "blocked": [], "blocked_signature": None,
                    "recovery_total": 0, "recovery_source_total": 0, "recovery": [], "recovery_operation_total": 0,
                    "recovery_operations": [], "historical_conflict_total": 0, "historical_conflicts": []}}
    result = state.reader_snapshot(2)
    assert result.pop("notification_signature") == digest(before["notifications"])
    assert result["source_queue"].pop("ready_signature") == digest([])
    assert result["source_queue"].pop("recovery_signature") == digest([])
    assert result == expected
    result["tracked_ids"]["channel"].append(999)
    result["cursors"]["channel"] = 999
    assert state.snapshot() == before


def test_reader_projection_uses_linear_grouper_without_copying_unselected_roots(setup, monkeypatch):
    from aqla_hyperliquid_mcp.server import reader_overview

    _, state, *_ = setup
    class CountedEvents(dict):
        scans = 0
        def values(self):
            for row in super().values():
                self.scans += 1
                yield row
    class UnselectedBody:
        def __deepcopy__(self, _):
            pytest.fail("The reader must not copy an unrelated full-state body")
    events = {str(index): {"event_id": str(index), "source_id": "ref-" + str(index), "status": "pending" if index % 3 == 0 else "report",
        "source": {"chat_id": str(index % 10), "message_id": index + 1, "sent_at": index, "text": "reader context"}} for index in range(1000)}
    state.data["events"] = events
    expected = reader_overview(state.data, 5)
    counted = CountedEvents(events)
    state.data["events"] = counted
    state.data["unselected_reader_test_body"] = UnselectedBody()
    def reject_snapshot():
        pytest.fail("The selective reader must not create a full state snapshot")
    monkeypatch.setattr(state, "snapshot", reject_snapshot)
    actual = state.reader_snapshot(5)
    assert actual == expected and counted.scans == len(events)
    actual["tracked_ids"]["0"].append(99999)
    assert state.reader_snapshot(5) == expected
    assert counted.scans == 2 * len(events)
    del state.data["unselected_reader_test_body"]




def test_conditional_generic_update_preserves_bool_types_order_and_explicit_revisions(setup, monkeypatch):
    _, state, *_ = setup
    writes, original = [], state._write
    monkeypatch.setattr(state, "_write", lambda data: writes.append(data["sequence"]) or original(data))
    before = state.snapshot()
    state.update_if_changed(lambda data: data.update(paused=False))
    assert not writes and state.snapshot() == before
    state.update_if_changed(lambda data: data.update(paused=0))
    assert len(writes) == 1 and type(state.snapshot()["paused"]) is int
    state.update_if_changed(lambda data: data.update(policy_revision=data["policy_revision"] + 1))
    assert len(writes) == 2
    state.update(lambda data: data["nonces"].update(one=1, two=2))
    state.update_if_changed(lambda data: data["nonces"].update(one=data["nonces"].pop("one")))
    assert len(writes) == 4 and list(state.snapshot()["nonces"]) == ["two", "one"]




def test_actual_nonce_uses_detached_metadata_and_keeps_persistence_before_send(setup, monkeypatch):
    from aqla_agent import journal

    _, state, fake, exchange, _ = setup
    monkeypatch.setattr(journal, "project", lambda *_: pytest.fail("Actual nonce used complete journal projection"))
    nonce = exchange._nonce()
    assert state.snapshot() == read_snapshot(state.path)
    assert nonce in state.snapshot()["nonces"].values()
    state._store.connection.execute("CREATE TRIGGER deny_nonce BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'nonce rejected'); END")
    before = state.snapshot()
    with pytest.raises(AqlaError, match="Durable state write failed"):
        exchange._nonce()
    assert state.snapshot() == read_snapshot(state.path) == before
    assert fake.actions == []


def test_scoped_nonce_matches_generic_metadata_and_selected_view_is_detached(setup, monkeypatch):
    from aqla_agent.router import Router

    settings, state, _, exchange, _ = setup
    router = Router(settings, state, exchange)
    # The existing account view initializes its first-use bookkeeping through
    # the full transaction; only subsequent nonces are pure metadata commits.
    first_sequence = state.snapshot()["sequence"]
    exchange._nonce()
    assert state.snapshot()["sequence"] == first_sequence + 1
    assert state.has_entry("account_events", "hyperliquid:" + settings.network)
    before = state.snapshot()
    captured = []
    original = state.update_metadata

    def observe(roots, callback):
        captured.append(set(roots))
        return original(roots, callback)

    monkeypatch.setattr(state, "update_metadata", observe)
    nonce = exchange._nonce()
    assert captured == [{"nonces", "account_nonces"}]
    expected = {**before, "nonces": {**before["nonces"], settings.network + ":" + exchange.wallet.address.lower(): nonce},
                "sequence": before["sequence"] + 1}
    assert state.snapshot() == read_snapshot(state.path) == expected
    view = router.engines["hyperliquid"].state.read("nonces")
    view["nonces"].clear()
    assert state.snapshot() == expected






def test_partial_metadata_actual_process_crash_keeps_committed_sqlite_and_restores(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "crash-state.sqlite3"
    script = """
import os
import sys
from aqla_hyperliquid_mcp.models import Settings
from aqla_hyperliquid_mcp.state import State
settings = Settings(network='testnet', account='0x' + '0' * 40, state_path=sys.argv[1])
state = State(settings)
if sys.argv[2] == 'commit':
    state.update_metadata(('nonces',), lambda part: part['nonces'].update(scope=17))
    os._exit(0)
state._store.connection.create_function('synthetic_crash', 0, lambda: os._exit(7))
state._store.connection.execute("CREATE TRIGGER crash_metadata BEFORE UPDATE ON state BEGIN SELECT synthetic_crash(); END")
state.update_metadata(('nonces',), lambda part: part['nonces'].update(scope=18))
os._exit(99)
"""
    first = subprocess.run([sys.executable, "-c", script, str(path), "commit"], check=False, timeout=20)
    assert first.returncode == 0
    committed = read_snapshot(path)
    crashed = subprocess.run([sys.executable, "-c", script, str(path), "crash"], check=False, timeout=20)
    assert crashed.returncode == 7
    assert read_snapshot(path) == committed
    settings = Settings(network="testnet", account="0x" + "0" * 40, state_path=path)
    with State(settings) as resumed:
        assert resumed.snapshot() == committed
        resumed._store.connection.execute("DROP TRIGGER crash_metadata")
        resumed.update_metadata(("nonces",), lambda part: part["nonces"].update(scope=19))
        assert resumed.snapshot() == read_snapshot(path)
