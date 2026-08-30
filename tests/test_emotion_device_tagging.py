"""Tests for #193-A: emotion entries are tagged with device_id.

Goal: verify that emotion log entries carry a ``device_id`` and
``source_type`` field so the UI can attribute per-device origin —
without hardcoding a dual-device split (must scale to N devices).
"""
import sys
import os
import json
import tempfile
import shutil
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import storage  # noqa: E402
import core  # noqa: E402


_ORIGINAL_DATA_DIR = storage.DATA_DIR


def _fresh_data_dir():
    d = tempfile.mkdtemp(prefix="emolog_")
    storage.DATA_DIR = d
    os.makedirs(os.path.join(d, "archive"), exist_ok=True)
    os.makedirs(os.path.join(d, "uploads"), exist_ok=True)
    return d


def _restore_data_dir():
    storage.DATA_DIR = _ORIGINAL_DATA_DIR
    # core._csm_set_thread_context pushes a Flask app context and sets
    # g.user_data_dir — which leaks into later tests that read flask.g.
    # Clear g attrs + reset the context var so per-test isolation holds.
    try:
        from flask import g, has_app_context
        from flask.globals import _cv_app
        if has_app_context():
            for attr in ("user_id", "user_data_dir", "is_admin"):
                try:
                    delattr(g, attr)
                except AttributeError:
                    pass
        # Pop the topmost app context(s) pushed by _csm_set_thread_context.
        while True:
            try:
                token = _cv_app.get(None)
            except LookupError:
                break
            if token is None:
                break
            try:
                token.pop()
            except Exception:
                break
    except Exception:
        pass


def test_chat_annotation_stamps_device_id():
    """_annotate_emotion_from_chat attaches device_id + source_type='chat'."""
    print("=" * 60)
    print("TEST: chat emotion annotation stamps device_id")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # Seed a recent user message so the pre-filter passes
        recent = [{"role": "user",
                   "text": "今天好难受，感觉什么都做不好",
                   "time": "2026-04-15 14:00:00"}]
        fake_result = {
            "timestamp": "2026-04-15T14:00:05",
            "mood": "sad",
            "intensity": 0.7,
            "valence": -0.5,
            "source_type": "chat",
        }
        with patch("storage.get_recent_user_messages", return_value=recent), \
             patch("storage.get_today_emotion_log", return_value=[]), \
             patch("prompt.call_emotion_annotation", return_value=dict(fake_result)), \
             patch("core._broadcast_emotion_update"):
            core._annotate_emotion_from_chat(
                user_id="_admin", user_data_dir=d, device_id="phone-xyz")

        log = storage._load_emotion_log()
        total += 1
        if "2026-04-15" in log and len(log["2026-04-15"]) == 1:
            print("  [PASS] emotion entry appended")
            passed += 1
        else:
            print(f"  [FAIL] expected 1 entry, got {log}")
            return passed, total

        entry = log["2026-04-15"][0]
        total += 1
        if entry.get("device_id") == "phone-xyz":
            print("  [PASS] device_id stamped onto entry")
            passed += 1
        else:
            print(f"  [FAIL] device_id missing, got {entry!r}")

        total += 1
        if entry.get("source_type") == "chat":
            print("  [PASS] source_type='chat' preserved")
            passed += 1
        else:
            print(f"  [FAIL] source_type wrong: {entry.get('source_type')!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Chat: {passed}/{total}\n")
    return passed, total


def test_screenshot_annotation_stamps_device_id():
    """_annotate_emotion_from_screenshot stamps device_id + source_type='auto_screenshot'."""
    print("=" * 60)
    print("TEST: screenshot emotion annotation stamps device_id")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        fake_result = {
            "timestamp": "2026-04-15T15:30:00",
            "mood": "focused",
            "intensity": 0.6,
            "valence": 0.3,
        }
        with patch("storage.get_recent_user_messages", return_value=[]), \
             patch("storage.get_today_emotion_log", return_value=[]), \
             patch("prompt.call_emotion_annotation_screenshot",
                   return_value=dict(fake_result)), \
             patch("core._broadcast_emotion_update"):
            core._annotate_emotion_from_screenshot(
                "user is editing code in VSCode",
                "2026-04-15 15:30:00",
                user_id="_admin", user_data_dir=d,
                device_id="macbook-local")

        log = storage._load_emotion_log()
        entry = log.get("2026-04-15", [{}])[0]

        total += 1
        if entry.get("device_id") == "macbook-local":
            print("  [PASS] device_id stamped")
            passed += 1
        else:
            print(f"  [FAIL] device_id missing: {entry!r}")

        total += 1
        if entry.get("source_type") == "auto_screenshot":
            print("  [PASS] source_type defaulted to 'auto_screenshot'")
            passed += 1
        else:
            print(f"  [FAIL] source_type wrong: {entry.get('source_type')}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Screenshot: {passed}/{total}\n")
    return passed, total


def test_n_device_aggregation():
    """Month aggregator preserves per-device counts (N-device safe)."""
    print("=" * 60)
    print("TEST: N devices aggregate independently in month summary")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # Seed entries from 4 different devices (not just 2 — proves N-scale)
        now_date = "2026-04-15"
        storage.append_emotion_log({
            "timestamp": f"{now_date}T09:00:00", "mood": "happy",
            "intensity": 0.5, "valence": 0.6,
            "source_type": "chat", "device_id": "phone-a",
        })
        storage.append_emotion_log({
            "timestamp": f"{now_date}T10:00:00", "mood": "happy",
            "intensity": 0.5, "valence": 0.5,
            "source_type": "chat", "device_id": "phone-b",
        })
        storage.append_emotion_log({
            "timestamp": f"{now_date}T11:00:00", "mood": "focused",
            "intensity": 0.7, "valence": 0.3,
            "source_type": "auto_screenshot", "device_id": "desktop-a",
        })
        storage.append_emotion_log({
            "timestamp": f"{now_date}T12:00:00", "mood": "focused",
            "intensity": 0.7, "valence": 0.2,
            "source_type": "auto_screenshot", "device_id": "desktop-b",
        })

        summary = storage.get_emotion_log_month("2026-04")
        day = summary.get(now_date, {})

        total += 1
        if day.get("count") == 4:
            print("  [PASS] 4 entries counted")
            passed += 1
        else:
            print(f"  [FAIL] count wrong: {day.get('count')}")

        total += 1
        devices = day.get("devices") or {}
        if (devices.get("phone-a") == 1 and devices.get("phone-b") == 1
                and devices.get("desktop-a") == 1
                and devices.get("desktop-b") == 1):
            print("  [PASS] per-device counts preserved (N=4 devices)")
            passed += 1
        else:
            print(f"  [FAIL] device breakdown wrong: {devices}")

        total += 1
        sources = day.get("sources") or {}
        if sources.get("chat") == 2 and sources.get("auto_screenshot") == 2:
            print("  [PASS] source split: 2 chat + 2 auto_screenshot")
            passed += 1
        else:
            print(f"  [FAIL] source split wrong: {sources}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  NDevice: {passed}/{total}\n")
    return passed, total


def test_device_id_empty_tolerated():
    """Empty device_id does not break entry or aggregation."""
    print("=" * 60)
    print("TEST: empty/missing device_id tolerated")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        fake_result = {
            "timestamp": "2026-04-15T16:00:00",
            "mood": "neutral", "intensity": 0.3, "valence": 0.0,
        }
        recent = [{"role": "user", "text": "嗯。今天工作做完了",
                   "time": "2026-04-15 15:59:00"}]
        with patch("storage.get_recent_user_messages", return_value=recent), \
             patch("storage.get_today_emotion_log", return_value=[]), \
             patch("prompt.call_emotion_annotation", return_value=dict(fake_result)), \
             patch("core._broadcast_emotion_update"):
            # device_id omitted → ""
            core._annotate_emotion_from_chat(
                user_id="_admin", user_data_dir=d, device_id="")

        log = storage._load_emotion_log()
        total += 1
        entry = log.get("2026-04-15", [{}])[0]
        if "device_id" not in entry or entry.get("device_id") == "":
            print("  [PASS] entry saved without device_id clutter")
            passed += 1
        else:
            print(f"  [FAIL] device_id unexpectedly set: {entry.get('device_id')}")

        # Aggregation should still work
        total += 1
        summary = storage.get_emotion_log_month("2026-04")
        day = summary.get("2026-04-15", {})
        if day.get("count") == 1 and day.get("devices") == {}:
            print("  [PASS] month summary tolerates missing device_id (empty devices map)")
            passed += 1
        else:
            print(f"  [FAIL] summary wrong: {day}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Empty: {passed}/{total}\n")
    return passed, total


def test_device_id_plumbed_into_pending_message():
    """receive_chat_message stashes _device_id so batch thread can use it."""
    print("=" * 60)
    print("TEST: receive_chat_message stores _device_id on pending message")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # Snapshot CSM state so other tests aren't polluted
        user_id = "_admin"
        core._csm_pending[user_id] = []
        core._csm_state[user_id] = "IDLE"

        # Freeze the background quiet timer so no generation fires
        with patch("core._csm_start_quiet_timer"):
            core.receive_chat_message("hi there",
                                      image=None, device_id="tablet-xyz")

        pending = core._csm_pending.get(user_id, [])
        total += 1
        if len(pending) == 1:
            print("  [PASS] one message queued")
            passed += 1
        else:
            print(f"  [FAIL] pending={pending}")
            return passed, total

        msg = pending[0]
        total += 1
        if msg.get("_device_id") == "tablet-xyz":
            print("  [PASS] _device_id 'tablet-xyz' stamped on pending msg")
            passed += 1
        else:
            print(f"  [FAIL] _device_id missing: {msg!r}")

        # Cleanup
        core._csm_pending[user_id] = []
        core._csm_state[user_id] = "IDLE"
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Plumbing: {passed}/{total}\n")
    return passed, total


def main():
    results = [
        test_chat_annotation_stamps_device_id(),
        test_screenshot_annotation_stamps_device_id(),
        test_n_device_aggregation(),
        test_device_id_empty_tolerated(),
        test_device_id_plumbed_into_pending_message(),
    ]
    p = sum(r[0] for r in results)
    t = sum(r[1] for r in results)
    print("=" * 60)
    print(f"TOTAL: {p}/{t} passed")
    print("=" * 60)
    if p < t:
        sys.exit(1)


if __name__ == "__main__":
    main()
