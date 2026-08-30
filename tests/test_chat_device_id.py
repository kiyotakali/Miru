"""Phase 2 regressions: device_id must flow through receive_chat_message →
chat_history persistence → SSE broadcast.

Without these guarantees the front-end SSE de-dup (line 7858 of index.html)
can't tell whether an incoming user-role message was sent from this same
device or another one, and either skips legitimate cross-device messages
or duplicates the local optimistic bubble.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_receive_chat_message_persists_device_id(tmp_path, monkeypatch):
    """user_msg written to chat_history.json must carry device_id."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import importlib
    import storage
    importlib.reload(storage)
    import core
    importlib.reload(core)

    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context():
        from flask import g
        g.user_id = "_admin"
        g.user_data_dir = str(tmp_path)
        # Fire and forget: receive_chat_message kicks off a background thread
        # for the LLM reply, which we don't care about here.
        with patch("care_engine.get_care_engine"), \
             patch("sleep_agent.get_sleep_agent"):
            core.receive_chat_message("hi", device_id="dev_abc123")

    # Read what landed on disk — last entry should be ours
    chat_path = os.path.join(tmp_path, "chat_history.json")
    assert os.path.exists(chat_path), "chat_history.json should be created"
    with open(chat_path) as f:
        history = json.load(f)
    user_msgs = [m for m in history if m.get("role") == "user"]
    assert user_msgs, "user message should be persisted"
    last = user_msgs[-1]
    assert last["text"] == "hi"
    assert last.get("device_id") == "dev_abc123", \
        f"device_id missing from persisted user message: {last}"


def test_receive_chat_message_broadcasts_with_device_id(tmp_path, monkeypatch):
    """SSE chat_message broadcast must include device_id so receivers can
    de-dup their own optimistically-rendered bubble."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import importlib
    import storage
    importlib.reload(storage)
    import core
    importlib.reload(core)
    import sse
    importlib.reload(sse)

    captured = []

    def _capture(event_type, data, *, user_id):
        captured.append({"event": event_type, "data": data, "user_id": user_id})

    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context():
        from flask import g
        g.user_id = "_admin"
        g.user_data_dir = str(tmp_path)
        with patch("sse.broadcast", side_effect=_capture), \
             patch("care_engine.get_care_engine"), \
             patch("sleep_agent.get_sleep_agent"):
            core.receive_chat_message("hello", device_id="dev_xyz789")

    chat_events = [c for c in captured if c["event"] == "chat_message"]
    assert chat_events, f"expected at least one chat_message broadcast, got {captured}"
    # The broadcast payload should be the user message dict, with device_id
    payload = chat_events[0]["data"]
    assert payload["role"] == "user"
    assert payload["text"] == "hello"
    assert payload.get("device_id") == "dev_xyz789", \
        f"device_id missing from broadcast: {payload}"


def test_receive_chat_message_no_device_id_omits_field(tmp_path, monkeypatch):
    """When device_id is empty/None, the field must be ABSENT (not stored as
    empty string), so SSE de-dup can rely on `if msg.device_id` truthiness."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    import importlib
    import storage
    importlib.reload(storage)
    import core
    importlib.reload(core)

    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context():
        from flask import g
        g.user_id = "_admin"
        g.user_data_dir = str(tmp_path)
        with patch("care_engine.get_care_engine"), \
             patch("sleep_agent.get_sleep_agent"):
            core.receive_chat_message("plain message", device_id="")

    with open(os.path.join(tmp_path, "chat_history.json")) as f:
        history = json.load(f)
    last = [m for m in history if m.get("role") == "user"][-1]
    assert "device_id" not in last, \
        f"empty device_id should be omitted, not stored: {last}"


if __name__ == "__main__":
    import tempfile
    import pytest
    pytest.main([__file__, "-v"])
