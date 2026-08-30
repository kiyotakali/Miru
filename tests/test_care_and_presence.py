"""Tests previously covered v2 CareEngine cooldown escalation + presence gating.

Both behaviors are removed in CareEngine v3 — the LLM now decides cadence
based on the rich snapshot, and there's no hardcoded presence check (the
LLM reads `activity_state` from the snapshot directly).

Equivalent v3 coverage lives in tests/test_care_engine_v3.py.

The screen-sensor and screenshot-interval tests are still applicable and
preserved below.
"""
import sys
import os
import time
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_screen_sensor_active_time():
    """ScreenSensor updates _last_screen_active_time on screen change."""
    print("\n" + "=" * 60)
    print("TEST: ScreenSensor._last_screen_active_time")
    print("=" * 60)

    from sensor import ScreenSensor
    from PIL import Image

    sens = ScreenSensor()
    passed = 0
    total = 0

    total += 1
    if sens.get_last_screen_active_time() is None:
        print(f"  [PASS] Initial active_time is None")
        passed += 1
    else:
        print(f"  [FAIL] Expected None, got {sens.get_last_screen_active_time()}")

    total += 1
    img1 = Image.new("RGB", (200, 150), color=(100, 100, 100))
    thumb1 = img1.copy()
    changed = sens._screen_changed(thumb1)
    if changed:
        with sens._lock:
            sens._last_screen_active_time = datetime.now()
    active_t = sens.get_last_screen_active_time()
    if active_t is not None and (datetime.now() - active_t).total_seconds() < 2:
        print(f"  [PASS] After first screenshot: active_time set to now")
        passed += 1
    else:
        print(f"  [FAIL] active_time not set after first screenshot")

    total += 1
    old_time = sens.get_last_screen_active_time()
    time.sleep(0.1)
    changed2 = sens._screen_changed(thumb1.copy())
    if not changed2:
        print(f"  [PASS] Same image: no change detected")
        passed += 1
    else:
        print(f"  [FAIL] Same image should not be detected as changed")

    total += 1
    img2 = Image.new("RGB", (200, 150), color=(255, 0, 0))
    thumb2 = img2.copy()
    changed3 = sens._screen_changed(thumb2)
    if changed3:
        with sens._lock:
            sens._last_screen_active_time = datetime.now()
        new_time = sens.get_last_screen_active_time()
        if new_time > old_time:
            print(f"  [PASS] Different image: change detected, active_time updated")
            passed += 1
        else:
            print(f"  [FAIL] active_time not updated after change")
    else:
        print(f"  [FAIL] Different image should be detected as changed")

    print(f"\n  Sensor: {passed}/{total} passed")
    assert passed == total


def test_screenshot_interval_min():
    """user_settings clamps screenshot interval below floor (currently 5s)."""
    import json
    import tempfile
    import user_settings
    from flask import Flask, g

    app = Flask(__name__)
    with app.app_context(), tempfile.TemporaryDirectory() as user_dir:
        g.user_data_dir = user_dir
        cfg_path = f"{user_dir}/user_settings.json"

        # Below-floor value should be clamped on read
        with open(cfg_path, "w") as f:
            json.dump({"auto_screenshot_interval": 1}, f)
        val = user_settings.get()["auto_screenshot_interval"]
        assert val == 5, f"Expected 5 (clamped), got {val}"

        # update() also clamps
        user_settings.update({"auto_screenshot_interval": 2})
        with open(cfg_path) as f:
            saved = json.load(f)
        assert saved["auto_screenshot_interval"] == 5

    print("TEST: screenshot interval enforces 5s floor — PASS")


if __name__ == "__main__":
    test_screen_sensor_active_time()
    test_screenshot_interval_min()
