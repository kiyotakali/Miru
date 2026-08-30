"""End-to-end test for Miru emotion event-driven evaluation (V3 — 2026-05-08).

Verifies that core._evaluate_miru_emotion + the rich-context plumbing
work together: given a faked chat history, emotion log, and care_log,
the helper functions extract the right signals (today_chat with
[图片] placeholders, emotion arc, time deltas), the LLM mock gets a
fully-formed prompt, and miru_emotion.json is updated with new state +
history entry.

Pytest hygiene: _evaluate_miru_emotion calls _csm_set_thread_context
which pushes a Flask app context + sets g.user_data_dir. Without explicit
teardown, the next test inherits a stale tmp-dir pointer. Each test that
exercises the full path resets g back to admin via _reset_flask_g().
"""
import os
import sys
import json
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _reset_flask_g():
    """Pop any Flask context pushed by _csm_set_thread_context.

    Without this, g.user_data_dir keeps pointing at our now-deleted
    tmp dir, and downstream tests that inspect the default per-user
    data dir (test_self_profile, test_user_settings) fail because
    they read the stale path.
    """
    try:
        from flask import g, has_app_context
        from flask.globals import _cv_app
        # Pop ALL active app contexts (defensive — usually 1)
        while has_app_context():
            ctx = _cv_app.get()
            if ctx is None:
                break
            ctx.pop()
    except Exception:
        pass


def _setup_data_dir():
    """Create a temp data dir and prime it with chat history + emotion log + care log."""
    tmp = tempfile.mkdtemp(prefix="miru_emotion_e2e_")
    today = datetime.now().strftime("%Y-%m-%d")

    # chat_history.json — mix of text, image (with text), image (no text), Miru reply
    chat = [
        {"role": "user", "text": "早上好", "time": f"{today} 09:30:00", "image": ""},
        {"role": "assistant", "text": "早呀，今天打算做什么？",
         "time": f"{today} 09:31:00", "image": ""},
        {"role": "user", "text": "下午要面试，有点紧张",
         "time": f"{today} 09:33:00", "image": ""},
        {"role": "assistant", "text": "嗯...想清楚自己想表达什么就好",
         "time": f"{today} 09:34:00", "image": ""},
        # Image-only message (historical → should become [图片] placeholder)
        {"role": "user", "text": "", "time": f"{today} 12:00:00",
         "image": "fake_lunch.jpg"},
        # Miru proactive (should be tagged as "Miru主动")
        {"role": "assistant", "text": "你今天早上说的面试，现在是不是快开始了？加油呀",
         "time": f"{today} 14:00:00", "image": "",
         "type": "proactive"},
        {"role": "user", "text": "嗯刚出门", "time": f"{today} 14:01:00", "image": ""},
        {"role": "user", "text": "面试结束了，挺顺利",
         "time": f"{today} 16:25:00", "image": ""},
    ]
    with open(os.path.join(tmp, "chat_history.json"), "w") as f:
        json.dump(chat, f)

    # emotion_log.json — { date: [entries] }
    emo_log = {
        today: [
            {"timestamp": f"{today} 09:35:00", "mood": "anxious",
             "valence": -0.4, "intensity": 0.6},
            {"timestamp": f"{today} 14:01:00", "mood": "anxious",
             "valence": -0.3, "intensity": 0.5},
            {"timestamp": f"{today} 16:25:00", "mood": "relieved",
             "valence": 0.5, "intensity": 0.6},
        ]
    }
    with open(os.path.join(tmp, "emotion_log.json"), "w") as f:
        json.dump(emo_log, f)

    # care_log.json — Miru proactive history (used by _seconds_since_last_miru_proactive)
    care_log = [
        {"ts": f"{today} 14:00:00", "decision": "speak",
         "message": "你今天早上说的面试，现在是不是快开始了？加油呀",
         "thought": "面试时间快到了，发个鼓励"},
        {"ts": f"{today} 14:30:00", "decision": "wait", "wait_minutes": 30,
         "thought": "面试中，先安静"},
    ]
    with open(os.path.join(tmp, "care_log.json"), "w") as f:
        json.dump(care_log, f)

    return tmp, today


def test_format_today_chat_handles_image_placeholders():
    """_format_today_chat_for_miru_emotion replaces historical images with [图片]."""
    print("=" * 60)
    print("TEST: today_chat formatter — [图片] placeholder")
    print("=" * 60)

    import storage
    import core
    tmp, today = _setup_data_dir()
    passed = 0
    total = 0

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp):
            text = core._format_today_chat_for_miru_emotion(datetime.now())

        total += 1
        if "[图片]" in text:
            print(f"  [PASS] [图片] placeholder appears in formatted chat")
            passed += 1
        else:
            print(f"  [FAIL] no [图片] placeholder; got:\n{text}")

        total += 1
        if "Miru主动" in text:
            print(f"  [PASS] proactive Miru message tagged as 'Miru主动'")
            passed += 1
        else:
            print(f"  [FAIL] proactive tag missing; got:\n{text}")

        total += 1
        if "下午要面试" in text and "面试结束了" in text:
            print(f"  [PASS] all today's messages present")
            passed += 1
        else:
            print(f"  [FAIL] missing messages; got:\n{text}")

        total += 1
        # Times should have HH:MM format
        if "[09:30]" in text and "[16:25]" in text:
            print(f"  [PASS] HH:MM timestamps present")
            passed += 1
        else:
            print(f"  [FAIL] timestamp format wrong; got:\n{text[:200]}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  ChatFormat: {passed}/{total} passed")
    return passed, total


def test_format_user_emotion_arc_buckets():
    """_format_user_emotion_arc produces time-bucketed valence summary."""
    print("\n" + "=" * 60)
    print("TEST: emotion arc — time bucket aggregation")
    print("=" * 60)

    import storage
    import core
    tmp, today = _setup_data_dir()
    passed = 0
    total = 0

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp):
            arc = core._format_user_emotion_arc(datetime.now())

        # Expect at least one bucket showing
        total += 1
        if "09-12" in arc or "12-15" in arc or "15-18" in arc:
            print(f"  [PASS] bucket format present in arc:\n{arc}")
            passed += 1
        else:
            print(f"  [FAIL] no bucket; got:\n{arc!r}")

        # Expect the dominant mood field
        total += 1
        if "主导 mood" in arc:
            print(f"  [PASS] dominant mood label present")
            passed += 1
        else:
            print(f"  [FAIL] no '主导 mood' marker; got:\n{arc!r}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  EmotionArc: {passed}/{total} passed")
    return passed, total


def test_seconds_since_helpers():
    """_seconds_since_last_user_msg + _seconds_since_last_miru_proactive."""
    print("\n" + "=" * 60)
    print("TEST: time-delta helpers")
    print("=" * 60)

    import storage
    import core
    tmp, today = _setup_data_dir()
    passed = 0
    total = 0

    try:
        # The latest user message in the fixture is at 16:25
        # Miru's last proactive in care_log is at 14:00
        # We force "now" to a known timestamp
        fixed_now = datetime.strptime(f"{today} 17:00:00", "%Y-%m-%d %H:%M:%S")

        with patch.object(storage, "get_data_dir", return_value=tmp):
            secs_user = core._seconds_since_last_user_msg(fixed_now)
            secs_miru = core._seconds_since_last_miru_proactive(fixed_now)

        total += 1
        # Expect ~35 minutes between 16:25 and 17:00
        if secs_user is not None and 30 * 60 <= secs_user <= 40 * 60:
            print(f"  [PASS] secs_since_user = {secs_user} (~35 min)")
            passed += 1
        else:
            print(f"  [FAIL] secs_since_user = {secs_user}, expected ~35min")

        total += 1
        # Expect ~3h between 14:00 and 17:00
        if secs_miru is not None and 2.5 * 3600 <= secs_miru <= 3.5 * 3600:
            print(f"  [PASS] secs_since_miru = {secs_miru} (~3h)")
            passed += 1
        else:
            print(f"  [FAIL] secs_since_miru = {secs_miru}, expected ~3h")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  TimeDeltas: {passed}/{total} passed")
    return passed, total


def test_evaluate_miru_emotion_writes_state_and_history():
    """End-to-end: _evaluate_miru_emotion → emo state + history updated."""
    print("\n" + "=" * 60)
    print("TEST: _evaluate_miru_emotion writes state + history")
    print("=" * 60)

    import storage
    import core
    import miru_emotion
    tmp, today = _setup_data_dir()
    passed = 0
    total = 0

    # Reset miru_emotion singleton so test runs are isolated
    miru_emotion._instances.clear()

    captured_args = []

    def mock_eval(**kwargs):
        captured_args.append(kwargs)
        # LLM no longer emits closeness/trust deltas (removed 2026-05-09).
        return {
            "mood": "为他松了一口气",
            "valence": 0.4,
            "arousal": 0.3,
            "reason": "面试顺利结束",
        }

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("memory_prompts.call_miru_emotion_eval", side_effect=mock_eval):
            # Empty out any pre-existing state
            emo_path = os.path.join(tmp, "miru_emotion.json")
            if os.path.exists(emo_path):
                os.remove(emo_path)

            core._evaluate_miru_emotion(
                trigger_kind="chat",
                trigger_text="面试结束了，挺顺利",
                user_id="_admin",
                user_data_dir=tmp,
                device_id="test",
            )

            # Verify the LLM was called with rich context
            total += 1
            if captured_args:
                kw = captured_args[0]
                checks = {
                    "trigger_kind": kw.get("trigger_kind") == "chat",
                    "trigger_text": kw.get("trigger_text") == "面试结束了，挺顺利",
                    "today_chat_present": (kw.get("today_chat") or "").strip() != "",
                    "secs_user_set": kw.get("seconds_since_user_msg") is not None,
                    "secs_miru_set": kw.get("seconds_since_miru_proactive") is not None,
                    "user_arc_present": (kw.get("user_emotion_arc") or "").strip() != "",
                    "current_state_dict": isinstance(kw.get("current_state"), dict),
                    "days_together_int": isinstance(kw.get("days_together"), int),
                    # The relationship param was removed in 2026-05-09 — must NOT be
                    # passed any more (would TypeError on the new signature).
                    "relationship_not_passed": "relationship" not in kw,
                }
                if all(checks.values()):
                    print(f"  [PASS] LLM called with all required context fields")
                    passed += 1
                else:
                    failed = [k for k, v in checks.items() if not v]
                    print(f"  [FAIL] missing fields: {failed}")
                    print(f"    got kwargs keys: {sorted(kw.keys())}")
            else:
                print(f"  [FAIL] LLM was not called")

            # Verify emotion state updated
            total += 1
            emo = miru_emotion.get_instance()
            state = emo.get_state()
            if state.get("mood") == "为他松了一口气":
                print(f"  [PASS] state.mood updated: {state['mood']}")
                passed += 1
            else:
                print(f"  [FAIL] state.mood not updated; got {state.get('mood')!r}")

            # Verify valence is reasonable (after inertia blend with 0)
            total += 1
            v = state.get("valence", 0)
            if v > 0:
                print(f"  [PASS] valence positive (after blend): {v}")
                passed += 1
            else:
                print(f"  [FAIL] valence not positive: {v}")

            # Verify history grew
            total += 1
            history = emo.get_history(limit=5)
            if history and history[-1].get("mood") == "为他松了一口气":
                print(f"  [PASS] history entry written")
                passed += 1
            else:
                print(f"  [FAIL] history empty or wrong: {history}")

            # Relationship meter is pinned to ceiling — must NOT have changed
            # because closeness/trust no longer accumulate.
            total += 1
            rel = emo.get_relationship()
            if rel == {"closeness": 1.0, "trust": 1.0}:
                print(f"  [PASS] relationship pinned to ceiling (no accumulation): {rel}")
                passed += 1
            else:
                print(f"  [FAIL] relationship not pinned: {rel}")

    finally:
        miru_emotion._instances.clear()
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  E2E: {passed}/{total} passed")
    return passed, total


def test_evaluate_miru_emotion_skips_on_none_result():
    """If LLM returns None, no state change should happen."""
    print("\n" + "=" * 60)
    print("TEST: _evaluate_miru_emotion — None result does not break state")
    print("=" * 60)

    import storage
    import core
    import miru_emotion
    tmp, today = _setup_data_dir()
    passed = 0
    total = 0

    miru_emotion._instances.clear()

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("memory_prompts.call_miru_emotion_eval", return_value=None):
            emo = miru_emotion.get_instance()
            before = emo.get_state()

            core._evaluate_miru_emotion(
                trigger_kind="chat",
                trigger_text="hi",
                user_id="_admin",
                user_data_dir=tmp,
                device_id="",
            )

            after = emo.get_state()
            total += 1
            if before.get("mood") == after.get("mood"):
                print(f"  [PASS] state unchanged on None result")
                passed += 1
            else:
                print(f"  [FAIL] state changed: {before['mood']} → {after['mood']}")

    finally:
        miru_emotion._instances.clear()
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Skip: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_format_today_chat_handles_image_placeholders())
    results.append(test_format_user_emotion_arc_buckets())
    results.append(test_seconds_since_helpers())
    results.append(test_evaluate_miru_emotion_writes_state_and_history())
    results.append(test_evaluate_miru_emotion_skips_on_none_result())

    total_p = sum(r[0] for r in results)
    total_t = sum(r[1] for r in results)

    print("\n" + "=" * 60)
    print(f"TOTAL: {total_p}/{total_t} passed")
    print("=" * 60)

    if total_p < total_t:
        sys.exit(1)


if __name__ == "__main__":
    main()
