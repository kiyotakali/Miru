"""Core business logic for ContextLife — no Flask dependency.

The Web UI (app.py) calls these functions.
"""

import hashlib
import json
import os
import random
import re
import threading
import uuid
from datetime import datetime, timedelta

import server_config
import user_settings
import self_profile
from prompt import call_chat_agent
from prompt_identity import (
    normalize_character_section,
    resolve_user_entity_label,
    safe_user_address,
)
from character import get_config
import storage

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _user_now() -> datetime:
    """Get current time in the user's configured timezone (falls back to server local)."""
    return user_settings.user_now()
MEMORY_SCOPE_USER = storage.MEMORY_SCOPE_USER
MEMORY_SCOPE_AIRI = storage.MEMORY_SCOPE_AIRI
MEMORY_SCOPE_ALL = storage.MEMORY_SCOPE_ALL


def _normalize_memory_scope(memory_scope=MEMORY_SCOPE_USER, allow_all=False):
    return storage._normalize_memory_scope(memory_scope, allow_all=allow_all)


def _get_active_commitments_for_runtime(memory_scope=MEMORY_SCOPE_ALL):
    import memory
    content = memory.read_file("commitments/active.md") or ""
    result = []
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            result.append({"title": line[6:].strip(), "status": "active"})
    return result


# ---------------------------------------------------------------------------
# Commitment parser — structured parsing of active.md / done.md
# ---------------------------------------------------------------------------

_DEADLINE_RE = re.compile(r'\(deadline:\s*([\d\-]+(?:\s+\d{1,2}:\d{2})?)\)')
_ADDED_RE = re.compile(r'\[added:\s*([\d\-\s:]+)\]')
_COMPLETED_RE = re.compile(r'\[completed:\s*([\d\-\s:]+)\]')
_DETAIL_RE = re.compile(r'\s--\s(.+?)(?:\s*\[|\s*\(deadline|\s*$)')


def _parse_commitment_line(line: str) -> dict | None:
    """Parse a single commitment line into structured dict."""
    line = line.strip()
    if not line.startswith("- ["):
        return None
    completed = line.startswith("- [x]")
    # Extract raw title (everything after "- [ ] " or "- [x] " up to first metadata)
    body = line[6:].strip() if line[5] == ']' else line[6:].strip()

    deadline = None
    m = _DEADLINE_RE.search(body)
    if m:
        deadline = m.group(1).strip()

    added = None
    m = _ADDED_RE.search(body)
    if m:
        added = m.group(1).strip()

    completed_at = None
    m = _COMPLETED_RE.search(body)
    if m:
        completed_at = m.group(1).strip()

    detail = None
    m = _DETAIL_RE.search(body)
    if m:
        detail = m.group(1).strip()

    # Clean title: remove metadata tags
    title = body
    for pat in [_DEADLINE_RE, _ADDED_RE, _COMPLETED_RE]:
        title = pat.sub("", title)
    title = re.sub(r'\s--\s.*', '', title).strip()
    # Remove trailing parenthetical context like "(与XX讨论提及)"
    title = title.rstrip()

    if not title:
        return None

    # Generate stable ID from added timestamp + title hash.
    # Must use hashlib (deterministic across processes). Python's built-in
    # hash() is seeded per-process, so every VPS restart would shift every
    # existing commitment's cid — causing frontend PUT/DELETE to 404 after
    # restart and the UI to "revert" completions.
    title_md5 = hashlib.md5(title.encode("utf-8")).hexdigest()
    if added:
        ts_digits = re.sub(r'[^0-9]', '', added)
        title_hash = int(title_md5[:4], 16) % 10000
        cid = f"c_{ts_digits}_{title_hash:04d}"
    else:
        cid = "c_" + title_md5[:10]

    # Compute urgency — supports both "YYYY-MM-DD" and "YYYY-MM-DD HH:MM"
    now = _user_now()
    today = now.strftime("%Y-%m-%d")
    urgency = "no_deadline"
    if deadline:
        dl_date_str = deadline[:10]  # always YYYY-MM-DD
        dl_has_time = len(deadline) > 10  # "2026-04-16 15:00"
        if dl_date_str < today:
            urgency = "overdue"
        elif dl_date_str == today:
            if dl_has_time:
                try:
                    dl_dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
                    hours_left = (dl_dt - now.replace(tzinfo=None)).total_seconds() / 3600
                    if hours_left <= 0:
                        urgency = "overdue"
                    elif hours_left <= 2:
                        urgency = "imminent"  # within 2 hours
                    else:
                        urgency = "today"
                except ValueError:
                    urgency = "today"
            else:
                urgency = "today"
        else:
            try:
                dl_date = datetime.strptime(dl_date_str, "%Y-%m-%d")
                days_left = (dl_date - datetime.strptime(today, "%Y-%m-%d")).days
                if days_left <= 1 and dl_has_time:
                    # Tomorrow but with time — check if within 6 hours
                    try:
                        dl_dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
                        hours_left = (dl_dt - now.replace(tzinfo=None)).total_seconds() / 3600
                        urgency = "approaching" if hours_left <= 6 else "soon" if days_left <= 3 else "normal"
                    except ValueError:
                        urgency = "soon" if days_left <= 3 else "normal"
                else:
                    urgency = "soon" if days_left <= 3 else "normal"
            except ValueError:
                urgency = "normal"

    status = "completed" if completed else ("overdue" if urgency == "overdue" else "active")

    return {
        "id": cid,
        "title": title,
        "detail": detail,
        "deadline": deadline,
        "added": added,
        "completed": completed_at,
        "status": status,
        "urgency": urgency,
    }


def parse_commitments(include_done=True) -> list[dict]:
    """Parse all commitments from active.md (and optionally done.md).

    When `include_done=False`, completed items are skipped — INCLUDING
    `[x]`-marked lines that are still physically in active.md. The nightly
    cleanup (`_nightly_commitment_cleanup`) only moves `[x]` lines to
    done.md once a day, so during the gap a completed item with an upcoming
    deadline (e.g. completed at 20:49 with a 23:00 deadline) would
    otherwise be reported as "🔴🔴 imminent" to the proactive agent and
    trigger a hallucinated reminder for an already-done task.

    Callers that explicitly want both open and completed items must use
    `include_done=True`.
    """
    import memory
    results = []
    seen_ids = set()

    content = memory.read_file("commitments/active.md") or ""
    for line in content.split("\n"):
        item = _parse_commitment_line(line)
        if not item or item["id"] in seen_ids:
            continue
        # Filter [x]-marked lines that haven't been migrated to done.md yet.
        # _parse_commitment_line already sets status="completed" for them.
        if not include_done and item.get("status") == "completed":
            continue
        seen_ids.add(item["id"])
        results.append(item)

    if include_done:
        done_content = memory.read_file("commitments/done.md") or ""
        for line in done_content.split("\n"):
            item = _parse_commitment_line(line)
            if item and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                item["status"] = "completed"
                results.append(item)

    # Sort: overdue first, then today, then soon, then by deadline, then no_deadline
    urgency_order = {"overdue": 0, "today": 1, "soon": 2, "normal": 3, "no_deadline": 4}
    results.sort(key=lambda x: (
        0 if x["status"] != "completed" else 1,
        urgency_order.get(x["urgency"], 5),
        x["deadline"] or "9999-99-99",
    ))
    return results


def _broadcast_data_changed(scope: str, user_id: str | None = None):
    """Broadcast cache invalidation event to that user's SSE clients.

    user_id defaults to the current Flask g.user_id (falls back to '_admin').
    Background threads that have called _csm_set_thread_context will have g
    populated; pure-thread callers must pass user_id explicitly.
    """
    try:
        if user_id is None:
            user_id = _current_user_id_safe()
        import sse
        sse.broadcast("data_changed", {"scope": scope}, user_id=user_id)
    except Exception:
        pass


def _broadcast_commitment_sync(user_id: str | None = None):
    """Broadcast current commitment list to that user's SSE clients.

    include_done=True is intentional: the frontend's _cachedCommitments is
    wholly replaced by this payload, so omitting historical completed items
    would make them disappear from the "已完成" section as soon as anyone
    completes another commitment. done.md grows slowly enough that this is
    fine for now (see the 2026-04-23 discussion in CLAUDE.md).
    """
    try:
        if user_id is None:
            user_id = _current_user_id_safe()
        import sse
        items = parse_commitments(include_done=True)
        sse.broadcast("commitment_sync", {"items": items}, user_id=user_id)
        _broadcast_data_changed("commitments", user_id=user_id)
        try:
            active_count = sum(1 for item in items if item.get("status") != "completed")
            urgent_count = sum(
                1 for item in items
                if item.get("status") != "completed"
                and item.get("urgency") in {"overdue", "imminent", "today", "approaching"}
            )
            from attention_engine import get_attention_engine
            get_attention_engine().record_signal("commitment_state", {
                "summary": f"commitments changed: active={active_count}, urgent={urgent_count}",
                "active_count": active_count,
                "urgent_count": urgent_count,
            })
        except Exception:
            pass
    except Exception as e:
        print(f"[CommitmentSync] broadcast error: {e}")


def add_commitment_manual(title: str, deadline: str = "", detail: str = "") -> dict:
    """Add a commitment from the frontend / agent tool / ScreenSlot Writer.

    Dedup: if an active commitment with the SAME normalized title AND same
    deadline already exists, skip the write and return {status:"skipped_dup"}.
    Normalization is case-insensitive + strips punctuation/whitespace, so
    "提交 CS231n A3" matches "提交CS231n A3" but won't match an unrelated
    "CS231n 答疑预约".

    Called by:
      - Web UI (frontend POST /api/commitments)
      - chat agent tool `add_commitment`
      - ScreenSlot Writer (per-screenshot new DDL detection)
    """
    import memory
    import re as _re

    # Dedup: scan active.md for any active item with same (normalized_title + deadline)
    def _normalize(s: str) -> str:
        s = (s or "").strip().lower()
        # Drop whitespace + common ASCII/CJK punctuation. We do NOT regex-class
        # CJK punctuation here (raw-string quote ambiguity is a pain); just
        # iterate a set of punctuation chars to strip.
        s = _re.sub(r"\s+", "", s)
        punct = (
            ".,:;'\"()[]{}!?-_/\\"
            "。，、：；‘’“”"  # 。,、:;‘’“”
            "（）【】「」！？·"  # ()【】「」!?·
        )
        s = "".join(c for c in s if c not in punct)
        return s

    norm_title = _normalize(title)
    norm_deadline = (deadline or "").strip()

    if norm_title:
        existing = parse_commitments(include_done=False)
        for item in existing:
            if _normalize(item.get("title", "")) == norm_title and \
               (item.get("deadline") or "").strip() == norm_deadline:
                return {"status": "skipped_dup", "title": title, "deadline": deadline,
                        "matched_id": item.get("id", "")}

    now = _user_now().strftime("%Y-%m-%d %H:%M")
    line = f"- [ ] {title}"
    if deadline:
        line += f" (deadline: {deadline})"
    if detail:
        line += f" -- {detail}"
    line += f"  [added: {now}]"

    memory.ensure_dirs()
    content = memory.read_file("commitments/active.md")
    if content is None:
        content = "# Active Commitments\n"
    if not content.endswith("\n"):
        content += "\n"
    content += line + "\n"
    memory.write_file("commitments/active.md", content)
    _broadcast_commitment_sync()
    return {"status": "ok", "title": title, "deadline": deadline}


def complete_commitment_by_title(title_query: str) -> dict:
    """Mark a commitment as completed by fuzzy title match.

    Used by ScreenSlot Writer when VLM observes a completion event:
    it outputs the active commitment title (loosely), we find the
    best Jaccard-ish match in active.md and complete it.

    Returns {"status": "ok"|"not_found", "matched_title": ...}
    """
    import re as _re
    q = (title_query or "").strip().lower()
    if not q:
        return {"status": "not_found", "reason": "empty query"}

    def _tokens(s):
        s = (s or "").lower()
        # split on whitespace + common punctuation; keep CJK clumps intact
        parts = _re.findall(r"[一-鿿]|[a-z0-9]+", s)
        return set(parts)

    q_tok = _tokens(q)
    if not q_tok:
        return {"status": "not_found", "reason": "no tokens"}

    items = parse_commitments(include_done=False)
    best = None
    best_score = 0.0
    for item in items:
        t = item.get("title", "")
        t_tok = _tokens(t)
        if not t_tok:
            continue
        overlap = len(q_tok & t_tok)
        union = len(q_tok | t_tok)
        if union == 0:
            continue
        score = overlap / union
        if score > best_score:
            best_score = score
            best = item

    # Require ≥ 0.3 Jaccard. CJK is character-tokenized, so 4 字 query
    # against 8 字 active title legitimately scores ~0.4-0.5. We pick 0.3
    # to forgive abbreviations like "A3" vs "Assignment3" while still
    # rejecting truly unrelated commitments.
    if best and best_score >= 0.3:
        result = complete_commitment_by_id(best.get("id", ""))
        result["matched_title"] = best.get("title", "")
        result["match_score"] = round(best_score, 3)
        return result
    return {
        "status": "not_found",
        "reason": f"best match score {best_score:.2f} < 0.3 threshold",
        "best_candidate": best.get("title", "") if best else None,
    }


def complete_commitment_by_id(cid: str) -> dict:
    """Mark a commitment as completed by its parsed ID."""
    import memory
    content = memory.read_file("commitments/active.md") or ""
    lines = content.split("\n")
    now = _user_now().strftime("%Y-%m-%d %H:%M")

    for i, line in enumerate(lines):
        item = _parse_commitment_line(line)
        if item and item["id"] == cid and item["status"] != "completed":
            completed_line = line.replace("- [ ]", "- [x]", 1)
            if "[completed:" not in completed_line:
                completed_line = completed_line.rstrip() + f"  [completed: {now}]"
            lines[i] = completed_line
            memory.write_file("commitments/active.md", "\n".join(lines))
            # Archive to done.md
            done_content = memory.read_file("commitments/done.md")
            if done_content is None:
                done_content = "# Completed Commitments\n"
            if not done_content.endswith("\n"):
                done_content += "\n"
            done_content += completed_line.strip() + "\n"
            memory.write_file("commitments/done.md", done_content)
            _broadcast_commitment_sync()
            return {"status": "ok", "id": cid}
    return {"status": "not_found", "id": cid}


def delete_commitment_by_id(cid: str) -> dict:
    """Remove a commitment line from active.md."""
    import memory
    content = memory.read_file("commitments/active.md") or ""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        item = _parse_commitment_line(line)
        if item and item["id"] == cid:
            lines.pop(i)
            memory.write_file("commitments/active.md", "\n".join(lines))
            _broadcast_commitment_sync()
            return {"status": "ok", "id": cid}
    return {"status": "not_found", "id": cid}


def edit_commitment_by_id(cid: str, title: str = None, deadline: str = None) -> dict:
    """Edit a commitment's title or deadline."""
    import memory
    content = memory.read_file("commitments/active.md") or ""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        item = _parse_commitment_line(line)
        if item and item["id"] == cid and item["status"] != "completed":
            # Rebuild the line with updated fields
            new_title = title if title is not None else item["title"]
            new_deadline = deadline if deadline is not None else (item["deadline"] or "")
            new_detail = item["detail"] or ""
            added = item["added"] or _user_now().strftime("%Y-%m-%d %H:%M")
            new_line = f"- [ ] {new_title}"
            if new_deadline:
                new_line += f" (deadline: {new_deadline})"
            if new_detail:
                new_line += f" -- {new_detail}"
            new_line += f"  [added: {added}]"
            lines[i] = new_line
            memory.write_file("commitments/active.md", "\n".join(lines))
            _broadcast_commitment_sync()
            return {"status": "ok", "id": cid}
    return {"status": "not_found", "id": cid}


def get_commitment_stats() -> dict:
    """Get commitment statistics for the current week."""
    items = parse_commitments(include_done=True)
    today = _user_now()
    # Current week: Monday to Sunday
    monday = today - timedelta(days=today.weekday())
    monday_str = monday.strftime("%Y-%m-%d")
    sunday_str = (monday + timedelta(days=6)).strftime("%Y-%m-%d")

    total_active = 0
    total_completed = 0
    overdue = 0
    completed_this_week = 0
    added_this_week = 0

    for item in items:
        if item["status"] == "completed":
            total_completed += 1
            if item.get("completed") and item["completed"][:10] >= monday_str:
                completed_this_week += 1
        else:
            total_active += 1
            if item["urgency"] == "overdue":
                overdue += 1
        if item.get("added") and item["added"][:10] >= monday_str:
            added_this_week += 1

    return {
        "total_active": total_active,
        "total_completed": total_completed,
        "overdue": overdue,
        "completed_this_week": completed_this_week,
        "added_this_week": added_this_week,
        "week_range": f"{monday_str} ~ {sunday_str}",
    }


# ---------------------------------------------------------------------------
# One-time timestamp migration (UTC → CST +8h)
# ---------------------------------------------------------------------------

_MIGRATION_FLAG = os.path.join(
    storage.get_data_dir(),
    ".tz_migrated",
)


def _shift_timestamp(ts_str, hours=8):
    """Shift a 'YYYY-MM-DD HH:MM:SS' string by +hours. Returns shifted string."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            dt += timedelta(hours=hours)
            return dt.strftime(fmt)
        except ValueError:
            continue
    return ts_str  # unrecognized format, leave as-is


def _shift_date(date_str, hours=8):
    """Shift a 'YYYY-MM-DD' date if it came from UTC midnight (conservative: no shift)."""
    return date_str  # dates don't need shifting, only timestamps


def migrate_timestamps_utc_to_cst():
    """One-time migration: shift all stored timestamps from UTC to CST (UTC+8h)."""
    if os.path.exists(_MIGRATION_FLAG):
        return

    print("[Migration] Shifting timestamps UTC → CST (+8h)...")

    # 1. timeline.json
    timeline = storage.read_json(storage.timeline_path())
    for entry in timeline:
        if entry.get("time"):
            entry["time"] = _shift_timestamp(entry["time"])
    storage.write_json(storage.timeline_path(), timeline)

    # 2. reminders.json
    reminders_path = storage.reminders_path()
    if os.path.exists(reminders_path):
        import json
        with open(reminders_path, "r", encoding="utf-8") as f:
            reminders = json.load(f)
        for r in reminders:
            if r.get("created_at"):
                r["created_at"] = _shift_timestamp(r["created_at"])
        with open(reminders_path, "w", encoding="utf-8") as f:
            json.dump(reminders, f, ensure_ascii=False, indent=2)

    # Write flag
    storage._ensure_dirs()
    with open(_MIGRATION_FLAG, "w") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    print("[Migration] Done.")


_CLEAR_ALL_CONFIRM_TEXT = "清空全部"


def migrate_reminders_to_chat_history():
    """One-time migration: copy reminder entries from timeline.json into chat_history.json."""
    timeline = storage.read_json(storage.timeline_path())
    chat_history = storage.read_json(storage.chat_history_path())

    existing_ids = {msg.get("id") for msg in chat_history}
    added = 0

    for entry in timeline:
        if entry.get("type") != "reminder":
            continue
        if entry.get("id") in existing_ids:
            continue
        chat_entry = {
            "id": entry["id"],
            "role": "assistant",
            "type": "reminder",
            "text": entry.get("text", ""),
            "image": entry.get("image", ""),
            "time": entry.get("time", ""),
        }
        chat_history.append(chat_entry)
        added += 1

    if added:
        chat_history.sort(key=lambda x: x.get("time", ""))
        storage.write_json(storage.chat_history_path(), chat_history)
        print(f"[Migration] Migrated {added} reminders → chat_history.json")



def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Unified Proactive Message System
# ---------------------------------------------------------------------------
# Legacy channel for morning/nightly/agent-driven proactive messages.
# AttentionEngine uses deliver_attention_intent_once() below so its
# speak_intent queue can be marked delivered/failed atomically.
# ---------------------------------------------------------------------------

import time as _time_module

# ---------------------------------------------------------------------------
# Per-user locks — each proactive channel needs one lock PER user, not a global one.
# A global Lock would cause the second user in a round-robin to immediately
# skip when the first user is mid-send.
# ---------------------------------------------------------------------------

_user_locks_guard = threading.Lock()  # protects the dicts themselves
_proactive_locks: dict[str, threading.Lock] = {}
_morning_locks: dict[str, threading.Lock] = {}
_nightly_locks: dict[str, threading.Lock] = {}
_attention_delivery_inflight_guard = threading.Lock()
_attention_delivery_inflight: set[tuple[str, str]] = set()


def _current_user_id_safe() -> str:
    """Read g.user_id; fall back to '_admin' only if we're outside a Flask context."""
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def _user_lock(locks_dict: dict, uid: str) -> threading.Lock:
    """Lazy-create and return this user's Lock for the given channel."""
    lock = locks_dict.get(uid)
    if lock is not None:
        return lock
    with _user_locks_guard:
        lock = locks_dict.get(uid)
        if lock is None:
            lock = threading.Lock()
            locks_dict[uid] = lock
        return lock


def _claim_attention_delivery(uid: str, intent_id: str) -> bool:
    """Claim one live Attention delivery within this server process."""
    if not uid or not intent_id:
        return False
    key = (uid, intent_id)
    with _attention_delivery_inflight_guard:
        if key in _attention_delivery_inflight:
            return False
        _attention_delivery_inflight.add(key)
        return True


def _release_attention_delivery(uid: str, intent_id: str):
    if not uid or not intent_id:
        return
    with _attention_delivery_inflight_guard:
        _attention_delivery_inflight.discard((uid, intent_id))


def _send_via_agent(intent_prompt, context_extra="", msg_type="proactive"):
    """Unified entry for all proactive messages.

    Build context → call_chat_agent → append to chat → enqueue sleep agent → set proactive time.
    Returns the chat message dict if sent, or None.
    """
    uid = _current_user_id_safe()
    lock = _user_lock(_proactive_locks, uid)
    # Serialize proactive sends so two delivery paths can't race WITHIN one user.
    # Different users no longer block each other.
    if not lock.acquire(timeout=5):
        print(f"[Proactive] Skipped for {uid} — another proactive send in progress")
        return None
    try:
        return _send_via_agent_locked(intent_prompt, context_extra, msg_type)
    finally:
        lock.release()


def _send_via_agent_locked(intent_prompt, context_extra="", msg_type="proactive"):
    # Global cooldown: skip if last proactive message was < 30 min ago
    last_proactive = storage.get_last_proactive_time()
    elapsed = _time_module.time() - last_proactive
    if elapsed < 1800:
        print(f"[Proactive] Skipped — last proactive message was {int(elapsed)}s ago")
        return None

    context_text = _build_chat_context()
    if context_extra:
        context_text += "\n" + context_extra

    history = storage.get_chat_history(limit=15)

    from tools import get_registry
    from ai_config import get_runtime_config
    registry = get_registry()
    tool_handlers = registry.get_handlers()
    provider = get_runtime_config().get("provider", "openai")
    tools_list = registry.to_tools(provider)

    try:
        result = call_chat_agent(
            context_text, history, intent_prompt, tool_handlers,
            tools_list=tools_list,
            agent_mode="proactive",
        )
    except Exception as e:
        print(f"[Proactive] Agent error: {e}")
        return None

    reply_text = result.get("reply", "")
    if not reply_text:
        return None

    now = _user_now()
    msg_id = f"{msg_type}_{now.strftime('%Y%m%d_%H%M%S')}"
    msg = {
        "id": msg_id,
        "role": "assistant",
        "text": reply_text,
        "type": msg_type,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    storage.append_chat_message(msg)
    storage.set_last_proactive_time()

    # Broadcast to this user's SSE clients
    try:
        import sse
        sse.broadcast("chat_message", msg, user_id=_current_user_id_safe())
    except Exception:
        pass

    # Queue for offline device push notifications
    try:
        storage.add_pending_notification(msg_id, reply_text, msg_type)
    except Exception:
        pass

    # NOTE: Proactive messages are NOT enqueued to memory.
    # Miru's self-talk is derived from existing memory; feeding it back as
    # a "fact source" would let her own guesses become facts (compounding
    # error). Only signals from real input sources (user chat, screenshots,
    # identity cascade) are observers.

    print(f"[Proactive] {msg_type} sent: {reply_text[:60]}...")
    return msg


def _pending_attention_intents():
    now = _user_now()
    out = []
    for item in storage.load_attention_intent_queue() or []:
        if not isinstance(item, dict) or item.get("status") != "pending":
            continue
        expires_at = item.get("expires_at") or ""
        if expires_at and expires_at < now.strftime("%Y-%m-%d %H:%M:%S"):
            continue
        out.append(item)
    return out


def _eligible_attention_intents(intent_id: str | None = None):
    now_s = _user_now().strftime("%Y-%m-%d %H:%M:%S")
    intents = _pending_attention_intents()
    if intent_id:
        intents = [x for x in intents if x.get("id") == intent_id]
    out = []
    for item in intents:
        next_after = item.get("next_delivery_after") or ""
        if next_after and next_after > now_s:
            continue
        out.append(item)
    return out


def _update_attention_intent(intent_id: str, patch: dict) -> dict | None:
    if not intent_id:
        return None
    queue = storage.load_attention_intent_queue() or []
    updated = None
    for idx, item in enumerate(queue):
        if not isinstance(item, dict) or item.get("id") != intent_id:
            continue
        merged = {**item, **(patch or {})}
        queue[idx] = merged
        updated = merged
        break
    if updated is not None:
        storage.save_attention_intent_queue(queue)
    return updated


def _proactive_toolset(tool_policy: str):
    """Return tools/handlers allowed for a proactive delivery decision."""
    from tools import get_registry
    from ai_config import get_runtime_config
    registry = get_registry()
    provider = get_runtime_config().get("provider", "openai")

    if tool_policy == "none":
        return {}, []

    all_handlers = registry.get_handlers()
    if tool_policy == "allow_readonly":
        names = {"archival_memory_search", "look_at_screen"}
    else:
        names = set(all_handlers.keys())

    handlers = {name: fn for name, fn in all_handlers.items() if name in names}
    tools_list = []
    for name in handlers:
        tool = registry.get(name)
        if tool is not None:
            tools_list.append(tool.to_tool(provider))
    return handlers, tools_list


def _build_attention_delivery_context(intent: dict) -> str:
    """Full textual context for turning an Attention intent into speech."""
    state = storage.load_attention_state() or {}
    recent_attention = storage.get_recent_attention_log(limit=8) or []
    delivery_log = storage.get_recent_attention_delivery_log(limit=5) or []
    history = storage.get_chat_history(limit=80) or []

    lines = [
        f"当前时间: {_user_now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "【Pending speak_intent】",
        json.dumps(intent if isinstance(intent, dict) else {}, ensure_ascii=False, indent=2),
        "",
        "【我当前没有说出口的状态】",
        json.dumps({
            "trigger": state.get("trigger"),
            "current_inner": state.get("current_inner"),
            "current_user_affect": state.get("current_user_affect"),
            "current_self_emotion": state.get("current_self_emotion"),
            "attention": state.get("attention"),
            "speak_intent_queue": state.get("speak_intent_queue"),
        }, ensure_ascii=False, indent=2),
        "",
        "【最近几段没有说出口的想法】",
    ]
    for entry in recent_attention[-8:]:
        if isinstance(entry, dict) and entry.get("channel") == "inner":
            lines.append(
                f"- [{entry.get('started_at') or entry.get('ts', '')} → {entry.get('updated_at') or ''}] "
                f"{entry.get('text') or entry.get('thought', '')}"
            )

    lines.extend(["", "【Recent conversation】"])
    for msg in history[-30:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        mtype = msg.get("type", "reply" if role == "assistant" else "user")
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- [{msg.get('time', '')}] {role}/{mtype}: {text[:260]}")

    lines.extend(["", "【Active commitments】"])
    try:
        for item in parse_commitments(include_done=False)[:12]:
            lines.append(
                f"- {item.get('urgency', '')}: {item.get('title', '')} "
                f"(deadline: {item.get('deadline', '')})"
            )
    except Exception:
        pass

    if delivery_log:
        lines.extend(["", "【Recent proactive delivery log】"])
        for entry in delivery_log[-5:]:
            if isinstance(entry, dict):
                lines.append(
                    f"- [{entry.get('ts', '')}] intent={entry.get('intent_id', '')} "
                    f"decision={entry.get('decision', '')} reason={entry.get('reason', '')}"
                )

    lines.append("")
    lines.append("我已经决定这个 speak_intent 值得开口。请把它作为主 agent 生成主动消息的上下文。")
    return "\n".join(lines)


def _resolve_proactive_resource_policy(intent: dict, resource_plan: dict | None) -> dict:
    """Return the fixed proactive main-agent policy.

    AttentionEngine owns "should Miru speak".  Once a speak_intent reaches
    delivery, the visible main agent always runs on the chat/pro tier.  The
    old cheap resource planner is intentionally ignored/retired here.
    """
    return {
        "decision": "send_now",
        "model_mode": "v4_pro",
        "tool_policy": "allow_readonly",
        "reasoning": False,
        "tier": "chat",
        "max_iterations": 2,
        "hard_rule_reasons": [],
        "policy_reason": "main_agent_fixed_pro",
        "resource_plan": resource_plan if isinstance(resource_plan, dict) else None,
        "direct_from_attention": True,
    }


def _build_direct_attention_delivery_plan(intent: dict,
                                          resource_plan: dict | None = None) -> dict:
    """Create a main-agent brief directly from AttentionEngine's speak_intent.

    AttentionEngine owns the decision to speak. This plan does not run a second
    send/defer/drop gate; it only translates the intent into instructions for
    the proactive main agent.
    """
    intent = intent if isinstance(intent, dict) else {}
    silent_boundaries = str(
        intent.get("silent_boundaries")
        or intent.get("boundaries")
        or intent.get("avoid")
        or ""
    ).strip()
    structural_avoid = [
        "不要提系统、日志、检测、AttentionEngine 或内部判断",
        "不要复述屏幕内容本身",
        "不要为了索取回应而多问问题",
        "不要把“我不吵你/不打扰你/需要我就叫我/我就在旁边”当成默认收尾",
    ]
    why_now = str(intent.get("why_now") or "").strip()
    care_motive = str(intent.get("care_motive") or "").strip()
    emotional_source = str(intent.get("emotional_source") or "").strip()
    user_need = str(intent.get("user_need") or "").strip()
    message_seed = str(intent.get("message_seed") or "").strip()
    tone = str(intent.get("suggested_tone") or "").strip()
    context_summary = str(intent.get("context_summary") or "").strip()
    approach = str(intent.get("approach") or "").strip()
    content_anchor = str(intent.get("content_anchor") or "").strip()
    miru_impulse = str(intent.get("miru_impulse") or "").strip()
    plan = resource_plan if isinstance(resource_plan, dict) else {}
    planner_brief = str(plan.get("context_brief") or "").strip()
    return {
        "decision": "send_now",
        "confidence": 1.0,
        "reason": why_now or care_motive or "我产生了想靠近用户的 speak_intent",
        "model_mode": plan.get("model_mode", "v4_pro"),
        "tool_policy": plan.get("tool_policy", "allow_readonly"),
        "cooldown_seconds": 0,
        "memory_files": plan.get("memory_files", []),
        "keywords": plan.get("keywords", []),
        "direct_from_attention": True,
        "resource_routing_reason": plan.get("routing_reason", ""),
        "delivery_brief": {
            "goal": care_motive or why_now or "把我想靠近用户的心情转成一条自然主动消息",
            "must_include": [],
            "must_avoid": structural_avoid[:6],
            "tone": tone or "短而有体温，像 Miru 自然加入用户的生活，不像提醒器或状态灯",
            "context_summary": planner_brief or context_summary or why_now or care_motive,
            "care_motive": care_motive,
            "emotional_source": emotional_source,
            "user_need": user_need,
            "approach": approach,
            "content_anchor": content_anchor,
            "miru_impulse": miru_impulse,
            "silent_boundaries": silent_boundaries,
            "silent_boundaries_usage": (
                "silent_boundaries 是内部边界，只用于控制语气和重复风险；"
                "不要把这些限制原样说给用户，不要把退让写成台词。"
            ),
            "message_seed": message_seed,
            "message_seed_usage": (
                "message_seed 只是情绪方向和开口灵感，不是必须逐字包含的句子；"
                "最终消息要围绕 care_motive、user_need、context_summary 自然改写。"
            ),
        },
    }


def _direct_attention_delivery_policy(intent: dict,
                                      resource_plan: dict | None = None) -> dict:
    """Structural delivery policy: no personality/timing veto."""
    return _resolve_proactive_resource_policy(intent, resource_plan)


def evaluate_attention_delivery_preflight_once(intent_id: str | None = None) -> dict | None:
    """Dry-run one pending Attention intent without sending.

    Kept under the old function name for admin/test compatibility. The live
    design no longer has a second preflight gate: if AttentionEngine created a
    speak_intent, the dry-run decision is the direct delivery plan.
    """
    intents = _pending_attention_intents()
    if intent_id:
        intents = [x for x in intents if x.get("id") == intent_id]
    if not intents:
        return None

    intent = intents[0]
    context = _build_attention_delivery_context(intent)
    resource_plan = None
    delivery_plan = _build_direct_attention_delivery_plan(intent, resource_plan)
    policy = _direct_attention_delivery_policy(intent, resource_plan)
    entry = {
        "ts": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
        "intent_id": intent.get("id", ""),
        "topic_key": intent.get("topic_key", ""),
        "decision": policy.get("decision"),
        "model_mode": policy.get("model_mode"),
        "tool_policy": policy.get("tool_policy"),
        "reason": delivery_plan.get("reason", ""),
        "confidence": delivery_plan.get("confidence", 1.0),
        "hard_rule_reasons": policy.get("hard_rule_reasons", []),
        "resource_plan": resource_plan,
        "delivery_plan": delivery_plan,
        "policy": policy,
        "delivery_status": "dryrun_direct",
        "context_preview": context[:4000],
    }
    storage.append_attention_delivery_log(entry)
    return entry


def _append_attention_proactive_message(reply_text: str, intent: dict,
                                        policy: dict) -> dict:
    now = _user_now()
    suffix = hashlib.sha1(
        f"{intent.get('id', '')}:{now.timestamp()}".encode("utf-8")
    ).hexdigest()[:6]
    msg_id = f"attention_proactive_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"
    msg = {
        "id": msg_id,
        "role": "assistant",
        "text": reply_text,
        "type": "proactive",
        "source": "attention_engine",
        "intent_id": intent.get("id", ""),
        "topic_key": intent.get("topic_key", ""),
        "care_motive": intent.get("care_motive", ""),
        "emotional_source": intent.get("emotional_source", ""),
        "user_need": intent.get("user_need", ""),
        "why_i_want_to_say": intent.get("why_i_want_to_say", ""),
        "approach": intent.get("approach", ""),
        "content_anchor": intent.get("content_anchor", ""),
        "miru_impulse": intent.get("miru_impulse", ""),
        "message_seed": intent.get("message_seed", ""),
        "attention_context_summary": intent.get("context_summary", ""),
        "model_mode": policy.get("model_mode", ""),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    storage.append_chat_message(msg)
    storage.set_last_proactive_time()

    try:
        import sse
        sse.broadcast("chat_message", msg, user_id=_current_user_id_safe())
    except Exception:
        pass

    try:
        storage.add_pending_notification(msg_id, reply_text, "proactive")
    except Exception:
        pass

    try:
        from attention_engine import get_attention_engine
        get_attention_engine().record_signal("chat_out", {
            "text": reply_text,
            "source": "attention_delivery",
            "intent_id": intent.get("id", ""),
        })
    except Exception:
        pass

    try:
        import persona_writer_state as pws
        pws.record_proactive_outcome({
            "id": f"proactive_sent:{msg_id}",
            "kind": "proactive_sent",
            "time": msg.get("time", ""),
            "proactive_message_id": msg_id,
            "proactive_text": reply_text,
            "intent_id": intent.get("id", ""),
            "topic_key": intent.get("topic_key", ""),
            "care_motive": intent.get("care_motive", ""),
            "why_i_want_to_say": intent.get("why_i_want_to_say", ""),
            "user_need": intent.get("user_need", ""),
            "approach": intent.get("approach", ""),
            "content_anchor": intent.get("content_anchor", ""),
            "miru_impulse": intent.get("miru_impulse", ""),
            "message_seed": intent.get("message_seed", ""),
            "model_mode": policy.get("model_mode", ""),
            "tool_policy": policy.get("tool_policy", ""),
        })
    except Exception as e:
        print(f"[AttentionDelivery] persona outcome sent record failed: {e}")

    return msg


def _parse_chat_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _attention_delivery_timing_suppression_reason(
        now: datetime | None = None) -> str:
    """Recheck user-chat cadence immediately before proactive delivery.

    Attention evaluates this rule when it creates a speak intent, but delivery
    happens on another thread. A user message can arrive while that intent is
    queued or while the proactive agent is generating, which makes the old
    intent stale and would otherwise produce a second, similar-looking reply.
    """
    from attention_engine import ACTIVE_CHAT_PROACTIVE_SILENCE_SECONDS

    now = now or _user_now()
    # Chat history stores the user's local wall-clock time without a UTC
    # offset. ``user_now()`` becomes timezone-aware once a device has saved a
    # timezone, so compare both values in that same naive local-time domain.
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    for msg in reversed(storage.get_chat_history(limit=200) or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        sent_at = _parse_chat_time(msg.get("time", ""))
        if sent_at is None:
            continue
        age_seconds = (now - sent_at).total_seconds()
        # A future timestamp can only come from clock/timezone skew. Failing
        # closed avoids sending an unsolicited message during active chat.
        if age_seconds < ACTIVE_CHAT_PROACTIVE_SILENCE_SECONDS:
            return "recent_user_message"
        return ""
    return ""


def _suppress_stale_attention_delivery(intent: dict, reason: str) -> dict:
    """Consume a queued intent that became stale before visible delivery."""
    now_s = _user_now().strftime("%Y-%m-%d %H:%M:%S")
    intent_id = intent.get("id", "")
    _update_attention_intent(intent_id, {
        "status": "suppressed",
        "delivery_status": "suppressed",
        "suppressed_at": now_s,
        "suppression_reason": reason,
    })
    entry = {
        "ts": now_s,
        "intent_id": intent_id,
        "topic_key": intent.get("topic_key", ""),
        "decision": "suppress",
        "hard_rule_reasons": [reason],
        "delivery_enabled": False,
        "delivery_status": "suppressed",
        "reason": reason,
    }
    storage.append_attention_delivery_log(entry)
    print(f"[AttentionDelivery] suppressed intent={intent_id} reason={reason}")
    return entry


def _record_proactive_response_if_any(user_msg: dict) -> None:
    """If this user message is the first reply after a proactive message,
    record the relationship outcome for Persona Writer.
    """
    if not isinstance(user_msg, dict) or user_msg.get("role") != "user":
        return
    try:
        history = storage.get_chat_history(limit=80) or []
        current_id = user_msg.get("id", "")
        current_idx = None
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].get("id") == current_id:
                current_idx = idx
                break
        if current_idx is None:
            return

        proactive = None
        for msg in reversed(history[:current_idx]):
            role = msg.get("role")
            if role == "user":
                return
            if role == "assistant":
                if msg.get("type") == "proactive" and msg.get("source") == "attention_engine":
                    proactive = msg
                    break
                return
        if not proactive:
            return

        sent_at = _parse_chat_time(proactive.get("time", ""))
        replied_at = _parse_chat_time(user_msg.get("time", ""))
        delay = None
        if sent_at and replied_at:
            delay = max(0, int((replied_at - sent_at).total_seconds()))

        import persona_writer_state as pws
        pws.record_proactive_outcome({
            "id": f"proactive_response:{proactive.get('id', '')}:{current_id}",
            "kind": "proactive_response",
            "time": user_msg.get("time", ""),
            "proactive_message_id": proactive.get("id", ""),
            "user_message_id": current_id,
            "proactive_text": proactive.get("text", ""),
            "user_reply": user_msg.get("text", ""),
            "response_delay_seconds": delay,
            "intent_id": proactive.get("intent_id", ""),
            "topic_key": proactive.get("topic_key", ""),
            "care_motive": proactive.get("care_motive", ""),
            "why_i_want_to_say": proactive.get("why_i_want_to_say", ""),
            "user_need": proactive.get("user_need", ""),
            "approach": proactive.get("approach", ""),
            "content_anchor": proactive.get("content_anchor", ""),
            "miru_impulse": proactive.get("miru_impulse", ""),
            "message_seed": proactive.get("message_seed", ""),
        })
    except Exception as e:
        print(f"[Chat] proactive response outcome record failed: {e}")


def deliver_attention_intent_once(intent_id: str | None = None) -> dict | None:
    """Deliver one pending Attention speak_intent.

    This is the live path:
      attention_intent_queue.json -> proactive main agent
      -> append chat/SSE/push -> mark intent delivered.

    AttentionEngine owns the decision to speak. This function performs only
    structural delivery work and rechecks the deterministic active-chat rule
    at the asynchronous delivery boundary. It does not use interruptibility
    scores, confidence thresholds, or a second send/defer/drop LLM gate.
    """
    intents = _eligible_attention_intents(intent_id)
    if not intents:
        return None

    intent = intents[0]
    resolved_intent_id = intent.get("id", "")
    uid = _current_user_id_safe()
    if not _claim_attention_delivery(uid, resolved_intent_id):
        print(
            f"[AttentionDelivery] skipped duplicate in-flight "
            f"intent={resolved_intent_id} user={uid}"
        )
        return None

    print(f"[AttentionDelivery] claimed intent={resolved_intent_id} user={uid}")
    try:
        suppression_reason = _attention_delivery_timing_suppression_reason()
        if suppression_reason:
            return _suppress_stale_attention_delivery(intent, suppression_reason)
        return _deliver_claimed_attention_intent(intent)
    finally:
        _release_attention_delivery(uid, resolved_intent_id)


def _deliver_claimed_attention_intent(intent: dict) -> dict | None:
    """Run a delivery after ``deliver_attention_intent_once`` claims it."""
    context = _build_attention_delivery_context(intent)
    from prompt import call_proactive_agent
    resource_plan = None
    delivery_plan = _build_direct_attention_delivery_plan(intent, resource_plan)
    policy = _direct_attention_delivery_policy(intent, resource_plan)
    now_s = _user_now().strftime("%Y-%m-%d %H:%M:%S")
    intent_id = intent.get("id", "")

    entry = {
        "ts": now_s,
        "intent_id": intent_id,
        "topic_key": intent.get("topic_key", ""),
        "decision": policy.get("decision"),
        "model_mode": policy.get("model_mode"),
        "tool_policy": policy.get("tool_policy"),
        "reason": delivery_plan.get("reason", ""),
        "confidence": delivery_plan.get("confidence", 1.0),
        "hard_rule_reasons": policy.get("hard_rule_reasons", []),
        "resource_plan": resource_plan,
        "delivery_plan": delivery_plan,
        "policy": policy,
        "delivery_enabled": True,
        "delivery_status": "not_sent",
    }

    handlers, tools_list = _proactive_toolset(policy.get("tool_policy", "none"))
    context_text = _build_chat_context()
    context_text += "\n【Attention Delivery Context】\n" + context
    history = storage.get_chat_history(limit=20)

    try:
        result = call_proactive_agent(
            context_text,
            history,
            intent,
            delivery_plan,
            handlers,
            tools_list=tools_list,
            tier=policy.get("tier", "chat"),
            reasoning=bool(policy.get("reasoning")),
            reasoning_budget=16000 if policy.get("reasoning") else 0,
            max_iterations=int(policy.get("max_iterations") or 1),
        )
        reply_text = (result.get("reply") or "").strip()
        if not reply_text:
            raise RuntimeError("proactive main agent returned empty reply")
        suppression_reason = _attention_delivery_timing_suppression_reason()
        if suppression_reason:
            return _suppress_stale_attention_delivery(intent, suppression_reason)
        msg = _append_attention_proactive_message(reply_text, intent, policy)
        _update_attention_intent(intent_id, {
            "status": "delivered",
            "delivery_status": "delivered",
            "delivered_at": msg.get("time", now_s),
            "message_id": msg.get("id", ""),
        })
        entry["delivery_status"] = "delivered"
        entry["message_id"] = msg.get("id", "")
        entry["reply_preview"] = reply_text[:160]
        entry["tool_calls"] = result.get("tool_calls", [])
        storage.append_attention_delivery_log(entry)
        print(f"[AttentionDelivery] sent intent={intent_id} msg={msg.get('id', '')}")
        return entry
    except Exception as e:
        err = str(e)[:240]
        _update_attention_intent(intent_id, {
            "delivery_status": "failed",
            "last_delivery_attempt_at": now_s,
            "delivery_error": err,
        })
        entry["delivery_status"] = "failed"
        entry["error"] = err
        storage.append_attention_delivery_log(entry)
        print(f"[AttentionDelivery] failed intent={intent_id}: {err}")
        return entry


def build_proactive_agent_preview(intent: dict, delivery_preflight: dict) -> dict:
    """Build the exact proactive main-agent context/messages without calling it."""
    from prompt import (
        _build_agent_system_prompt,
        _build_chat_messages,
        build_proactive_agent_user_message,
    )
    context_text = _build_chat_context()
    context_text += "\n【Attention Delivery Context】\n" + _build_attention_delivery_context(intent)
    user_prompt = build_proactive_agent_user_message(intent, delivery_preflight)
    return {
        "system_prompt": _build_agent_system_prompt(agent_mode="proactive"),
        "context_text": context_text,
        "messages": _build_chat_messages(storage.get_chat_history(limit=20), user_prompt),
        "user_prompt": user_prompt,
    }


# Schedule reminders are represented as commitments and surfaced through
# AttentionEngine/main-agent context instead of a separate reminder planner.
# The old _generate_daily_reminder_plan / check_reminders system is removed.


def _send_push_notifications(reminders):
    import json as _json

    runtime = server_config.get()
    vapid_private = runtime.get("vapid_private_key", "")
    vapid_public = runtime.get("vapid_public_key", "")
    if not vapid_private or not vapid_public:
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    subs = storage.read_json(storage.push_subscriptions_path())
    if not subs:
        return

    vapid_claims = {"sub": "mailto:contextlife@noreply.dev"}
    dead_endpoints = []

    cfg = get_config()
    for reminder in reminders:
        payload = _json.dumps({
            "title": f"{cfg.name} 提醒 💫",
            "body": reminder.get("text", ""),
            "tag": reminder.get("id", "reminder"),
            "url": "/",
        })
        for sub in subs:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=vapid_private,
                    vapid_claims=vapid_claims,
                )
            except WebPushException as e:
                if "410" in str(e) or "404" in str(e):
                    dead_endpoints.append(sub.get("endpoint"))
            except Exception:
                pass

    if dead_endpoints:
        dead_set = set(dead_endpoints)
        subs = [s for s in subs if s.get("endpoint") not in dead_set]
        storage.write_json(storage.push_subscriptions_path(), subs)


def send_push_message(title: str, body: str, tag: str = "miru-care", url: str = "/"):
    """Send a Web Push notification to all subscribed devices."""
    import json as _json

    runtime = server_config.get()
    vapid_private = runtime.get("vapid_private_key", "")
    vapid_public = runtime.get("vapid_public_key", "")
    if not vapid_private or not vapid_public:
        return

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    subs = storage.read_json(storage.push_subscriptions_path())
    if not subs:
        return

    vapid_claims = {"sub": "mailto:contextlife@noreply.dev"}
    payload = _json.dumps({"title": title, "body": body, "tag": tag, "url": url})
    dead_endpoints = []

    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims=vapid_claims,
            )
        except WebPushException as e:
            if "410" in str(e) or "404" in str(e):
                dead_endpoints.append(sub.get("endpoint"))
        except Exception:
            pass

    if dead_endpoints:
        dead_set = set(dead_endpoints)
        subs = [s for s in subs if s.get("endpoint") not in dead_set]
        storage.write_json(storage.push_subscriptions_path(), subs)


# ---------------------------------------------------------------------------
# Legacy Data Cleanup
# ---------------------------------------------------------------------------

def cleanup_legacy_data():
    """Clean up old data files for new agent architecture."""
    import json as _json
    data_dir = storage.get_data_dir()
    cleaned = []

    # Files to reset to empty arrays
    reset_files = [
        ("chat_history.json", []),
        ("daily_reviews.json", []),
        ("tomorrow_plans.json", []),
    ]
    for fname, empty_val in reset_files:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(empty_val, f)
            cleaned.append(f"reset {fname}")

    # Files to delete entirely
    delete_files = [
        "tomorrow_plan.json",
        "daily_reminder_plan.json",
    ]
    for fname in delete_files:
        path = os.path.join(data_dir, fname)
        if os.path.exists(path):
            os.remove(path)
            cleaned.append(f"deleted {fname}")

    return {"status": "ok", "cleaned": cleaned}


def clear_all_records(confirm_text=""):
    """Dangerous operation: wipe all user records (keeps only user_settings.json).

    Deletes every file and sub-directory in the user's data dir except
    user_settings.json so that preferences survive a reset.
    """
    if str(confirm_text or "").strip() != _CLEAR_ALL_CONFIRM_TEXT:
        return {"ok": False, "error": u"二次确认未通过，请输入\u201c清空全部\u201d后重试。"}

    import shutil

    data_dir = storage.get_data_dir()
    if not os.path.isdir(data_dir):
        return {"ok": False, "error": "用户数据目录不存在"}

    # Safety: data_dir must be under data/users/<uid>/ — refuse to wipe root data/
    norm = os.path.normpath(data_dir)
    if not ("users" in norm.split(os.sep)):
        return {"ok": False, "error": "安全检查失败：数据目录不在用户隔离路径下"}

    # Files/dirs to preserve (settings survive a data reset)
    KEEP = {"user_settings.json"}

    removed_files = 0
    removed_dirs = 0
    errors = []

    for entry in os.listdir(data_dir):
        if entry in KEEP:
            continue
        full = os.path.join(data_dir, entry)
        try:
            if os.path.isfile(full) or os.path.islink(full):
                os.remove(full)
                removed_files += 1
            elif os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
                removed_dirs += 1
        except Exception as e:
            errors.append(f"{entry}: {e}")

    return {
        "ok": True,
        "message": "所有记录已清空",
        "summary": {
            "removed_files": removed_files,
            "removed_dirs": removed_dirs,
            "preserved": list(KEEP),
            "errors": errors[:5] if errors else [],
        },
    }


# ---------------------------------------------------------------------------
# Companion Chat Channel (Claude Agent)
# ---------------------------------------------------------------------------


# _build_tool_handlers() removed — tools now live in tools/ package (auto-discovered via ToolRegistry)


def _format_seconds_compact(seconds) -> str:
    try:
        seconds = max(0, float(seconds))
    except (TypeError, ValueError):
        return "未知"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


def _format_attention_context_for_agent() -> str:
    """Compact Miru inner activity for any main-agent mode.

    This gives the main agent Miru's recent psychological continuity without
    dumping raw attention logs into every user/proactive turn. It is injected
    for both reactive replies and proactive delivery.
    """
    try:
        state = storage.load_attention_state() or {}
        recent = storage.get_recent_attention_log(limit=5) or []
        queue = storage.load_attention_intent_queue() or []
    except Exception:
        return ""

    lines = []
    inner = state.get("current_inner") or {}
    if inner:
        lines.append(
            "- 我最近没有说出口的想法: "
            f"topic={inner.get('topic_key', '') or '(none)'}, "
            f"state={inner.get('state', '') or '?'}, "
            f"duration={inner.get('duration_seconds', 0)}s, "
            f"ticks={inner.get('tick_count', 0)}, "
            f"text={inner.get('text') or inner.get('last_thought', '')}"
        )

    affect = state.get("current_user_affect") or state.get("user_affect") or {}
    if affect:
        lines.append(
            "- 我对用户状态的感觉: "
            f"mood={affect.get('mood', '')}, "
            f"valence={affect.get('valence', '')}, "
            f"arousal={affect.get('arousal', '')}, "
            f"confidence={affect.get('confidence', '')}, "
            f"text={affect.get('text') or affect.get('evidence', '')}"
        )

    self_emotion = state.get("current_self_emotion") or state.get("miru_inner") or {}
    if self_emotion:
        lines.append(
            "- 我自己的心情: "
            f"mood={self_emotion.get('mood', '')}, "
            f"valence={self_emotion.get('valence', '')}, "
            f"arousal={self_emotion.get('arousal', '')}, "
            f"text={self_emotion.get('text') or self_emotion.get('reason', '')}"
        )

    thoughts = []
    for entry in recent[-5:]:
        if not isinstance(entry, dict):
            continue
        if entry.get("channel") != "inner":
            continue
        thought = (entry.get("text") or entry.get("thought") or "").strip()
        if thought:
            started = entry.get("started_at") or entry.get("ts", "")
            updated = entry.get("updated_at") or entry.get("ts", "")
            duration = entry.get("duration_seconds", 0)
            thoughts.append(f"[{started[-8:]}→{updated[-8:]} {duration}s] {thought}")
    if thoughts:
        lines.append("- 最近几段真实内心:\n  " + "\n  ".join(thoughts))

    pending = [
        item for item in queue
        if isinstance(item, dict) and item.get("status") == "pending"
    ][-5:]
    if pending:
        items = []
        for item in pending:
            items.append(
                f"[{item.get('priority', 'low')}] {item.get('topic_key', '')}: "
                f"{item.get('context_summary', '')}"
            )
        lines.append("- 我已经想说但还没说出口的意图:\n  " + "\n  ".join(items))

    if not lines:
        return ""
    return (
        "【我最近没有说出口的想法】\n"
        "这些是我最近没有说出口的理解、情绪和关注焦点；"
        "用于保持语气、连续性和时机判断，不代表用户刚刚说了这些。\n"
        + "\n".join(lines)
        + "\n注意：不要提系统名、日志、检测或 AttentionEngine；像自己的心理连续性一样自然使用。\n"
    )


def _build_chat_context():
    """Build rich context for companion chat.

    Two-tier memory model (Letta-style):
    1. Core Memory — always injected, agent-editable
    2. Archival Memory — searchable via tools, index shown for reference
    """
    now = _user_now()
    context_text = f"当前时间: {now.strftime('%Y-%m-%d %H:%M (%A)')}\n"

    # Identity ground-truth — authoritative user attributes (name/role/etc.)
    # cascaded from onboarding & settings. Injected first so the agent never
    # acts on conflicting info that might have leaked into memory from OCR
    # errors or low-confidence inferences.
    try:
        import identity as _identity
        gt = _identity.compose_ground_truth_block(header=True)
        if gt:
            context_text += "\n" + gt + "\n"
    except Exception:
        pass

    # Core Memory blocks (always in context, agent-editable)
    try:
        import core_memory
        cm_text = core_memory.format_for_context()
        if cm_text:
            context_text += f"\n【核心记忆 Core Memory】\n{cm_text}\n"
    except Exception:
        pass

    # Archival Memory index (truncated reference — agent can search for details)
    try:
        import memory as _mem
        index_content = _mem.read_index()
        if index_content and index_content.strip():
            lines = index_content.split("\n")
            if len(lines) > 100:
                index_content = "\n".join(lines[:100]) + "\n..."
            context_text += f"\n【归档记忆索引 Archival Memory Index】\n{index_content}\n"
    except Exception:
        pass

    # Active commitments — structured summary with urgency
    try:
        items = parse_commitments(include_done=False)
        if items:
            urgency_map = {"overdue": "⚠️逾期", "imminent": "🔴🔴2h内", "today": "🔴今天", "approaching": "🟠6h内", "soon": "🟡3天内"}
            lines = []
            for it in items[:15]:
                tag = urgency_map.get(it["urgency"], "")
                dl = f" (截止: {it['deadline']})" if it.get("deadline") else ""
                prefix = f"{tag} " if tag else "- "
                lines.append(f"{prefix}{it['title']}{dl}")
            context_text += f"\n【活跃承诺 ({len(items)}条)】\n" + "\n".join(lines) + "\n"
            context_text += "（对话中发现承诺已完成时，请主动调用 complete_commitment）\n"
    except Exception:
        pass

    # Recent emotion context — shapes tone and empathy depth
    try:
        recent_emotions = storage.get_recent_emotion_entries(hours=1)
        if recent_emotions:
            total_weight = 0
            weighted_valence = 0
            for i, e in enumerate(recent_emotions):
                weight = 1 + i
                weighted_valence += e.get("valence", 0) * weight
                total_weight += weight
            avg_valence = weighted_valence / total_weight if total_weight else 0

            last = recent_emotions[-1]
            mood = last.get("mood", "neutral")
            source = last.get("source", "")

            if mood != "neutral" and abs(avg_valence) > 0.15:
                user_label = resolve_user_entity_label()
                if avg_valence < -0.2:
                    context_text += (
                        f"\n【情绪感知】{user_label}近期情绪偏低落（{mood}，{source}）。"
                        f"如果{user_label}的消息涉及心情，请用心回应、具体共情，"
                        f"不要说套话。不要主动提及\"情绪系统\"或\"检测到\"。\n"
                    )
                elif avg_valence > 0.2:
                    context_text += (
                        f"\n【情绪感知】{user_label}近期心情不错（{mood}）。"
                        f"回应时自然带上愉快的语气。不要主动提及\"情绪系统\"。\n"
                    )
    except Exception:
        pass

    # AttentionEngine continuity — what Miru has been quietly tracking.
    try:
        attention_ctx = _format_attention_context_for_agent()
        if attention_ctx:
            context_text += "\n" + attention_ctx
    except Exception:
        pass

    # Recent screen observations — per-device, passive awareness of what user is doing
    try:
        from screen_analyzer import get_analyzer
        all_obs = get_analyzer().get_all_recent_observations(max_age_minutes=10)
        if all_obs:
            obs_lines = []
            for o in all_obs:
                obs_lines.append(f"- {o['device_name']} ({o['time'].strftime('%H:%M')}): {o['observation']}")
            context_text += "\n【最近屏幕观测】\n" + "\n".join(obs_lines) + "\n"
    except Exception:
        pass

    # Miru's own emotional state — influences her tone.
    # Note: relationship stage is injected ONCE upstream by prompt._build_chat_system_prompt
    # (which folds in soul.md's "Relationship Stages" block), so we do NOT also
    # inject it here — that was double-injection.
    try:
        import miru_emotion
        emo_ctx = miru_emotion.get_instance().format_for_context()
        if emo_ctx:
            context_text += f"\n{emo_ctx}\n"
    except Exception:
        pass

    return context_text


def build_companion_bootstrap(chat_limit=20):
    """Build a frontend-facing bootstrap payload for companion runtimes.

    This keeps the existing character/chat core intact while exposing a single
    aggregation endpoint that a richer UI runtime (such as AIRI) can consume.
    """
    try:
        limit = int(chat_limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    cfg = get_config()
    schedule = storage.get_today_schedule()
    active_commitments = _get_active_commitments_for_runtime(memory_scope=MEMORY_SCOPE_ALL)
    recent_chat = storage.get_chat_history(limit=limit)

    return {
        "generated_at": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
        "character": {
            "name": cfg.name,
            "user_address": cfg.user_address,
            "avatar_url": "/assets/character-avatar.jpeg",
            "appearance": cfg.appearance,
            "personality": cfg.personality,
        },
        "context_summary": _build_chat_context(),
        "schedule": schedule,
        "active_commitments": active_commitments,
        "active_commitment_count": len(active_commitments),
        "recent_chat": recent_chat,
        "recent_chat_count": len(recent_chat),
    }


def _format_memory_retrieval(result):
    """Format memory search results as context text for the main agent."""
    if not result:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(str(r) for r in result[:10])
    if isinstance(result, dict):
        # Extract meaningful text from dict response
        for key in ("memory_context", "text", "reply", "message", "content"):
            if result.get(key):
                return str(result[key])
    return ""


def _resolve_chat_agent_policy(preflight: dict | None,
                               user_text: str,
                               user_image_descs: list[str] | None = None) -> dict:
    """Visible main-agent policy.

    Preflight is now memory pre-retrieval only.  User-facing replies always
    use the chat/pro tier with tools enabled, for both text and image turns.
    """
    return {
        "mode": "v4_pro",
        "tier": "chat",
        "reasoning": False,
        "reasoning_budget": 0,
        "max_iterations": 8,
        "allow_tools": True,
        "reason": "main_agent_fixed_pro",
    }


def _friendly_chat_generation_error(exc: Exception) -> str:
    """Convert model/setup failures into text that is safe to show in chat."""
    raw = str(exc or "").strip()
    lower = raw.lower()
    if (
        "api key" in lower
        or "ai tier" in lower
        or "not configured" in lower
        or "admin ui" in lower
        or "模型配置还没有完成" in raw
    ):
        return (
            "我还没有配置好模型，暂时没法认真回复你。"
            "请先到设置里的「模型」页填写视觉、聊天、记忆三类 API Key，"
            "保存后我就可以继续陪你聊天了。"
        )
    if "connection" in lower or "timeout" in lower or "无法连接" in raw:
        return "我刚才没连上模型服务，可能是网络或服务器临时不稳定。等一下再试一次，好吗？"
    return "我刚才处理消息时出了点小问题。等一下再发我一次，我会再认真看。"


_STYLE_MAP = {"casual": "随意轻松", "polite": "礼貌温和", "playful": "活泼俏皮"}
_SCHED_MAP = {"early_bird": "早睡早起", "night_owl": "夜猫子", "irregular": "作息不固定"}
_OCC_MAP = {"student": "学生", "developer": "开发者",
            "designer": "设计师", "creator": "创作者", "other": "其他"}


def initialize_from_questionnaire(answers: dict) -> dict:
    """Write onboarding answers into core_memory and self_profile, then
    inject a single customized first-greeting message into chat_history.

    Called from POST /api/onboarding/submit. Per-user — relies on Flask g
    being set by the auth middleware so storage paths resolve correctly.
    """
    import core_memory

    human_lines = []
    if answers.get("name"):
        human_lines.append(f"用户名字: {answers['name']}")
    if answers.get("occupation"):
        human_lines.append(f"身份: {answers['occupation']}")
    if answers.get("focus"):
        human_lines.append(f"最近在忙: {answers['focus']}")
    if answers.get("style"):
        human_lines.append(f"喜欢的说话风格: {_STYLE_MAP.get(answers['style'], answers['style'])}")
    if answers.get("schedule"):
        human_lines.append(f"作息习惯: {_SCHED_MAP.get(answers['schedule'], answers['schedule'])}")

    actions = []
    try:
        storage.mark_onboarding_completed(answers)
        actions.append("onboarding: completed")
    except Exception as e:
        print(f"[Onboarding] completion meta failed: {e}")
        actions.append("onboarding: meta write failed")

    if human_lines:
        content = "\n".join(human_lines) + "\n"
        result = core_memory.append("human", content)
        if result.get("ok"):
            actions.append(f"core_memory.human: +{len(content)} chars")

    # Update self_profile name
    if answers.get("name"):
        try:
            self_profile.update_profile({"canonical_name": answers["name"]})
            actions.append(f"self_profile.name: {answers['name']}")
        except Exception:
            pass

    # Cascade to identity.json (authoritative source for the fact pipeline).
    # Onboarding answers always get confidence=1.0 and become pinned facts.
    try:
        import identity
        ident = identity.cascade_from_onboarding(answers)
        if ident.get("ok") and ident.get("changed"):
            actions.append(f"identity: +{','.join(ident['changed'])}")
    except Exception as e:
        print(f"[Onboarding] identity cascade failed: {e}")

    # Inject the customized first greeting in a BACKGROUND THREAD
    # (2026-05-08: was synchronous → onboarding submit blocked 5-30s on
    # LLM call, user saw spinning overlay forever and often clicked again,
    # which broke the empty-history dedup and produced duplicate greetings.
    # Now: HTTP returns ms-fast, frontend closes overlay immediately, the
    # greeting arrives via SSE when the LLM finishes).
    try:
        uid = _current_user_id_safe()
        udir = storage.get_data_dir()
        threading.Thread(
            target=_inject_custom_first_greeting_bg,
            args=(answers, uid, udir),
            daemon=True,
        ).start()
        actions.append("first_greeting: dispatched (background)")
    except Exception as e:
        print(f"[Onboarding] first-greeting dispatch failed: {e}")

    return {"actions": actions}


# Per-user lock to prevent concurrent greeting generation. Each onboarding
# submit checks/updates this set ATOMICALLY before forking the LLM call.
_first_greeting_lock = threading.Lock()
_first_greeting_in_progress: set[str] = set()


def _inject_custom_first_greeting_bg(answers: dict, user_id: str, user_data_dir: str | None):
    """Background-thread wrapper. Sets up Flask context + dedup guard,
    then calls the actual greeting generator."""
    _csm_set_thread_context(user_id, user_data_dir)
    with _first_greeting_lock:
        if user_id in _first_greeting_in_progress:
            print(f"[Onboarding] greeting already in progress for {user_id}; skipping duplicate")
            return
        # Also re-check chat_history under the lock — another thread may have
        # finished generating between this dispatch and now.
        try:
            history = storage.read_json(storage.chat_history_path()) or []
        except Exception:
            history = []
        if history:
            print(f"[Onboarding] chat_history non-empty for {user_id}; skipping greeting")
            return
        _first_greeting_in_progress.add(user_id)
    try:
        _inject_custom_first_greeting(answers)
    except Exception as e:
        print(f"[Onboarding] first-greeting bg generation failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with _first_greeting_lock:
            _first_greeting_in_progress.discard(user_id)


def _default_first_greeting(character_name: str) -> str:
    return f"……我是 {character_name}。以后就由我陪在你旁边了。"


def _chat_tier_ready_for_greeting() -> bool:
    """Return True only when the owner has actually configured a chat model."""
    try:
        import ai_config
        cfg = ai_config.get_tier_config("chat")
        return bool(
            (cfg.get("api_key") or "").strip()
            and (cfg.get("host") or "").strip()
            and (cfg.get("model") or "").strip()
        )
    except Exception:
        return False


def _inject_custom_first_greeting(answers: dict) -> dict | None:
    """Generate and store Miru's first message based on onboarding answers.

    Idempotent: if chat_history already has any message, returns None
    (won't overwrite an established conversation).

    Returns the inserted message dict, or None if skipped/failed.
    """
    history = storage.read_json(storage.chat_history_path())
    if history:
        # Already has messages — never double-inject.
        return None

    cfg = get_config()
    now = _user_now()
    time_period = "早上" if now.hour < 12 else ("下午" if now.hour < 18 else "晚上")

    name = (answers.get("name") or "").strip()
    occupation_raw = (answers.get("occupation") or "").strip()
    occupation_label = _OCC_MAP.get(occupation_raw, occupation_raw) if occupation_raw else ""
    focus = (answers.get("focus") or "").strip()
    style_raw = (answers.get("style") or "").strip()
    style_label = _STYLE_MAP.get(style_raw, style_raw) if style_raw else ""
    schedule_raw = (answers.get("schedule") or "").strip()
    schedule_label = _SCHED_MAP.get(schedule_raw, schedule_raw) if schedule_raw else ""

    user_label = name if name else resolve_user_entity_label()
    speech_address = name if name else safe_user_address(cfg.user_address)

    system_prompt = (
        f"你是 {cfg.name}，刚通过{user_label}填写的简短自我介绍认识了 ta。\n"
        f"内部称呼规则：本 prompt 里的“你”指 {cfg.name}，{user_label} 指当前用户。\n\n"
        "【你的人格】\n"
        f"{normalize_character_section((cfg.personality or '')[:400], user_label=user_label, miru_name=cfg.name)}\n\n"
        "【说话方式】\n"
        f"{normalize_character_section((cfg.speech_patterns or '')[:300], user_label=user_label, miru_name=cfg.name)}\n\n"
        "任务：写一句破冰话，让 ta 立刻感觉到「我被认真记住了 / 我刚才说的话被认真听见了」。\n\n"
        "要求：\n"
        f"- 第一人称「我」，称呼对方用「{speech_address}」\n"
        "- 必须**自然引用** ta 提供的信息中至少 1 条（focus / 身份 / 风格 / 作息），\n"
        "  但不是清单式复述，是顺势聊起来\n"
        "- 50-80 字，2-3 句\n"
        "- 加藤惠式自然温柔，不夸张不戏剧化，不要「哇」「呀」开头\n"
        f"- 时段感：现在是{time_period}\n"
        + (f"- ta 希望我用「{style_label}」的风格说话\n" if style_label else "")
        + "- 不要问无聊问题（避免「你今天过得怎么样」）\n"
        "- 可以问一个跟 ta focus 相关的具体小问题作收尾，但点到为止\n"
        "- 当作刚听完 ta 介绍后说出来的第一句话，不是写信\n"
        "- 直接输出消息文本，不要带引号、不要分段编号\n"
    )

    user_prompt = (
        "ta 刚告诉我:\n"
        f"- 怎么称呼: {name or '(没说)'}\n"
        f"- 身份: {occupation_label or '(没说)'}\n"
        f"- 最近在忙: {focus or '(没说)'}\n"
        f"- 希望我用什么风格: {style_label or '(没说)'}\n"
        f"- 作息: {schedule_label or '(没说)'}\n\n"
        f"现在 {time_period}。请生成你想说的第一句话。"
    )

    greeting = None
    skip_llm_greeting = bool(answers.get("_skip_llm_greeting"))
    if not skip_llm_greeting and _chat_tier_ready_for_greeting():
        try:
            from prompt import _call_llm_text
            raw = _call_llm_text(
                system_prompt, user_prompt,
                temperature=0.8, max_tokens=10000, tier="chat",
                call_label="OnboardingGreeting",
            )
            if isinstance(raw, str):
                greeting = raw.strip().strip('"').strip("'").strip()
        except Exception as e:
            print(f"[Onboarding] greeting LLM failed: {e}")

    if not greeting or len(greeting) < 5:
        greeting = _default_first_greeting(cfg.name)

    # Defense-in-depth re-check AFTER the slow LLM call. Even if two
    # threads slipped past the upstream lock, only the first to reach
    # this point writes the message. (chat_history is the single source
    # of truth for "has Miru spoken yet".)
    try:
        latest_history = storage.read_json(storage.chat_history_path()) or []
        if latest_history:
            print(f"[Onboarding] history populated during LLM call; skipping greeting append")
            return None
    except Exception:
        pass

    msg = {
        # No `type` field on purpose — this is a self-introduction, not a
        # proactive care message. AttentionEngine uses chat history as context,
        # but this first greeting is still a self-introduction, not a care ping.
        "id": "greeting_" + now.strftime("%Y%m%d_%H%M%S"),
        "role": "assistant",
        "text": greeting,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    storage.append_chat_message(msg)
    try:
        storage.ensure_first_meet_date()
    except Exception:
        pass

    # SSE push so the SPA shows the greeting the moment the onboarding
    # overlay closes (instead of waiting for a poll).
    try:
        import sse
        sse.broadcast("chat_message", msg, user_id=_current_user_id_safe())
    except Exception as e:
        print(f"[Onboarding] greeting broadcast failed: {e}")

    print(f"[Onboarding] first greeting injected ({len(greeting)} chars)")
    return msg


# ---------------------------------------------------------------------------
# Morning Message — unified daily greeting (08:00-08:30)
# ---------------------------------------------------------------------------

def check_morning_message():
    """DEPRECATED — spontaneous wake-up greetings are now represented by
    AttentionEngine speak_intent, not sent by this maintenance loop.

    Kept as no-op for backward compatibility with app.py reminder loop calls.
    """
    pass


def _check_morning_message_unlocked():
    now = _user_now()
    today_str = now.strftime("%Y-%m-%d")

    # Only 08:00-08:30 window
    if now.hour != 8 or now.minute > 30:
        return

    # Dedup
    key = f"morning_message_{today_str}"
    sent_keys = storage.get_sent_reminder_keys(today_str)
    if key in sent_keys:
        return

    # Also skip if old-format keys exist (backward compat from previous runs)
    if f"morning_reminder_{today_str}" in sent_keys or storage.get_last_greeting_date() == today_str:
        storage.record_reminder(key, today_str, "", None, skip_chat=True)
        return

    # Need chat history to exist (first greeting handles empty)
    history = storage.read_json(storage.chat_history_path())
    if not history:
        return

    # Simple awake check: if user sent messages after 7 AM today, they're already active
    user_msgs_today = [m for m in history
                       if (m.get("time") or "")[:10] == today_str and m.get("role") == "user"]
    if user_msgs_today:
        latest_hour = 0
        for m in user_msgs_today:
            try:
                latest_hour = max(latest_hour, int((m.get("time") or "")[11:13]))
            except (ValueError, IndexError):
                pass
        if latest_hour >= 7:
            # User already active today — mark done, _get_greeting_context handles in-chat greet
            storage.record_reminder(key, today_str, "", None, skip_chat=True)
            storage.set_last_greeting_date(today_str)
            return

    # Build rich context for the agent
    import memory

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_journal = memory.read_file(f"journal/{yesterday}.md") or ""
    commitments = memory.read_file("commitments/active.md") or ""

    context_parts = []
    if yesterday_journal:
        context_parts.append(f"【昨日日记】\n{yesterday_journal[:500]}")
    if commitments:
        context_parts.append(f"【活跃承诺】\n{commitments[:500]}")

    # 周一: 在日记 context 里附上周完成的承诺，给周一日记一个回顾笔触
    is_monday = now.weekday() == 0
    if is_monday:
        done_text = memory.read_file("commitments/done.md") or ""
        if done_text:
            context_parts.append(f"【上周完成的承诺】\n{done_text[:500]}")

    # Days together
    meta = storage.get_relationship_meta()
    first_meet = meta.get("first_meet_date")
    days_together = 0
    if first_meet:
        try:
            days_together = (now - datetime.strptime(first_meet, "%Y-%m-%d")).days
        except ValueError:
            pass

    # Miru's mood
    miru_mood = ""
    try:
        import miru_emotion
        miru_mood = miru_emotion.get_instance().get_mood_text()
    except Exception:
        pass

    cfg = get_config()
    user_label = resolve_user_entity_label()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    intent_prompt = (
        f"（自动触发：早安）\n"
        f"今天是 {now.strftime('%Y-%m-%d')} {weekday_names[now.weekday()]}，"
        f"你和{user_label}认识了{days_together}天。\n"
        f"你现在的心情：{miru_mood or '平静'}。\n\n"
        f"请给{user_label}发一条早安消息。要求：\n"
        f"- 像给一个你在意的人发微信一样自然\n"
        f"- 如果有昨天的记忆，自然地提到\n"
        f"- 如果有快到deadline的承诺，自然提一句\n"
    )
    if is_monday:
        intent_prompt += f"- 今天是周一，可以简单回顾一下上周做了什么\n"
    intent_prompt += (
        f"- 可以用 archival_memory_search 搜索记忆来让消息更有温度\n"
        f"- 50-150字\n"
    )

    context_extra = "\n".join(context_parts)
    msg = _send_via_agent(intent_prompt, context_extra=context_extra, msg_type="morning_message")

    if msg:
        storage.record_reminder(key, today_str, msg.get("text", "")[:80], None, skip_chat=True)
        storage.set_last_greeting_date(today_str)
        print(f"[MorningMessage] Sent for {today_str}")
    else:
        print(f"[MorningMessage] Agent did not send (cooldown or error)")


# ---------------------------------------------------------------------------
# Async Chat System — 3-state machine: IDLE → COLLECTING → GENERATING
# ---------------------------------------------------------------------------
#
# States:
#   IDLE       — no pending messages, no generation running
#   COLLECTING — messages accumulating, quiet timer running
#   GENERATING — LLM generating (classify + generate + followup wait)
#
# Transitions:
#   IDLE + msg → COLLECTING (start quiet timer)
#   COLLECTING + msg → COLLECTING (reset quiet timer)
#   COLLECTING + quiet → GENERATING (start _process_batch thread)
#   GENERATING + msg → pending.append (no timer, generation checks on finish)
#   GENERATING + done + pending empty → send reply → IDLE
#   GENERATING + done + pending exists → discard reply → COLLECTING or GENERATING
#

import time as _time

# ---------------------------------------------------------------------------
# Daily greeting system — morning greetings & late-night care
# ---------------------------------------------------------------------------

def _get_greeting_context():
    """Return greeting instruction for the agent if applicable.

    Checks:
    1. First message today → morning greeting
    2. Late night (23:00-03:00) → late night care (once per night)

    Returns instruction string to prepend to agent prompt, or empty string.
    """
    now = _user_now()
    today_str = now.strftime("%Y-%m-%d")
    current_hour = now.hour

    meta = storage.get_greeting_meta()
    cfg = get_config()
    user_label = resolve_user_entity_label()

    # --- Morning greeting: first message of the day ---
    if meta.get("last_morning_date") != today_str:
        # Get character's current activity from plan
        activity_hint = _get_character_activity_hint(now)
        meta["last_morning_date"] = today_str
        storage.save_greeting_meta(meta)

        time_period = "早上"
        if current_hour >= 12 and current_hour < 18:
            time_period = "下午"
        elif current_hour >= 18:
            time_period = "晚上"

        greeting = (
            f"[系统提示 — 今日首次对话]\n"
            f"这是{user_label}今天第一次来找你。"
            f"请在回复开头自然地打个{time_period}招呼，"
            f"像一个活生生的陪伴者看到{user_label}来了一样。"
        )
        if activity_hint:
            greeting += f"\n你当前正在{activity_hint}，可以自然地提一句你在做什么。"
        greeting += (
            f"\n注意：招呼要自然简短（1句话），然后正常回应{user_label}的消息。"
            f"不要用模板化的问候。每天的招呼应该不一样，像真人一样有变化。"
        )
        print(f"[Greeting] Morning greeting triggered for {today_str}")
        return greeting

    # --- Late night care: 23:00-03:00, once per night ---
    is_late = current_hour >= 23 or current_hour < 3
    night_key = today_str if current_hour >= 23 else (now - _timedelta(days=1)).strftime("%Y-%m-%d")
    if is_late and meta.get("last_latenight_date") != night_key:
        activity_hint = _get_character_activity_hint(now)
        meta["last_latenight_date"] = night_key
        storage.save_greeting_meta(meta)

        greeting = (
            f"[系统提示 — 深夜关怀]\n"
            f"现在是深夜{now.strftime('%H:%M')}，{user_label}还在活跃。"
            f"请在回复中自然地关心一下{user_label}还没休息这件事，"
            f"像一个关心{user_label}的陪伴者一样温柔提醒。"
        )
        if activity_hint:
            greeting += f"\n你当前{activity_hint}，可以用角色的方式融入回复。"
        greeting += (
            f"\n注意：关心要自然轻柔（不是命令式的\"快去睡觉\"），然后正常回应消息。"
        )
        print(f"[Greeting] Late night care triggered at {now.strftime('%H:%M')}")
        return greeting

    return ""


def _get_character_activity_hint(now):
    """Get character's current activity hint."""
    return ""


from datetime import timedelta as _timedelta


def _csm_set_thread_context(user_id, user_data_dir=None):
    """Set Flask g context for a background thread (CSM, background tasks).

    This allows storage.get_data_dir() and other g-aware functions to
    resolve the correct per-user data directory outside of HTTP requests.
    Pushes a Flask app context if one is not already active.
    """
    try:
        from flask import g, has_app_context
        if not has_app_context():
            # Import the Flask app and push its context
            import app as _app_mod
            ctx = _app_mod.app.app_context()
            ctx.push()
        g.user_id = user_id
        if user_data_dir:
            g.user_data_dir = user_data_dir
        else:
            import auth
            g.user_data_dir = auth.get_user_data_dir(user_id)
        g.is_admin = (user_id == "_admin")
    except (RuntimeError, ImportError):
        pass


_csm_lock = threading.Lock()   # protects all per-user state dicts

# Per-user CSM state (keyed by user_id)
_csm_state = {}          # {user_id: "IDLE"|"COLLECTING"|"GENERATING"}
_csm_pending = {}        # {user_id: [msg, ...]}
_csm_quiet_timer = {}    # {user_id: Timer | None}
_csm_typing = {}         # {user_id: bool}
_csm_last_msg_time = {}  # {user_id: float}

_QUIET_PERIOD = 1.5            # seconds of silence before processing


def _csm_get(user_id):
    """Return (state, pending, timer, typing, last_time) for user, with defaults."""
    return (
        _csm_state.get(user_id, "IDLE"),
        _csm_pending.setdefault(user_id, []),
        _csm_quiet_timer.get(user_id),
        _csm_typing.get(user_id, False),
        _csm_last_msg_time.get(user_id, 0.0),
    )


def get_typing_status(user_id=None):
    """Return whether Airi is currently generating a reply for a user."""
    if user_id is None:
        try:
            from flask import g
            user_id = getattr(g, "user_id", "_admin")
        except (RuntimeError, ImportError):
            user_id = "_admin"
    return _csm_typing.get(user_id, False)


def receive_chat_message(text, image=None, device_id=None):
    """Receive a user message. Returns immediately."""
    # Capture user context from Flask g (before leaving request scope)
    try:
        from flask import g
        user_id = getattr(g, "user_id", "_admin")
        user_data_dir = getattr(g, "user_data_dir", None)
    except (RuntimeError, ImportError):
        user_id = "_admin"
        user_data_dir = None

    now = _user_now()
    msg_id = now.strftime("chat_%Y%m%d_%H%M%S") + f"_{random.randint(100,999)}"

    user_msg = {
        "id": msg_id,
        "role": "user",
        "text": text,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if image:
        user_msg["image"] = image
        # Image → text description at the system boundary. Chat / memory
        # tiers are pure-text now; only vision tier ever sees raw bytes.
        # Failure here is not fatal — describe_image() returns "(图片识别失败)"
        # so downstream readers always see *something*.
        try:
            img_path = os.path.join(storage._uploads_dir(), image)
            if os.path.exists(img_path):
                from vision import describe_image_file
                desc = describe_image_file(img_path, hint="用户在聊天里发的图片")
                if desc:
                    user_msg["image_desc"] = desc
        except Exception as e:
            print(f"[csm] vision.describe_image failed: {e}")
            user_msg["image_desc"] = "(图片识别失败)"
    # Phase 2: persist device_id on the user message itself, not just on the
    # transient SSE broadcast. The SSE broadcast already echoes it for live
    # de-dup, but historical re-renders (loadCompanionChatHistory after
    # reconnect, polling fallback, multi-device sync) need the field too —
    # otherwise the receiver can't tell which device actually sent the line.
    if device_id:
        user_msg["device_id"] = device_id
    storage.append_chat_message(user_msg)
    _record_proactive_response_if_any(user_msg)

    # Broadcast to all SSE clients (other devices see the message instantly)
    try:
        import sse
        sse.broadcast("chat_message", user_msg, user_id=user_id)
    except Exception:
        pass

    # Notify AttentionEngine: chat_in is a strong attention signal. The engine
    # updates Miru's inner state and may queue a speak_intent for direct delivery.
    try:
        from attention_engine import get_attention_engine
        get_attention_engine().record_signal("chat_in", {
            "text": text,
            "device_id": device_id or "",
        })
    except Exception:
        pass

    # User message → sleep_agent v2 queue (3-min debounce, batched).
    # The sleep agent collects msgs, periodically asks 1 cheap LLM to
    # summarize the batch into self-contained "fragments", and then fires
    # each fragment through the 4-pass router. This intentionally lags by
    # a few minutes — Miru's main agent already has chat_history in its
    # prompt, so memory freshness is irrelevant to chat quality.
    try:
        from sleep_agent import get_sleep_agent
        get_sleep_agent().enqueue("user", text, user_msg["time"],
                                   image=user_msg.get("image"))
    except Exception as e:
        print(f"[csm] sleep_agent enqueue failed: {e}")

    with _csm_lock:
        # Add internal metadata (not persisted — already saved above)
        user_msg["_recv_time"] = _time.time()
        user_msg["_user_id"] = user_id
        user_msg["_user_data_dir"] = user_data_dir
        user_msg["_device_id"] = device_id or ""
        _csm_pending.setdefault(user_id, []).append(user_msg)
        _csm_last_msg_time[user_id] = user_msg["_recv_time"]

        state = _csm_state.get(user_id, "IDLE")
        if state == "IDLE":
            _csm_state[user_id] = "COLLECTING"
            _csm_start_quiet_timer(user_id)
        elif state == "COLLECTING":
            _csm_start_quiet_timer(user_id)  # reset
        # GENERATING: just append to pending, generation will check on finish

    return {"status": "received", "msg_id": msg_id}


def _csm_start_quiet_timer(user_id):
    """Start/reset quiet timer for a user. Must be called under _csm_lock."""
    old = _csm_quiet_timer.get(user_id)
    if old is not None:
        old.cancel()
    t = threading.Timer(_QUIET_PERIOD, _csm_on_quiet, args=(user_id,))
    t.daemon = True
    t.start()
    _csm_quiet_timer[user_id] = t


def _csm_on_quiet(user_id):
    """Quiet period expired — transition COLLECTING → GENERATING for user."""
    with _csm_lock:
        _csm_quiet_timer[user_id] = None
        pending = _csm_pending.get(user_id, [])
        if _csm_state.get(user_id) != "COLLECTING" or not pending:
            return
        batch = list(pending)
        pending.clear()
        _csm_state[user_id] = "GENERATING"

    threading.Thread(target=_csm_process_batch, args=(batch, user_id), daemon=True).start()


def _csm_process_batch(batch, user_id):
    """Single entry point for all generation. Runs in one background thread.
    Always sends to main agent — no classification, no followup path."""

    # Merge any messages that arrived while thread was starting
    with _csm_lock:
        pending = _csm_pending.get(user_id, [])
        if pending:
            batch.extend(pending)
            pending.clear()
            print(f"[Chat] Merged {len(batch)} messages before generation")

    _csm_do_full_reply(batch, user_id)


def _csm_do_full_reply(batch, user_id):
    """Generate full reply for a specific user. Runs in background thread.

    Hardened against silent thread death. Any uncaught exception inside
    this function used to leave _csm_state[user_id]="GENERATING" forever,
    silently swallowing every subsequent message into _csm_pending. The
    outer try/except below guarantees the state machine recovers and the
    user sees an error message instead of a permanent freeze.
    """
    # Restore Flask-like context so storage.get_data_dir() resolves correctly
    user_data_dir = batch[0].get("_user_data_dir") if batch else None
    _csm_set_thread_context(user_id, user_data_dir)

    _csm_typing[user_id] = True
    last_msg_id = batch[-1].get("id", "") if batch else ""

    # Defensive default — guarantees reply_msg is defined even if BOTH the
    # success path AND the personalized error fallback raise. Without this,
    # `_csm_try_deliver(reply_msg, ...)` below would NameError, the daemon
    # thread would die silently, and state would be stuck.
    reply_msg = {
        "id": last_msg_id + "_reply",
        "role": "assistant",
        "text": "（出现了未预期的错误，请重新发送一遍。）",
        "time": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    device_id_for_emotion = ""

    try:
        combined_text = "\n".join(m.get("text", "") for m in batch)

        try:
            import sse as _sse_mod
            _sse_mod.broadcast("typing_start", {}, user_id=user_id)
        except Exception:
            pass
        try:
            context_text = _build_chat_context()

            # Daily greeting / late-night care injection
            greeting_ctx = _get_greeting_context()
            if greeting_ctx:
                context_text += "\n" + greeting_ctx

            # Collect image *descriptions* from batch. The chat tier is
            # pure-text (DeepSeek V4-flash), so vision.describe_image()
            # has already converted each upload into 120-180 字 Chinese
            # text inside core.receive_chat_message — we just read it back
            # from msg.image_desc here.
            user_image_descs = []
            for m in batch:
                if m.get("image"):
                    desc = (m.get("image_desc") or "").strip()
                    if desc:
                        user_image_descs.append(desc)
                    else:
                        # Older messages without image_desc — fallback to a
                        # generic placeholder so the agent at least knows
                        # an image was attached.
                        user_image_descs.append("(图片)")

            preflight = {}

            # Preflight is text-only memory pre-retrieval. Image content is
            # folded in through image_desc; model policy stays fixed pro.
            try:
                from prompt import call_chat_preflight
                preflight = call_chat_preflight(
                    combined_text, user_image_descs=user_image_descs or None,
                )
                memory_context = _format_memory_retrieval(
                    preflight.get("memory_context", "") if isinstance(preflight, dict) else preflight
                )
                if memory_context:
                    context_text += "\n" + memory_context
            except Exception as e:
                print(f"[Chat] Preflight failed (non-fatal): {e}")
                preflight = {}

            history = storage.get_chat_history(limit=20)
            batch_ids = {m["id"] for m in batch}
            history = [m for m in history if m.get("id") not in batch_ids]

            from tools import get_registry
            from ai_config import get_runtime_config
            registry = get_registry()
            tool_handlers = registry.get_handlers()
            provider = get_runtime_config().get("provider", "openai")
            tools_list = registry.to_tools(provider)
            chat_policy = _resolve_chat_agent_policy(
                preflight, combined_text, user_image_descs=user_image_descs or None,
            )
            effective_tools = tools_list if chat_policy["allow_tools"] else []
            print(
                f"[Chat] memory preflight -> fixed {chat_policy['mode']} tier={chat_policy['tier']} "
                f"reasoning={chat_policy['reasoning']} tools={len(effective_tools)} "
                f"reason={chat_policy.get('reason', '')}"
            )

            result = call_chat_agent(
                context_text, history, combined_text, tool_handlers,
                tools_list=effective_tools, user_image_descs=user_image_descs or None,
                max_iterations=chat_policy["max_iterations"],
                tier=chat_policy["tier"],
                reasoning=chat_policy["reasoning"],
                reasoning_budget=chat_policy["reasoning_budget"],
            )

            reply_msg = {
                "id": last_msg_id + "_reply",
                "role": "assistant",
                "text": result.get("reply", ""),
                "tool_calls": result.get("tool_calls", []),
                "time": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        except Exception as e:
            print(f"[Chat] Generation error: {e}")
            import traceback
            traceback.print_exc()
            reply_msg = {
                "id": last_msg_id + "_reply",
                "role": "assistant",
                "text": _friendly_chat_generation_error(e),
                "time": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        finally:
            # 2026-05-09 fix: do NOT clear _csm_typing or broadcast typing_stop
            # here. That used to cause the main chat window's "正在输入..."
            # indicator to flash off→on whenever the user sent a follow-up
            # message during generation: this finally fires immediately on
            # LLM completion, but `_csm_try_deliver` below then sees a
            # non-empty pending and starts a new cycle which re-sets
            # _csm_typing=True ~10ms later. SSE clients that don't have an
            # optimistic lock (the main window — pet.html does have one,
            # which is why pet wasn't affected) saw the flash.
            #
            # The clear + broadcast is now inside _csm_try_deliver, on the
            # pending-empty (truly-done) branch only. The OUTER except below
            # (line ~1733) still runs an emergency typing_stop for the
            # crash case where _csm_try_deliver never gets called.
            pass

        # --- Deliver or discard (always reached, reply_msg is always set) ---
        # Pick most-recent device from batch for emotion tagging
        for m in reversed(batch):
            if m.get("_device_id"):
                device_id_for_emotion = m["_device_id"]
                break
        _csm_try_deliver(reply_msg, user_id, device_id_for_emotion)

    except Exception as crash:
        # ----- Safety net -----
        # Reaching here means something inside the inner block escaped (e.g.,
        # NameError because reply_msg got unset in some unexpected path, or
        # _csm_try_deliver itself crashed). The state machine MUST recover so
        # the user can keep chatting.
        print(f"[Chat] CRITICAL: uncaught error in _csm_do_full_reply: {crash}")
        import traceback
        traceback.print_exc()
        try:
            _csm_typing[user_id] = False
        except Exception:
            pass
        # Broadcast typing_stop so any stuck "正在输入..." indicator clears
        try:
            import sse as _sse_emergency
            _sse_emergency.broadcast("typing_stop", {}, user_id=user_id)
        except Exception:
            pass
        # Best-effort: surface SOMETHING to the user via SSE + persistence.
        # Either step failing must not stop the rest from running.
        try:
            import sse as _sse_emergency2
            _sse_emergency2.broadcast("chat_message", reply_msg, user_id=user_id)
        except Exception:
            pass
        try:
            storage.append_chat_message(reply_msg)
        except Exception:
            pass
        # Force the state machine back to a runnable state. Without this the
        # user is permanently silenced — every new message would just queue
        # into _csm_pending and never be processed.
        try:
            with _csm_lock:
                if _csm_pending.get(user_id):
                    _csm_start_next_cycle(user_id)
                else:
                    _csm_state_to_idle(user_id)
        except Exception:
            pass



def _csm_try_deliver(reply_msg, user_id, device_id=""):
    """Deliver reply if pending is empty, otherwise discard and reprocess.

    2026-05-09: also owns the typing_stop / _csm_typing=False side-effect
    (moved from _csm_do_full_reply.finally). When pending is non-empty we
    DON'T flip typing — the next cycle will keep it True, so SSE clients
    see one continuous "typing" state across the cycles.
    """
    with _csm_lock:
        pending = _csm_pending.get(user_id, [])
        if pending:
            # Cross-cycle: keep _csm_typing True so the main window's typing
            # indicator stays solid. _csm_start_next_cycle either runs
            # immediately (state→GENERATING, sets _csm_typing=True again on
            # the new thread, no flash) or waits for the quiet timer (state
            # stays GENERATING here, _csm_typing stays True from the prior
            # cycle).
            print(f"[Chat] Suppressing stale response — {len(pending)} new messages arrived")
            _csm_start_next_cycle(user_id)
            return
        # Pending is empty — TRUE end of generation. Clear typing here.
        # A storage write failure (disk full, JSON encode error, broken
        # Flask g) must NOT block the state reset: leaving the user
        # permanently silenced waiting for a non-existent reply is strictly
        # worse than a missed persistence record.
        try:
            storage.append_chat_message(reply_msg)
        except Exception as e:
            print(f"[Chat] storage.append_chat_message failed (non-fatal for state): {e}")
        _csm_typing[user_id] = False
        _csm_state_to_idle(user_id)
        print(f"[Chat] Reply delivered: {reply_msg.get('text', '')[:60]}...")

    # Broadcast assistant message FIRST, then typing_stop. Order matters:
    # the frontend's chat_message handler also calls _hideTypingIndicator
    # (so the indicator is gone the moment the bubble lands), and the
    # subsequent typing_stop is then a no-op. If we sent typing_stop first,
    # there'd be a sub-second window where typing is hidden but the bubble
    # hasn't appeared yet — looks like the indicator vanished early.
    try:
        import sse
        sse.broadcast("chat_message", reply_msg, user_id=user_id)
    except Exception:
        pass
    try:
        import sse as _sse_stop
        _sse_stop.broadcast("typing_stop", {}, user_id=user_id)
    except Exception:
        pass

    # Enqueue Miru's reply to sleep_agent so the v2 LLM has full
    # conversation context. The LLM prompt is explicit that fragments
    # must only describe the *user* — Miru's words are context only.
    try:
        text = reply_msg.get("text", "")
        if text:
            from sleep_agent import get_sleep_agent
            get_sleep_agent().enqueue("assistant", text, reply_msg.get("time"))
    except Exception:
        pass

    # Notify AttentionEngine that Miru replied.  The engine decides whether
    # this changes user affect / Miru inner state, but it does not deliver a
    # proactive message in phase 1.
    try:
        from attention_engine import peek_attention_engine
        inst = peek_attention_engine(user_id)
        if inst is not None:
            inst.record_signal("chat_out", {
                "text": reply_msg.get("text", ""),
                "device_id": device_id or "",
            })
    except Exception:
        pass

    # NOTE: assistant reply is NOT enqueued to memory.
    # Miru's reply is derived from her existing memory + LLM inference;
    # feeding her own words back as a "fact source" creates a feedback
    # loop where guesses become facts. Memory is fed only by real input
    # signals (user chat / screenshots / identity cascade) — see Curator.

    # User emotion and Miru's own emotion are now updated by AttentionEngine
    # from the full attention snapshot.  The legacy per-chat emotion workers
    # remain callable for tests/backward compatibility, but production no
    # longer dispatches them here.


def _csm_start_next_cycle(user_id):
    """Called under lock. Pending exists — start new cycle for user."""
    pending = _csm_pending.get(user_id, [])
    elapsed = _time.time() - _csm_last_msg_time.get(user_id, 0.0)
    if elapsed >= _QUIET_PERIOD:
        # User has been quiet long enough → process immediately
        batch = list(pending)
        pending.clear()
        _csm_state[user_id] = "GENERATING"
        threading.Thread(target=_csm_process_batch, args=(batch, user_id), daemon=True).start()
    else:
        # User might still be typing → wait for quiet period
        _csm_state[user_id] = "COLLECTING"
        _csm_start_quiet_timer(user_id)


def _csm_finish_generation(user_id):
    """Transition out of GENERATING when there's nothing to deliver."""
    with _csm_lock:
        if _csm_pending.get(user_id):
            _csm_start_next_cycle(user_id)
        else:
            _csm_state_to_idle(user_id)


def _csm_state_to_idle(user_id):
    """Set state to IDLE for user. Must be called under lock."""
    _csm_state[user_id] = "IDLE"


# Legacy synchronous wrapper (kept for backward compatibility)
def chat_with_companion(text):
    """Process a chat message with Airi synchronously.

    Used by the companion API. For the main web UI, use receive_chat_message().
    """
    now = _user_now()
    msg_id = now.strftime("chat_%Y%m%d_%H%M%S")

    # Notify AttentionEngine: legacy synchronous chat path.
    try:
        from attention_engine import get_attention_engine
        get_attention_engine().record_signal("chat_in", {"text": text})
    except Exception:
        pass

    user_msg = {
        "id": msg_id,
        "role": "user",
        "text": text,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    storage.append_chat_message(user_msg)
    _record_proactive_response_if_any(user_msg)

    context_text = _build_chat_context()

    # Daily greeting / late-night care injection
    greeting_ctx = _get_greeting_context()
    if greeting_ctx:
        context_text += "\n" + greeting_ctx

    preflight = {}
    try:
        from prompt import call_chat_preflight
        preflight = call_chat_preflight(text)
        memory_context = _format_memory_retrieval(
            preflight.get("memory_context", "") if isinstance(preflight, dict) else preflight
        )
        if memory_context:
            context_text += "\n" + memory_context
    except Exception as e:
        print(f"[Chat] Preflight failed (non-fatal): {e}")
        preflight = {}

    history = storage.get_chat_history(limit=20)
    if history and history[-1].get("id") == msg_id:
        history = history[:-1]

    from tools import get_registry
    from ai_config import get_runtime_config
    registry = get_registry()
    tool_handlers = registry.get_handlers()
    provider = get_runtime_config().get("provider", "openai")
    tools_list = registry.to_tools(provider)
    chat_policy = _resolve_chat_agent_policy(preflight, text)
    effective_tools = tools_list if chat_policy["allow_tools"] else []
    print(
        f"[Chat] memory preflight -> fixed {chat_policy['mode']} tier={chat_policy['tier']} "
        f"reasoning={chat_policy['reasoning']} tools={len(effective_tools)} "
        f"reason={chat_policy.get('reason', '')}"
    )

    try:
        result = call_chat_agent(
            context_text, history, text, tool_handlers,
            tools_list=effective_tools,
            max_iterations=chat_policy["max_iterations"],
            tier=chat_policy["tier"],
            reasoning=chat_policy["reasoning"],
            reasoning_budget=chat_policy["reasoning_budget"],
        )
    except Exception as e:
        print(f"[Chat] Agent error: {e}")
        error_reply = _friendly_chat_generation_error(e)
        reply_msg = {
            "id": msg_id + "_reply",
            "role": "assistant",
            "text": error_reply,
            "time": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        storage.append_chat_message(reply_msg)
        return {"reply": error_reply, "tool_calls": []}

    reply_text = result.get("reply", "")
    tool_calls = result.get("tool_calls", [])

    reply_msg = {
        "id": msg_id + "_reply",
        "role": "assistant",
        "text": reply_text,
        "tool_calls": tool_calls,
        "time": _user_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    storage.append_chat_message(reply_msg)

    try:
        from attention_engine import get_attention_engine
        get_attention_engine().record_signal("chat_out", {"text": reply_text})
    except Exception:
        pass

    # User message → sleep_agent v2 queue.
    # Both the user message AND Miru's reply go into the queue so the
    # cheap-LLM summarizer can see the conversation as a whole; the LLM
    # is instructed (in v2 prompt) to NOT extract facts from Miru's
    # words, so the reply only serves as context for understanding the
    # user's messages.
    try:
        from sleep_agent import get_sleep_agent
        agent = get_sleep_agent()
        agent.enqueue("user", text, user_msg["time"])
        if reply_text:
            agent.enqueue("assistant", reply_text, reply_msg["time"])
    except Exception as e:
        print(f"[chat] sleep_agent enqueue failed: {e}")

    # Miru/user emotion updates are handled by AttentionEngine in production.

    return {"reply": reply_text, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Emotion Annotation (background, per chat interaction)
# ---------------------------------------------------------------------------

def _is_transactional_message(text):
    """Check if a message is purely transactional (no emotional content).

    Returns True for messages like "好的", "收到", "ok", "嗯" etc.
    Does NOT filter short messages with real emotion like "烦死了", "太开心了".
    """
    import re
    if not text:
        return True
    t = text.strip()
    if len(t) > 20:
        return False
    # Exact match: pure acknowledgement / filler words
    _TRANSACTIONAL = re.compile(
        r'^('
        r'好的?|行|嗯+|哦+|ok+|okay|好吧|收到|了解|知道了|明白|'
        r'谢谢|thanks|thx|好嘞|得|可以|没问题|对|是的|'
        r'晚安|早|拜拜|bye|再见|gn|nn|88'
        r')[\s。！!.~～]*$',
        re.IGNORECASE
    )
    return bool(_TRANSACTIONAL.match(t))


def _annotate_emotion_from_chat(user_id="_admin", user_data_dir=None, device_id=""):
    """Annotate emotion from the most recent user messages.

    Called in background after each reply delivery.
    Reads recent 30min messages + today's emotion log, calls cheap LLM,
    writes result to emotion_log.json.

    ``device_id`` is stamped onto the emotion log entry so the UI can show
    per-device origin (supports N devices — no hardcoded PC/phone split).
    """
    _csm_set_thread_context(user_id, user_data_dir)
    try:
        recent_messages = storage.get_recent_user_messages(minutes=30)
        if not recent_messages:
            return

        # Pre-filter: skip if the latest message is purely transactional
        latest_text = (recent_messages[-1].get("text") or "").strip()
        if _is_transactional_message(latest_text):
            print(f"[Emotion] Skipped transactional: \"{latest_text[:30]}\"")
            return

        # Use the last few messages as "current" context
        current_messages = recent_messages[-3:]
        today_log = storage.get_today_emotion_log()

        from prompt import call_emotion_annotation
        result = call_emotion_annotation(current_messages, recent_messages, today_log)

        if result:
            # Tag origin so UI + analytics can attribute per-device/per-source.
            result.setdefault("source_type", "chat")
            if device_id:
                result["device_id"] = device_id
            storage.append_emotion_log(result)
            print(f"[Emotion] Chat annotation: {result['mood']} "
                  f"(intensity={result['intensity']}, valence={result['valence']}, "
                  f"device={device_id or '-'})")
            _broadcast_emotion_update(result)
        else:
            print("[Emotion] Chat annotation: uncertain / skipped")
    except Exception as e:
        print(f"[Emotion] Chat annotation error: {e}")


def _process_screen_observation_async(observation: str, significance: int,
                                      device_id: str, device_name: str,
                                      captured_at: str,
                                      user_id: str = "_admin",
                                      user_data_dir: str = None):
    """Per-screenshot ScreenSlot Writer pipeline (2026-05-16).

    Replaces the old screen_sleep_agent.enqueue → fragments → trigger_route_async
    chain. Each sig>=3 screenshot triggers one policy-routed LLM call that decides:
      - slot_writes:      新事实路由 (复用 chat route_with_slot_write)
      - commitments:      新 DDL → core.add_commitment_manual (with dedup)
      - completed_commitments: 已完成的承诺 → complete_commitment_by_title (fuzzy)

    Cost policy: routine sig=3 uses memory tier without reasoning for both
    ScreenSlot Writer and screenshot Pass 4; explicit DDL or concrete project
    updates use the stronger chat tier, but keep provider reasoning disabled
    for OpenAI-compatible proxy compatibility.

    Called as a daemon thread from screen_analyzer.analyze. Flask context is
    restored via _csm_set_thread_context, mirroring _annotate_emotion_from_screenshot.
    """
    _csm_set_thread_context(user_id, user_data_dir)
    gate_entry_id = ""
    try:
        if not observation or not observation.strip():
            return
        if significance < 3:
            return  # sanity (caller should already filter)

        # Build active_commitments list
        active = []
        for item in parse_commitments(include_done=False):
            active.append({
                "title":    item.get("title", ""),
                "deadline": item.get("deadline", ""),
            })

        # ScreenSemanticGate filters repeated / low-value observations before
        # the expensive ScreenSlotWriter.  The log is written immediately, so
        # later screenshots can see that this observation already passed Gate
        # even if the writer is still running.
        gate_error = ""
        try:
            from memory_prompts_v3 import call_screen_semantic_gate
            recent_gate_history = storage.get_recent_screen_semantic_gate_entries(limit=30)
            gate = call_screen_semantic_gate(
                observation=observation,
                significance=significance,
                recent_history=recent_gate_history,
                active_commitments=active,
                tier="memory",
                reasoning=False,
                max_tokens=300,
                reasoning_budget=0,
                pass_label="ScreenSemanticGate",
            )
            should_continue = bool(getattr(gate, "should_continue", False)) if gate else False
            if gate is None:
                gate_error = "llm_retry_exhausted"
        except Exception as e:
            should_continue = False
            gate_error = str(e)[:300]

        gate_entry_id = storage.append_screen_semantic_gate_log(
            observation=observation,
            significance=significance,
            should_continue=should_continue,
            device_id=device_id,
            captured_at=captured_at,
            status="passed" if should_continue else ("gate_error" if gate_error else "skipped"),
            error=gate_error,
        )
        if not should_continue:
            print(
                f"[ScreenSemanticGate] skipped sig={significance} "
                f"err={gate_error!r} obs={observation[:60]!r}"
            )
            return

        from memory_prompts_v3 import call_screen_slot_writer
        from memory_router import route_with_slot_write, load_all_slots
        import identity as _identity

        # Build slot_index (4 domains, all active slots, summary level)
        slot_index = {}
        for domain in ("project", "person", "topic", "self"):
            slots = []
            for s in load_all_slots(domain):
                if s.get("status") == "archived":
                    continue
                slots.append({
                    "id":          s.get("id", ""),
                    "title":       s.get("title", ""),
                    "summary":     s.get("summary", ""),
                    "aliases":     s.get("aliases", []),
                    "last_active": s.get("last_active", ""),
                })
            slot_index[domain] = slots

        # Identity ground truth + user name
        try:
            user_name = _identity.get_user_name() or "用户"
        except Exception:
            user_name = "用户"
        try:
            id_dict = _identity.read_identity() or {}
            id_lines = []
            for k in ("name", "role", "occupation", "schedule", "style"):
                v = id_dict.get(k)
                if v:
                    id_lines.append(f"- {k}: {v}")
            identity_block = "\n".join(id_lines) if id_lines else "(无)"
        except Exception:
            identity_block = "(无)"

        now = _user_now()
        now_iso = now.isoformat(timespec="seconds")
        today = now.strftime("%Y-%m-%d")
        llm_policy = _screen_slot_writer_policy(observation, significance)

        result = call_screen_slot_writer(
            user_name=user_name,
            identity_ground_truth=identity_block,
            slot_index=slot_index,
            active_commitments=active,
            observation=observation,
            significance=significance,
            device_name=device_name or device_id or "unknown",
            captured_at=captured_at or now_iso,
            current_time=now_iso,
            today_date=today,
            tier=llm_policy["tier"],
            reasoning=llm_policy["reasoning"],
            max_tokens=llm_policy["max_tokens"],
            reasoning_budget=llm_policy["reasoning_budget"],
            pass_label=f"ScreenSlotWriterV3:{llm_policy['name']}",
        )

        if result is None:
            print(f"[ScreenSlotWriter] retry exhausted, skip "
                  f"obs={observation[:40]!r}")
            if gate_entry_id:
                storage.update_screen_semantic_gate_log(
                    gate_entry_id,
                    status="writer_retry_exhausted",
                    writer={"error": "retry_exhausted"},
                )
            return

        # 落地 slot_writes
        source_ctx = (
            f"(screenshot device={device_name or device_id} "
            f"time={captured_at} sig={significance})"
        )
        sw_count = 0
        for sw in result.slot_writes:
            try:
                r = route_with_slot_write(
                    sw,
                    source_context=source_ctx,
                    pass4_tier=llm_policy["pass4_tier"],
                    pass4_reasoning=llm_policy["pass4_reasoning"],
                    pass4_max_tokens=llm_policy["pass4_max_tokens"],
                    pass4_reasoning_budget=llm_policy["pass4_reasoning_budget"],
                )
                if r.get("ok"):
                    sw_count += 1
            except Exception as e:
                print(f"[ScreenSlotWriter] route_with_slot_write failed: {e}")

        # 落地新 commitments (含 dedup, 已在 add_commitment_manual 内)
        cm_added = cm_dup = 0
        for c in result.commitments:
            try:
                r = add_commitment_manual(c.title, c.deadline, c.detail or "")
                if r.get("status") == "ok":
                    cm_added += 1
                elif r.get("status") == "skipped_dup":
                    cm_dup += 1
            except Exception as e:
                print(f"[ScreenSlotWriter] add_commitment failed: {e}")

        # 完成承诺
        cc_done = cc_miss = 0
        for title in result.completed_commitments:
            try:
                r = complete_commitment_by_title(title)
                if r.get("status") == "ok":
                    cc_done += 1
                else:
                    cc_miss += 1
            except Exception as e:
                print(f"[ScreenSlotWriter] complete_commitment failed: {e}")

        print(
            f"[ScreenSlotWriter] sig={significance} policy={llm_policy['name']} "
            f"slot_writes={sw_count}/{len(result.slot_writes)} "
            f"commitments={cm_added}+{cm_dup}dup/{len(result.commitments)} "
            f"completed={cc_done}+{cc_miss}miss/{len(result.completed_commitments)} "
            f"skipped={result.skipped_reason!r}"
        )
        if gate_entry_id:
            storage.update_screen_semantic_gate_log(
                gate_entry_id,
                status="writer_done",
                writer={
                    "slot_writes": sw_count,
                    "slot_writes_planned": len(result.slot_writes),
                    "commitments": cm_added,
                    "commitments_dup": cm_dup,
                    "commitments_planned": len(result.commitments),
                    "completed_commitments": cc_done,
                    "completed_miss": cc_miss,
                    "completed_planned": len(result.completed_commitments),
                    "skipped_reason": result.skipped_reason or "",
                    "policy": llm_policy["name"],
                },
            )

    except Exception as e:
        if gate_entry_id:
            storage.update_screen_semantic_gate_log(
                gate_entry_id,
                status="writer_error",
                error=str(e)[:500],
            )
        print(f"[ScreenSlotWriter] async pipeline error: {e}")


def _annotate_emotion_from_screenshot(screenshot_tldr, screenshot_time,
                                      user_id="_admin", user_data_dir=None,
                                      device_id=""):
    """Annotate emotion from auto-screenshot observation.

    Called in background after a significant screen observation.
    ``device_id`` records which sensor produced the screenshot so the
    emotion UI can scope per-device (N-device safe).
    """
    _csm_set_thread_context(user_id, user_data_dir)
    try:
        recent_messages = storage.get_recent_user_messages(minutes=30)
        today_log = storage.get_today_emotion_log()

        from prompt import call_emotion_annotation_screenshot
        result = call_emotion_annotation_screenshot(
            screenshot_tldr, screenshot_time, recent_messages, today_log
        )

        if result:
            result.setdefault("source_type", "auto_screenshot")
            if device_id:
                result["device_id"] = device_id
            storage.append_emotion_log(result)
            print(f"[Emotion] Screenshot annotation: {result['mood']} "
                  f"(intensity={result['intensity']}, valence={result['valence']}, "
                  f"device={device_id or '-'})")
            _broadcast_emotion_update(result)
        else:
            print("[Emotion] Screenshot annotation: uncertain / skipped")
    except Exception as e:
        print(f"[Emotion] Screenshot annotation error: {e}")


# ---------------------------------------------------------------------------
# Miru's own emotion evaluation
#
# Triggered AFTER each user-emotion annotation, for every chat reply and
# every significant screenshot. Lets Miru "feel" the event from her own
# point of view (mood / valence / arousal). Closeness/trust meters were
# removed 2026-05-09 — relationship_stage is now a hardcoded constant.
#
# Design (2026-05-08):
#   - One LLM call per event (memory tier, gemini-3.1-pro-preview).
#   - Context is rich enough that Miru can sense being-ignored / being-cared-for
#     just from chat timestamps + arc, without explicit absence triggers.
#   - For chat path, the user's CURRENT message image (if any) is passed
#     multimodally; HISTORICAL images in today_chat are replaced by [图片]
#     placeholders so we don't re-upload old uploads.
#   - For screenshot path, the VLM observation text is enough; we don't
#     re-upload the original frame to avoid a second VLM bill.
# ---------------------------------------------------------------------------

def _format_today_chat_for_miru_emotion(now_dt, max_entries: int = 30) -> str:
    """Render today's chat history with role labels + [图片] image placeholders.

    Returns a multi-line string for prompt injection. Does NOT include
    raw image bytes — historical images become "[图片]" markers so Miru
    can see "this message had an image" without us paying for the upload.
    """
    today_str = now_dt.strftime("%Y-%m-%d")
    try:
        history = storage.get_chat_history(200)
    except Exception:
        return ""
    if not history:
        return ""

    lines = []
    cfg = get_config()
    addr = resolve_user_entity_label()
    name = cfg.name

    for msg in history:
        ts = msg.get("time") or ""
        if not ts.startswith(today_str):
            continue
        hhmm = ts[11:16] if len(ts) >= 16 else ""
        role = msg.get("role", "")
        text = (msg.get("text") or "").strip()
        has_image = bool(msg.get("image"))
        img_desc = (msg.get("image_desc") or "").strip()
        if role == "user":
            tag = addr
        elif role == "assistant":
            # Mark proactive care messages so Miru can tell them apart from replies
            mtype = msg.get("type", "") or ""
            if mtype == "proactive" or msg.get("source") == "care_engine":
                tag = f"{name}主动"
            else:
                tag = name
        else:
            continue

        # Image placeholder uses the cached vision description so Miru can
        # see *what* the user sent, not just "(some image)". For old
        # messages without image_desc, fall back to a generic [图片] mark.
        if has_image:
            tag_img = f"[图片：{img_desc}]" if img_desc else "[图片]"
            if text:
                body = f"{text[:160]} {tag_img}"
            else:
                body = tag_img
        else:
            body = text[:200]
        if not body:
            continue
        lines.append(f"  [{hhmm}] {tag}：{body}")

    # Trim to last N entries (keep recent, drop earliest of the day)
    if len(lines) > max_entries:
        lines = lines[-max_entries:]
    return "\n".join(lines)


def _format_user_emotion_arc(now_dt) -> str:
    """Render today's user emotion log as a coarse trajectory.

    Bucketed into 09-12 / 12-15 / 15-18 / 18-21 / 21-24 / 00-09 windows
    with average valence + dominant mood. Kept for the legacy Miru emotion
    eval path; production emotion updates now come from AttentionEngine.
    """
    try:
        today = storage.get_today_emotion_log() or []
    except Exception:
        return ""
    if not today:
        return ""

    buckets = {"00-09": [], "09-12": [], "12-15": [],
               "15-18": [], "18-21": [], "21-24": []}
    for e in today:
        ts = e.get("timestamp", "")
        if len(ts) < 13:
            continue
        try:
            hour = int(ts[11:13])
        except ValueError:
            continue
        if hour < 9:
            key = "00-09"
        elif hour < 12:
            key = "09-12"
        elif hour < 15:
            key = "12-15"
        elif hour < 18:
            key = "15-18"
        elif hour < 21:
            key = "18-21"
        else:
            key = "21-24"
        buckets[key].append(e)

    lines = []
    for key in ("00-09", "09-12", "12-15", "15-18", "18-21", "21-24"):
        entries = buckets[key]
        if not entries:
            continue
        try:
            avg_v = sum(float(e.get("valence", 0) or 0) for e in entries) / len(entries)
        except Exception:
            avg_v = 0.0
        moods = {}
        for e in entries:
            m = e.get("mood", "")
            if m and m != "uncertain":
                moods[m] = moods.get(m, 0) + 1
        dom = max(moods.items(), key=lambda x: x[1])[0] if moods else "?"
        sign = "+" if avg_v >= 0 else ""
        lines.append(f"  {key}: avg valence {sign}{avg_v:.2f}, 主导 mood={dom} (n={len(entries)})")

    # Last 30 minutes
    from datetime import timedelta as _td
    cutoff = (now_dt - _td(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    recent = [e for e in today if (e.get("timestamp", "") or "") >= cutoff]
    if recent:
        try:
            avg_v = sum(float(e.get("valence", 0) or 0) for e in recent) / len(recent)
        except Exception:
            avg_v = 0.0
        sign = "+" if avg_v >= 0 else ""
        lines.append(f"  最近30min: 平均 valence {sign}{avg_v:.2f} (n={len(recent)})")

    return "\n".join(lines)


def _seconds_since_last_user_msg(now_dt) -> int | None:
    """Seconds since user's most recent message. None if no messages."""
    try:
        history = storage.get_chat_history(50)
    except Exception:
        return None
    if not history:
        return None
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        ts = msg.get("time", "")
        if not ts:
            continue
        try:
            from datetime import datetime as _dt
            t = _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return max(0, int((now_dt - t).total_seconds()))
        except (ValueError, TypeError):
            continue
    return None


def _seconds_since_last_miru_proactive(now_dt) -> int | None:
    """Seconds since Miru's most recent proactive care message.

    Reads from care_log.json (only entries with decision=='speak').
    Falls back to None if there's no speak entry yet.
    """
    try:
        recent = storage.get_recent_care_log(limit=50) or []
    except Exception:
        return None
    for e in reversed(recent):
        if e.get("decision") != "speak":
            continue
        ts = e.get("ts", "")
        if not ts:
            continue
        try:
            from datetime import datetime as _dt
            t = _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return max(0, int((now_dt - t).total_seconds()))
        except (ValueError, TypeError):
            continue
    return None


def _evaluate_miru_emotion(trigger_kind: str, trigger_text: str,
                           trigger_image_desc: str = "",
                           user_id: str = "_admin",
                           user_data_dir: str | None = None,
                           device_id: str = ""):
    """Run Miru's emotional reaction to a single event.

    Triggered in a background thread after each chat reply OR each
    significant screenshot — in parallel with user emotion annotation.

    Args:
        trigger_kind: "chat" or "screenshot"
        trigger_text: User's last message OR the screenshot observation
        trigger_image_desc: 120-180 字 Chinese description of the image
            attached to the trigger event (chat path only). Empty string
            means no image. Memory tier is pure-text — vision conversion
            already happened upstream in core.receive_chat_message via
            vision.describe_image().
    """
    _csm_set_thread_context(user_id, user_data_dir)
    try:
        from datetime import datetime as _dt
        import miru_emotion
        from memory_prompts import call_miru_emotion_eval

        emo = miru_emotion.get_instance()
        current_state = emo.get_state()
        days_together = emo.get_days_together()
        now = _dt.now()

        # Build rich context — let Miru SEE the timeline, not have it
        # pre-judged for her.
        today_chat = _format_today_chat_for_miru_emotion(now)
        user_emotion_arc = _format_user_emotion_arc(now)
        secs_user = _seconds_since_last_user_msg(now)
        secs_miru = _seconds_since_last_miru_proactive(now)

        # core_memory blocks (mirror what main agent + AttentionEngine see)
        human_block = ""
        persona_block = ""
        try:
            import core_memory
            blocks = core_memory.get_all_blocks() or {}
            human_block = blocks.get("human", "") or ""
            persona_block = blocks.get("persona", "") or ""
        except Exception:
            pass

        # Splice image description into trigger_text so the LLM sees
        # "what just happened" as a single text payload.
        trigger_text_with_img = trigger_text or ""
        if trigger_image_desc:
            if trigger_text_with_img:
                trigger_text_with_img = f"{trigger_text_with_img}\n[图片：{trigger_image_desc}]"
            else:
                trigger_text_with_img = f"[图片：{trigger_image_desc}]"

        result = call_miru_emotion_eval(
            trigger_kind=trigger_kind,
            trigger_text=trigger_text_with_img,
            today_chat=today_chat,
            seconds_since_user_msg=secs_user,
            seconds_since_miru_proactive=secs_miru,
            user_emotion_arc=user_emotion_arc,
            current_state=current_state,
            days_together=days_together,
            human_block=human_block,
            persona_block=persona_block,
        )

        if not result:
            print(f"[MiruEmotion] {trigger_kind}: skipped (no result)")
            return

        updated = emo.update_from_llm(result)
        print(f"[MiruEmotion] {trigger_kind}: {updated.get('mood')} "
              f"v={updated.get('valence')} a={updated.get('arousal')} "
              f"reason={(result.get('reason') or '')[:40]}")
    except Exception as e:
        print(f"[MiruEmotion] eval error ({trigger_kind}): {e}")
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Emotion update SSE broadcast
#
# The legacy "白月光 Proactive Emotion Care" path (hardcoded 3-negative or
# 4-positive streak triggers, separate `_check_proactive_emotion_care` /
# `_send_proactive_care` flow, `emotion_proactive_meta.json` cooldown file)
# was removed on 2026-04-27. AttentionEngine now reads the full moment
# snapshot and records speak_intent; delivery through the main agent is a
# future layer. See attention_engine.py.
# ---------------------------------------------------------------------------


def _broadcast_emotion_update(entry: dict, user_id: str | None = None):
    """Push emotion update to that user's SSE clients for live ambiance."""
    try:
        if user_id is None:
            user_id = _current_user_id_safe()
        import sse
        sse.broadcast("emotion_updated", {"entry": entry}, user_id=user_id)
    except Exception:
        pass




# ---------------------------------------------------------------------------
# Screenshot memory cost policy
# ---------------------------------------------------------------------------

_SCREEN_DDL_HINTS = (
    "ddl", "deadline", "due", "todo", "to-do", "待办", "提醒",
    "截止", "交付", "提交", "今晚", "明天", "后天",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "开会", "会议", "面试", "考试", "答辩",
)

_SCREEN_PROJECT_HINTS = (
    "项目", "论文", "代码", "bug", "pr", "pull request", "commit",
    "部署", "上线", "测试", "实验", "ablation", "训练", "模型",
    "vscode", "vs code", "github", "xcode", "android studio",
    "前端", "后端", "接口", "构建", "编译",
)

_SCREEN_PROJECT_CHANGE_HINTS = (
    "完成", "修复", "实现", "新增", "删除", "重构", "提交", "合并",
    "发布", "上线", "失败", "报错", "通过", "解决", "定位",
    "fixed", "implemented", "merged", "deployed", "failed", "error",
    "passed", "build", "test",
)


def _screen_slot_writer_policy(observation: str, significance: int) -> dict:
    """Choose the cheapest LLM mode that still protects important screenshots.

    Screenshot significance is a salience signal for Attention, not enough by
    itself to justify the strong path.  Only explicit DDL signals or concrete
    project state changes use chat tier because they may create durable
    commitments or important timeline facts.  Even then reasoning stays off so
    generic OpenAI-compatible providers are not given vendor-specific thinking
    parameters.
    """
    text = (observation or "").lower()
    reasons = []

    if any(k in text for k in _SCREEN_DDL_HINTS):
        reasons.append("ddl")

    has_project = any(k in text for k in _SCREEN_PROJECT_HINTS)
    has_change = any(k in text for k in _SCREEN_PROJECT_CHANGE_HINTS)
    if has_project and has_change:
        reasons.append("project_update")

    if reasons:
        return {
            "name": "strong_" + "+".join(reasons[:3]),
            "tier": "chat",
            "reasoning": False,
            "max_tokens": 50000,
            "reasoning_budget": 0,
            "pass4_tier": "memory",
            "pass4_reasoning": False,
            "pass4_max_tokens": 1000,
            "pass4_reasoning_budget": 0,
        }

    return {
        "name": f"routine_sig{significance}_low_cost",
        "tier": "memory",
        "reasoning": False,
        "max_tokens": 12000,
        "reasoning_budget": 0,
        "pass4_tier": "memory",
        "pass4_reasoning": False,
        "pass4_max_tokens": 1000,
        "pass4_reasoning_budget": 0,
    }


# ---------------------------------------------------------------------------
# Auto Daily Review + Tomorrow Plan (23:30)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Nightly Message — warm review at 23:30
# Replaces: check_auto_review, check_morning_reminder, check_auto_review_plan
# ---------------------------------------------------------------------------

def check_nightly_message():
    """Nightly maintenance — chat archive, index consolidation, pattern updates.

    AttentionEngine observes late-active context and may form speak_intent.
    This function only runs maintenance and does not send a nightly message.
    Per-user lock — different users are processed independently.
    """
    uid = _current_user_id_safe()
    lock = _user_lock(_nightly_locks, uid)
    if not lock.acquire(blocking=False):
        return
    try:
        _check_nightly_maintenance()
    except Exception as e:
        print(f"[NightlyMaintenance] Error for {uid}: {e}")
    finally:
        lock.release()


def _check_nightly_maintenance():
    """Run nightly maintenance tasks once per day (23:25-23:35 window).

    Tasks: chat archive, index consolidation, pattern update, commitment cleanup.
    AttentionEngine observes late-active context; this function only maintains data.
    """
    now = _user_now()
    today_str = now.strftime("%Y-%m-%d")

    # Only 23:25-23:35 window
    if now.hour != 23 or abs(now.minute - 30) > 5:
        return

    # Dedup — only run maintenance once per day
    key = f"nightly_maintenance_{today_str}"
    sent_keys = storage.get_sent_reminder_keys(today_str)
    # Also check legacy key to avoid double-running on upgrade day
    if key in sent_keys or f"nightly_message_{today_str}" in sent_keys:
        return

    storage.record_reminder(key, today_str, "", None, skip_chat=True)
    _run_nightly_maintenance()


def _nightly_commitment_cleanup():
    """Clean up commitments: remove completed from active.md, LLM-judge stale overdue items."""
    import memory as _mem

    content = _mem.read_file("commitments/active.md")
    if not content:
        return

    lines = content.split("\n")
    today = _user_now().strftime("%Y-%m-%d")
    keep_lines = []
    removed_completed = 0
    overdue_candidates = []  # (line_index_in_keep, line_text, item_dict)

    for line in lines:
        item = _parse_commitment_line(line)
        if item is None:
            # Header or blank line — keep
            keep_lines.append(line)
            continue

        # Rule 1: Remove already-completed items (they're already in done.md)
        if item["status"] == "completed" or line.strip().startswith("- [x]"):
            removed_completed += 1
            continue

        # Rule 2: Collect overdue > 7 days for LLM judgment
        if item.get("deadline") and item["urgency"] == "overdue":
            try:
                dl_date = datetime.strptime(item["deadline"], "%Y-%m-%d")
                days_overdue = (datetime.strptime(today, "%Y-%m-%d") - dl_date).days
                if days_overdue > 7:
                    overdue_candidates.append((len(keep_lines), line, item))
            except ValueError:
                pass

        keep_lines.append(line)

    if removed_completed > 0:
        print(f"[CommitmentCleanup] Removed {removed_completed} completed items from active.md")

    # Rule 2: LLM judges stale overdue items
    archived_by_llm = 0
    if overdue_candidates:
        try:
            items_text = "\n".join(
                f"- ID:{c[2]['id']} | {c[2]['title']} (deadline: {c[2]['deadline']}, 逾期{(datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(c[2]['deadline'], '%Y-%m-%d')).days}天)"
                for c in overdue_candidates
            )
            from prompt import _call_llm_json
            system = (
                "你是一个任务管理助手。以下是逾期超过7天的承诺列表。\n"
                "请判断哪些应该被归档（不再追踪），哪些仍然有价值应该保留。\n\n"
                "归档的标准：\n"
                "- 一次性事件且明显已过时（如：吃午饭、某天下午开会、打游戏放松、去某地玩、某天早睡）\n"
                "- 有明确时间点的临时安排且已过（如：参加下午的面试、晚上八点开会）\n"
                "- 重复/近似条目（同一件事记录了多次，保留最新的一条即可）\n\n"
                "保留的标准：\n"
                "- 长期目标/技能学习（如：学某个技术、读某篇论文、投论文）\n"
                "- 持续性任务（如：准备面试面经、修复代码bug）\n"
                "- 用户关心的重要事项（如：等待录取通知）\n\n"
                "输出JSON: {\"archive\": [\"ID1\", \"ID2\", ...], \"keep\": [\"ID3\", ...], \"reason\": \"简述理由\"}"
            )
            result = _call_llm_json(
                system, items_text, temperature=0.2, max_tokens=10000,
                call_label="CommitmentCleanupStaleOverdue",
            )
            archive_ids = set(result.get("archive", []))
            reason = result.get("reason", "")

            if archive_ids:
                # Remove archived lines (reverse order to keep indices valid)
                indices_to_remove = set()
                for idx, line_text, item in overdue_candidates:
                    if item["id"] in archive_ids:
                        indices_to_remove.add(idx)
                        archived_by_llm += 1

                keep_lines = [l for i, l in enumerate(keep_lines) if i not in indices_to_remove]

                # Append to done.md with auto-archive note
                done_content = _mem.read_file("commitments/done.md")
                if done_content is None:
                    done_content = "# Completed Commitments\n"
                if not done_content.endswith("\n"):
                    done_content += "\n"
                now = _user_now().strftime("%Y-%m-%d %H:%M")
                for idx, line_text, item in overdue_candidates:
                    if item["id"] in archive_ids:
                        done_content += f"- [x] {item['title']} (deadline: {item['deadline']})  [auto-archived: {now}]\n"
                _mem.write_file("commitments/done.md", done_content)

                print(f"[CommitmentCleanup] LLM archived {archived_by_llm} stale items: {reason}")

        except Exception as e:
            print(f"[CommitmentCleanup] LLM judgment failed (non-fatal): {e}")

    # Rule 3: Flag no-deadline items older than 14 days (add a soft reminder tag)
    # (not removing, just noting for awareness)
    old_no_deadline = 0
    for line in keep_lines:
        item = _parse_commitment_line(line)
        if item and item["urgency"] == "no_deadline" and item.get("added"):
            try:
                added_date = datetime.strptime(item["added"][:10], "%Y-%m-%d")
                age = (datetime.strptime(today, "%Y-%m-%d") - added_date).days
                if age > 14:
                    old_no_deadline += 1
            except ValueError:
                pass
    if old_no_deadline > 0:
        print(f"[CommitmentCleanup] {old_no_deadline} no-deadline items older than 14 days")

    # Write back if anything changed
    if removed_completed > 0 or archived_by_llm > 0:
        _mem.write_file("commitments/active.md", "\n".join(keep_lines))
        _broadcast_commitment_sync()
        print(f"[CommitmentCleanup] active.md updated (removed {removed_completed} completed + {archived_by_llm} archived)")


def _run_nightly_maintenance():
    """Chat archive + index consolidation + pattern update — runs once per nightly window."""
    try:
        result = storage.archive_old_chat_messages()
        if result.get("archived", 0) > 0:
            print(f"[Auto] Chat archive: {result}")
        import memory as _mem
        idx_result = _mem.consolidate_index()
        if idx_result.get("deduped", 0) > 0 or idx_result.get("journal_pruned", 0) > 0:
            print(f"[Auto] Index consolidation: {idx_result}")
    except Exception as e:
        print(f"[Auto] Nightly maintenance failed (non-fatal): {e}")

    # Update daily patterns (sleep/work) for AttentionEngine/sleep inference.
    try:
        from daily_patterns import update_daily_patterns
        update_daily_patterns()
    except Exception as e:
        print(f"[Auto] Pattern update failed (non-fatal): {e}")

    # Compact append-only memory slots before the 23:45 journal reads them.
    try:
        import memory_router as _mr
        compact_result = _mr.run_slot_daily_compactor(date_str=_user_now().strftime("%Y-%m-%d"))
        if compact_result.get("compacted") or compact_result.get("failed") or compact_result.get("skipped"):
            print(f"[Auto] Slot daily compactor: {compact_result}")
    except Exception as e:
        print(f"[Auto] Slot daily compactor failed (non-fatal): {e}")

    # Commitment cleanup: remove completed, LLM-judge stale overdue items
    try:
        _nightly_commitment_cleanup()
    except Exception as e:
        print(f"[Auto] Commitment cleanup failed (non-fatal): {e}")
