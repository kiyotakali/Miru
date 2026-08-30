"""Activity state inference from multi-device signals.

Signal-driven activity detection — no hardcoded time-of-day assumptions.
Uses real activity signals (screenshot_log + chat_history) combined with
the user's historical patterns (patterns/sleep.md) to determine state.

Three states:
  active  — device in use (recent screenshot/chat within ACTIVE_THRESHOLD)
  idle    — no recent activity but within user's typical active window
  offline — extended inactivity, likely sleeping or away

Signal sources (priority order):
  1. screenshot_log.json  — persistent, only logged when pixel-change detected
  2. chat_history.json    — user messages (role == "user")
  3. screen_analyzer._last_observation  — in-memory fallback for "right now"

All three are per-user isolated (via storage.get_data_dir()).
Time convention: NAIVE datetime (server local tz).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


# === Tunables ===

# Recent activity within this window → "active" (device in use)
ACTIVE_THRESHOLD_MINUTES = 30

# No activity for this long while within active window → "idle" (away briefly)
# Beyond this while outside active window → "offline"
IDLE_THRESHOLD_MINUTES = 60

# Fallback when no pattern data: idle beyond this → "offline"
NO_PATTERN_OFFLINE_MINUTES = 180

# When computing infer_daily_activity, times before this hour count as
# "late-night extension of previous day", not "earliest of this day".
EARLIEST_CUTOFF_HOUR = 5


# === Internal: signal collection ===

def _collect_timestamps(since: datetime, until: datetime) -> list[datetime]:
    """Merge activity timestamps from all sources inside [since, until).

    Returns a sorted + deduplicated list of naive datetimes.
    All devices are included — no device filtering.
    """
    ts: list[datetime] = []

    # Source 1: persistent screenshot log (authoritative for device activity)
    try:
        import storage
        start_key = since.strftime("%Y-%m-%d")
        end_key = until.strftime("%Y-%m-%d")
        for t in storage.get_screenshot_timestamps(start_key, end_key):
            if since <= t < until:
                ts.append(t)
    except Exception as e:
        print(f"[ActivityInfer] screenshot_log read failed: {e}")

    # Source 2: chat history (user messages only) — date-range query so we
    # never miss messages to an arbitrary recency limit.
    try:
        import storage
        start_key = since.strftime("%Y-%m-%d")
        end_key = until.strftime("%Y-%m-%d")
        history = storage.get_chat_messages_in_range(start_key, end_key)
        for m in history:
            if m.get("role") != "user":
                continue
            raw = m.get("time", "")
            if not raw or len(raw) < 16:
                continue
            try:
                t = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if since <= t < until:
                ts.append(t)
    except Exception as e:
        print(f"[ActivityInfer] chat_history read failed: {e}")

    # Source 3: in-memory ScreenAnalyzer fallback (covers the "just now"
    # gap where the most recent screenshot may not yet have been flushed
    # to storage — only relevant for realtime checks).
    try:
        from screen_analyzer import get_analyzer
        analyzer = get_analyzer()
        with analyzer._lock:
            for _dev, (_text, dt) in analyzer._last_observation.items():
                if since <= dt < until:
                    ts.append(dt)
    except Exception:
        pass

    # Dedup by exact timestamp + sort
    ts = sorted(set(ts))
    return ts


# === Pattern analysis ===

def _parse_typical_window() -> dict | None:
    """Derive user's typical active window from patterns/sleep.md.

    Returns {"first": "HH:MM", "last": "HH:MM", "first_minutes": int,
             "last_minutes": int} or None if insufficient data.

    Data format in patterns/sleep.md:
      2026-04-15: 首条消息 ~08:30, 末条消息 ~01:15
      近5天平均: 首条消息 ~08:45, 末条消息 ~01:00
    """
    try:
        import memory
        content = memory.read_file("patterns/sleep.md")
        if not content:
            return None
    except Exception:
        return None

    firsts = []
    lasts = []
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        # Match lines with "首条消息 ~HH:MM" and "末条消息 ~HH:MM"
        m_first = re.search(r"首条消息\s*~?\s*(\d{1,2}:\d{2})", line)
        m_last = re.search(r"末条消息\s*~?\s*(\d{1,2}:\d{2})", line)
        if m_first and m_last:
            # Skip summary lines, only use per-day entries
            if line.startswith("近") or line.startswith("平均"):
                continue
            firsts.append(m_first.group(1))
            lasts.append(m_last.group(1))

    if len(firsts) < 2:
        return None

    def _hhmm_to_minutes(s: str) -> int:
        h, m = map(int, s.split(":"))
        return h * 60 + m

    # Use median for robustness against outliers
    first_mins = sorted([_hhmm_to_minutes(f) for f in firsts])
    last_mins = sorted([_hhmm_to_minutes(l) for l in lasts])
    median_first = first_mins[len(first_mins) // 2]
    median_last = last_mins[len(last_mins) // 2]

    def _minutes_to_hhmm(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    return {
        "first": _minutes_to_hhmm(median_first),
        "last": _minutes_to_hhmm(median_last),
        "first_minutes": median_first,
        "last_minutes": median_last,
    }


def _in_active_window(now: datetime, window: dict) -> bool:
    """Check if `now` falls within the user's typical active window.

    The active window may wrap midnight (e.g., 08:45 → 01:00).
    """
    current_min = now.hour * 60 + now.minute
    first = window["first_minutes"]
    last = window["last_minutes"]

    if first <= last:
        # Simple case: e.g., 06:00 → 22:00
        return first <= current_min <= last
    else:
        # Wraps midnight: e.g., 08:45 → 01:00
        # Active if current >= first OR current <= last
        return current_min >= first or current_min <= last


def _minutes_past_typical_end(now: datetime, window: dict) -> int:
    """How many minutes past the user's typical last-activity time.

    Returns 0 if still within active window, positive if past end.
    """
    current_min = now.hour * 60 + now.minute
    last = window["last_minutes"]

    if _in_active_window(now, window):
        return 0

    # Calculate distance past the end point
    # Handle midnight wrap: if last=60 (01:00) and current=120 (02:00),
    # user is 60 min past.
    diff = current_min - last
    if diff < 0:
        diff += 1440  # wrap around midnight
    return diff


# === Public: realtime activity state ===

def infer_activity_state() -> dict:
    """Determine the user's current device-usage state.

    Returns:
        {
            "state": "active" | "idle" | "offline",
            "minutes_idle": int | None,
            "last_activity_at": str | None,
            "typical_window": {"first": "HH:MM", "last": "HH:MM"} | None,
            "in_active_window": bool | None,
            "minutes_past_typical_end": int,
            "reason": str,
        }

    Multi-device: aggregates screenshot_log + chat from ALL devices.
    No hardcoded time-of-day assumptions.
    """
    now = datetime.now()

    # Collect activity from last 4 hours (all devices)
    since = now - timedelta(hours=4)
    activity = _collect_timestamps(since, now + timedelta(seconds=1))

    # Load user's typical active window from patterns
    window = _parse_typical_window()
    in_window = _in_active_window(now, window) if window else None
    past_end = _minutes_past_typical_end(now, window) if window else 0

    base = {
        "typical_window": {"first": window["first"], "last": window["last"]} if window else None,
        "in_active_window": in_window,
        "minutes_past_typical_end": past_end,
    }

    # --- No activity at all in 4 hours ---
    if not activity:
        if in_window is True:
            # Within active window but no signals → user stepped away
            return {**base, "state": "idle", "minutes_idle": None,
                    "last_activity_at": None,
                    "reason": "no activity in 4h, but within typical active window"}
        elif in_window is False:
            # Outside active window, no signals → likely sleeping/away
            return {**base, "state": "offline", "minutes_idle": None,
                    "last_activity_at": None,
                    "reason": "no activity in 4h, outside typical active window"}
        else:
            # No pattern data, no signals → conservative: offline
            return {**base, "state": "offline", "minutes_idle": None,
                    "last_activity_at": None,
                    "reason": "no activity in 4h, no pattern data"}

    # --- Has activity — check recency ---
    last = activity[-1]
    minutes_idle = int((now - last).total_seconds() / 60)
    last_at = last.strftime("%Y-%m-%d %H:%M:%S")

    # Recent activity → active
    if minutes_idle < ACTIVE_THRESHOLD_MINUTES:
        return {**base, "state": "active", "minutes_idle": minutes_idle,
                "last_activity_at": last_at,
                "reason": f"active {minutes_idle}min ago"}

    # Stale activity — determine idle vs offline
    if minutes_idle >= IDLE_THRESHOLD_MINUTES:
        if in_window is True:
            # Within active window but idle 60+ min → still "idle" (lunch/meeting)
            return {**base, "state": "idle", "minutes_idle": minutes_idle,
                    "last_activity_at": last_at,
                    "reason": f"idle {minutes_idle}min, within active window"}
        elif in_window is False:
            return {**base, "state": "offline", "minutes_idle": minutes_idle,
                    "last_activity_at": last_at,
                    "reason": f"idle {minutes_idle}min, outside active window"}
        else:
            # No pattern data — use longer threshold
            if minutes_idle >= NO_PATTERN_OFFLINE_MINUTES:
                return {**base, "state": "offline", "minutes_idle": minutes_idle,
                        "last_activity_at": last_at,
                        "reason": f"idle {minutes_idle}min ≥ {NO_PATTERN_OFFLINE_MINUTES}min, no pattern"}
            return {**base, "state": "idle", "minutes_idle": minutes_idle,
                    "last_activity_at": last_at,
                    "reason": f"idle {minutes_idle}min, no pattern data (conservative)"}

    # 30-60 min idle → idle regardless
    return {**base, "state": "idle", "minutes_idle": minutes_idle,
            "last_activity_at": last_at,
            "reason": f"idle {minutes_idle}min (short-term away)"}


# === Backward-compat wrapper ===

def infer_sleep_state() -> dict:
    """Backward-compatible wrapper. Maps three-state to is_asleep bool.

    active  → is_asleep: False
    idle    → is_asleep: False
    offline → is_asleep: True
    """
    state = infer_activity_state()
    is_asleep = state["state"] == "offline"
    return {
        "is_asleep": is_asleep,
        "minutes_idle": state.get("minutes_idle"),
        "last_activity_at": state.get("last_activity_at"),
        "reason": state.get("reason", ""),
    }


# === Public: historical per-day stats ===

def infer_daily_activity(date_str: str) -> dict:
    """Earliest / latest activity for a specific day.

    Used by care_engine for pattern updates. Combines chat + screenshot signals.

    Args:
        date_str: 'YYYY-MM-DD' in server-local tz.

    Returns:
        {
            "date": date_str,
            "earliest": "HH:MM" | None,     # first activity at/after 05:00
            "latest":   "HH:MM" | None,     # last activity before midnight
            "late_night": "HH:MM" | None,   # last activity in 00:00-04:59
            "activity_count": int,
        }
    """
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {
            "date": date_str,
            "earliest": None,
            "latest": None,
            "late_night": None,
            "activity_count": 0,
        }
    day_end = day_start + timedelta(days=1)

    activity = _collect_timestamps(day_start, day_end)

    daytime = [t for t in activity if t.hour >= EARLIEST_CUTOFF_HOUR]
    nighttime = [t for t in activity if t.hour < EARLIEST_CUTOFF_HOUR]

    return {
        "date": date_str,
        "earliest": daytime[0].strftime("%H:%M") if daytime else None,
        "latest": daytime[-1].strftime("%H:%M") if daytime else None,
        "late_night": nighttime[-1].strftime("%H:%M") if nighttime else None,
        "activity_count": len(activity),
    }


def infer_range_activity(start_date: str, end_date: str) -> dict:
    """Per-day activity for [start_date, end_date] inclusive.

    Returns {date_str: {earliest, latest, late_night, activity_count}}.
    Only days with any activity are included.
    """
    try:
        d = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {}
    out = {}
    while d <= end:
        key = d.strftime("%Y-%m-%d")
        daily = infer_daily_activity(key)
        if daily["activity_count"] > 0:
            out[key] = daily
        d += timedelta(days=1)
    return out
