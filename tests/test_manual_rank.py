import copy
from decimal import Decimal

import pytest
from test_ranking_and_roi import rank_data

from aqla_agent.ranking import evaluate, set_manual_rank
from aqla_hyperliquid_mcp.models import AqlaError


def evidence(values):
    return {"channel": [{"setup_id": f"synthetic-{index}", "net_r": str(value), "closed_at": index,
                          "trade_ids": [str(index)]} for index, value in enumerate(values)]}, {}


def test_manual_rank_preserves_history_seen_evidence_metrics_and_idempotent_repeat(monkeypatch):
    data = rank_data()
    rows = evidence([1] * 20)
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: copy.deepcopy(rows))
    data["source_names"] = {"channel": "Synthetic channel"}
    data["source_ranks"]["channel"].update(evaluated=["older-retained"], up={"tier": "E", "confirmations": 2}, down=1)
    result = set_manual_rank(data, "Synthetic channel", "C", 1, "12345")
    rank = result["rank"]
    assert rank["revision"] == 2 and rank["origin"] == "manual" and rank["bootstrap"] is False
    assert rank["up"] is None and rank["down"] == 0 and rank["evaluated"][0] == "older-retained"
    assert len(rank["evaluated"]) == 21 and rank["metrics"]["count"] == 20
    before = copy.deepcopy(data)
    assert set_manual_rank(data, "channel", "C", 1, "12345")["changed"] is False
    assert data == before
    assert evaluate(data) == [] and rank["tier"] == "C" and rank["up"] is None
    lowered = set_manual_rank(data, "channel", "F", 2, "12345")
    assert lowered["rank"]["revision"] == 3 and len(data["ranking_history"]) == 2
    assert evaluate(data) == [] and lowered["rank"]["tier"] == "F"


def test_mainnet_promotion_cannot_bypass_current_hard_drawdown(monkeypatch):
    data = rank_data()
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: evidence([2, -5]))
    before = copy.deepcopy(data)
    with pytest.raises(AqlaError, match="hard drawdown"):
        set_manual_rank(data, "channel", "C", 1, "12345")
    assert data == before
    result = set_manual_rank(data, "channel", "D", 1, "12345")
    assert Decimal(result["rank"]["metrics"]["drawdown_r"]) == 5


def test_concurrent_revision_and_unknown_source_rejected(monkeypatch):
    data = rank_data()
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: evidence([]))
    set_manual_rank(data, "channel", "C", 1, "12345")
    for source, revision in (("channel", 1), ("unknown", 1), ("channel", True)):
        with pytest.raises(AqlaError):
            set_manual_rank(data, source, "F", revision, "12345")


def test_new_evidence_and_late_corrections_evaluate_normally_after_manual_rank(monkeypatch):
    data = rank_data()
    current = [1] * 20
    monkeypatch.setattr("aqla_agent.ranking.outcomes", lambda _: evidence(current))
    set_manual_rank(data, "channel", "C", 1, "12345")
    current.append(1)
    evaluate(data)
    assert "synthetic-20" in data["source_ranks"]["channel"]["evaluated"]
    current[-1] = -10  # A correction to seen evidence still enforces drawdown.
    changes = evaluate(data)
    assert changes[0]["old"] == "C" and changes[0]["new"] == "D"
