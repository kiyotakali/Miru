"""Unit tests for persona_writer_state — accumulated trigger + truncation."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import persona_writer_state as pws  # noqa: E402
import memory  # noqa: E402


def _setup_tmp(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="persona_state_")
    monkeypatch.setattr(memory, "_memory_dir", lambda: tmp, raising=True)
    return tmp


# ─────────────────────────────────────────────────────────────────────
# Basic load / save / empty
# ─────────────────────────────────────────────────────────────────────

def test_load_empty_when_no_file(monkeypatch):
    _setup_tmp(monkeypatch)
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 0
    assert meta["pending_batch_dialogs"] == []
    assert meta["pending_new_slots"] == []
    assert meta["pending_proactive_outcomes"] == []
    assert meta["last_run_ts"] == ""


def test_save_then_load_roundtrip(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.save_meta({
        "batches_since_last_run": 2,
        "pending_batch_dialogs": [[{"role": "user", "text": "hi"}]],
        "pending_new_slots": [{"domain": "project", "slot_id": "x", "kind": "new", "summary": "y"}],
        "last_run_ts": "2026-05-13T10:00:00",
    })
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 2
    assert len(meta["pending_batch_dialogs"]) == 1
    assert meta["pending_new_slots"][0]["slot_id"] == "x"
    assert meta["pending_proactive_outcomes"] == []


def test_load_corrupt_file_returns_empty(monkeypatch):
    tmp = _setup_tmp(monkeypatch)
    path = os.path.join(tmp, "_slots", "persona_writer_meta.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 0


# ─────────────────────────────────────────────────────────────────────
# record_batch
# ─────────────────────────────────────────────────────────────────────

def test_record_batch_increments_counter(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.record_batch(
        batch=[{"role": "user", "text": "msg1", "time": "2026-05-13T10:00"}],
        slot_writes=[],
    )
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 1
    assert len(meta["pending_batch_dialogs"]) == 1

    pws.record_batch(batch=[], slot_writes=[])
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 2


def test_record_batch_appends_slot_writes(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.record_batch(
        batch=[],
        slot_writes=[
            {"domain": "project", "slot_id": "p1", "kind": "match", "summary": "s1"},
            {"domain": "person", "slot_id": "u1", "kind": "new", "summary": "s2"},
        ],
    )
    meta = pws.load_meta()
    assert len(meta["pending_new_slots"]) == 2
    assert meta["pending_new_slots"][1]["kind"] == "new"


# ─────────────────────────────────────────────────────────────────────
# Truncation
# ─────────────────────────────────────────────────────────────────────

def test_truncate_batch_long_batch_keeps_head_tail(monkeypatch):
    """A batch with > 30 messages is reduced to first 10 + marker + last 10."""
    _setup_tmp(monkeypatch)
    big_batch = [
        {"role": "user", "text": f"msg{i}", "time": "t"} for i in range(50)
    ]
    pws.record_batch(batch=big_batch, slot_writes=[])
    meta = pws.load_meta()
    stored = meta["pending_batch_dialogs"][0]
    assert len(stored) == 21  # 10 + 1 marker + 10
    assert stored[0]["text"] == "msg0"
    assert stored[-1]["text"] == "msg49"
    assert "省略中间" in stored[10]["text"]


def test_truncate_batch_short_batch_kept_as_is(monkeypatch):
    _setup_tmp(monkeypatch)
    small_batch = [{"role": "user", "text": "msg", "time": "t"}]
    pws.record_batch(batch=small_batch, slot_writes=[])
    meta = pws.load_meta()
    assert meta["pending_batch_dialogs"][0] == small_batch


def test_enforce_char_cap_drops_oldest(monkeypatch):
    _setup_tmp(monkeypatch)
    # Push many big batches until char cap forces drops
    big_msg = "x" * 1000
    for i in range(20):
        pws.record_batch(
            batch=[{"role": "user", "text": big_msg, "time": "t"}],
            slot_writes=[],
        )
    meta = pws.load_meta()
    # Should not exceed MAX_DIALOG_TOTAL_CHARS by much
    total = sum(len(json.dumps(b, ensure_ascii=False))
                for b in meta["pending_batch_dialogs"])
    assert total <= pws.MAX_DIALOG_TOTAL_CHARS + 1000  # tolerate 1 batch overflow


def test_trim_new_slots_caps_to_20(monkeypatch):
    _setup_tmp(monkeypatch)
    slots = []
    for i in range(30):
        kind = "new" if i % 2 == 0 else "match"
        slots.append({"domain": "project", "slot_id": f"s{i}",
                      "kind": kind, "summary": "x"})
    pws.record_batch(batch=[], slot_writes=slots)
    meta = pws.load_meta()
    assert len(meta["pending_new_slots"]) == pws.MAX_PENDING_NEW_SLOTS


def test_trim_new_slots_prefers_new_kind(monkeypatch):
    """When trimming, prefer to keep kind=new over kind=match."""
    _setup_tmp(monkeypatch)
    slots = []
    # 30 entries: first 25 are 'match', last 5 are 'new'
    for i in range(25):
        slots.append({"domain": "project", "slot_id": f"m{i}",
                      "kind": "match", "summary": "x"})
    for i in range(5):
        slots.append({"domain": "project", "slot_id": f"n{i}",
                      "kind": "new", "summary": "x"})
    pws.record_batch(batch=[], slot_writes=slots)
    meta = pws.load_meta()
    kept = meta["pending_new_slots"]
    # All 5 'new' should be kept
    kept_new_ids = [s["slot_id"] for s in kept if s["kind"] == "new"]
    assert len(kept_new_ids) == 5
    assert "n0" in kept_new_ids


# ─────────────────────────────────────────────────────────────────────
# should_run
# ─────────────────────────────────────────────────────────────────────

def test_should_run_false_under_threshold(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.save_meta({**pws._empty_meta(), "batches_since_last_run": 2})
    assert pws.should_run() is False


def test_should_run_true_at_threshold(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.save_meta({**pws._empty_meta(),
                   "batches_since_last_run": pws.PERSONA_TRIGGER_BATCHES})
    assert pws.should_run() is True


def test_should_run_true_above_threshold(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.save_meta({**pws._empty_meta(),
                   "batches_since_last_run": pws.PERSONA_TRIGGER_BATCHES + 5})
    assert pws.should_run() is True


# ─────────────────────────────────────────────────────────────────────
# reset_after_run
# ─────────────────────────────────────────────────────────────────────

def test_reset_after_run_clears_pending_on_success(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.record_batch(
        batch=[{"role": "user", "text": "msg", "time": "t"}],
        slot_writes=[{"domain": "project", "slot_id": "x",
                      "kind": "new", "summary": "y"}],
    )
    pws.reset_after_run(success=True)
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 0
    assert meta["pending_batch_dialogs"] == []
    assert meta["pending_new_slots"] == []
    assert meta["pending_proactive_outcomes"] == []
    assert meta["last_run_ts"]  # timestamp set


def test_reset_after_run_clears_pending_on_failure(monkeypatch):
    """Failure should ALSO clear pending — option X: don't retry bad data."""
    _setup_tmp(monkeypatch)
    pws.record_batch(batch=[], slot_writes=[])
    pws.record_batch(batch=[], slot_writes=[])
    pws.reset_after_run(success=False)
    meta = pws.load_meta()
    assert meta["batches_since_last_run"] == 0
    assert meta["pending_batch_dialogs"] == []


def test_record_proactive_outcome_dedupes_and_renders(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.record_proactive_outcome({
        "id": "proactive_response:p1:u1",
        "kind": "proactive_response",
        "time": "2026-05-18 21:05:00",
        "proactive_message_id": "p1",
        "user_message_id": "u1",
        "topic_key": "warm_presence",
        "proactive_text": "我在这儿。",
        "user_reply": "嗯，这样挺好。",
        "why_i_want_to_say": "我不想让他觉得我冷掉。",
        "response_delay_seconds": 120,
    })
    # Same id updates, does not duplicate.
    pws.record_proactive_outcome({
        "id": "proactive_response:p1:u1",
        "kind": "proactive_response",
        "time": "2026-05-18 21:05:00",
        "proactive_message_id": "p1",
        "user_message_id": "u1",
        "topic_key": "warm_presence",
        "proactive_text": "我在这儿。",
        "user_reply": "嗯，这样真的挺好。",
        "why_i_want_to_say": "我不想让他觉得我冷掉。",
        "response_delay_seconds": 120,
    })

    meta = pws.load_meta()
    assert len(meta["pending_proactive_outcomes"]) == 1
    out = pws.concat_proactive_outcomes_for_llm(meta)
    assert "proactive_response" in out
    assert "真的挺好" in out
    assert "我不想让他觉得我冷掉" in out


def test_should_run_true_after_three_proactive_responses(monkeypatch):
    _setup_tmp(monkeypatch)
    for i in range(3):
        pws.record_proactive_outcome({
            "id": f"proactive_response:p{i}:u{i}",
            "kind": "proactive_response",
            "time": "2026-05-18 21:05:00",
            "proactive_message_id": f"p{i}",
            "user_message_id": f"u{i}",
            "user_reply": "嗯",
        })
    assert pws.should_run() is True


# ─────────────────────────────────────────────────────────────────────
# concat_dialogs_for_llm
# ─────────────────────────────────────────────────────────────────────

def test_concat_dialogs_empty(monkeypatch):
    _setup_tmp(monkeypatch)
    assert pws.concat_dialogs_for_llm() == "(无累计对话)"


def test_concat_dialogs_renders_batches(monkeypatch):
    _setup_tmp(monkeypatch)
    pws.record_batch(
        batch=[
            {"role": "user", "text": "今天调 bug", "time": "2026-05-13T10:00:00"},
            {"role": "assistant", "text": "好辛苦", "time": "2026-05-13T10:01:00"},
        ],
        slot_writes=[],
    )
    pws.record_batch(
        batch=[
            {"role": "user", "text": "终于修好了", "time": "2026-05-13T11:00:00"},
        ],
        slot_writes=[],
    )
    out = pws.concat_dialogs_for_llm()
    assert "batch 1" in out
    assert "batch 2" in out
    assert "今天调 bug" in out
    assert "终于修好了" in out


if __name__ == "__main__":
    import unittest
    unittest.main()
