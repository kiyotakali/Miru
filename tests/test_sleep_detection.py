"""Tests for sleep/idle detection: nighttime quiet period, OS idle, screen stale.

All tests are offline — mock system calls, use controlled timestamps.
"""
import os
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class SleepDetectionTests:
    """Test CareEngine nighttime suppression and enhanced presence detection."""

    def __init__(self):
        self.passed = 0
        self.total = 0

    def check(self, condition, pass_msg, fail_msg):
        self.total += 1
        if condition:
            print(f"  [PASS] {pass_msg}")
            self.passed += 1
        else:
            print(f"  [FAIL] {fail_msg}")

    # ---------------------------------------------------------------
    # Nighttime Quiet Period
    # ---------------------------------------------------------------

    def test_hard_quiet_floor_blocks_at_3am(self):
        """sleep_inference hard floor (02-05) should always mark user asleep."""
        print("\nTEST: hard quiet floor blocks at 3 AM")
        from unittest.mock import patch
        from datetime import datetime as _dt
        with patch("sleep_inference.datetime") as mock_dt:
            mock_dt.now.return_value = _dt(2026, 3, 31, 3, 0, 0)
            mock_dt.strptime = _dt.strptime
            import sleep_inference
            state = sleep_inference.infer_sleep_state()
        self.check(state["is_asleep"], "asleep at 3 AM (hard floor)",
                   f"not asleep: {state.get('reason')}")

    def test_care_skipped_when_offline(self):
        """CareEngine.check() should skip when activity state is offline."""
        print("\nTEST: care skipped when activity state is offline")
        from unittest.mock import patch
        from care_engine import CareEngine
        engine = CareEngine(user_id="_admin")
        engine._last_sent_time = 0
        sent = []
        engine._on_should_care = lambda intent: sent.append(intent)
        with patch("sleep_inference.infer_activity_state",
                   return_value={"state": "offline", "minutes_idle": None,
                                 "last_activity_at": None, "typical_window": None,
                                 "in_active_window": None, "minutes_past_typical_end": 0,
                                 "reason": "no activity in 4h, no pattern data"}):
            engine.check()
        self.check(len(sent) == 0, "no care sent while offline",
                   f"sent {len(sent)} messages while offline")

    def test_care_proceeds_when_active(self):
        """CareEngine.check() should proceed when activity state is active."""
        print("\nTEST: care proceeds when activity state is active")
        from unittest.mock import patch
        from care_engine import CareEngine
        engine = CareEngine(user_id="_admin")
        engine._last_sent_time = 0
        with patch("sleep_inference.infer_activity_state",
                   return_value={"state": "active", "minutes_idle": 5,
                                 "last_activity_at": "2026-04-16 10:00:00",
                                 "typical_window": None,
                                 "in_active_window": None, "minutes_past_typical_end": 0,
                                 "reason": "active 5min ago"}), \
             patch("care_engine.CareEngine._user_is_present", return_value=True), \
             patch("care_engine.CareEngine._gather_memory_context", return_value="ctx"), \
             patch("care_engine.CareEngine._gather_current_state", return_value="state"), \
             patch("care_engine.call_care_eval",
                   return_value={"should_send": False, "activity_mode": "idle",
                                 "internal_note": "", "reason": "nothing urgent"}):
            engine.check()
        self.check(True, "check() proceeded past activity gate", "activity gate blocked")

    def test_no_hardcoded_quiet_hours(self):
        """Old QUIET_HOUR/HARD_QUIET constants should be removed."""
        print("\nTEST: no hardcoded quiet hour constants")
        import care_engine, sleep_inference
        has_old = (hasattr(care_engine, "QUIET_HOUR_START") or
                   hasattr(sleep_inference, "HARD_QUIET_START") or
                   hasattr(sleep_inference, "NIGHT_START_HOUR"))
        self.check(not has_old,
                   "old hardcoded hour constants removed",
                   "found leftover hardcoded hour constants")

    # ---------------------------------------------------------------
    # OS-level Idle Detection
    # ---------------------------------------------------------------

    def test_os_idle_overrides_screen_activity(self):
        """If OS reports idle > 30min and no device active, user is NOT present."""
        print("\nTEST: OS idle overrides screen activity")
        from care_engine import CareEngine
        engine = CareEngine()

        # Mock: OS idle 45 minutes, no device active
        with patch("sensor.get_system_idle_seconds", return_value=2700), \
             patch("device_manager.is_any_device_active", return_value=False):
            present = engine._user_is_present()

        self.check(not present, "user NOT present (OS idle + no device active)",
                   "user falsely detected as present")

    def test_os_idle_short_allows_presence(self):
        """If OS reports idle < 30min, device activity should determine presence."""
        print("\nTEST: OS idle < 30min allows device-based presence")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", return_value=300), \
             patch("device_manager.is_any_device_active", return_value=True):
            present = engine._user_is_present()

        self.check(present, "user present (OS idle 5min + device active)",
                   "user falsely detected as away")

    def test_os_idle_unavailable_falls_through(self):
        """If OS idle returns None (unsupported platform), fall through to device check."""
        print("\nTEST: OS idle unavailable falls through to device check")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", return_value=None), \
             patch("device_manager.is_any_device_active", return_value=True):
            present = engine._user_is_present()

        self.check(present, "user present (OS idle unavailable, device active)",
                   "user falsely away when OS idle unavailable")

    def test_os_idle_exception_falls_through(self):
        """If OS idle raises exception, fall through gracefully."""
        print("\nTEST: OS idle exception falls through")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", side_effect=RuntimeError("no ioreg")), \
             patch("device_manager.is_any_device_active", return_value=True):
            present = engine._user_is_present()

        self.check(present, "graceful fallback on OS idle error",
                   "crashed on OS idle error")

    # ---------------------------------------------------------------
    # Screen Stale Detection
    # ---------------------------------------------------------------

    def test_no_device_active_not_present(self):
        """If no device has been active for 30+ minutes, user is not present."""
        print("\nTEST: no device active 30+ min → not present")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", return_value=None), \
             patch("device_manager.is_any_device_active", return_value=False), \
             patch("miru_emotion.get_instance") as mock_emo:
            mock_emo.return_value.get_hours_since_interaction.return_value = 2.0
            present = engine._user_is_present()

        self.check(not present, "not present (no device active, no recent chat)",
                   "falsely present")

    def test_chat_message_keeps_presence(self):
        """Recent chat message should indicate presence even if no device active."""
        print("\nTEST: recent chat message keeps presence")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", return_value=None), \
             patch("device_manager.is_any_device_active", return_value=False), \
             patch("miru_emotion.get_instance") as mock_emo:
            mock_emo.return_value.get_hours_since_interaction.return_value = 0.1  # 6 min
            present = engine._user_is_present()

        self.check(present, "present (recent chat overrides no device activity)",
                   "falsely away despite recent chat")

    def test_all_signals_expired_not_present(self):
        """No device active, no chat, no OS signal → not present."""
        print("\nTEST: all signals expired → not present")
        from care_engine import CareEngine
        engine = CareEngine()

        with patch("sensor.get_system_idle_seconds", return_value=None), \
             patch("device_manager.is_any_device_active", return_value=False), \
             patch("miru_emotion.get_instance") as mock_emo:
            mock_emo.return_value.get_hours_since_interaction.return_value = 3.0
            present = engine._user_is_present()

        self.check(not present, "not present (all signals expired)",
                   "falsely present with expired signals")

    # ---------------------------------------------------------------
    # Combined Scenario: The Sleep Bug
    # ---------------------------------------------------------------

    def test_offline_scenario_no_messages_sent(self):
        """Simulate offline scenario: activity state blocks care."""
        print("\nTEST: full offline scenario — no messages while offline")
        from unittest.mock import patch
        from care_engine import CareEngine
        engine = CareEngine(user_id="_admin")
        engine._last_sent_time = 0
        sent = []
        engine._on_should_care = lambda intent: sent.append(intent)

        with patch("sleep_inference.infer_activity_state",
                   return_value={"state": "offline", "minutes_idle": None,
                                 "last_activity_at": None, "typical_window": None,
                                 "in_active_window": None, "minutes_past_typical_end": 0,
                                 "reason": "mock offline"}):
            for _ in range(3):
                engine.check()

        self.check(len(sent) == 0, "0 messages during offline",
                   f"{len(sent)} messages sent during offline")

    # ---------------------------------------------------------------
    # get_system_idle_seconds platform dispatch
    # ---------------------------------------------------------------

    def test_idle_macos_parsing(self):
        """_get_idle_macos should parse ioreg HIDIdleTime correctly."""
        print("\nTEST: macOS idle time parsing")
        from sensor import _get_idle_macos
        # Mock ioreg output with known idle time (5 seconds = 5,000,000,000 ns)
        mock_output = '    |   "HIDIdleTime" = 5000000000\n'
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = mock_output
            mock_run.return_value = mock_result
            idle = _get_idle_macos()

        self.check(idle is not None and abs(idle - 5.0) < 0.01,
                   f"parsed 5s idle correctly (got {idle})",
                   f"wrong idle time: {idle}")

    def test_idle_macos_no_match(self):
        """_get_idle_macos should return None if HIDIdleTime not found."""
        print("\nTEST: macOS idle time not found → None")
        from sensor import _get_idle_macos
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "no idle info here\n"
            mock_run.return_value = mock_result
            idle = _get_idle_macos()

        self.check(idle is None, "returned None when no HIDIdleTime", f"returned {idle}")

    def test_idle_dispatch_platform(self):
        """get_system_idle_seconds should dispatch based on platform."""
        print("\nTEST: idle dispatch by platform")
        from sensor import get_system_idle_seconds

        # Mock as macOS
        with patch("sys.platform", "darwin"), \
             patch("sensor._get_idle_macos", return_value=42.0):
            result = get_system_idle_seconds()
            self.check(result == 42.0, "macOS dispatch works", f"got {result}")

        # Mock as Windows
        with patch("sys.platform", "win32"), \
             patch("sensor._get_idle_windows", return_value=99.0):
            result = get_system_idle_seconds()
            self.check(result == 99.0, "Windows dispatch works", f"got {result}")

        # Mock as Linux (unsupported)
        with patch("sys.platform", "linux"):
            result = get_system_idle_seconds()
            self.check(result is None, "Linux returns None", f"got {result}")

    # ---------------------------------------------------------------
    # (Legacy nighttime absence-emotion suppression tests removed
    #  on 2026-05-08 — _check_absence was deleted from CareEngine v3.
    #  Miru now feels "absence" by reading time-since-last-user-msg
    #  directly from the LLM evaluation context. See
    #  core._evaluate_miru_emotion + memory_prompts.call_miru_emotion_eval.)
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # Integration: imports work
    # ---------------------------------------------------------------

    def test_care_engine_imports_idle(self):
        """CareEngine._user_is_present should import get_system_idle_seconds."""
        print("\nTEST: CareEngine imports idle detection")
        import inspect
        from care_engine import CareEngine
        source = inspect.getsource(CareEngine._user_is_present)
        self.check("get_system_idle_seconds" in source,
                   "imports get_system_idle_seconds",
                   "missing idle detection import")

    def test_care_engine_uses_activity_state(self):
        """CareEngine.check should use three-state activity inference."""
        print("\nTEST: CareEngine.check uses infer_activity_state")
        import inspect
        from care_engine import CareEngine
        source = inspect.getsource(CareEngine.check)
        self.check("infer_activity_state" in source,
                   "check() calls infer_activity_state",
                   "check() missing activity state inference")

    def run_all(self):
        print("\n" + "=" * 60)
        print("SUITE: Sleep/Idle Detection")
        print("=" * 60)

        # Three-state activity inference (replaces old hardcoded quiet hours)
        self.test_hard_quiet_floor_blocks_at_3am()
        self.test_care_skipped_when_offline()
        self.test_care_proceeds_when_active()
        self.test_no_hardcoded_quiet_hours()

        # OS idle
        self.test_os_idle_overrides_screen_activity()
        self.test_os_idle_short_allows_presence()
        self.test_os_idle_unavailable_falls_through()
        self.test_os_idle_exception_falls_through()

        # Screen stale
        self.test_no_device_active_not_present()
        self.test_chat_message_keeps_presence()
        self.test_all_signals_expired_not_present()

        # Combined scenario
        self.test_offline_scenario_no_messages_sent()

        # Platform dispatch
        self.test_idle_macos_parsing()
        self.test_idle_macos_no_match()
        self.test_idle_dispatch_platform()

        # (Absence emotion suppression tests removed 2026-05-08.)

        # Integration
        self.test_care_engine_imports_idle()
        self.test_care_engine_uses_activity_state()

        print(f"\n  Sleep Detection: {self.passed}/{self.total} passed")
        return self.passed, self.total


def main():
    tests = SleepDetectionTests()
    p, t = tests.run_all()

    print("\n" + "=" * 60)
    print(f"TOTAL: {p}/{t} passed")
    print("=" * 60)

    if p < t:
        sys.exit(1)


if __name__ == "__main__":
    main()
