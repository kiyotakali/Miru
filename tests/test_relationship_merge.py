"""Tests for #193-D: merged relationship_meta.json into miru_emotion.

Goal: verify that first_meet_date is stored in miru_emotion.relationship_meter,
legacy relationship_meta.json is migrated on first read, and the API surface
remains backward compatible (storage.get_relationship_meta / ensure_first_meet_date
still return the legacy dict shape).
"""
import os
import sys
import json
import tempfile
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import storage  # noqa: E402
import miru_emotion  # noqa: E402


_ORIGINAL_DATA_DIR = storage.DATA_DIR


def _fresh_data_dir():
    """Create a clean per-test data dir + reset the MiruEmotion singleton."""
    d = tempfile.mkdtemp(prefix="relmerge_")
    storage.DATA_DIR = d
    os.makedirs(os.path.join(d, "archive"), exist_ok=True)
    os.makedirs(os.path.join(d, "uploads"), exist_ok=True)
    # Blow away any cached MiruEmotion instance so _load() re-reads from disk
    miru_emotion._instances.clear()
    return d


def _restore_data_dir():
    storage.DATA_DIR = _ORIGINAL_DATA_DIR
    miru_emotion._instances.clear()


def test_ensure_initializes_today_when_no_data():
    """Fresh install: ensure_first_meet_date seeds today's date into miru_emotion."""
    print("=" * 60)
    print("TEST: fresh install bootstraps first_meet_date to today")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        emo = miru_emotion.get_instance()
        fmd = emo.ensure_first_meet_date()
        today = datetime.now().strftime("%Y-%m-%d")

        total += 1
        if fmd == today:
            print("  [PASS] first_meet_date defaults to today")
            passed += 1
        else:
            print(f"  [FAIL] expected {today}, got {fmd!r}")

        # And it persisted
        total += 1
        raw = storage.load_miru_emotion()
        meter = (raw or {}).get("relationship_meter", {})
        if meter.get("first_meet_date") == today:
            print("  [PASS] first_meet_date persisted to miru_emotion.json")
            passed += 1
        else:
            print(f"  [FAIL] not persisted: {meter}")

        # No legacy file should have been created
        total += 1
        legacy_path = os.path.join(d, "relationship_meta.json")
        if not os.path.exists(legacy_path):
            print("  [PASS] no legacy relationship_meta.json created")
            passed += 1
        else:
            print(f"  [FAIL] legacy file created: {legacy_path}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Fresh: {passed}/{total}\n")
    return passed, total


def test_migrates_legacy_relationship_meta():
    """Existing relationship_meta.json with first_meet_date is migrated on first read."""
    print("=" * 60)
    print("TEST: legacy relationship_meta.json migrates into miru_emotion")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # Seed legacy file with a specific date
        legacy_date = "2025-11-14"
        legacy_path = os.path.join(d, "relationship_meta.json")
        with open(legacy_path, "w") as f:
            json.dump({"first_meet_date": legacy_date,
                       "some_extra_field": "will_be_dropped"}, f)

        # Trigger migration
        emo = miru_emotion.get_instance()
        fmd = emo.ensure_first_meet_date()

        total += 1
        if fmd == legacy_date:
            print(f"  [PASS] migrated legacy date {legacy_date}")
            passed += 1
        else:
            print(f"  [FAIL] expected {legacy_date}, got {fmd!r}")

        # Check it's now in miru_emotion.json
        total += 1
        raw = storage.load_miru_emotion()
        meter = (raw or {}).get("relationship_meter", {})
        if meter.get("first_meet_date") == legacy_date:
            print("  [PASS] migrated value persisted to miru_emotion.json")
            passed += 1
        else:
            print(f"  [FAIL] not in miru_emotion: {meter}")

        # Second call should be idempotent (not re-read legacy, not overwrite)
        total += 1
        fmd2 = emo.ensure_first_meet_date()
        if fmd2 == legacy_date:
            print("  [PASS] second call returns same value (idempotent)")
            passed += 1
        else:
            print(f"  [FAIL] second call changed: {fmd2!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Migration: {passed}/{total}\n")
    return passed, total


def test_miru_emotion_value_takes_precedence():
    """If miru_emotion already has a date, legacy file is ignored (no overwrite)."""
    print("=" * 60)
    print("TEST: miru_emotion first_meet_date is not overwritten by legacy file")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # Pre-seed miru_emotion with an authoritative date
        authoritative = "2025-06-01"
        state = {
            "current": {"mood": "neutral", "valence": 0.0, "arousal": 0.2,
                        "updated_at": "2025-06-01T00:00:00", "reason": ""},
            "history": [],
            "relationship_meter": {
                "closeness": 0.5, "trust": 0.5,
                "last_interaction": "",
                "first_meet_date": authoritative,
            },
        }
        storage.save_miru_emotion(state)

        # Write a conflicting legacy file
        legacy_path = os.path.join(d, "relationship_meta.json")
        with open(legacy_path, "w") as f:
            json.dump({"first_meet_date": "2099-01-01"}, f)

        emo = miru_emotion.get_instance()
        fmd = emo.ensure_first_meet_date()

        total += 1
        if fmd == authoritative:
            print(f"  [PASS] returned authoritative {authoritative}, ignored legacy")
            passed += 1
        else:
            print(f"  [FAIL] got {fmd!r}, expected {authoritative}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Precedence: {passed}/{total}\n")
    return passed, total


def test_days_together_calculation():
    """get_days_together returns correct elapsed days, 0 when unset."""
    print("=" * 60)
    print("TEST: get_days_together arithmetic")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        emo = miru_emotion.get_instance()

        # Unset → 0
        total += 1
        if emo.get_days_together() == 0:
            print("  [PASS] unset first_meet_date → 0 days")
            passed += 1
        else:
            print(f"  [FAIL] expected 0, got {emo.get_days_together()}")

        # 10 days ago
        ten_days_ago = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        state = storage.load_miru_emotion() or {}
        state.setdefault("relationship_meter", {})["first_meet_date"] = ten_days_ago
        storage.save_miru_emotion(state)
        miru_emotion._instances.clear()  # force reload

        emo = miru_emotion.get_instance()
        days = emo.get_days_together()
        total += 1
        if days == 10:
            print(f"  [PASS] 10-day-old date → {days} days")
            passed += 1
        else:
            print(f"  [FAIL] expected 10, got {days}")

        # Future date → clamps to 0
        future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        state["relationship_meter"]["first_meet_date"] = future
        storage.save_miru_emotion(state)
        miru_emotion._instances.clear()
        emo = miru_emotion.get_instance()
        total += 1
        if emo.get_days_together() == 0:
            print("  [PASS] future date clamps to 0")
            passed += 1
        else:
            print(f"  [FAIL] future got {emo.get_days_together()}")

        # Malformed date → 0 (no crash)
        state["relationship_meter"]["first_meet_date"] = "not-a-date"
        storage.save_miru_emotion(state)
        miru_emotion._instances.clear()
        emo = miru_emotion.get_instance()
        total += 1
        if emo.get_days_together() == 0:
            print("  [PASS] malformed date → 0 (no crash)")
            passed += 1
        else:
            print(f"  [FAIL] malformed got {emo.get_days_together()}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Arithmetic: {passed}/{total}\n")
    return passed, total


def test_storage_delegators_backward_compat():
    """Legacy callers (storage.ensure_first_meet_date / get_relationship_meta)
    still see the dict shape they expect."""
    print("=" * 60)
    print("TEST: storage.* delegators preserve dict shape")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        # get_relationship_meta when nothing set → empty dict (matches legacy)
        total += 1
        meta = storage.get_relationship_meta()
        if meta == {}:
            print("  [PASS] get_relationship_meta() returns {} when unset")
            passed += 1
        else:
            print(f"  [FAIL] expected {{}}, got {meta!r}")

        # ensure_first_meet_date returns dict with the field
        total += 1
        meta = storage.ensure_first_meet_date()
        if isinstance(meta, dict) and "first_meet_date" in meta and meta["first_meet_date"]:
            print(f"  [PASS] ensure_first_meet_date() → {meta!r}")
            passed += 1
        else:
            print(f"  [FAIL] bad shape: {meta!r}")

        # get_relationship_meta now returns the value
        total += 1
        meta2 = storage.get_relationship_meta()
        if meta2.get("first_meet_date") == meta["first_meet_date"]:
            print("  [PASS] get_relationship_meta() returns set value")
            passed += 1
        else:
            print(f"  [FAIL] mismatch: {meta2!r} vs {meta!r}")

        # save_relationship_meta also round-trips
        total += 1
        storage.save_relationship_meta({"first_meet_date": "2024-01-01"})
        meta3 = storage.get_relationship_meta()
        if meta3.get("first_meet_date") == "2024-01-01":
            print("  [PASS] save_relationship_meta round-trips")
            passed += 1
        else:
            print(f"  [FAIL] save_relationship_meta broken: {meta3!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  Compat: {passed}/{total}\n")
    return passed, total


def test_api_endpoint_returns_merged_fields():
    """/api/miru-emotion/current returns first_meet_date + days_together.

    We bypass Flask auth by patching auth.check_request to set g ourselves —
    that's simpler than minting a real admin token for a pure API shape test.
    """
    print("=" * 60)
    print("TEST: /api/miru-emotion/current returns merged fields")
    print("=" * 60)
    d = _fresh_data_dir()
    passed, total = 0, 0
    try:
        import app as app_module
        import auth as auth_module
        flask_app = app_module.app
        flask_app.config["TESTING"] = True

        from flask import g as _g

        def fake_check_request(req=None):
            _g.user_id = "_admin"
            _g.user_data_dir = d
            _g.is_admin = False
            return None

        from unittest.mock import patch
        with patch.object(auth_module, "check_request", side_effect=fake_check_request):
            with flask_app.test_client() as client:
                r = client.get("/api/miru-emotion/current")

                total += 1
                if r.status_code == 200:
                    print("  [PASS] endpoint returns 200")
                    passed += 1
                else:
                    print(f"  [FAIL] status={r.status_code} body={r.data[:200]!r}")
                    return passed, total

                data = r.get_json() or {}

                total += 1
                if "first_meet_date" in data and data["first_meet_date"]:
                    print(f"  [PASS] first_meet_date in payload: {data['first_meet_date']}")
                    passed += 1
                else:
                    print(f"  [FAIL] first_meet_date missing: {list(data.keys())}")

                total += 1
                if "days_together" in data and isinstance(data["days_together"], int):
                    print(f"  [PASS] days_together in payload: {data['days_together']}")
                    passed += 1
                else:
                    print(f"  [FAIL] days_together missing: {data.get('days_together')}")

                total += 1
                if "character_name" in data and data["character_name"]:
                    print(f"  [PASS] character_name in payload: {data['character_name']}")
                    passed += 1
                else:
                    print(f"  [FAIL] character_name missing: {data.get('character_name')}")

                # Still has the legacy shape (current/relationship/stage)
                total += 1
                if "current" in data and "relationship" in data and "stage" in data:
                    print("  [PASS] legacy keys (current/relationship/stage) still present")
                    passed += 1
                else:
                    print(f"  [FAIL] legacy keys missing: {list(data.keys())}")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _restore_data_dir()
    print(f"\n  API: {passed}/{total}\n")
    return passed, total


def main():
    results = [
        test_ensure_initializes_today_when_no_data(),
        test_migrates_legacy_relationship_meta(),
        test_miru_emotion_value_takes_precedence(),
        test_days_together_calculation(),
        test_storage_delegators_backward_compat(),
        test_api_endpoint_returns_merged_fields(),
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
