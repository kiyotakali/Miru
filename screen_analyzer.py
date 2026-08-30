from __future__ import annotations

"""ScreenAnalyzer — backend-side VLM analysis of screenshots.

Receives JPEG bytes (from local ScreenSensor or remote device POSTs),
runs VLM analysis, deduplicates observations, and routes results to
ScreenSlot Writer and AttentionEngine.

This is the "brain" half of the old ScreenObserver — the "sensor" half
(capture + change detection + POST) is now in sensor.py.
"""

import base64
import re
import threading
import time
from datetime import datetime

from memory_prompts import call_screen_observation


# Config
DEFAULT_VLM_INTERVAL = 10  # seconds between VLM calls per device
MIN_SIGNIFICANCE_FOR_ATTENTION = 2  # sig=2 enters Attention, not memory
MIN_SIGNIFICANCE_FOR_CARE = MIN_SIGNIFICANCE_FOR_ATTENTION  # legacy import alias


class ScreenAnalyzer:
    """Analyzes screenshots via VLM, deduplicates, and routes to downstream agents."""

    def __init__(self):
        self._lock = threading.Lock()
        # Per-device VLM cooldown: {device_id: last_vlm_time}
        self._per_device_vlm_cooldown: dict[str, float] = {}
        self._vlm_interval: int = DEFAULT_VLM_INTERVAL
        # Per-device dedup: {device_id: [recent_obs_texts]}
        self._recent_obs_per_device: dict[str, list[str]] = {}
        # Last observation per device: {device_id: (text, datetime)}
        self._last_observation: dict[str, tuple[str, datetime]] = {}

    def set_vlm_interval(self, interval: int):
        """Update the minimum interval between VLM calls (seconds)."""
        with self._lock:
            self._vlm_interval = max(int(interval), 5)
            print(f"[ScreenAnalyzer] VLM interval updated to {self._vlm_interval}s")

    def get_last_observation(self, device_id: str | None = None) -> tuple[str, datetime | None]:
        """Return (observation_text, timestamp) of the most recent observation.

        If device_id is given, returns that device's last observation.
        Otherwise returns the most recent across all devices.
        """
        with self._lock:
            if device_id and device_id in self._last_observation:
                return self._last_observation[device_id]

            # Find most recent across all devices
            best_text, best_time = "", None
            for text, dt in self._last_observation.values():
                if best_time is None or dt > best_time:
                    best_text, best_time = text, dt
            return best_text, best_time

    def get_all_recent_observations(self, max_age_minutes: float = 10) -> list[dict]:
        """Return recent observations from ALL devices within max_age_minutes.

        Returns list of {device_id, device_name, observation, time} sorted by time desc.
        """
        now = datetime.now()
        results = []
        with self._lock:
            for dev_id, (text, dt) in self._last_observation.items():
                age = (now - dt).total_seconds() / 60
                if age <= max_age_minutes and text:
                    results.append({
                        "device_id": dev_id,
                        "observation": text,
                        "time": dt,
                    })
        # Resolve device names outside lock
        for r in results:
            try:
                from device_manager import get_device_display_name
                r["device_name"] = get_device_display_name(r["device_id"])
            except Exception:
                r["device_name"] = r["device_id"]
        results.sort(key=lambda r: r["time"], reverse=True)
        return results

    def analyze(self, jpeg_bytes: bytes, device_id: str = "local",
                captured_at: str | None = None) -> dict | None:
        """Analyze a screenshot via VLM.

        Returns {observation, significance, device_id} on success, None if
        skipped (cooldown or duplicate).
        *captured_at*: ISO timestamp of when the screenshot was taken (may
        differ from now if uploaded from a queue after reconnect).
        """
        # Persist activity trace BEFORE VLM cooldown — every upload is proof
        # of device activity, regardless of whether VLM analyzes it. This is
        # what sleep_inference uses to detect wake/sleep patterns.
        try:
            import storage
            storage.append_screenshot_log(device_id, captured_at=captured_at)
        except Exception as e:
            print(f"[ScreenAnalyzer] screenshot_log write failed: {e}")

        # Check per-device VLM cooldown
        now = time.time()
        with self._lock:
            last_vlm = self._per_device_vlm_cooldown.get(device_id, 0)
            if now - last_vlm < self._vlm_interval:
                return None
            self._per_device_vlm_cooldown[device_id] = now

        # Get device display name and last observation for VLM context
        device_name = ""
        last_obs_text = ""
        try:
            from device_manager import get_device_display_name
            device_name = get_device_display_name(device_id)
        except Exception:
            pass
        with self._lock:
            if device_id in self._last_observation:
                last_obs_text = self._last_observation[device_id][0]

        # Convert to base64 for VLM
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")

        try:
            result = call_screen_observation(
                b64, "image/jpeg",
                device_name=device_name,
                last_observation=last_obs_text,
            )
            if isinstance(result, tuple):
                observation, significance = result
            else:
                observation, significance = result, 3
        except Exception as e:
            print(f"[ScreenAnalyzer] VLM call failed: {e}")
            with self._lock:
                self._per_device_vlm_cooldown[device_id] = 0
            return None

        if not observation:
            return None

        # Per-device dedup: skip if too similar to THIS device's recent observations
        if self._is_duplicate(observation, device_id):
            print(f"[ScreenAnalyzer] Skipped duplicate [{device_id}]: {observation[:40]}...")
            return None

        # Track for future per-device dedup
        if device_id not in self._recent_obs_per_device:
            self._recent_obs_per_device[device_id] = []
        dev_history = self._recent_obs_per_device[device_id]
        dev_history.append(observation)
        if len(dev_history) > 5:
            self._recent_obs_per_device[device_id] = dev_history[-5:]

        # Parse captured_at into datetime; fall back to now() for live uploads
        if captured_at:
            try:
                obs_time = datetime.fromisoformat(captured_at.replace("T", " ").replace("Z", ""))
            except (ValueError, TypeError):
                obs_time = datetime.now()
        else:
            obs_time = datetime.now()

        # Cache observation
        with self._lock:
            self._last_observation[device_id] = (observation, obs_time)

        # Resolve device display name for downstream context
        device_label = device_name or device_id
        ts = obs_time.isoformat()[:19]

        # Screenshot observation → ScreenSlot Writer (per-screenshot, 2026-05-16).
        #
        # Architecture: each sig>=3 screenshot triggers ScreenSlot Writer to
        # decide slot_writes + commitments + completed_commitments. Routine
        # sig=3 runs on low-cost memory tier; strong/DDL/project-update
        # observations use chat tier without provider reasoning.
        #
        # Routing is async via threading — the HTTP /api/device/screenshot
        # handler returns immediately; the LLM runs in background. Flask
        # context is re-pushed inside the thread.
        #
        # sig 1-2 are silently dropped here (no information value).
        if significance >= 3:
            try:
                import threading as _threading
                import core as _core
                # Capture per-user context for the background thread —
                # screen_analyzer is per-user singleton but the Flask g may
                # have moved on by the time the thread runs.
                try:
                    from flask import g
                    _bg_uid = getattr(g, "user_id", "_admin")
                    _bg_data_dir = getattr(g, "user_data_dir", None)
                except Exception:
                    _bg_uid, _bg_data_dir = "_admin", None
                _threading.Thread(
                    target=_core._process_screen_observation_async,
                    args=(observation, significance,
                          device_id, device_label, ts,
                          _bg_uid, _bg_data_dir),
                    daemon=True,
                ).start()
            except Exception as e:
                print(f"[ScreenAnalyzer] screen slot writer dispatch failed: {e}")

        print(f"[ScreenAnalyzer] Observed [{device_label}] "
              f"(sig={significance}): {observation[:60]}...")

        # Push sig>=2 observations to AttentionEngine.  sig=2 deliberately
        # does not write memory slots; it only changes Miru's inner state.
        try:
            if significance >= MIN_SIGNIFICANCE_FOR_ATTENTION:
                from attention_engine import get_attention_engine
                get_attention_engine().record_signal("screenshot", {
                    "device_id": device_id,
                    "device_name": device_label,
                    "observation": observation,
                    "significance": significance,
                })
        except Exception:
            pass

        return {
            "observation": observation,
            "significance": significance,
            "device_id": device_id,
        }

    def analyze_for_tool(self, jpeg_bytes: bytes) -> dict | None:
        """Analyze screenshot for the look_at_screen tool (bypasses cooldown).

        Returns {observation, significance} or None.
        """
        # Get last observation for context
        last_obs_text = ""
        device_name = ""
        with self._lock:
            if "local" in self._last_observation:
                last_obs_text = self._last_observation["local"][0]
        try:
            from device_manager import get_device_display_name
            device_name = get_device_display_name("local")
        except Exception:
            pass

        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        try:
            result = call_screen_observation(
                b64, "image/jpeg",
                device_name=device_name,
                last_observation=last_obs_text,
            )
            if isinstance(result, tuple):
                observation, significance = result
            else:
                observation, significance = result, 3
        except Exception as e:
            return None

        if not observation:
            return None

        # Update cached observation
        obs_time = datetime.now()
        with self._lock:
            self._last_observation["local"] = (observation, obs_time)
            self._per_device_vlm_cooldown["local"] = time.time()

        # Persist activity trace (same rationale as analyze())
        try:
            import storage
            storage.append_screenshot_log("local")
        except Exception as e:
            print(f"[ScreenAnalyzer] screenshot_log write failed: {e}")

        # AttentionEngine signal only — analyze_for_tool is for the
        # look_at_screen tool (chat agent's query), and any facts the
        # agent wants to remember should be emitted by the chat agent's
        # own emit_facts mechanism, not extracted from a side observation.
        device_label = device_name or "local"

        if significance >= MIN_SIGNIFICANCE_FOR_ATTENTION:
            try:
                from attention_engine import get_attention_engine
                get_attention_engine().record_signal("screenshot", {
                    "device_id": "local",
                    "device_name": device_label,
                    "observation": observation,
                    "significance": significance,
                    "source": "look_at_screen",
                })
            except Exception:
                pass

        return {"observation": observation, "significance": significance}

    def _is_duplicate(self, observation: str, device_id: str = "") -> bool:
        """Check if observation is too similar to THIS device's recent ones (Jaccard > 0.7)."""
        recent = self._recent_obs_per_device.get(device_id, [])
        if not recent:
            return False

        def extract_terms(text):
            return set(re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}', text.lower()))

        new_terms = extract_terms(observation)
        if not new_terms:
            return False

        for prev in recent[-3:]:
            prev_terms = extract_terms(prev)
            if not prev_terms:
                continue
            overlap = len(new_terms & prev_terms)
            union = len(new_terms | prev_terms)
            if union > 0 and overlap / union > 0.7:
                return True
        return False


# Per-user instances
_instances: dict[str, ScreenAnalyzer] = {}


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def get_analyzer() -> ScreenAnalyzer:
    uid = _current_user_id()
    if uid not in _instances:
        _instances[uid] = ScreenAnalyzer()
    return _instances[uid]
