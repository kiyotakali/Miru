"""Attention proactive message id uniqueness after CareEngine retirement."""

import os
import re
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_attention_proactive_msg_id_has_hash_suffix(tmp_path, monkeypatch):
    import core
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    with patch("storage.set_last_proactive_time"), \
         patch("storage.add_pending_notification"), \
         patch("sse.broadcast"), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        msg = core._append_attention_proactive_message(
            "我在这儿。",
            {"id": "intent_one", "topic_key": "warm"},
            {"model_mode": "v4_pro", "tool_policy": "allow_readonly"},
        )

    assert re.match(r"^attention_proactive_\d{8}_\d{6}_[0-9a-f]{6}$", msg["id"])
    history = storage.get_chat_history(limit=5)
    assert history[0]["id"] == msg["id"]


def test_attention_proactive_same_second_ids_differ(tmp_path, monkeypatch):
    import core
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    with patch("storage.set_last_proactive_time"), \
         patch("storage.add_pending_notification"), \
         patch("sse.broadcast"), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        msg_a = core._append_attention_proactive_message(
            "第一句",
            {"id": "intent_a", "topic_key": "warm"},
            {"model_mode": "v4_pro", "tool_policy": "allow_readonly"},
        )
        msg_b = core._append_attention_proactive_message(
            "第二句",
            {"id": "intent_b", "topic_key": "warm"},
            {"model_mode": "v4_pro", "tool_policy": "allow_readonly"},
        )

    assert msg_a["id"] != msg_b["id"]
    assert msg_a["source"] == "attention_engine"
    assert msg_b["type"] == "proactive"
