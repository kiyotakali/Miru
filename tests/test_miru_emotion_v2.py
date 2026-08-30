"""Test the redesigned Miru emotion engine (V4 — 2026-05-09).

V4: closeness/trust meters were removed. update_from_llm only consumes
mood/valence/arousal/reason. get_relationship_stage() returns the constant
"很亲近" so the chat agent's tone always defaults to the warmest band.
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _fresh_engine():
    """Create a MiruEmotion instance with a mock storage backend."""
    import miru_emotion
    emo = miru_emotion.MiruEmotion()
    # Bypass file storage — use in-memory state
    emo._state = miru_emotion._deep_copy(miru_emotion.DEFAULT_STATE)
    emo._state["current"]["updated_at"] = datetime.now().isoformat()[:19]
    # Stub _save to no-op
    emo._save = lambda: None
    return emo


def test_update_from_llm_basic():
    """Test that update_from_llm applies LLM result with inertia blending."""
    print("=" * 60)
    print("TEST: update_from_llm — basic")
    print("=" * 60)

    from miru_emotion import INERTIA_NEW, INERTIA_OLD

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Initial state: neutral, valence=0, arousal=0.2
    total += 1
    state = emo.get_state()
    if state["mood"] == "neutral" and state["valence"] == 0.0:
        print(f"  [PASS] Initial: neutral, valence=0")
        passed += 1
    else:
        print(f"  [FAIL] Initial: {state}")

    # Apply a happy LLM result (no closeness/trust deltas any more)
    total += 1
    result = emo.update_from_llm({
        "mood": "开心",
        "valence": 0.6,
        "arousal": 0.5,
        "reason": "用户分享了好消息",
    })
    expected_valence = round(INERTIA_NEW * 0.6 + INERTIA_OLD * 0.0, 3)
    if result["mood"] == "开心" and result["valence"] == expected_valence:
        print(f"  [PASS] After happy: mood=开心, valence={result['valence']} (expected {expected_valence})")
        passed += 1
    else:
        print(f"  [FAIL] After happy: {result}, expected valence={expected_valence}")

    # Check reason is set
    total += 1
    if result["reason"] == "用户分享了好消息":
        print(f"  [PASS] Reason set correctly")
        passed += 1
    else:
        print(f"  [FAIL] Reason: {result['reason']}")

    # Check relationship meter pinned to ceiling — meter no longer accumulates.
    total += 1
    rel = emo.get_relationship()
    if rel == {"closeness": 1.0, "trust": 1.0}:
        print(f"  [PASS] Relationship pinned to ceiling: {rel}")
        passed += 1
    else:
        print(f"  [FAIL] Expected ceiling, got: {rel}")

    # Check history has entry, and entry must NOT carry closeness/trust.
    total += 1
    history = emo.get_history(limit=5)
    no_meter_keys = (
        len(history) == 1
        and history[0]["trigger"] == "llm_eval"
        and "closeness" not in history[0]
        and "trust" not in history[0]
    )
    if no_meter_keys:
        print(f"  [PASS] History entry written without closeness/trust fields")
        passed += 1
    else:
        print(f"  [FAIL] History: {history}")

    print(f"\n  Basic: {passed}/{total} passed")
    assert passed == total


def test_update_from_llm_inertia():
    """Test that inertia blending smooths emotion transitions."""
    print("\n" + "=" * 60)
    print("TEST: update_from_llm — inertia blending")
    print("=" * 60)

    from miru_emotion import INERTIA_NEW, INERTIA_OLD

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Apply happy (valence=0.6)
    emo.update_from_llm({"mood": "开心", "valence": 0.6, "arousal": 0.5, "reason": "r1"})
    v1 = emo.get_state()["valence"]

    # Now apply sad (valence=-0.5) — should NOT jump to -0.5 due to inertia
    total += 1
    emo.update_from_llm({"mood": "心疼", "valence": -0.5, "arousal": 0.4, "reason": "r2"})
    v2 = emo.get_state()["valence"]
    expected_v2 = round(INERTIA_NEW * (-0.5) + INERTIA_OLD * v1, 3)
    if v2 == expected_v2:
        print(f"  [PASS] Inertia: {v1} → {v2} (expected {expected_v2}), not a hard jump to -0.5")
        passed += 1
    else:
        print(f"  [FAIL] Inertia: got {v2}, expected {expected_v2}")

    # Verify it's between v1 and -0.5 (blended)
    total += 1
    if -0.5 < v2 < v1:
        print(f"  [PASS] Blended: -0.5 < {v2} < {v1}")
        passed += 1
    else:
        print(f"  [FAIL] Not properly blended: {v2}")

    print(f"\n  Inertia: {passed}/{total} passed")
    assert passed == total


def test_update_from_llm_clamping():
    """Test that extreme valence/arousal LLM values are clamped (deltas no longer exist)."""
    print("\n" + "=" * 60)
    print("TEST: update_from_llm — value clamping")
    print("=" * 60)

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Extreme values
    total += 1
    result = emo.update_from_llm({
        "mood": "test",
        "valence": 5.0,    # way out of range
        "arousal": -2.0,   # way out of range
        "reason": "",
    })
    if result["valence"] <= 1.0 and result["arousal"] >= 0.0:
        print(f"  [PASS] Clamped: valence={result['valence']}<=1.0, arousal={result['arousal']}>=0.0")
        passed += 1
    else:
        print(f"  [FAIL] Not clamped: {result}")

    # Even when caller still passes legacy delta keys, they must be IGNORED
    # (no exception, no closeness/trust mutation).
    total += 1
    emo2 = _fresh_engine()
    result2 = emo2.update_from_llm({
        "mood": "happy", "valence": 0.5, "arousal": 0.4, "reason": "",
        "closeness_delta": 0.5, "trust_delta": -0.5,  # legacy keys — must be no-op
    })
    rel = emo2.get_relationship()
    if rel == {"closeness": 1.0, "trust": 1.0} and result2["mood"] == "happy":
        print(f"  [PASS] Legacy delta keys ignored gracefully")
        passed += 1
    else:
        print(f"  [FAIL] Legacy keys leaked: rel={rel}, result={result2}")

    print(f"\n  Clamping: {passed}/{total} passed")
    assert passed == total


def test_mood_sanitization():
    """Replaces test_update_absence (2026-05-08 — absence path removed).

    Verifies _apply_emotion sanitizes mood:
      - None / empty / non-string → coerced to 'neutral'
      - Whitespace stripped
      - Length capped to _MOOD_MAX_LEN
    """
    print("\n" + "=" * 60)
    print("TEST: mood sanitization in _apply_emotion")
    print("=" * 60)

    from miru_emotion import _MOOD_MAX_LEN
    passed = 0
    total = 0

    cases = [
        ({"mood": None, "valence": 0.1, "arousal": 0.2}, "neutral"),
        ({"mood": "", "valence": 0.1, "arousal": 0.2}, "neutral"),
        ({"mood": "   ", "valence": 0.1, "arousal": 0.2}, "neutral"),
        ({"mood": "  心疼  ", "valence": -0.2, "arousal": 0.4}, "心疼"),
        ({"mood": 123, "valence": 0.1, "arousal": 0.2}, "neutral"),
        ({"mood": "x" * 100, "valence": 0.1, "arousal": 0.2}, "x" * _MOOD_MAX_LEN),
    ]

    for i, (inp, expected_mood) in enumerate(cases):
        total += 1
        emo = _fresh_engine()
        result = emo.update_from_llm(inp)
        if result["mood"] == expected_mood:
            print(f"  [PASS] case {i+1}: mood={result['mood']!r}")
            passed += 1
        else:
            print(f"  [FAIL] case {i+1}: expected {expected_mood!r}, got {result['mood']!r}")

    print(f"\n  Sanitization: {passed}/{total} passed")
    assert passed == total


def test_time_decay():
    """Test that time decay still works."""
    print("\n" + "=" * 60)
    print("TEST: Time decay")
    print("=" * 60)

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Set a strong emotion
    emo.update_from_llm({"mood": "开心", "valence": 0.8, "arousal": 0.7, "reason": "test"})
    v_before = emo._state["current"]["valence"]

    # Simulate 2 hours passing
    total += 1
    two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat()[:19]
    emo._state["current"]["updated_at"] = two_hours_ago
    state = emo.get_state()
    v_after = state["valence"]
    if abs(v_after) < abs(v_before):
        print(f"  [PASS] Decay: valence {v_before} → {v_after} after 2h")
        passed += 1
    else:
        print(f"  [FAIL] No decay: {v_before} → {v_after}")

    # Simulate 10 hours — should decay significantly (>50% reduction)
    total += 1
    v_before_long = emo._state["current"]["valence"]
    emo._state["current"]["updated_at"] = (datetime.now() - timedelta(hours=10)).isoformat()[:19]
    state2 = emo.get_state()
    if abs(state2["valence"]) < abs(v_before_long) * 0.5:
        print(f"  [PASS] Long decay: valence={v_before_long} → {state2['valence']} after 10h (>50% reduction)")
        passed += 1
    else:
        print(f"  [FAIL] Long decay: {v_before_long} → {state2['valence']} (expected >50% reduction)")

    print(f"\n  Decay: {passed}/{total} passed")
    assert passed == total


def test_mood_text_free_form():
    """Test that get_mood_text works with LLM free-form mood words."""
    print("\n" + "=" * 60)
    print("TEST: get_mood_text — free-form moods")
    print("=" * 60)

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Known English key
    total += 1
    emo.update_from_llm({"mood": "happy", "valence": 0.5, "arousal": 0.4, "reason": "聊得很开心"})
    text = emo.get_mood_text()
    if "开心" in text and "聊得很开心" in text:
        print(f"  [PASS] English key 'happy' → displays as '开心': {text}")
        passed += 1
    else:
        print(f"  [FAIL] Expected '开心' + reason, got: {text}")

    # Chinese mood word (LLM output)
    total += 1
    emo.update_from_llm({"mood": "有点心疼", "valence": -0.2, "arousal": 0.3, "reason": "对方压力很大"})
    text2 = emo.get_mood_text()
    if "有点心疼" in text2:
        print(f"  [PASS] Chinese mood '有点心疼' used directly: {text2}")
        passed += 1
    else:
        print(f"  [FAIL] Expected '有点心疼', got: {text2}")

    # Neutral state → empty text
    total += 1
    emo3 = _fresh_engine()
    text3 = emo3.get_mood_text()
    if text3 == "":
        print(f"  [PASS] Neutral → empty text")
        passed += 1
    else:
        print(f"  [FAIL] Neutral should be empty, got: '{text3}'")

    print(f"\n  MoodText: {passed}/{total} passed")
    assert passed == total


def test_relationship_stage_constant():
    """Stage is now a constant — no thresholds, no closeness drives it."""
    print("\n" + "=" * 60)
    print("TEST: Relationship stage is hardcoded to 很亲近")
    print("=" * 60)

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Default state → 很亲近
    total += 1
    if emo.get_relationship_stage() == "很亲近":
        print(f"  [PASS] Default stage = 很亲近")
        passed += 1
    else:
        print(f"  [FAIL] Default: {emo.get_relationship_stage()}")

    # Manually setting closeness in legacy state must NOT change the result —
    # the function ignores state entirely.
    total += 1
    emo._state["relationship_meter"]["closeness"] = 0.05  # would have been "刚认识" pre-fix
    if emo.get_relationship_stage() == "很亲近":
        print(f"  [PASS] Stage stays 很亲近 even with stale low closeness in state")
        passed += 1
    else:
        print(f"  [FAIL] Stage drifted: {emo.get_relationship_stage()}")

    # get_relationship() also returns ceiling regardless of stale state.
    total += 1
    rel = emo.get_relationship()
    if rel == {"closeness": 1.0, "trust": 1.0}:
        print(f"  [PASS] get_relationship() pinned to {rel}")
        passed += 1
    else:
        print(f"  [FAIL] get_relationship() not pinned: {rel}")

    print(f"\n  Stage: {passed}/{total} passed")
    assert passed == total


def test_history_limit():
    """Test that history is capped at MAX_HISTORY."""
    print("\n" + "=" * 60)
    print("TEST: History limit")
    print("=" * 60)

    from miru_emotion import MAX_HISTORY

    emo = _fresh_engine()
    passed = 0
    total = 0

    # Insert MAX_HISTORY + 10 entries
    total += 1
    for i in range(MAX_HISTORY + 10):
        emo.update_from_llm({"mood": f"m{i}", "valence": 0.1, "arousal": 0.2, "reason": f"r{i}"})
    history = emo._state["history"]
    if len(history) == MAX_HISTORY:
        print(f"  [PASS] History capped at {MAX_HISTORY} (inserted {MAX_HISTORY + 10})")
        passed += 1
    else:
        print(f"  [FAIL] History length={len(history)}, expected {MAX_HISTORY}")

    # Oldest entry should be m10 (first 10 dropped)
    total += 1
    if history[0]["mood"] == "m10":
        print(f"  [PASS] Oldest entry is m10 (first 10 dropped)")
        passed += 1
    else:
        print(f"  [FAIL] Oldest entry: {history[0]['mood']}, expected m10")

    print(f"\n  HistLimit: {passed}/{total} passed")
    assert passed == total


def test_dead_methods_removed():
    """Verify dead/legacy methods (update_absence, record_interaction,
    get_hours_since_interaction) are gone."""
    print("\n" + "=" * 60)
    print("TEST: legacy methods removed")
    print("=" * 60)

    import miru_emotion

    passed = 0
    total = 0

    total += 1
    if not hasattr(miru_emotion, 'TRIGGER_TABLE'):
        print(f"  [PASS] TRIGGER_TABLE no longer exists")
        passed += 1
    else:
        print(f"  [FAIL] TRIGGER_TABLE still exists!")

    emo = _fresh_engine()

    # 2026-05-09: removed alongside meter system.
    for method in ("update_absence", "record_interaction", "get_hours_since_interaction"):
        total += 1
        if not hasattr(emo, method):
            print(f"  [PASS] {method}() removed")
            passed += 1
        else:
            print(f"  [FAIL] {method}() still present")

    # update_from_llm survives.
    total += 1
    if hasattr(emo, 'update_from_llm'):
        print(f"  [PASS] update_from_llm() exists")
        passed += 1
    else:
        print(f"  [FAIL] update_from_llm() missing")

    print(f"\n  API: {passed}/{total} passed")
    assert passed == total


def main():
    test_update_from_llm_basic()
    test_update_from_llm_inertia()
    test_update_from_llm_clamping()
    test_mood_sanitization()
    test_time_decay()
    test_mood_text_free_form()
    test_relationship_stage_constant()
    test_history_limit()
    test_dead_methods_removed()
    print("\n" + "=" * 60)
    print("All Miru emotion v2 tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
