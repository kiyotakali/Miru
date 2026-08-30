"""Test Miru emotion API endpoints and frontend rendering (V4 — 2026-05-09).

V4: closeness/trust meters were removed. The /api/miru-emotion/current endpoint
still returns `relationship` (constant {closeness:1.0, trust:1.0}) and `stage`
(constant "很亲近") for backwards compatibility, but the frontend no longer
renders progress bars or the stage chip.
"""
import sys
import os
from unittest.mock import patch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import auth
from flask import g


def _fake_check_request(req=None):
    """Bypass auth in tests."""
    g.user_id = "u_test000000"
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "users", "u_test000000")
    g.is_admin = False
    return None


def test_api_current():
    """Test /api/miru-emotion/current endpoint."""
    print("=" * 60)
    print("TEST: /api/miru-emotion/current")
    print("=" * 60)

    import miru_emotion

    # Create mock instance
    emo = miru_emotion.MiruEmotion()
    emo._state = miru_emotion._deep_copy(miru_emotion.DEFAULT_STATE)
    emo._state["current"]["updated_at"] = datetime.now().isoformat()[:19]
    emo._state["current"]["mood"] = "开心"
    emo._state["current"]["valence"] = 0.4
    emo._save = lambda: None

    passed = 0
    total = 0

    with patch("miru_emotion.get_instance", return_value=emo), \
         patch.object(auth, "check_request", _fake_check_request):
        from app import app
        client = app.test_client()
        resp = client.get("/api/miru-emotion/current")

        total += 1
        if resp.status_code == 200:
            print(f"  [PASS] Status 200")
            passed += 1
        else:
            print(f"  [FAIL] Status {resp.status_code}")

        data = resp.get_json()

        # Backwards-compat schema: relationship + stage still present.
        total += 1
        if "current" in data and "relationship" in data and "stage" in data:
            print(f"  [PASS] Response schema preserved (current/relationship/stage)")
            passed += 1
        else:
            print(f"  [FAIL] Missing keys: {list(data.keys())}")

        total += 1
        if data.get("current", {}).get("mood") == "开心":
            print(f"  [PASS] Current mood: 开心")
            passed += 1
        else:
            print(f"  [FAIL] Mood: {data.get('current', {}).get('mood')}")

        # Stage now hardcoded to "很亲近" (closeness/trust meters removed).
        total += 1
        if data.get("stage") == "很亲近":
            print(f"  [PASS] Stage hardcoded to 很亲近")
            passed += 1
        else:
            print(f"  [FAIL] Stage should be 很亲近, got: {data.get('stage')}")

        # Relationship meter pinned to 1.0 / 1.0.
        total += 1
        rel = data.get("relationship") or {}
        if rel.get("closeness") == 1.0 and rel.get("trust") == 1.0:
            print(f"  [PASS] Relationship pinned to ceiling")
            passed += 1
        else:
            print(f"  [FAIL] Relationship not pinned: {rel}")

    print(f"\n  Current: {passed}/{total} passed")
    assert passed == total, f"miru_emotion /current: {passed}/{total}"


def test_api_history():
    """Test /api/miru-emotion/history endpoint."""
    print("\n" + "=" * 60)
    print("TEST: /api/miru-emotion/history")
    print("=" * 60)

    import miru_emotion

    emo = miru_emotion.MiruEmotion()
    emo._state = miru_emotion._deep_copy(miru_emotion.DEFAULT_STATE)
    emo._state["current"]["updated_at"] = datetime.now().isoformat()[:19]
    emo._save = lambda: None

    # Add some history entries (LLM no longer emits closeness/trust deltas)
    for i in range(5):
        emo.update_from_llm({
            "mood": f"mood_{i}", "valence": 0.1 * i, "arousal": 0.2,
            "reason": f"reason_{i}",
        })

    passed = 0
    total = 0

    with patch("miru_emotion.get_instance", return_value=emo), \
         patch.object(auth, "check_request", _fake_check_request):
        from app import app
        client = app.test_client()

        # Default limit
        resp = client.get("/api/miru-emotion/history")
        total += 1
        if resp.status_code == 200:
            print(f"  [PASS] Status 200")
            passed += 1
        else:
            print(f"  [FAIL] Status {resp.status_code}")

        data = resp.get_json()
        total += 1
        if isinstance(data, list) and len(data) == 5:
            print(f"  [PASS] Got {len(data)} entries")
            passed += 1
        else:
            print(f"  [FAIL] Expected 5 entries, got {type(data)} with {len(data) if isinstance(data, list) else '?'}")

        # Check entry structure
        total += 1
        if data and "mood" in data[0] and "valence" in data[0] and "timestamp" in data[0]:
            print(f"  [PASS] Entry has mood, valence, timestamp")
            passed += 1
        else:
            print(f"  [FAIL] Missing fields in entry: {data[0] if data else 'empty'}")

        # Closeness/trust must NOT appear in new history snapshots.
        total += 1
        if data and "closeness" not in data[0] and "trust" not in data[0]:
            print(f"  [PASS] Entry does NOT carry closeness/trust (removed)")
            passed += 1
        else:
            print(f"  [FAIL] closeness/trust leaked into history entry: {data[0] if data else 'empty'}")

        # With limit param
        resp2 = client.get("/api/miru-emotion/history?limit=2")
        data2 = resp2.get_json()
        total += 1
        if isinstance(data2, list) and len(data2) == 2:
            print(f"  [PASS] Limit=2 returns {len(data2)} entries")
            passed += 1
        else:
            print(f"  [FAIL] Limit=2 returned {len(data2) if isinstance(data2, list) else '?'}")

    print(f"\n  History: {passed}/{total} passed")
    assert passed == total, f"miru_emotion /history: {passed}/{total}"


def test_frontend_button():
    """Test that the Miru emotion button + the trimmed-down render functions exist."""
    print("\n" + "=" * 60)
    print("TEST: Frontend button + JS functions")
    print("=" * 60)

    passed = 0
    total = 0
    root = os.path.dirname(os.path.dirname(__file__))

    with open(os.path.join(root, "templates/index.html"), encoding="utf-8") as f:
        html = f.read()

    # Button exists
    total += 1
    if 'id="navMiruEmotion"' in html:
        print(f"  [PASS] navMiruEmotion button exists")
        passed += 1
    else:
        print(f"  [FAIL] navMiruEmotion button missing")

    # JS function exists
    total += 1
    if 'async function navMiruEmotion()' in html:
        print(f"  [PASS] navMiruEmotion() function exists")
        passed += 1
    else:
        print(f"  [FAIL] navMiruEmotion() function missing")

    total += 1
    if 'function buildMiruEmotionHtml(' in html:
        print(f"  [PASS] buildMiruEmotionHtml() exists")
        passed += 1
    else:
        print(f"  [FAIL] buildMiruEmotionHtml() missing")

    # Calls correct API
    total += 1
    if '/api/miru-emotion/current' in html and '/api/miru-emotion/history' in html:
        print(f"  [PASS] Fetches both /current and /history APIs")
        passed += 1
    else:
        print(f"  [FAIL] Missing API fetch calls")

    # Meter bar function and 亲密度/信任度 labels must be GONE.
    total += 1
    has_meter = ('function buildMeterBar' in html) or ('亲密度' in html) or ('信任度' in html)
    if not has_meter:
        print(f"  [PASS] Relationship meter UI removed (no buildMeterBar / 亲密度 / 信任度)")
        passed += 1
    else:
        print(f"  [FAIL] Meter UI leaked: buildMeterBar={('function buildMeterBar' in html)} "
              f"亲密度={'亲密度' in html} 信任度={'信任度' in html}")

    # Stage chip rendering must be GONE.
    total += 1
    if "<span class=\"stage\">' + escapeHtml(stage)" not in html:
        print(f"  [PASS] Stage chip removed from render path")
        passed += 1
    else:
        print(f"  [FAIL] Stage chip still rendered")

    print(f"\n  Frontend: {passed}/{total} passed")
    assert passed == total, f"frontend: {passed}/{total}"


def main():
    test_api_current()
    test_api_history()
    test_frontend_button()
    print("\n" + "=" * 60)
    print("All Miru emotion API tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
