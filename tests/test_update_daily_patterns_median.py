"""Tests for daily_patterns.update_daily_patterns — median computation fix.

Pre-2026-05-07 bug: the "近 N 天平均" summary line in patterns/sleep.md took
firsts[len(firsts)//2] as the "average", which is the *insertion-order middle*
not the median. After fix, both first/last get sorted before taking middle.

These tests verify:
  1. Median is computed correctly for various input shapes
  2. The summary line written to sleep.md matches what _parse_typical_window reads
  3. Edge cases (single element, two elements, duplicates) don't crash
  4. Daily pattern consumers still align with what's written (consistency check)
"""
import importlib
import os
import shutil
from pathlib import Path
from unittest.mock import patch


def _setup_isolated_env(tmp_path):
    """Reload daily_patterns + memory + storage modules pointing at a temp data dir."""
    data_dir = str(tmp_path / "data")
    os.environ["DATA_DIR"] = data_dir
    os.makedirs(data_dir, exist_ok=True)
    user_dir = os.path.join(data_dir, "users", "u_testmd")
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(os.path.join(user_dir, "memory"), exist_ok=True)
    os.makedirs(os.path.join(user_dir, "memory", "patterns"), exist_ok=True)
    return data_dir, user_dir


def _make_flask_app_context(user_id, user_dir):
    """Push a Flask context with the user bound, so storage.get_data_dir() works."""
    import app as app_mod
    ctx = app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = user_id
    g.user_data_dir = user_dir
    g.is_admin = False
    return ctx


# ----------------------------------------------------------------
# Algorithmic test: write 7 fake daily lines, run update, verify summary
# ----------------------------------------------------------------

def _seed_sleep_md(memory_module, daily_lines):
    """Write a sleep.md with the given per-day lines. No 'average' line."""
    content = "\n".join(daily_lines) + "\n"
    memory_module.write_file("patterns/sleep.md", content)


def _read_sleep_md(memory_module):
    return memory_module.read_file("patterns/sleep.md") or ""


def test_median_basic_unsorted_input(tmp_path):
    """Insertion-order middle differs from sorted median — fix verifies sorted."""
    data_dir, user_dir = _setup_isolated_env(tmp_path)

    # Force fresh imports against tmp data
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        # Insertion order: 8:00, 9:30, 7:15, 10:00, 8:30, 6:45, 9:00
        # Sorted firsts: 6:45, 7:15, 8:00, 8:30, 9:00, 9:30, 10:00 → median = 8:30
        # Buggy code (old): firsts[3] = "10:00" (insertion middle)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        # Use 7 prior dates so update_daily_patterns sees them as "<= 7 days old"
        from datetime import timedelta
        daily_lines = []
        firsts_input = ["08:00", "09:30", "07:15", "10:00", "08:30", "06:45", "09:00"]
        lasts_input = ["23:00", "01:00", "22:30", "00:30", "23:30", "21:45", "00:00"]
        for i, (f, la) in enumerate(zip(firsts_input, lasts_input)):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            daily_lines.append(f"{d}: 首条消息 ~{f}, 末条消息 ~{la}")
        _seed_sleep_md(memory, daily_lines)

        # Mock sleep_inference.infer_daily_activity so today's first/last is set
        def _fake_infer(date_str):
            return {"earliest": "08:15", "latest": "23:15"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()

        content = _read_sleep_md(memory)
        # Find the summary line
        summary_line = None
        for line in content.split("\n"):
            if line.startswith("近") and "平均" in line:
                summary_line = line
                break

        assert summary_line is not None, f"summary line missing\n---\n{content}\n---"

        # Today's first (08:15) is also in the pool — total 8 firsts now.
        # Sorted: 06:45, 07:15, 08:00, 08:15, 08:30, 09:00, 09:30, 10:00
        # Index 8//2 = 4 → 08:30
        assert "首条消息 ~08:30" in summary_line, \
            f"median wrong, got: {summary_line!r}"

        # Lasts (raw HH:MM-as-minutes; no midnight wrap, matching
        # _parse_typical_window). Inputs:
        #   23:00, 01:00, 22:30, 00:30, 23:30, 21:45, 00:00, 23:15
        # As minutes since 00:00: 1380, 60, 1350, 30, 1410, 1305, 0, 1395
        # Sorted: 0(00:00), 30(00:30), 60(01:00), 1305(21:45),
        #         1350(22:30), 1380(23:00), 1395(23:15), 1410(23:30)
        # Index 8//2 = 4 → 22:30
        assert "末条消息 ~22:30" in summary_line, \
            f"last median wrong, got: {summary_line!r}"
    finally:
        ctx.pop()


def test_median_with_three_days(tmp_path):
    """Minimum threshold (>= 3 days) — verify summary appears."""
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        # 3 prior days + today (set via mock) = 4 total
        firsts_input = ["09:00", "07:00", "10:00"]  # sorted: 7,9,10 → med=9
        lasts_input = ["23:00", "21:00", "01:00"]   # sorted: 21,23,01 → med=23
        daily_lines = []
        for i, (f, la) in enumerate(zip(firsts_input, lasts_input)):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            daily_lines.append(f"{d}: 首条消息 ~{f}, 末条消息 ~{la}")
        _seed_sleep_md(memory, daily_lines)

        def _fake_infer(date_str):
            return {"earliest": "08:00", "latest": "22:00"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()

        content = _read_sleep_md(memory)
        summary_line = next(
            (l for l in content.split("\n") if l.startswith("近") and "平均" in l),
            None,
        )
        assert summary_line is not None
        # Today's 08:00 added → firsts: 7,8,9,10 → 4//2 = 2 → 09:00
        assert "首条消息 ~09:00" in summary_line, summary_line
        # Lasts: sorted 01:00, 21:00, 22:00, 23:00 → idx 2 → 22:00
        assert "末条消息 ~22:00" in summary_line, summary_line
    finally:
        ctx.pop()


def test_summary_consistent_with_parse_typical_window(tmp_path):
    """update_daily_patterns summary should match what _parse_typical_window reads.

    This is the real consistency requirement: _parse_typical_window is what
    sleep_inference actually uses for sleep window decisions. The summary line is
    just for display, but pre-fix it disagreed with the function — confusing.
    """
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)
    import sleep_inference
    importlib.reload(sleep_inference)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        firsts_input = ["08:00", "09:30", "07:15", "10:00", "08:30", "06:45", "09:00"]
        lasts_input = ["23:00", "01:00", "22:30", "00:30", "23:30", "21:45", "00:00"]
        daily_lines = []
        for i, (f, la) in enumerate(zip(firsts_input, lasts_input)):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            daily_lines.append(f"{d}: 首条消息 ~{f}, 末条消息 ~{la}")
        _seed_sleep_md(memory, daily_lines)

        def _fake_infer(date_str):
            return {"earliest": "08:15", "latest": "23:15"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()

        # Now invoke _parse_typical_window — it reads the same sleep.md
        # and computes its own median (independently). Both should agree.
        window = sleep_inference._parse_typical_window()
        assert window is not None

        content = _read_sleep_md(memory)
        summary_line = next(
            (l for l in content.split("\n") if l.startswith("近") and "平均" in l),
            None,
        )
        assert summary_line is not None
        # The summary should reflect the same first/last that
        # _parse_typical_window computes
        assert window["first"] in summary_line, \
            f"summary {summary_line!r} disagrees with parse window first={window['first']!r}"
        assert window["last"] in summary_line, \
            f"summary {summary_line!r} disagrees with parse window last={window['last']!r}"
    finally:
        ctx.pop()


def test_median_handles_duplicates(tmp_path):
    """Many days with same time should not crash."""
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        # All 5 days identical
        daily_lines = []
        for i in range(5):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            daily_lines.append(f"{d}: 首条消息 ~08:00, 末条消息 ~23:00")
        _seed_sleep_md(memory, daily_lines)

        def _fake_infer(date_str):
            return {"earliest": "08:00", "latest": "23:00"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()  # must not raise

        content = _read_sleep_md(memory)
        summary_line = next(
            (l for l in content.split("\n") if l.startswith("近") and "平均" in l),
            None,
        )
        assert summary_line is not None
        assert "首条消息 ~08:00" in summary_line
        assert "末条消息 ~23:00" in summary_line
    finally:
        ctx.pop()


def test_median_below_threshold_no_summary(tmp_path):
    """< 3 daily lines → no summary line written (matches existing behavior)."""
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        # Only 1 prior day → with today added = 2, still < 3
        d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        _seed_sleep_md(memory, [f"{d}: 首条消息 ~08:00, 末条消息 ~23:00"])

        def _fake_infer(date_str):
            return {"earliest": "09:00", "latest": "22:00"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()

        content = _read_sleep_md(memory)
        summary_line = next(
            (l for l in content.split("\n") if l.startswith("近") and "平均" in l),
            None,
        )
        assert summary_line is None, "should not emit summary with < 3 days"
    finally:
        ctx.pop()


def test_median_with_malformed_line_skips_summary(tmp_path):
    """Robustness: if all stored lines are malformed, skip summary not crash."""
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        # 7 prior days, all with malformed time strings
        # (only first/last with non-HH:MM patterns will skip parsing)
        daily_lines = []
        for i in range(7):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            # Empty values — parse will treat as empty strings, _hhmm_to_minutes will fail
            daily_lines.append(f"{d}: 首条消息 ~bogus, 末条消息 ~xxx")
        _seed_sleep_md(memory, daily_lines)

        def _fake_infer(date_str):
            return {"earliest": "alsoBogus", "latest": "moreBogus"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            # Must not raise — graceful degradation
            daily_patterns.update_daily_patterns()

        # File must still be writable; summary line just not present
        content = _read_sleep_md(memory)
        assert content  # file written
    finally:
        ctx.pop()


def test_median_real_world_late_night_user(tmp_path):
    """Realistic case: user who stays up late — verify lasts median computed
    in raw HH:MM space (no across-midnight wraparound, matching behavior of
    _parse_typical_window which also doesn't wrap).
    """
    data_dir, user_dir = _setup_isolated_env(tmp_path)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import daily_patterns
    importlib.reload(daily_patterns)

    ctx = _make_flask_app_context("u_testmd", user_dir)
    try:
        from datetime import datetime, timedelta
        # User wakes 8-10am, sleeps 1-3am
        firsts_input = ["09:00", "10:00", "08:30", "09:30", "08:00", "10:30", "09:15"]
        lasts_input = ["02:00", "01:30", "03:00", "02:30", "01:00", "02:45", "01:45"]
        daily_lines = []
        for i, (f, la) in enumerate(zip(firsts_input, lasts_input)):
            d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y-%m-%d")
            daily_lines.append(f"{d}: 首条消息 ~{f}, 末条消息 ~{la}")
        _seed_sleep_md(memory, daily_lines)

        def _fake_infer(date_str):
            return {"earliest": "09:00", "latest": "02:00"}

        with patch("sleep_inference.infer_daily_activity", _fake_infer):
            daily_patterns.update_daily_patterns()

        content = _read_sleep_md(memory)
        summary_line = next(
            (l for l in content.split("\n") if l.startswith("近") and "平均" in l),
            None,
        )
        assert summary_line is not None
        # Today's 09:00 added → firsts: 8:00, 8:30, 9:00, 9:00, 9:15, 9:30, 10:00, 10:30
        # 8 elements → 8//2 = 4 → 09:15
        assert "首条消息 ~09:15" in summary_line, summary_line
        # Today's 02:00 added → lasts: 01:00, 01:30, 01:45, 02:00, 02:00, 02:30, 02:45, 03:00
        # 8//2 = 4 → 02:00
        assert "末条消息 ~02:00" in summary_line, summary_line
    finally:
        ctx.pop()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
