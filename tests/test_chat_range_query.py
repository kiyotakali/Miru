"""Tests for storage.get_chat_messages_in_range — used by sleep_inference
to pull ALL messages in a date range regardless of total chat_history
size (no more recency-limit truncation)."""
import importlib
import json
import os


def _setup(tmp_path):
    data_dir = str(tmp_path / "data")
    os.environ["DATA_DIR"] = data_dir
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)
    import storage
    importlib.reload(storage)
    return storage


def _write_history(storage, messages):
    path = storage.chat_history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(messages, f)


def _write_archive(storage, month_key, messages):
    from storage import _archive_dir
    os.makedirs(_archive_dir(), exist_ok=True)
    with open(os.path.join(_archive_dir(), f"chat_history_{month_key}.json"), "w") as f:
        json.dump(messages, f)


def test_returns_messages_in_range(tmp_path):
    storage = _setup(tmp_path)
    _write_history(storage, [
        {"role": "user", "text": "old", "time": "2026-04-05 10:00:00"},
        {"role": "user", "text": "mon", "time": "2026-04-06 08:00:00"},
        {"role": "user", "text": "wed", "time": "2026-04-08 14:00:00"},
        {"role": "user", "text": "sun", "time": "2026-04-12 22:00:00"},
        {"role": "user", "text": "future", "time": "2026-04-13 09:00:00"},
    ])
    result = storage.get_chat_messages_in_range("2026-04-06", "2026-04-12")
    assert [m["text"] for m in result] == ["mon", "wed", "sun"]


def test_not_limited_by_count(tmp_path):
    """Big-history case — must return all in-range messages even if they
    would be beyond a recency-based limit."""
    storage = _setup(tmp_path)
    # Seed 3000 messages where the first 100 are in our target week and the
    # remaining 2900 are outside the range (later dates).
    msgs = []
    for i in range(100):
        msgs.append({
            "role": "user", "text": f"in{i}",
            "time": f"2026-04-06 {i // 60:02d}:{i % 60:02d}:00" if i < 60
                    else f"2026-04-07 {(i - 60) // 60:02d}:{(i - 60) % 60:02d}:00",
        })
    for i in range(2900):
        msgs.append({
            "role": "user", "text": f"out{i}",
            "time": "2026-04-20 12:00:00",
        })
    _write_history(storage, msgs)
    result = storage.get_chat_messages_in_range("2026-04-06", "2026-04-07")
    # All 100 in-range messages must be present (not truncated to 500 or any limit)
    assert len(result) == 100
    assert all(m["text"].startswith("in") for m in result)


def test_merges_archive(tmp_path):
    storage = _setup(tmp_path)
    _write_history(storage, [
        {"role": "user", "text": "current", "time": "2026-04-10 09:00:00"},
    ])
    _write_archive(storage, "2026-04", [
        {"role": "user", "text": "archived", "time": "2026-04-08 10:00:00"},
    ])
    result = storage.get_chat_messages_in_range("2026-04-06", "2026-04-12")
    texts = [m["text"] for m in result]
    assert "archived" in texts and "current" in texts


def test_dedup_across_archive(tmp_path):
    """If a message exists in both live history and archive (rare edge case),
    it should only appear once."""
    storage = _setup(tmp_path)
    shared = {"role": "user", "text": "dup", "time": "2026-04-08 10:00:00"}
    _write_history(storage, [shared])
    _write_archive(storage, "2026-04", [shared])
    result = storage.get_chat_messages_in_range("2026-04-06", "2026-04-12")
    assert len(result) == 1
