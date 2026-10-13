import pytest

from aqla_agent.delivery import NotificationOutbox, text_chunks
from aqla_hyperliquid_mcp.models import AqlaError


def test_unicode_chunks_preserve_every_character():
    text = "📈" * 2500 + "\n**result**"
    chunks = text_chunks(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3000 for chunk in chunks)


def notice(state, identity="n", error="Trade check failed"):
    state.update_metadata(("notifications",), lambda data: data["notifications"].__setitem__(identity, {"error": error}))


def test_delivery_is_frozen_versioned_and_successful_parts_survive_restart(setup):
    _, state, fake, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    part = outbox.next_part("first")
    assert part["delivery_id"] and "Trade check failed" in part["text"]
    notice(state, error="A NEW failure")
    outbox.complete("first", part["delivery_id"], part["part"], True)
    assert state.snapshot()["notifications"]["n"]["error"] == "A NEW failure"
    resumed = NotificationOutbox(state, "operator")
    resumed.bind("next")
    changed = resumed.next_part("next")
    assert changed["delivery_id"] != part["delivery_id"]
    assert "A NEW failure" in changed["text"]
    resumed.complete("next", changed["delivery_id"], changed["part"], True)
    assert resumed.next_part("next")["delivery_id"] is None
    assert state.snapshot()["notifications"] == {}
    assert fake.actions == []


def test_restart_during_send_marks_uncertainty_and_does_not_ack(setup):
    _, state, _, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    first = outbox.next_part("first")
    restarted = NotificationOutbox(state, "operator")
    restarted.bind("replacement")
    retry = restarted.next_part("replacement")
    assert retry["delivery_id"] == first["delivery_id"]
    assert "könnte bereits angekommen" in retry["text"]
    assert "n" in state.snapshot()["notifications"]
    with pytest.raises(AqlaError, match="owner changed"):
        restarted.complete("first", first["delivery_id"], first["part"], True)


def test_known_success_is_never_resent_and_failed_send_is_backed_off(setup):
    _, state, _, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    first = outbox.next_part("first")
    outbox.complete("first", first["delivery_id"], first["part"], False)
    assert outbox.next_part("first")["delivery_id"] is None
    assert "n" in state.snapshot()["notifications"]


def test_lost_completion_rpc_expires_send_lease_and_confirmation_is_idempotent(setup):
    _, state, _, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    first = outbox.next_part("first")
    state.update_metadata(("notification_outbox",), lambda data:
        data["notification_outbox"][first["delivery_id"]].update(send_lease_until=0))
    recovered = outbox.next_part("first")
    assert recovered["delivery_id"] == first["delivery_id"]
    assert "könnte bereits angekommen" in recovered["text"]
    outbox.complete("first", first["delivery_id"], first["part"], True)
    assert outbox.complete("first", first["delivery_id"], first["part"], True)["already_confirmed"]


def test_exhausted_jobs_have_explicit_health_and_manual_requeue(setup):
    _, state, _, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    first = outbox.next_part("first")
    state.update_metadata(("notification_outbox",), lambda data:
        data["notification_outbox"][first["delivery_id"]].update(attempts=5))
    outbox.complete("first", first["delivery_id"], first["part"], False)
    assert outbox.overview()["exhausted"] == 1
    assert outbox.next_part("first")["delivery_id"] is None
    assert outbox.retry("first")["requeued"] == 1
    assert outbox.next_part("first")["delivery_id"] == first["delivery_id"]


@pytest.mark.parametrize("restart", [False, True])
def test_fifth_lost_confirmation_is_exhausted_and_recoverable(setup, restart):
    _, state, _, _, _ = setup
    notice(state)
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    first = outbox.next_part("first")
    state.update_metadata(("notification_outbox",), lambda data:
        data["notification_outbox"][first["delivery_id"]].update(attempts=5, send_lease_until=0))
    if restart:
        outbox.bind("next")
    owner = "next" if restart else "first"
    assert outbox.next_part(owner)["delivery_id"] is None
    assert outbox.overview()["exhausted"] == 1
    assert outbox.retry(owner)["requeued"] == 1
    assert outbox.next_part(owner)["delivery_id"] == first["delivery_id"]


def test_fresh_notification_is_queued_past_one_hundred_exhausted_notices(setup):
    _, state, fake, _, _ = setup
    state.update_metadata(("notifications",), lambda data: data["notifications"].update({
        f"old-{index:03}": {"error": "old fact", "observed_at": index} for index in range(100)}))
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    outbox.prepare()
    state.update_metadata(("notification_outbox",), lambda data: [
        row.update(status="exhausted", attempts=5) for row in data["notification_outbox"].values()])
    before = state.read("notification_outbox")["notification_outbox"]
    notice(state, identity="fresh", error="new fact")
    part = outbox.next_part("first")
    assert part["delivery_id"] not in before and "new fact" in part["text"]
    current = state.read("notification_outbox")["notification_outbox"]
    assert all(current[identity] == row for identity, row in before.items())
    outbox.complete("first", part["delivery_id"], part["part"], True)
    assert "fresh" not in state.read("notifications")["notifications"]
    assert len(state.read("notifications")["notifications"]) == 100 and not fake.actions


def test_fresh_grouped_sibling_does_not_rewrite_exhausted_frozen_delivery(setup):
    _, state, fake, _, _ = setup
    state.update(lambda data: data["events"].__setitem__("event", {
        "event_id": "event", "source_id": "channel:1", "source": {"chat_id": "channel"},
        "interpretation": {"source_refs": ["channel:1"]}}))
    state.update_metadata(("notifications",), lambda data: data["notifications"].__setitem__(
        "old", {"event_id": "event", "error": "old fact"}))
    outbox = NotificationOutbox(state, "operator")
    outbox.bind("first")
    old = outbox.next_part("first")
    state.update_metadata(("notification_outbox",), lambda data: data["notification_outbox"][old["delivery_id"]].update(attempts=5))
    outbox.complete("first", old["delivery_id"], old["part"], False)
    frozen = state.read("notification_outbox")["notification_outbox"][old["delivery_id"]]
    state.update_metadata(("notifications",), lambda data: data["notifications"].__setitem__(
        "fresh", {"event_id": "event", "error": "new fact"}))
    fresh = outbox.next_part("first")
    assert fresh["delivery_id"] != old["delivery_id"]
    queued = state.read("notification_outbox")["notification_outbox"]
    assert queued[old["delivery_id"]] == frozen and set(queued[fresh["delivery_id"]]["versions"]) == {"fresh"}
    outbox.complete("first", fresh["delivery_id"], fresh["part"], True)
    assert set(state.read("notifications")["notifications"]) == {"old"} and not fake.actions
