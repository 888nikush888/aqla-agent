"""Native tool hooks share the synthetic Hermes context fixture."""
import pytest
from test_hermes_integration import extension  # noqa: F401 -- synthetic context

from aqla_agent.tool_runtime import GUIDANCE, singleton


@pytest.mark.parametrize("raw,expected", [("false", False), ("true", True), (False, False), (True, True)])
def test_monthly_pre_hook_normalizes_before_publication(extension, raw, expected):  # noqa: F811
    ext, ctx = extension
    calls = []
    ctx.call_mcp = lambda *args: calls.append(args) or {"ok": True}
    with ext.claim_scope(("operator", "fresh")):
        result = ext.before_tool("mcp__aqla__monthly_report", {"month": "2026-10", "diagnostics_only": raw})
        assert not calls  # Local monthly reads no longer request an external publication.
        if isinstance(raw, str):
            assert result["args"]["diagnostics_only"] is expected


@pytest.mark.parametrize("raw", [None, "", "false-ish", [], {}, 0, 1])
def test_monthly_invalid_diagnostic_flag_cannot_publish(extension, raw):  # noqa: F811
    ext, ctx = extension
    calls = []
    ctx.call_mcp = lambda *args: calls.append(args) or {"ok": True}
    with ext.claim_scope(("operator", "fresh")):
        result = ext.before_tool("mcp__aqla__monthly_report", {"month": "2026-10", "diagnostics_only": raw})
        assert result["action"] == "block" and calls == []


@pytest.mark.parametrize("raw", [{"name": "skill_view", "arguments": {"name": "aqla-master"}},
    {"calls": [{"name": "skill_view", "arguments": {"name": "aqla-master"}}]},
    {"calls": {"name": "skill_view", "arguments": '{"name":"aqla-master"}'}}])
def test_singleton_core_normalization_preserves_exact_arguments(raw):
    assert singleton(raw) == ("skill_view", {"name": "aqla-master"})


@pytest.mark.parametrize("raw", [{"name": "mcp__aqla__trade_execute", "arguments": {"broken": True}},
    {"name": "skill_manage", "arguments": {}}, {"name": "tool_call", "arguments": {}},
    {"calls": [{"name": "skills_list", "arguments": {}}, {"name": "skill_view", "arguments": {}}]},
    {"name": "skills_list", "arguments": None}, {"name": "read_file", "arguments": {"path": "anything"}}])
def test_mutations_nested_multi_entry_and_unavailable_calls_are_not_repaired(raw):
    assert singleton(raw) is None


def test_fixed_guidance_is_bounded_and_preserves_source_authority():
    assert len(GUIDANCE.encode()) < 600
    assert "untrusted" in GUIDANCE and "source_lookup" in GUIDANCE and "Europe/Berlin" in GUIDANCE
