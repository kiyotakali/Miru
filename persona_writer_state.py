"""Persona Writer accumulated-trigger state machine (Sleep Agent v3 C2).

The Slot Writer agent runs every chat batch flush. The Persona Writer agent
runs less frequently — accumulating N=4 batches first so the LLM has a long
enough observation window to detect persona-level changes rather than
short-term fluctuations.

State file (per-user): data/users/<uid>/memory/_slots/persona_writer_meta.json

Schema:
    {
      "batches_since_last_run": int,
      "pending_batch_dialogs": [
        [{"role": "user"|"assistant", "text": "...", "time": "..."}, ...],
        ...
      ],
      "pending_new_slots": [
        {"domain": "project", "slot_id": "...", "kind": "match"|"new", "summary": "..."},
        ...
      ],
      "pending_proactive_outcomes": [
        {"kind": "proactive_sent"|"proactive_response", ...},
        ...
      ],
      "last_run_ts": "ISO 8601 string"
    }

Truncation:
    - per-batch: 30 messages max (first 10 + last 10, omit middle when >30)
    - total pending_batch_dialogs char cap: 8000
    - pending_new_slots: 20 entries max (prefer kind=new, then by recency)

Trigger:
    - batches_since_last_run >= PERSONA_TRIGGER_BATCHES (4) → run
    - On run success → reset all pending state
    - On run failure → reset all pending state (option X: don't retry bad data)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

import memory


# Trigger threshold: accumulate this many batches before running.
PERSONA_TRIGGER_BATCHES = 4

# Truncation knobs.
MAX_MESSAGES_PER_BATCH = 30
MAX_DIALOG_TOTAL_CHARS = 8000
MAX_PENDING_NEW_SLOTS = 20
MAX_PENDING_PROACTIVE_OUTCOMES = 30
MAX_PROACTIVE_OUTCOME_TOTAL_CHARS = 6000


_lock = threading.Lock()


def _meta_path() -> str:
    return os.path.join(memory._memory_dir(), "_slots",
                         "persona_writer_meta.json")


def _empty_meta() -> dict:
    return {
        "batches_since_last_run": 0,
        "pending_batch_dialogs": [],
        "pending_new_slots": [],
        "pending_proactive_outcomes": [],
        "last_run_ts": "",
    }


def load_meta() -> dict:
    """Read persona_writer_meta.json. Returns empty meta on missing/corrupt."""
    path = _meta_path()
    if not os.path.exists(path):
        return _empty_meta()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        # Defensive normalization
        out = _empty_meta()
        if isinstance(data.get("batches_since_last_run"), int):
            out["batches_since_last_run"] = data["batches_since_last_run"]
        if isinstance(data.get("pending_batch_dialogs"), list):
            out["pending_batch_dialogs"] = [
                b for b in data["pending_batch_dialogs"]
                if isinstance(b, list)
            ]
        if isinstance(data.get("pending_new_slots"), list):
            out["pending_new_slots"] = [
                s for s in data["pending_new_slots"]
                if isinstance(s, dict)
            ]
        if isinstance(data.get("pending_proactive_outcomes"), list):
            out["pending_proactive_outcomes"] = [
                s for s in data["pending_proactive_outcomes"]
                if isinstance(s, dict)
            ]
        if isinstance(data.get("last_run_ts"), str):
            out["last_run_ts"] = data["last_run_ts"]
        return out
    except (json.JSONDecodeError, OSError):
        return _empty_meta()


def save_meta(meta: dict) -> bool:
    """Atomic write of persona_writer_meta.json."""
    path = _meta_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"[persona_writer_state] save failed: {e}")
        return False


def _truncate_batch(batch: list[dict]) -> list[dict]:
    """Cap a single batch to MAX_MESSAGES_PER_BATCH messages.

    If batch is longer, keep first 10 + last 10 + a marker dict in between.
    """
    if len(batch) <= MAX_MESSAGES_PER_BATCH:
        return batch
    head = batch[:10]
    tail = batch[-10:]
    marker = {"role": "system",
              "text": f"... (省略中间 {len(batch) - 20} 条消息)",
              "time": ""}
    return head + [marker] + tail


def _enforce_char_cap(dialogs: list[list[dict]]) -> list[list[dict]]:
    """Drop oldest batches if total char count exceeds MAX_DIALOG_TOTAL_CHARS.

    Computed using JSON serialization size as a fast proxy.
    """
    while dialogs:
        size = sum(len(json.dumps(b, ensure_ascii=False)) for b in dialogs)
        if size <= MAX_DIALOG_TOTAL_CHARS:
            break
        dialogs.pop(0)  # drop oldest
    return dialogs


def _trim_new_slots(slots: list[dict]) -> list[dict]:
    """Cap pending_new_slots to MAX_PENDING_NEW_SLOTS, preferring new > match,
    then most recent (later entries first)."""
    if len(slots) <= MAX_PENDING_NEW_SLOTS:
        return slots
    # Walk from end (most recent first) keeping 'new' before 'match'
    # at equal recency.
    indexed = list(enumerate(slots))
    # Stable sort: primary key = -index (recent first), secondary = kind=="new" first
    indexed.sort(key=lambda t: (0 if t[1].get("kind") == "new" else 1, -t[0]))
    keep = [t[1] for t in indexed[:MAX_PENDING_NEW_SLOTS]]
    # Restore original chronological order for the kept slice
    kept_orig_idxs = sorted(
        i for i, s in enumerate(slots) if s in keep
    )
    return [slots[i] for i in kept_orig_idxs[:MAX_PENDING_NEW_SLOTS]]


def _trim_proactive_outcomes(items: list[dict]) -> list[dict]:
    if not items:
        return []
    # Deduplicate by stable id, keeping the newest version.
    by_id = {}
    ordered_ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id") or "").strip()
        if not eid:
            eid = (
                f"{item.get('kind', 'event')}:"
                f"{item.get('proactive_message_id', '')}:"
                f"{item.get('user_message_id', '')}:"
                f"{item.get('time', '')}"
            )
            item["id"] = eid
        if eid not in by_id:
            ordered_ids.append(eid)
        by_id[eid] = item
    kept = [by_id[eid] for eid in ordered_ids][-MAX_PENDING_PROACTIVE_OUTCOMES:]
    while kept:
        size = len(json.dumps(kept, ensure_ascii=False))
        if size <= MAX_PROACTIVE_OUTCOME_TOTAL_CHARS:
            break
        kept.pop(0)
    return kept


def record_batch(batch: list[dict], slot_writes: list[dict]) -> dict:
    """After a chat batch flush, record the batch + its slot_writes.

    Args:
        batch: the chat message list that was processed.
        slot_writes: list of {"domain", "slot_id", "kind", "summary"} dicts
                     describing what slot system wrote.

    Returns the updated meta (after persistence).
    """
    with _lock:
        meta = load_meta()
        meta["batches_since_last_run"] = meta.get("batches_since_last_run", 0) + 1
        meta["pending_batch_dialogs"].append(_truncate_batch(batch))
        meta["pending_batch_dialogs"] = _enforce_char_cap(
            meta["pending_batch_dialogs"]
        )
        for s in slot_writes:
            if not isinstance(s, dict):
                continue
            entry = {
                "domain": s.get("domain", ""),
                "slot_id": s.get("slot_id", ""),
                "kind": s.get("kind", ""),
                "summary": (s.get("summary") or "").strip()[:120],
            }
            meta["pending_new_slots"].append(entry)
        meta["pending_new_slots"] = _trim_new_slots(meta["pending_new_slots"])
        save_meta(meta)
        return meta


def record_proactive_outcome(event: dict) -> dict:
    """Record proactive-send / user-response relationship feedback.

    These events are for Persona Writer only.  They should help Miru update
    the ``persona`` core block with how主动靠近 actually landed, without
    turning Miru's own proactive text into factual slot memory.
    """
    if not isinstance(event, dict):
        event = {}
    clean = {
        "id": str(event.get("id") or "").strip(),
        "kind": str(event.get("kind") or "proactive_event").strip()[:40],
        "time": str(event.get("time") or "").strip()[:32],
        "proactive_message_id": str(event.get("proactive_message_id") or "").strip()[:80],
        "user_message_id": str(event.get("user_message_id") or "").strip()[:80],
        "intent_id": str(event.get("intent_id") or "").strip()[:80],
        "topic_key": str(event.get("topic_key") or "").strip()[:80],
        "care_motive": str(event.get("care_motive") or "").strip()[:240],
        "why_i_want_to_say": str(event.get("why_i_want_to_say") or "").strip()[:240],
        "user_need": str(event.get("user_need") or "").strip()[:200],
        "message_seed": str(event.get("message_seed") or "").strip()[:200],
        "proactive_text": str(event.get("proactive_text") or "").strip()[:300],
        "user_reply": str(event.get("user_reply") or "").strip()[:300],
        "response_delay_seconds": event.get("response_delay_seconds"),
        "model_mode": str(event.get("model_mode") or "").strip()[:40],
        "tool_policy": str(event.get("tool_policy") or "").strip()[:40],
    }
    with _lock:
        meta = load_meta()
        meta["pending_proactive_outcomes"].append(clean)
        meta["pending_proactive_outcomes"] = _trim_proactive_outcomes(
            meta["pending_proactive_outcomes"]
        )
        save_meta(meta)
        return meta


def should_run(meta: dict | None = None) -> bool:
    """True if accumulated batches >= trigger threshold."""
    m = meta if meta is not None else load_meta()
    if m.get("batches_since_last_run", 0) >= PERSONA_TRIGGER_BATCHES:
        return True
    outcomes = m.get("pending_proactive_outcomes", []) or []
    responses = [x for x in outcomes if isinstance(x, dict) and x.get("kind") == "proactive_response"]
    return len(responses) >= 3


def reset_after_run(success: bool) -> dict:
    """Clear pending state and bump last_run_ts.

    success=True means LLM ran cleanly; success=False means it failed
    (validation / retry exhausted). Per design doc §5.3 we clear pending
    on BOTH — failure shouldn't let bad data accumulate indefinitely.
    """
    with _lock:
        new_meta = _empty_meta()
        new_meta["last_run_ts"] = datetime.now().isoformat(timespec="seconds")
        save_meta(new_meta)
        return new_meta


def concat_dialogs_for_llm(meta: dict | None = None) -> str:
    """Render accumulated pending_batch_dialogs as a single text block
    for the Persona Writer LLM."""
    m = meta if meta is not None else load_meta()
    dialogs = m.get("pending_batch_dialogs", []) or []
    if not dialogs:
        return "(无累计对话)"
    lines = []
    for i, batch in enumerate(dialogs, 1):
        lines.append(f"━━━ batch {i} ({len(batch)} 条消息) ━━━")
        for msg in batch:
            ts = (msg.get("time") or "")[:19]
            role = msg.get("role", "?")
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"[{ts}] {role}: {text}")
    return "\n".join(lines)


def concat_proactive_outcomes_for_llm(meta: dict | None = None) -> str:
    """Render pending proactive relationship feedback for Persona Writer."""
    m = meta if meta is not None else load_meta()
    events = m.get("pending_proactive_outcomes", []) or []
    if not events:
        return "(无主动开口反馈)"
    lines = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind", "event")
        ts = (event.get("time") or "")[:19]
        topic = event.get("topic_key", "")
        if kind == "proactive_response":
            delay = event.get("response_delay_seconds")
            delay_text = f", 用户约 {delay}s 后回应" if isinstance(delay, int) else ""
            lines.append(
                f"[{ts}] proactive_response topic={topic}{delay_text}\n"
                f"  我当时想说: {event.get('why_i_want_to_say') or event.get('care_motive')}\n"
                f"  我发出的话: {event.get('proactive_text')}\n"
                f"  用户回应: {event.get('user_reply')}"
            )
        else:
            lines.append(
                f"[{ts}] proactive_sent topic={topic}\n"
                f"  我想靠近的理由: {event.get('why_i_want_to_say') or event.get('care_motive')}\n"
                f"  我发出的话: {event.get('proactive_text')}"
            )
    return "\n".join(lines)
