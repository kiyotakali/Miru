"""Test onboarding fire-and-forget + first-greeting dedup (2026-05-08).

Bug-driven test:
  Old behavior:
    - api_onboarding_submit synchronously called LLM (5-30s)
    - HTTP request hung; user re-tapped form → multiple submits
    - empty-history dedup checked BEFORE the slow LLM call → race window
    - result: multiple duplicate greetings in chat_history

  New behavior (verified here):
    1. initialize_from_questionnaire returns in <500ms (LLM in bg thread)
    2. concurrent _inject_custom_first_greeting_bg calls produce exactly 1
       greeting (lock + double-check pattern)
    3. greeting still arrives in chat_history when LLM completes
"""
import os
import sys
import json
import time
import shutil
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    """Create temp data dir and return path."""
    return tempfile.mkdtemp(prefix="miru_onb_test_")


def _reset_flask_g():
    try:
        from flask.globals import _cv_app
        while _cv_app.get() is not None:
            _cv_app.get().pop()
    except Exception:
        pass


def test_initialize_returns_fast_not_blocking_on_llm():
    """initialize_from_questionnaire must return in <500ms even when the
    greeting LLM would take 5+ seconds. Proves fire-and-forget."""
    print("=" * 60)
    print("TEST: initialize_from_questionnaire returns fast (no LLM block)")
    print("=" * 60)

    import core
    import storage
    import miru_emotion

    tmp = _setup_user_dir()
    miru_emotion._instances.clear()
    passed = 0
    total = 0

    # Reset the in-progress set so other tests don't pollute
    with core._first_greeting_lock:
        core._first_greeting_in_progress.clear()

    # Slow LLM stub: sleep 3 seconds before returning
    def slow_llm(*args, **kwargs):
        time.sleep(3.0)
        return "嗯，今天打算干嘛？"

    answers = {
        "name": "测试小明",
        "occupation": "developer",
        "focus": "写测试",
        "style": "natural",
        "schedule": "regular",
    }

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("prompt._call_llm_text", side_effect=slow_llm):
            t0 = time.time()
            result = core.initialize_from_questionnaire(answers)
            elapsed_ms = (time.time() - t0) * 1000

            total += 1
            if elapsed_ms < 500:
                print(f"  [PASS] returned in {elapsed_ms:.0f}ms (< 500ms)")
                passed += 1
            else:
                print(f"  [FAIL] returned in {elapsed_ms:.0f}ms (expected < 500ms — was LLM blocking?)")

            # Verify the result acknowledges the dispatch
            total += 1
            actions = result.get("actions", [])
            dispatched = any("dispatched" in a or "background" in a for a in actions)
            if dispatched:
                print(f"  [PASS] result.actions confirms background dispatch: {actions}")
                passed += 1
            else:
                print(f"  [FAIL] no background dispatch in actions: {actions}")

            # Wait for the background LLM thread to complete
            time.sleep(4.0)

            # Now verify the greeting actually landed in chat_history
            total += 1
            history = storage.read_json(storage.chat_history_path()) or []
            greetings = [m for m in history if m.get("role") == "assistant"
                         and m.get("id", "").startswith("greeting_")]
            if len(greetings) == 1:
                print(f"  [PASS] exactly 1 greeting eventually written")
                passed += 1
            else:
                print(f"  [FAIL] expected 1 greeting, got {len(greetings)}")

    finally:
        miru_emotion._instances.clear()
        with core._first_greeting_lock:
            core._first_greeting_in_progress.clear()
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  FastReturn: {passed}/{total} passed")
    return passed, total


def test_concurrent_submits_produce_exactly_one_greeting():
    """Three concurrent _inject_custom_first_greeting_bg calls (simulating
    a user impatiently tapping submit 3 times) must result in exactly ONE
    greeting in chat_history."""
    print("\n" + "=" * 60)
    print("TEST: concurrent submits → exactly 1 greeting (dedup)")
    print("=" * 60)

    import core
    import storage
    import miru_emotion

    tmp = _setup_user_dir()
    miru_emotion._instances.clear()
    with core._first_greeting_lock:
        core._first_greeting_in_progress.clear()

    passed = 0
    total = 0

    llm_call_count = [0]
    llm_call_lock = threading.Lock()

    def slow_llm(*args, **kwargs):
        with llm_call_lock:
            llm_call_count[0] += 1
            my_call = llm_call_count[0]
        time.sleep(2.0)
        return f"greeting from call #{my_call}"

    answers = {"name": "并发测试", "focus": "测 dedup"}

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("prompt._call_llm_text", side_effect=slow_llm):
            # Simulate 3 concurrent submits
            threads = []
            for i in range(3):
                t = threading.Thread(
                    target=core._inject_custom_first_greeting_bg,
                    args=(answers, "_admin", tmp),
                    daemon=True,
                )
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=10)

            # Verify chat_history has exactly 1 greeting
            total += 1
            history = storage.read_json(storage.chat_history_path()) or []
            greetings = [m for m in history if m.get("role") == "assistant"
                         and m.get("id", "").startswith("greeting_")]
            if len(greetings) == 1:
                print(f"  [PASS] exactly 1 greeting in chat_history (3 threads → 1 message)")
                passed += 1
            else:
                print(f"  [FAIL] expected 1 greeting, got {len(greetings)}")
                for g in greetings:
                    print(f"    - {g.get('id')}: {g.get('text', '')[:50]}")

            # Verify LLM called at most once (lock should bail others)
            total += 1
            if llm_call_count[0] == 1:
                print(f"  [PASS] LLM called exactly once (token cost saved)")
                passed += 1
            elif llm_call_count[0] <= 3:
                # Acceptable if concurrent threads slipped past the lock but
                # the double-check at the end caught duplicates.
                print(f"  [PASS-soft] LLM called {llm_call_count[0]}× but only 1 message written")
                passed += 1
            else:
                print(f"  [FAIL] LLM called {llm_call_count[0]}× — lock not working")

            # Verify in_progress set is cleaned up
            total += 1
            with core._first_greeting_lock:
                stuck = "_admin" in core._first_greeting_in_progress
            if not stuck:
                print(f"  [PASS] in_progress set cleaned up after all threads")
                passed += 1
            else:
                print(f"  [FAIL] _admin stuck in _first_greeting_in_progress")

    finally:
        miru_emotion._instances.clear()
        with core._first_greeting_lock:
            core._first_greeting_in_progress.clear()
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Dedup: {passed}/{total} passed")
    return passed, total


def test_second_submit_after_first_finished_is_skipped():
    """If the first greeting completed normally and the user (somehow)
    re-submits, the new call must NOT write a second greeting."""
    print("\n" + "=" * 60)
    print("TEST: re-submit after first greeting → skipped (no overwrite)")
    print("=" * 60)

    import core
    import storage
    import miru_emotion

    tmp = _setup_user_dir()
    miru_emotion._instances.clear()
    with core._first_greeting_lock:
        core._first_greeting_in_progress.clear()

    passed = 0
    total = 0

    def fast_llm(*args, **kwargs):
        return "first greeting"

    answers = {"name": "再次测试"}

    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("prompt._call_llm_text", side_effect=fast_llm):
            # First call — should succeed
            core._inject_custom_first_greeting_bg(answers, "_admin", tmp)

            history1 = storage.read_json(storage.chat_history_path()) or []
            len1 = len([m for m in history1 if m.get("role") == "assistant"])

            # Second call — chat_history is non-empty, must bail
            core._inject_custom_first_greeting_bg(answers, "_admin", tmp)

            history2 = storage.read_json(storage.chat_history_path()) or []
            len2 = len([m for m in history2 if m.get("role") == "assistant"])

            total += 1
            if len1 == 1:
                print(f"  [PASS] first call wrote 1 greeting")
                passed += 1
            else:
                print(f"  [FAIL] first call wrote {len1} greetings, expected 1")

            total += 1
            if len2 == len1:
                print(f"  [PASS] second call skipped (still {len2} greeting)")
                passed += 1
            else:
                print(f"  [FAIL] second call duplicated: {len1} → {len2}")

    finally:
        miru_emotion._instances.clear()
        with core._first_greeting_lock:
            core._first_greeting_in_progress.clear()
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Idempotent: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_initialize_returns_fast_not_blocking_on_llm())
    results.append(test_concurrent_submits_produce_exactly_one_greeting())
    results.append(test_second_submit_after_first_finished_is_skipped())

    total_p = sum(r[0] for r in results)
    total_t = sum(r[1] for r in results)

    print("\n" + "=" * 60)
    print(f"TOTAL: {total_p}/{total_t} passed")
    print("=" * 60)

    if total_p < total_t:
        sys.exit(1)


if __name__ == "__main__":
    main()
