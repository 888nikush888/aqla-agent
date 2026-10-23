"""Transient receipt backfill failures retain previously proved funding history."""
import copy
from types import SimpleNamespace

import pytest
from test_monthly import data, trade

from aqla_agent.evidence import verified_coverage_matches
from aqla_agent.reporting import Reporting


@pytest.mark.asyncio
async def test_verified_coverage_survives_failed_legacy_entry_backfill(monkeypatch):
    row = trade()
    row.pop("entry_fill_quantity_confirmed")
    snapshot = data(row)
    before = copy.deepcopy(row["funding_coverage"])
    state = SimpleNamespace(snapshot=lambda: copy.deepcopy(snapshot), update=lambda callback: callback(snapshot))
    reporting = Reporting(state, SimpleNamespace(engines={"hyperliquid": object()}))

    async def unavailable(*_args):
        raise TimeoutError("Synthetic historical receipt read failure")

    monkeypatch.setattr(reporting, "hydrate", unavailable)
    result = await reporting.refresh(row["trade_id"])

    assert result == {"complete": False, "reason": "history_unavailable"}
    assert row["funding_coverage"] == before
    assert verified_coverage_matches(row)
    assert row["reporting_last_error"] == "Historische Entry-/Exit-Belege nicht vollständig verfügbar"
