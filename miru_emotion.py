"""Miru Emotion Engine — Miru's own internal emotional state.

Unlike emotion_log (which tracks the USER's emotions), this module manages
Miru's own feelings. The live production update path is AttentionEngine:
attention_engine.py evaluates Miru's inner state from the full moment
snapshot and calls update_from_attention().  The older
core._evaluate_miru_emotion + memory_prompts.call_miru_emotion_eval path is
kept for compatibility tests and historical debugging.

The LLM sees the full event-time context (today's chat with timestamps,
seconds-since-last-user-msg, user's emotion arc, persona/human core_memory
blocks, current Miru state) so it can FEEL signals like "I haven't been
replied to for hours" without us hardcoding absence presets.

Data stored in data/miru_emotion.json. Thread-safe via lock.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from difflib import SequenceMatcher

import storage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Inertia: how much of the new emotion vs current
INERTIA_NEW = 0.6
INERTIA_OLD = 0.4

# Time decay per hour
VALENCE_DECAY_RATE = 0.10   # 10% toward 0 per hour
AROUSAL_DECAY_RATE = 0.10   # 10% toward resting (0.2) per hour
AROUSAL_RESTING = 0.2

# History limit — covers ~1-2 weeks of emotion updates for trend analysis
# and timeline display. An active day can hit 100+ entries; 500 buys ~3-5
# days comfortably, ~2 weeks for typical users. Entries are small JSON
# dicts so file-size cost is negligible.
MAX_HISTORY = 500
# Miru's visible emotion history should be event-like. AttentionEngine may
# update current emotion often, but stable same-mood attention updates are
# coalesced so the UI reads like feelings, not telemetry.
ATTENTION_HISTORY_DEDUP_SECONDS = 2 * 60 * 60
ATTENTION_HISTORY_TEXT_SIMILARITY = 0.88

# Mood string sanity bounds — we DON'T enforce a whitelist (Miru is allowed
# free-form Chinese phrases like "有点小委屈"), but we cap obviously bad
# inputs (None / empty / overlong / non-string) so the UI never breaks.
_MOOD_MAX_LEN = 30

# Default state.
# `relationship_meter` only carries first_meet_date now — closeness/trust were
# removed in 2026-05-09 because they only ever monotonically accumulated to
# ceiling and the only behaviour they drove (3-bucket relationship_stage) is
# now hardcoded to "very close" so Miru's tone is consistently warm. Old
# data files may still have legacy closeness/trust fields; we ignore them.
DEFAULT_STATE = {
    "current": {
        "mood": "neutral",
        "valence": 0.0,
        "arousal": 0.2,
        "updated_at": "",
        "reason": "",
    },
    "history": [],
    "relationship_meter": {
        # Empty string = "not yet bootstrapped"; ensure_first_meet_date()
        # migrates from legacy data/relationship_meta.json or defaults to today.
        "first_meet_date": "",
    },
}


def _text_similarity(a, b) -> float:
    left = " ".join(str(a or "").split())
    right = " ".join(str(b or "").split())
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()

# Mood display names (Chinese) — used as fallback for known mood words.
# LLM can output any mood string; if not in this dict, the raw string is used.
MOOD_DISPLAY = {
    "happy": "开心",
    "content": "平静愉悦",
    "excited": "兴奋",
    "worried": "担心",
    "pouty": "有点小委屈",
    "hurt": "有点受伤",
    "lonely": "有点失落",
    "annoyed": "有点生气",
    "touched": "被感动",
    "neutral": "平静",
    "sleepy": "犯困",
}


class MiruEmotion:
    """Miru's internal emotional state engine."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict | None = None

    def _load(self) -> dict:
        """Load state from storage, or initialize defaults.

        Must be called under self._lock.
        """
        if self._state is not None:
            return self._state
        data = storage.load_miru_emotion()
        if not data or "current" not in data:
            data = _deep_copy(DEFAULT_STATE)
            data["current"]["updated_at"] = datetime.now().isoformat()[:19]
            storage.save_miru_emotion(data)
        self._state = data
        return data

    def _save(self):
        """Persist current state."""
        if self._state is not None:
            storage.save_miru_emotion(self._state)

    def update_from_llm(self, llm_result: dict) -> dict:
        """Apply LLM-evaluated emotion to Miru's state.

        Legacy path: called by core._evaluate_miru_emotion after the
        chat-triggered emotion eval LLM call.

        Args:
            llm_result: {
                "mood": str,           # free-form mood word (Chinese or English)
                "valence": float,      # -1.0 to 1.0
                "arousal": float,      # 0.0 to 1.0
                "reason": str,         # why Miru feels this way
            }

        Returns: Updated current state dict
        """
        mood = llm_result.get("mood", "neutral")
        valence = max(-1.0, min(1.0, float(llm_result.get("valence", 0))))
        arousal = max(0.0, min(1.0, float(llm_result.get("arousal", 0.2))))
        reason = llm_result.get("reason", "")

        return self._apply_emotion(
            mood=mood, valence=valence, arousal=arousal,
            reason=reason, trigger="llm_eval",
        )

    def update_from_attention(self, attention_result: dict) -> dict:
        """Apply AttentionEngine-evaluated inner emotion to Miru's state.

        AttentionEngine is now the live source for Miru's moment-to-moment
        feelings.  New payloads are first-person state segments with a stable
        id; while a feeling continues we update the same history row's
        duration/tick_count instead of appending telemetry-like duplicates.
        Legacy payloads without an id still use the old inertia path.
        """
        if attention_result.get("id") or attention_result.get("segment_id"):
            return self._apply_attention_segment(attention_result)

        mood = attention_result.get("mood", "neutral")
        valence = max(-1.0, min(1.0, float(attention_result.get("valence", 0))))
        arousal = max(0.0, min(1.0, float(attention_result.get("arousal", 0.2))))
        reason = (
            attention_result.get("reason")
            or attention_result.get("thought")
            or ""
        )

        return self._apply_emotion(
            mood=mood, valence=valence, arousal=arousal,
            reason=reason, trigger="attention",
        )

    def _apply_attention_segment(self, segment: dict) -> dict:
        mood = segment.get("mood", "neutral")
        if not isinstance(mood, str):
            mood = "neutral"
        mood = mood.strip() or "neutral"
        if len(mood) > _MOOD_MAX_LEN:
            mood = mood[:_MOOD_MAX_LEN]
        valence = max(-1.0, min(1.0, float(segment.get("valence", 0))))
        arousal = max(0.0, min(1.0, float(segment.get("arousal", 0.2))))
        reason = (
            segment.get("text")
            or segment.get("reason")
            or segment.get("last_seen_summary")
            or ""
        )
        seg_id = segment.get("id") or segment.get("segment_id")
        now_s = segment.get("updated_at") or datetime.now().isoformat()[:19]
        timestamp = segment.get("started_at") or now_s

        with self._lock:
            state = self._load()
            current = state["current"]
            current["mood"] = mood
            current["valence"] = round(valence, 3)
            current["arousal"] = round(arousal, 3)
            current["updated_at"] = now_s.replace(" ", "T")
            current["reason"] = reason
            current["segment_id"] = seg_id
            current["started_at"] = segment.get("started_at") or ""
            current["duration_seconds"] = int(segment.get("duration_seconds") or 0)
            current["tick_count"] = int(segment.get("tick_count") or 1)

            entry = {
                "id": seg_id,
                "segment_id": seg_id,
                "channel": "self_emotion",
                "timestamp": timestamp.replace(" ", "T"),
                "started_at": (segment.get("started_at") or timestamp).replace(" ", "T"),
                "updated_at": now_s.replace(" ", "T"),
                "ended_at": (segment.get("ended_at") or "").replace(" ", "T") if segment.get("ended_at") else None,
                "duration_seconds": int(segment.get("duration_seconds") or 0),
                "tick_count": int(segment.get("tick_count") or 1),
                "mood": mood,
                "valence": current["valence"],
                "arousal": current["arousal"],
                "trigger": "attention",
                "reason": reason,
                "text": reason,
                "change_reason": segment.get("change_reason") or "",
            }
            history = state.setdefault("history", [])
            replaced = False
            for idx, item in enumerate(history):
                if isinstance(item, dict) and (item.get("id") or item.get("segment_id")) == seg_id:
                    history[idx] = {**item, **entry}
                    replaced = True
                    break
            if not replaced:
                history.append(entry)
            if len(history) > MAX_HISTORY:
                state["history"] = history[-MAX_HISTORY:]

            self._save()
            return dict(current)

    def _apply_emotion(self, *, mood: str, valence: float, arousal: float,
                       reason: str, trigger: str) -> dict:
        """Internal: blend new emotion with current state and persist.

        Sanitizes mood to a non-empty string capped at _MOOD_MAX_LEN.
        We deliberately do NOT enforce a whitelist — Miru's mood is meant
        to be free-form Chinese (like "有点小委屈"), and the UI uses
        MOOD_DISPLAY only as an English→Chinese fallback for known keys.
        """
        # Mood sanitization (single source of truth for UI safety)
        if not isinstance(mood, str):
            mood = "neutral"
        mood = mood.strip()
        if not mood:
            mood = "neutral"
        if len(mood) > _MOOD_MAX_LEN:
            mood = mood[:_MOOD_MAX_LEN]

        with self._lock:
            state = self._load()
            current = state["current"]
            now = datetime.now()

            # Apply time decay first
            self._apply_time_decay(current, now)

            # Blend new emotion with current (inertia)
            new_valence = INERTIA_NEW * valence + INERTIA_OLD * current["valence"]
            new_arousal = INERTIA_NEW * arousal + INERTIA_OLD * current["arousal"]

            # Clamp
            new_valence = max(-1.0, min(1.0, new_valence))
            new_arousal = max(0.0, min(1.0, new_arousal))

            # Update current
            current["mood"] = mood
            current["valence"] = round(new_valence, 3)
            current["arousal"] = round(new_arousal, 3)
            current["updated_at"] = now.isoformat()[:19]
            current["reason"] = reason

            history_entry = {
                "timestamp": now.isoformat()[:19],
                "mood": mood,
                "valence": current["valence"],
                "arousal": current["arousal"],
                "trigger": trigger,
                "reason": reason,
            }
            if not self._should_append_history(state["history"], history_entry, now):
                self._save()
                return dict(current)

            state["history"].append(history_entry)
            if len(state["history"]) > MAX_HISTORY:
                state["history"] = state["history"][-MAX_HISTORY:]

            self._save()
            return dict(current)

    def _should_append_history(self, history: list, entry: dict,
                               now: datetime) -> bool:
        if entry.get("trigger") != "attention" or not history:
            return True
        last = history[-1] if isinstance(history[-1], dict) else {}
        if last.get("trigger") != "attention":
            return True
        try:
            last_ts = datetime.strptime(last.get("timestamp", ""), "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            try:
                last_ts = datetime.strptime(last.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return True
        elapsed = (now - last_ts).total_seconds()
        if elapsed < 0 or elapsed >= ATTENTION_HISTORY_DEDUP_SECONDS:
            return True
        if (last.get("mood") or "") != (entry.get("mood") or ""):
            return True
        if abs(float(last.get("valence") or 0) - float(entry.get("valence") or 0)) > 0.08:
            return True
        if abs(float(last.get("arousal") or 0) - float(entry.get("arousal") or 0)) > 0.10:
            return True
        reason_similarity = _text_similarity(last.get("reason"), entry.get("reason"))
        if reason_similarity >= ATTENTION_HISTORY_TEXT_SIMILARITY:
            return False
        # Same mood + nearly identical blended values within the cooldown is
        # one continuing feeling even when the model paraphrases the reason.
        return False

    def get_state(self) -> dict:
        """Get current emotional state with time-decay applied."""
        with self._lock:
            state = self._load()
            current = state["current"]
            self._apply_time_decay(current, datetime.now())
            self._save()
            return dict(current)

    def get_relationship(self) -> dict:
        """Return relationship meter — closeness/trust pinned to ceiling.

        The closeness/trust meters were removed in 2026-05-09; this method
        now returns hardcoded 1.0 / 1.0 so any remaining downstream readers
        (e.g. /api/miru-emotion/current → frontend) keep their schema, and
        any prompt path that still mentions a number sees "fully close /
        fully trusted". The frontend hides the meter UI; this is just
        compat surface.
        """
        return {"closeness": 1.0, "trust": 1.0}

    def get_relationship_stage(self) -> str:
        """Return the relationship stage label.

        Hardcoded to "很亲近" since 2026-05-09 — see soul.md `# Relationship
        Stages` for the prose description that drives Miru's tone in the
        chat agent system prompt.
        """
        return "很亲近"

    def get_mood_text(self) -> str:
        """Get a natural-language description of current mood for prompt injection.

        Returns something like:
        - "现在心情不错，刚才聊天很开心"
        - "有点失落，好久没收到消息了"
        """
        current = self.get_state()
        mood = current.get("mood", "neutral")
        valence = current.get("valence", 0)
        reason = current.get("reason", "")

        # Don't generate text for very neutral states
        if mood in ("neutral", "平静") and abs(valence) < 0.1:
            return ""

        # Use MOOD_DISPLAY for known English keys, otherwise use mood as-is
        # (LLM may output Chinese mood words like "有点担心", "心疼" directly)
        display = MOOD_DISPLAY.get(mood, mood)

        parts = [display]
        if reason and reason not in ("absence_short", "absence_long"):
            parts.append(reason)

        return "，".join(parts)

    def format_for_context(self) -> str:
        """Format emotion state for injection into chat context."""
        mood_text = self.get_mood_text()
        if not mood_text:
            return ""
        return (
            f"【你现在的心情】{mood_text}\n"
            "（这是你自己的真实感受。自然地让它影响你的语气和措辞，"
            "但不要刻意说出来，除非对方问你心情怎么样。）"
        )

    # ------------------------------------------------------------------
    # First-meet date (#193-D: merged from storage.relationship_meta.json)
    # ------------------------------------------------------------------

    def ensure_first_meet_date(self) -> str:
        """Return first_meet_date, creating/migrating if missing.

        Migration order:
            1. If miru_emotion already has first_meet_date → return it
            2. Else if legacy data/relationship_meta.json has one → migrate + persist
            3. Else initialize to today

        Safe to call repeatedly — only writes on first call per user.
        """
        with self._lock:
            state = self._load()
            meter = state.setdefault("relationship_meter", {})
            fmd = meter.get("first_meet_date") or ""
            if fmd:
                return fmd

            # Try legacy migration from relationship_meta.json
            try:
                legacy_path = os.path.join(
                    storage.get_data_dir(), "relationship_meta.json")
                if os.path.exists(legacy_path):
                    legacy = storage.read_json(legacy_path) or {}
                    legacy_fmd = legacy.get("first_meet_date") or ""
                    if legacy_fmd:
                        fmd = legacy_fmd
            except Exception:
                # Storage unavailable in this context (e.g., no flask.g) — fall through
                pass

            if not fmd:
                fmd = datetime.now().strftime("%Y-%m-%d")

            meter["first_meet_date"] = fmd
            self._save()
            return fmd

    def get_first_meet_date(self) -> str:
        """Return first_meet_date if set, else empty string (no initialization)."""
        with self._lock:
            state = self._load()
            return state.get("relationship_meter", {}).get("first_meet_date", "") or ""

    def get_days_together(self) -> int:
        """Days elapsed since first_meet_date. 0 if not set or in the future."""
        fmd = self.get_first_meet_date()
        if not fmd:
            return 0
        try:
            d = datetime.strptime(fmd, "%Y-%m-%d")
            delta = (datetime.now() - d).days
            return max(0, delta)
        except (ValueError, TypeError):
            return 0

    def get_history(self, limit: int = 10) -> list[dict]:
        """Get recent emotion history."""
        with self._lock:
            state = self._load()
            return list(state.get("history", [])[-limit:])

    def _apply_time_decay(self, current: dict, now: datetime):
        """Apply time-based decay toward neutral.

        Valence decays toward 0, arousal decays toward AROUSAL_RESTING.
        """
        updated_at = current.get("updated_at", "")
        if not updated_at:
            return

        try:
            last_update = datetime.fromisoformat(updated_at)
        except (ValueError, TypeError):
            return

        hours_elapsed = (now - last_update).total_seconds() / 3600
        if hours_elapsed < 0.1:  # Less than 6 minutes, skip
            return

        # Decay valence toward 0
        valence = current.get("valence", 0)
        decay_factor = (1 - VALENCE_DECAY_RATE) ** hours_elapsed
        new_valence = valence * decay_factor

        # Decay arousal toward resting level
        arousal = current.get("arousal", AROUSAL_RESTING)
        arousal_decay = (1 - AROUSAL_DECAY_RATE) ** hours_elapsed
        new_arousal = AROUSAL_RESTING + (arousal - AROUSAL_RESTING) * arousal_decay

        current["valence"] = round(new_valence, 3)
        current["arousal"] = round(new_arousal, 3)
        current["updated_at"] = now.isoformat()[:19]

        # If valence is very close to 0, shift mood to neutral/content
        if abs(new_valence) < 0.05 and current.get("mood") not in ("neutral", "content"):
            current["mood"] = "neutral"
            current["reason"] = ""


def _deep_copy(d):
    """Simple deep copy for our JSON-like dicts."""
    import json
    return json.loads(json.dumps(d))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instances: dict[str, MiruEmotion] = {}


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def get_instance() -> MiruEmotion:
    uid = _current_user_id()
    if uid not in _instances:
        _instances[uid] = MiruEmotion()
    return _instances[uid]
