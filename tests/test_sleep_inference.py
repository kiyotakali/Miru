"""Tests for sleep_inference.py — realtime and historical activity detection.

All tests are offline: they mock storage and screen_analyzer so no real
files are read.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class SleepInferenceTests:
    def __init__(self):
        self.passed = 0
        self.total = 0

    def check(self, cond, pass_msg, fail_msg):
        self.total += 1
        if cond:
            print(f"  [PASS] {pass_msg}")
            self.passed += 1
        else:
            print(f"  [FAIL] {fail_msg}")

    # ---------------------------------------------------------------
    # Hard quiet floor
    # ---------------------------------------------------------------

    def test_hard_floor_at_3am(self):
        print("\nTEST: hard floor asleep at 3 AM")
        import sleep_inference
        with patch("sleep_inference.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 14, 3, 0, 0)
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(state["is_asleep"] and "hard quiet" in state["reason"],
                   "asleep via hard floor at 3 AM",
                   f"got {state}")

    def test_hard_floor_boundary_2am(self):
        print("\nTEST: hard floor starts at 2 AM")
        import sleep_inference
        with patch("sleep_inference.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 14, 2, 0, 0)
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(state["is_asleep"], "asleep at 2:00 AM", f"got {state}")

    def test_hard_floor_boundary_5am(self):
        """5:00 AM is exclusive end — should NOT be in hard floor."""
        print("\nTEST: hard floor ends at 5 AM (exclusive)")
        import sleep_inference
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = datetime(2026, 4, 14, 5, 0, 0)
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        # 5 AM is outside hard floor BUT inside night window (10 end).
        # With no activity in 4h → night_window + no_activity → asleep.
        # That's expected behavior, just not via hard floor.
        self.check(state["is_asleep"] and "hard quiet" not in state["reason"],
                   "5 AM asleep via night window (not hard floor)",
                   f"got {state}")

    # ---------------------------------------------------------------
    # Night-window + gap logic
    # ---------------------------------------------------------------

    def test_awake_with_recent_activity_at_11pm(self):
        """Night owl coding at 11 PM with recent screenshot → awake."""
        print("\nTEST: night owl at 11 PM stays awake")
        import sleep_inference
        now = datetime(2026, 4, 14, 23, 30, 0)
        recent = now - timedelta(minutes=5)
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[recent]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(not state["is_asleep"],
                   f"awake at 23:30 with 5min-old screenshot ({state})",
                   f"falsely asleep: {state}")

    def test_asleep_with_long_gap_at_11pm(self):
        """No activity for 2+ hours at 11 PM → asleep."""
        print("\nTEST: long gap at 11 PM → asleep")
        import sleep_inference
        now = datetime(2026, 4, 14, 23, 30, 0)
        old = now - timedelta(minutes=120)
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[old]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(state["is_asleep"],
                   f"asleep at 23:30 after 120min gap ({state})",
                   f"falsely awake: {state}")

    def test_awake_at_6am_with_activity(self):
        """Early riser screenshot at 6:00 AM → awake (outside hard floor)."""
        print("\nTEST: early riser at 6 AM with screenshot → awake")
        import sleep_inference
        now = datetime(2026, 4, 14, 6, 15, 0)
        recent = now - timedelta(minutes=5)
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[recent]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(not state["is_asleep"],
                   f"awake at 6:15 AM with recent screenshot ({state})",
                   f"falsely asleep: {state}")

    def test_awake_at_1am_with_activity(self):
        """Night owl at 1 AM actively working → awake (old behavior said quiet)."""
        print("\nTEST: 1 AM coder with recent activity → awake (new behavior)")
        import sleep_inference
        now = datetime(2026, 4, 14, 1, 30, 0)
        recent = now - timedelta(minutes=2)
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[recent]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(not state["is_asleep"],
                   f"awake at 1:30 AM with 2min-old activity ({state})",
                   f"incorrectly flagged asleep: {state}")

    def test_daytime_gap_not_asleep(self):
        """3-hour gap at 2 PM (lunch + meeting) — NOT asleep (outside night)."""
        print("\nTEST: daytime long gap ≠ asleep")
        import sleep_inference
        now = datetime(2026, 4, 14, 14, 0, 0)
        old = now - timedelta(minutes=180)
        with patch("sleep_inference.datetime") as mock_dt, \
             patch("storage.get_screenshot_timestamps", return_value=[old]), \
             patch("storage.get_chat_history", return_value=[]):
            mock_dt.now.return_value = now
            mock_dt.strptime = datetime.strptime
            state = sleep_inference.infer_sleep_state()
        self.check(not state["is_asleep"],
                   f"awake at 2 PM despite 3h gap ({state})",
                   f"incorrectly asleep during lunch gap: {state}")

    # ---------------------------------------------------------------
    # Multi-source merge
    # ---------------------------------------------------------------

    def test_merges_chat_and_screenshot(self):
        """infer_daily_activity should merge both sources."""
        print("\nTEST: daily activity merges chat + screenshot")
        import sleep_inference
        day = datetime(2026, 4, 14)
        chat_msgs = [
            {"role": "user", "time": "2026-04-14 12:00:00", "text": "hi"},
            {"role": "user", "time": "2026-04-14 22:00:00", "text": "bye"},
        ]
        screenshots = [
            datetime(2026, 4, 14, 8, 15, 0),   # earlier than any chat
            datetime(2026, 4, 14, 18, 30, 0),
        ]
        with patch("storage.get_chat_history", return_value=chat_msgs), \
             patch("storage.get_screenshot_timestamps", return_value=screenshots):
            daily = sleep_inference.infer_daily_activity("2026-04-14")
        # earliest should be 08:15 (from screenshot), not 12:00 (first chat)
        self.check(daily["earliest"] == "08:15" and daily["latest"] == "22:00",
                   f"merged earliest={daily['earliest']}, latest={daily['latest']}",
                   f"expected 08:15/22:00, got {daily}")

    def test_late_night_not_mixed_with_daytime(self):
        """Times before 05:00 are 'late_night', not 'earliest'."""
        print("\nTEST: late-night times stay separate from earliest")
        import sleep_inference
        screenshots = [
            datetime(2026, 4, 14, 2, 30, 0),   # late night
            datetime(2026, 4, 14, 9, 0, 0),    # morning (should be earliest)
            datetime(2026, 4, 14, 22, 0, 0),   # evening (should be latest)
        ]
        with patch("storage.get_chat_history", return_value=[]), \
             patch("storage.get_screenshot_timestamps", return_value=screenshots):
            daily = sleep_inference.infer_daily_activity("2026-04-14")
        self.check(daily["earliest"] == "09:00"
                   and daily["latest"] == "22:00"
                   and daily["late_night"] == "02:30",
                   f"correct split: earliest={daily['earliest']}, "
                   f"latest={daily['latest']}, late_night={daily['late_night']}",
                   f"wrong split: {daily}")

    def test_empty_day_returns_nones(self):
        print("\nTEST: empty day returns None fields")
        import sleep_inference
        with patch("storage.get_chat_history", return_value=[]), \
             patch("storage.get_screenshot_timestamps", return_value=[]):
            daily = sleep_inference.infer_daily_activity("2026-04-14")
        self.check(
            daily["earliest"] is None and daily["latest"] is None
            and daily["late_night"] is None and daily["activity_count"] == 0,
            "all-None for empty day",
            f"got {daily}",
        )

    # ---------------------------------------------------------------
    # User isolation (storage hooks)
    # ---------------------------------------------------------------

    def test_append_no_context_silent(self):
        """append_screenshot_log should silently no-op without user context."""
        print("\nTEST: append without user context is silent")
        import storage
        # get_data_dir raises when no Flask g — should catch and return
        with patch("storage.get_data_dir", side_effect=Exception("no context")):
            # Should not raise
            try:
                storage.append_screenshot_log("test-device")
                ok = True
            except Exception:
                ok = False
        self.check(ok, "silent no-op without context", "raised unexpectedly")

    def run_all(self):
        print("\n" + "=" * 60)
        print("SUITE: Sleep Inference")
        print("=" * 60)
        self.test_hard_floor_at_3am()
        self.test_hard_floor_boundary_2am()
        self.test_hard_floor_boundary_5am()
        self.test_awake_with_recent_activity_at_11pm()
        self.test_asleep_with_long_gap_at_11pm()
        self.test_awake_at_6am_with_activity()
        self.test_awake_at_1am_with_activity()
        self.test_daytime_gap_not_asleep()
        self.test_merges_chat_and_screenshot()
        self.test_late_night_not_mixed_with_daytime()
        self.test_empty_day_returns_nones()
        self.test_append_no_context_silent()
        print(f"\n  Sleep Inference: {self.passed}/{self.total} passed")
        return self.passed, self.total


def main():
    tests = SleepInferenceTests()
    p, t = tests.run_all()
    print("\n" + "=" * 60)
    print(f"TOTAL: {p}/{t} passed")
    print("=" * 60)
    if p < t:
        sys.exit(1)


if __name__ == "__main__":
    main()
