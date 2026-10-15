import copy

import pytest
from oracles.ranking_ed233a2 import outcomes as original_outcomes
from test_monthly import data as monthly_data
from test_monthly import trade as monthly_trade

from aqla_agent import ranking
from aqla_hyperliquid_mcp.models import digest


class CountingMapping(dict):
    def __init__(self, values, *, maximum=None):
        super().__init__(values)
        self.scanned = 0
        self.maximum = maximum

    def values(self):
        for row in super().values():
            self.scanned += 1
            if self.maximum is not None:
                assert self.scanned <= self.maximum, "The ranking build repeatedly scans the complete mapping"
            yield row


def ranking_snapshot(*, aliased=False, ranked=False):
    rows = []
    for index in range(120):
        row = monthly_trade("filled-" + str(index))
        row["plan"].update(source_refs=["ref-" + str(index)], stop_loss="90")
        if index % 2:
            row["plan"]["setup_id"] = "family-" + str(index)
        if ranked:
            row["plan"]["rank_snapshot"] = {"source_id": "rank-source", "policy_revision": 7, "source_revision": 3, "network": "mainnet"}
        row["initial_sl_risk"] = {"amount": "22"}
        if index % 11 == 0:
            row.pop("funding_coverage")
        if index % 13 == 0:
            row["external_conflict"] = "Independent foreign position evidence"
        rows.append(row)
        if index % 10 == 0:
            second = copy.deepcopy(row)
            second["trade_id"] = "second-" + str(index)
            rows.append(second)
        if index % 7 == 0:
            pending = copy.deepcopy(row)
            pending.update(trade_id="unfilled-" + str(index), fill_events={}, filled_quantity="0", status="waiting_entry")
            rows.append(pending)
        if index % 9 == 0:
            missing = copy.deepcopy(row)
            missing.update(trade_id="missing-fills-" + str(index), fill_events={}, filled_quantity="2", status="open")
            missing["plan"]["setup_id"] = "independent-" + str(index)
            rows.append(missing)
    data = monthly_data(*rows)
    data["source_aliases"] = {"-1001": "alias-source"} if aliased else {}
    data["events"] = {"event-" + str(index): {"source_id": "ref-" + str(index), "targets": {
        "one": {"status": "planned" if index % 3 == 0 else "rejected"},
        "two": {"status": "uncertain" if index % 17 == 0 else "closed"}}} for index in range(120)}
    data["events"]["foreign"] = {"source_id": "foreign-ref", "targets": {"other": {"status": "pending"}}}
    return data


@pytest.mark.parametrize("aliased,ranked", [(False, False), (True, False), (False, True), (True, True)])
def test_indexed_outcomes_match_full_legacy_multileg_pending_foreign_oracle(aliased, ranked):
    snapshot = ranking_snapshot(aliased=aliased, ranked=ranked)
    before = copy.deepcopy(snapshot)
    assert ranking.outcomes(snapshot) == original_outcomes(snapshot)
    assert snapshot == before
    # Later positive funding evidence must produce the same corrections and order.
    for row in snapshot["trades"].values():
        if row.get("fill_events") and "funding_coverage" not in row:
            row["funding_events"] = {"late": {"amount": "-.25"}}
            row["funding_coverage"] = {"start": row["entry_accounting"]["entry_at"], "end": row["closed_at"],
                "complete": True, "fill_fingerprint": digest(row["fill_events"]), "payments_fingerprint": digest(row["funding_events"])}
    assert ranking.outcomes(snapshot) == original_outcomes(snapshot)


def test_five_thousand_families_build_indexes_with_linear_mapping_scans():
    count = 5000
    trades = {str(index): {"trade_id": str(index), "chat_id": "source", "status": "open", "filled_quantity": "1",
        "fill_events": {"entry": {"kind": "entry"}}, "plan": {"source_refs": ["ref-" + str(index)]}} for index in range(count)}
    events = {str(index): {"source_id": "ref-" + str(index), "targets": {"leg": {"status": "planned"}}} for index in range(count)}
    snapshot = {"trades": CountingMapping(trades, maximum=2 * count), "events": CountingMapping(events, maximum=2 * count)}
    valid, excluded = ranking.outcomes(snapshot)
    assert valid == {} and len(excluded["source"]) == count
    assert snapshot["trades"].scanned <= 2 * count and snapshot["events"].scanned <= 2 * count


def test_full_proof_history_window_and_rank_bindings_match_original_evaluation(monkeypatch):
    snapshot = ranking_snapshot(aliased=True)
    snapshot["events"] = {}
    snapshot["trades"] = {key: row for key, row in snapshot["trades"].items() if key.startswith("filled-")}
    snapshot.update(ranking_policy=ranking.RankingPolicy(enabled=True).model_dump(mode="json"), ranking_revision=7,
        source_ranks={"alias-source": {"tier": "F", "bootstrap": False, "revision": 3, "evaluated": [], "up": None, "down": 0}}, ranking_history={})
    actual, expected = copy.deepcopy(snapshot), copy.deepcopy(snapshot)
    indexed = ranking.outcomes
    monkeypatch.setattr(ranking, "now_ms", lambda: 123456)
    monkeypatch.setattr(ranking, "outcomes", original_outcomes)
    ranking.evaluate(expected)
    monkeypatch.setattr(ranking, "outcomes", indexed)
    ranking.evaluate(actual)
    assert actual == expected
    rank = actual["source_ranks"]["alias-source"]
    assert len(rank["evidence"]) > 50 and rank["metrics"]["count"] == 50
    assert len(rank["evaluated"]) == len(rank["evidence"])
    assert actual["ranking_revision"] == 7


@pytest.mark.parametrize("aliased,ranked", [(False, False), (True, False), (False, True), (True, True)])
def test_selected_families_match_filtered_frozen_oracle_with_all_pending_siblings(aliased, ranked):
    snapshot = ranking_snapshot(aliased=aliased, ranked=ranked)
    selected = {"filled-0", "filled-10", "filled-11", "filled-14", "filled-17", "filled-23", "missing-fills-27"}
    before = copy.deepcopy(snapshot)
    expected = tuple({source: retained for source, rows in section.items()
                      if (retained := [row for row in rows if selected.intersection(row["trade_ids"])])}
                     for section in original_outcomes(snapshot))
    assert ranking.outcomes(snapshot, selected_ids=selected) == expected
    assert snapshot == before
