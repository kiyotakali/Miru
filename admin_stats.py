"""Global stats aggregation for the admin dashboard.

Reads each user's data dir directly (chat_history.json, commitments,
screenshot_log, etc.) and rolls up totals. The dashboard refreshes often,
so the result is cached for a configurable TTL (default 60s) to avoid
hammering the disk.

Independent module — does NOT import any user-facing routing or business
logic, by design (admin functionality must stay off the user pipeline).
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import auth

_CACHE_TTL_SECONDS = 60
_cache: dict | None = None
_cache_at: float = 0.0
_cache_lock = threading.Lock()


def _safe_read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _user_data_dir(uid: str) -> str:
    return os.path.join(auth._BASE_DATA_DIR, "users", uid)


def _count_chat_messages(user_dir: str, since_iso: str | None = None) -> int:
    """Count messages in chat_history.json (and optionally by date floor)."""
    history = _safe_read_json(os.path.join(user_dir, "chat_history.json"), [])
    if not isinstance(history, list):
        return 0
    if since_iso is None:
        return len(history)
    return sum(1 for m in history if m.get("time", "") >= since_iso)


def _count_screenshots(user_dir: str, since_iso: str | None = None) -> int:
    """Sum screenshot events in screenshot_log.json.

    File format is dict-by-date: {"YYYY-MM-DD": [{"t": iso, "d": dev_id}, ...], ...}
    NOT a flat list. since_iso is treated as a date-prefix filter on the keys.
    """
    log = _safe_read_json(os.path.join(user_dir, "screenshot_log.json"), {})
    if not isinstance(log, dict):
        return 0
    cutoff_date = since_iso[:10] if since_iso else None
    total = 0
    for date_key, entries in log.items():
        if not isinstance(entries, list):
            continue
        if cutoff_date and date_key < cutoff_date:
            continue
        total += len(entries)
    return total


def _latest_screenshot_time(user_dir: str) -> str:
    """Return the iso timestamp of the most recent screenshot, or ''."""
    log = _safe_read_json(os.path.join(user_dir, "screenshot_log.json"), {})
    if not isinstance(log, dict):
        return ""
    latest = ""
    for entries in log.values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            t = e.get("t", "") if isinstance(e, dict) else ""
            if t > latest:
                latest = t
    return latest


# Regex mirrors of core._parse_commitment_line — kept here as small, self
# contained copies so admin_stats does not have to import core (which would
# pull in business logic, broadcast hooks, and Flask `g` requirements).
_DEADLINE_RE = re.compile(r'\(deadline:\s*([^)]+)\)')
_ADDED_RE = re.compile(r'\[added:\s*([^\]]+)\]')
_COMPLETED_RE = re.compile(r'\[completed:\s*([^\]]+)\]')


def _commitment_key(line: str):
    """Return (id, completed) for a commitment line, or None if it's not one.

    Matches core._parse_commitment_line semantics: only "- [" / "- [x]"
    list items count. Free-text bullets like "- 备忘内容" are ignored
    (consistent with how core.parse_commitments would skip them — earlier
    drafts of admin_stats over-counted because they used a looser regex).
    """
    s = line.strip()
    if not s.startswith("- ["):
        return None
    completed = s.startswith("- [x]")
    body = s[6:].strip()
    if not body:
        return None
    title = _DEADLINE_RE.sub('', body)
    title = _ADDED_RE.sub('', title)
    title = _COMPLETED_RE.sub('', title)
    title = re.sub(r'\s--\s.*', '', title).strip()
    if not title:
        return None
    title_md5 = hashlib.md5(title.encode("utf-8")).hexdigest()
    m = _ADDED_RE.search(body)
    added = m.group(1).strip() if m else None
    if added:
        ts_digits = re.sub(r'[^0-9]', '', added)
        title_hash = int(title_md5[:4], 16) % 10000
        cid = f"c_{ts_digits}_{title_hash:04d}"
    else:
        cid = "c_" + title_md5[:10]
    return cid, completed


def _count_commitments(user_dir: str) -> dict:
    """Count active vs completed commitments using the canonical id-dedup
    logic from core.parse_commitments.

    Files: <user_dir>/memory/commitments/{active,done}.md
    Order matters: active.md is parsed first (its [x] entries count as done,
    its [ ] entries as active); done.md fills in remaining ids as done only.
    """
    base = os.path.join(user_dir, "memory", "commitments")
    seen: set[str] = set()
    active = 0
    done = 0

    active_path = os.path.join(base, "active.md")
    if os.path.exists(active_path):
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = _commitment_key(line)
                    if not parsed:
                        continue
                    cid, completed = parsed
                    if cid in seen:
                        continue
                    seen.add(cid)
                    if completed:
                        done += 1
                    else:
                        active += 1
        except OSError:
            pass

    done_path = os.path.join(base, "done.md")
    if os.path.exists(done_path):
        try:
            with open(done_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = _commitment_key(line)
                    if not parsed:
                        continue
                    cid, _ = parsed
                    if cid in seen:
                        continue
                    seen.add(cid)
                    done += 1  # done.md entries always count as completed
        except OSError:
            pass

    return {"active": active, "done": done}


def _latest_user_msg_time(user_dir: str) -> str:
    """Return the time string of the most recent user-role chat message, or ''."""
    history = _safe_read_json(os.path.join(user_dir, "chat_history.json"), [])
    if not isinstance(history, list):
        return ""
    latest = ""
    for m in history:
        if m.get("role") == "user":
            t = m.get("time", "")
            if t > latest:
                latest = t
    return latest


def _attention_engine_running_count() -> int:
    """How many AttentionEngine instances are currently alive."""
    try:
        import attention_engine
        return len(getattr(attention_engine, "_instances", {}))
    except Exception:
        return 0


def _care_engine_running_count() -> int:
    """Legacy metric name kept for older admin UI/tests."""
    return _attention_engine_running_count()


def compute_stats() -> dict:
    """Walk every user's data dir, build the global rollup."""
    users = auth.list_users()  # {uid: {token, status, ...}}
    now = datetime.now()
    today_floor = now.strftime("%Y-%m-%d 00:00:00")
    week_floor = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    total = active = suspended = active_7d = 0
    msg_total = msg_today = msg_week = 0
    shot_total = shot_today = 0
    commit_active = commit_done = 0
    storage_bytes = 0

    for uid, u in users.items():
        total += 1
        status = u.get("status", "active")
        if status == "active":
            active += 1
        else:
            suspended += 1

        ud = _user_data_dir(uid)

        # Engagement window: any user-role chat in the last 7 days
        latest_msg = _latest_user_msg_time(ud)
        if latest_msg and latest_msg >= week_floor:
            active_7d += 1

        msg_total += _count_chat_messages(ud)
        msg_today += _count_chat_messages(ud, since_iso=today_floor)
        msg_week += _count_chat_messages(ud, since_iso=week_floor)

        shot_total += _count_screenshots(ud)
        shot_today += _count_screenshots(ud, since_iso=today_floor)

        c = _count_commitments(ud)
        commit_active += c["active"]
        commit_done += c["done"]

        storage_bytes += _dir_size_bytes(ud)

    return {
        "users": {
            "total": total,
            "active": active,
            "suspended": suspended,
            "active_7d": active_7d,
        },
        "messages": {
            "total": msg_total,
            "today": msg_today,
            "this_week": msg_week,
        },
        "screenshots": {
            "total": shot_total,
            "today": shot_today,
        },
        "commitments": {
            "active": commit_active,
            "done": commit_done,
        },
        "storage_bytes": storage_bytes,
        "attention_engine_running": _attention_engine_running_count(),
        "care_engine_running": _care_engine_running_count(),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_stats(force_refresh: bool = False) -> dict:
    """Return cached stats; recompute if expired or forced."""
    global _cache, _cache_at
    with _cache_lock:
        now = time.time()
        if not force_refresh and _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
            cached = dict(_cache)
            cached["_cache_age_s"] = int(now - _cache_at)
            return cached
        fresh = compute_stats()
        _cache = fresh
        _cache_at = now
        result = dict(fresh)
        result["_cache_age_s"] = 0
        return result


def get_user_detail(user_id: str) -> dict | None:
    """Per-user statistics for the user-detail admin page."""
    users = auth.list_users()
    if user_id not in users:
        return None
    u = users[user_id]
    ud = _user_data_dir(user_id)
    now = datetime.now()
    today_floor = now.strftime("%Y-%m-%d 00:00:00")
    week_floor = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    commits = _count_commitments(ud)
    return {
        "user_id": user_id,
        "status": u.get("status", "active"),
        "created_at": u.get("created_at", ""),
        "invitation_code": u.get("invitation_code", ""),
        "messages": {
            "total": _count_chat_messages(ud),
            "today": _count_chat_messages(ud, since_iso=today_floor),
            "this_week": _count_chat_messages(ud, since_iso=week_floor),
        },
        "screenshots": {
            "total": _count_screenshots(ud),
            "today": _count_screenshots(ud, since_iso=today_floor),
        },
        "commitments": commits,
        "storage_bytes": _dir_size_bytes(ud),
        "last_user_message_at": _latest_user_msg_time(ud),
        "last_screenshot_at": _latest_screenshot_time(ud),
    }


def invalidate_cache() -> None:
    """Drop the cached stats — call after user create/delete/suspend."""
    global _cache, _cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0
