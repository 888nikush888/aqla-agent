"""Own entry and exit evidence independently gate account recovery."""
from types import SimpleNamespace

import pytest

from aqla_agent.router import Router


@pytest.mark.parametrize("pending", ["quantity_evidence_pending", "exit_quantity_evidence_pending"])
def test_account_recovery_waits_for_each_own_quantity_proof(pending):
    trade = {"status": "open", pending: True}
    data = {"operations": {}, "trades": {"trade": trade}}
    engine = SimpleNamespace(state=SimpleNamespace(read=lambda *_args: data), recovery_checked=True)

    assert Router.account_recovery_category(engine, []) == "incomplete"
    trade[pending] = False
    assert Router.account_recovery_category(engine, []) == "complete"


def test_resolved_exit_evidence_does_not_clear_an_unproved_entry():
    data = {"operations": {}, "trades": {"trade": {
        "status": "open", "quantity_evidence_pending": True, "exit_quantity_evidence_pending": False}}}
    engine = SimpleNamespace(state=SimpleNamespace(read=lambda *_args: data), recovery_checked=True)

    assert Router.account_recovery_category(engine, []) == "incomplete"
