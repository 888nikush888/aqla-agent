import copy

from aqla_agent.delivery import NotificationOutbox
from aqla_agent.notification_policy import completed_waves, select
from aqla_hyperliquid_mcp.models import digest


def data(notices):
    return {"notifications": notices, "trades": {}, "events": {}, "notification_outbox": {}, "notice_policy_state": {}}


def test_transient_pair_stays_quiet_and_is_not_a_delivery_ack():
    report = [{"account_key": "hyperliquid:testnet", "code": "reconciliation_unavailable"}]
    value = data({"first": {"recovery_reports": report, "observed_at": 1000},
                  "second": {"recovery_reports": report, "resolved": True, "observed_at": 14000}})
    messages, suppressed, _ = select(value, covered=set(), stamp=20000)
    assert not messages and set(suppressed) == {"first", "second"}
    assert value["notifications"].keys() == {"first", "second"}


def test_persistent_outage_is_delayed_but_protection_is_immediate():
    value = data({"slow": {"recovery_reports": [{"account_key": "hyperliquid:mainnet", "code": "reconciliation_unavailable"}], "observed_at": 1000}})
    messages, _, deferred = select(value, covered=set(), stamp=119999)
    assert not messages and deferred
    messages, _, _ = select(value, covered=set(), stamp=121000)
    assert len(messages) == 1
    value["notifications"]["protection:hyperliquid:mainnet"] = {"severity": "urgent", "error": "Stop not confirmed", "exchange": "hyperliquid"}
    messages, _, _ = select(value, covered=set(), stamp=1001)
    assert len(messages) == 1 and messages[0]["severity"] == "urgent"


def test_routine_suppression_is_audited_without_claiming_delivery(setup):
    _, state, fake, *_ = setup
    notice = {"event_id": "synthetic", "error": "Signal expired"}
    state.update_metadata(("notifications",), lambda value: value["notifications"].__setitem__("rejection", notice))
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("one")
    assert outbox.next_part("one")["delivery_id"] is None
    snapshot = state.snapshot()
    row = snapshot["notice_history"]["rejection:" + digest(notice)]
    assert row["notice"] == notice and row["disposition"] == "dashboard_only"
    assert snapshot["notification_outbox"] == {} and not fake.actions


def test_wave_counter_or_one_side_fill_cannot_prove_a_cycle():
    trade = {"trade_id": "t", "waverider": {"cycle": 99}, "entry_orders": [{"kind": "waverider", "cycle": 2, "cloid": "buy", "quantity": "1"}],
             "exit_orders": [{"kind": "waverider", "cycle": 2, "cloid": "sell"}],
             "order_observations": {"buy": {"status": "filled", "oid": 2}, "sell": {"status": "filled", "oid": 1}},
             "fill_events": {"a": {"oid": 1, "sz": "1", "px": "105"}}}
    assert not list(completed_waves(trade))
    trade["fill_events"]["b"] = {"oid": 2, "sz": "0.5", "px": "101"}
    assert not list(completed_waves(trade))
    trade["fill_events"]["c"] = {"oid": 2, "sz": "0.5", "px": "101"}
    assert len(list(completed_waves(trade))) == 1
    changed = copy.deepcopy(trade)
    changed["order_observations"]["buy"]["status"] = "open"
    assert not list(completed_waves(changed))


def test_same_outage_can_alert_again_after_a_confirmed_recovery():
    reports = [{"account_key": "hyperliquid:testnet", "code": "reconciliation_unavailable"}]
    incident = digest(["hyperliquid:testnet"])
    value = data({"again": {"recovery_reports": reports, "observed_at": 200000}})
    value["notification_outbox"] = {"old": {"status": "sent", "event_keys": ["incident:" + incident + ":1000"]}}
    value["notice_policy_state"] = {"announced_incidents": {incident: False}}
    messages, _, _ = select(value, covered=set(), stamp=320001)
    assert len(messages) == 1 and messages[0]["event_keys"] == ["incident:" + incident + ":200000"]


def test_late_complete_funding_edits_original_close_without_a_second_message(setup):
    from test_monthly import trade

    _, state, fake, *_ = setup
    complete = trade()
    partial = copy.deepcopy(complete)
    partial["funding_coverage"] = {"complete": False}
    def insert(value):
        value["trades"][partial["trade_id"]] = partial
        value["notifications"]["close"] = {"trade_id": partial["trade_id"], "observed_at": 0, "lifecycle": {"kind": "closed"}}
    state.update(insert)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("one")
    first = outbox.next_part("one")
    assert "unvollständig" in first["text"]
    outbox.complete("one", first["delivery_id"], first["part"], True, "1234")
    state.update(lambda value: value["trades"][partial["trade_id"]].update(funding_coverage=complete["funding_coverage"]))
    correction = outbox.next_part("one")
    assert correction["edit_message_id"] == "1234" and "Netto:" in correction["text"] and "Funding:" in correction["text"]
    outbox.complete("one", correction["delivery_id"], correction["part"], True, "1234")
    assert outbox.next_part("one")["delivery_id"] is None and not fake.actions
