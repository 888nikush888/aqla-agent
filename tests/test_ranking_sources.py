import copy
import json

import pytest
from test_ranking_and_roi import rank_data

from aqla_agent.journal import read_page
from aqla_agent.ranking import active_sources, set_manual_rank, status_view
from aqla_agent.sources import change_sources
from aqla_hyperliquid_mcp.models import AqlaError
from aqla_hyperliquid_mcp.server import close_server, create_server


def selection(data):
    template = rank_data("C")["source_ranks"]["channel"]
    data.update(sources=["@current"], source_aliases={"@current": "-101", "@retired": "-202"},
                source_names={"-101": "Current", "-202": "Retired"},
                source_ranks={"-101": copy.deepcopy(template), "-202": copy.deepcopy(template), "-303": copy.deepcopy(template)},
                ranking_history={str(index): {"source_id": identity, "time": index, "old": "F", "new": "C",
                                             "metrics": {}, "network": "mainnet", "revision": 1}
                                 for index, identity in enumerate(("-101", "-202", "-303"))})


def test_current_rank_view_resolves_only_selected_aliases_without_mutating_history():
    data = rank_data()
    selection(data)
    data["source_ranks"]["@current"] = {**data["source_ranks"]["-101"], "tier": "S"}
    before = copy.deepcopy(data)
    assert active_sources(data) == {"-101"}
    current = status_view(data)
    assert set(current) == {"-101"} and current["-101"]["tier"] == "C"
    history = status_view(data, include_inactive=True)
    assert set(history) == {"-101", "-202", "-303"}
    assert history["-101"]["active"] is True and history["-202"]["active"] is False
    assert data == before


@pytest.mark.parametrize("action", ["remove_sources", "set_sources"])
def test_source_removal_retires_current_rank_but_keeps_evidence_and_history(action):
    data = rank_data()
    selection(data)
    data["configuration_revision"] = 7
    data["source_ranks"]["-101"]["evidence"] = [{"setup_id": "retained", "net_r": "1", "closed_at": 1}]
    saved = copy.deepcopy(data)
    change_sources(data, action, ["-101"] if action == "remove_sources" else [], 7)
    assert status_view(data) == {} and data["sources"] == []
    assert data["source_ranks"] == saved["source_ranks"] and data["ranking_history"] == saved["ranking_history"]
    assert status_view(data, include_inactive=True)["-101"]["evidence"] == saved["source_ranks"]["-101"]["evidence"]


@pytest.mark.parametrize("selector", ["-202", "@retired", "Retired", "-303"])
def test_inactive_rank_cannot_be_changed_or_reactivated_through_stale_alias(selector):
    data = rank_data()
    selection(data)
    before = copy.deepcopy(data)
    with pytest.raises(AqlaError, match="configured active source"):
        set_manual_rank(data, selector, "S", 1, "12345")
    assert data == before


def payload(result):
    return result.structured_content or json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_mcp_defaults_to_current_sources_and_reads_inactive_history_without_setup(setup):
    settings, state, fake, exchange, _ = setup
    settings.telegram_owner_id = "12345"
    state.update(selection)
    server = create_server(settings, state=state, exchange=exchange)
    before = state.snapshot()
    try:
        result = payload(await server.call_tool("ranking_status", {}))
        assert set(result["sources"]) == {"-101"} and result["inactive_sources"] == {}
        assert [row["source_id"] for row in result["history"]] == ["-101"]
        history = payload(await server.call_tool("ranking_status", {"source": "Retired"}))
        assert history["sources"] == {} and set(history["inactive_sources"]) == {"-202"}
        assert history["inactive_sources"]["-202"]["active"] is False
        assert [row["source_id"] for row in history["history"]] == ["-202"]
        assert "Never re-add/remove" in history["inactive_source_policy"]
        token = server.aqla_authority.sign("operator", "read-only-native-owner")
        configuration = payload(await server.call_tool("status", {"view": "configuration", "context_auth": token}))
        assert set(configuration["source_ranks"]) == {"-101"}
        full = payload(await server.call_tool("status", {"context_auth": token}))
        assert set(full["source_ranks"]) == {"-101"}
        assert "Retired" not in "\n".join(full["telegram_view"]["status"])
        assert state.snapshot() == before and not fake.actions
        assert {row["ID"] for row in read_page(before, "Ränge")["rows"]} == {"-101", "-202", "-303"}
    finally:
        await close_server(server)
