import json

import pytest

from aqla_agent.conversation import Conversations


def test_native_transcript_survives_restart_and_keeps_input_identity(tmp_path):
    store = Conversations(tmp_path)
    user = {"role": "user", "content": "Risiko auf 0,5 Prozent", "message_id": "5", "display_metadata": {"gateway_input_owner": "native-owner"}}
    assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "control", "arguments": "{}"}}]}
    store.append("session_1", user)
    store.append("session_1", assistant)
    restored = Conversations(tmp_path)
    assert restored.load("session_1") == [user, assistant]
    assert restored.has_owner("session_1", "native-owner")
    assert restored.has_message("session_1", "5")
    assert not list(tmp_path.glob("*.db"))
    assert restored.rewrite("session_1", [user]) is True
    assert restored.load("session_1") == [user]


def test_corrupt_transcript_is_not_treated_as_an_empty_conversation(tmp_path):
    (tmp_path / "session_1.jsonl").write_text("{incomplete\n")
    with pytest.raises(json.JSONDecodeError):
        Conversations(tmp_path).load("session_1")


def test_session_identity_cannot_escape_the_native_transcript_directory(tmp_path):
    with pytest.raises(ValueError):
        Conversations(tmp_path).append("../../another-file", {"role": "user"})
