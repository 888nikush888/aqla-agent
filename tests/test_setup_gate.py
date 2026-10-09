import pytest

from aqla_agent.setup import SetupBinding, SetupGate
from aqla_hyperliquid_mcp.models import AqlaError


def gate_fixture():
    now = [1000.0]
    gate = SetupGate(True, clock=lambda: now[0])
    binding = SetupBinding("12345", "12345", "synthetic-gateway", "synthetic-session")
    generation = gate.bind(binding)
    identity = "a" * 64
    gate.register(binding, identity, 1300, generation)
    return gate, binding, identity, now


def test_fixed_monotonic_lease_does_not_renew_on_reads_or_clock_reversal():
    gate, binding, identity, now = gate_fixture()
    assert gate.status(binding, identity)["remaining_seconds"] == 300
    now[0] += 299
    gate.require(identity, binding)
    assert gate.status(binding, identity)["remaining_seconds"] == 1
    now[0] -= 50
    assert gate.status(binding, identity)["remaining_seconds"] == 1
    now[0] = 1300
    with pytest.raises(AqlaError, match="Setup grant"):
        gate.require(identity, binding)


def test_no_hash_direct_mcp_forged_binding_and_restart_never_grant():
    gate, binding, identity, _ = gate_fixture()
    for other in (None, SetupBinding("999", "999", binding.gateway_id, binding.session_id),
                  SetupBinding(binding.owner_id, binding.chat_id, "other", binding.session_id)):
        with pytest.raises(AqlaError):
            gate.require(identity, other)
    restarted = SetupGate(True, clock=lambda: 1000)
    restarted.bind(binding)
    with pytest.raises(AqlaError):
        restarted.register(binding, identity, 1300, gate.generation)
    unavailable = SetupGate(False, clock=lambda: 1000)
    generation = unavailable.bind(binding)
    with pytest.raises(AqlaError):
        unavailable.register(binding, identity, 1300, generation)


def test_session_rebind_revoke_and_replayed_extension_are_rejected():
    gate, binding, identity, now = gate_fixture()
    now[0] = 1100
    with pytest.raises(AqlaError, match="renewed"):
        gate.register(binding, identity, 1301, gate.generation)
    changed = SetupBinding(binding.owner_id, binding.chat_id, binding.gateway_id, "next-session")
    gate.bind(changed)
    with pytest.raises(AqlaError):
        gate.require(identity, binding)
    gate.revoke(binding.owner_id)
    assert not gate.status(binding, identity)["active"]
    gate.bind(binding)
    with pytest.raises(AqlaError, match="renewed"):
        gate.register(binding, identity, 1300, gate.generation)


@pytest.mark.asyncio
async def test_queue_or_await_expiry_is_checked_at_the_actual_mutation():
    gate, binding, identity, now = gate_fixture()
    gate.require(identity, binding)
    now[0] = 1300
    writes = []
    with pytest.raises(AqlaError):
        gate.require(identity, binding)
        writes.append("critical mutation")
    assert writes == []
