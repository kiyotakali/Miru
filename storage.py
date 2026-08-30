import json
import os
import shutil
from datetime import datetime

from PIL import Image, ImageOps

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
MEMORY_SCOPE_USER = "user"
MEMORY_SCOPE_AIRI = "airi"
MEMORY_SCOPE_ALL = "all"
SUPPORTED_MEMORY_SCOPES = (MEMORY_SCOPE_USER, MEMORY_SCOPE_AIRI)


def get_data_dir() -> str:
    """Return active user's data directory (multi-tenant aware).

    In Flask request context: returns g.user_data_dir (set by auth middleware).
    Outside request context (tests, background tasks): returns DATA_DIR.
    """
    try:
        from flask import g
    except ImportError:
        return DATA_DIR
    try:
        data_dir = getattr(g, "user_data_dir", None)
    except RuntimeError:
        return DATA_DIR
    if data_dir:
        user_id = getattr(g, "user_id", None)
        try:
            import auth
            if not auth.is_active_user_context(user_id, data_dir):
                raise RuntimeError(f"inactive/deleted user context: {user_id}")
        except ImportError:
            pass
        return data_dir
    return DATA_DIR


def _archive_dir() -> str:
    return os.path.join(get_data_dir(), "archive")


def _uploads_dir() -> str:
    return os.path.join(get_data_dir(), "uploads")


def _ensure_dirs():
    d = get_data_dir()
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "archive"), exist_ok=True)
    os.makedirs(os.path.join(d, "uploads"), exist_ok=True)


def _normalize_memory_scope(memory_scope=MEMORY_SCOPE_USER, allow_all=False):
    scope = str(memory_scope or MEMORY_SCOPE_USER).strip().lower()
    if scope in ("", "default", "main"):
        scope = MEMORY_SCOPE_USER
    if allow_all and scope in ("all", "*"):
        return MEMORY_SCOPE_ALL
    if scope not in SUPPORTED_MEMORY_SCOPES:
        return MEMORY_SCOPE_USER
    return scope



def compress_image(input_path, max_size=1600, quality=85):
    """Compress an image: longest edge <= max_size, JPEG quality 85.

    - Auto-fix EXIF rotation (phone photos)
    - PNG with alpha channel stays PNG; otherwise convert to JPEG
    - GIF skipped (preserve animation)
    - Returns final filename (may change extension e.g. .webp -> .jpg)
    """
    ext = os.path.splitext(input_path)[1].lower()

    # Skip GIF (may be animated)
    if ext == ".gif":
        return os.path.basename(input_path)

    try:
        img = Image.open(input_path)
    except Exception:
        return os.path.basename(input_path)

    # Auto-fix EXIF rotation
    img = ImageOps.exif_transpose(img)

    # Resize if needed
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Determine output format
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

    if has_alpha:
        # Keep as PNG
        img.save(input_path, "PNG", optimize=True)
        return os.path.basename(input_path)
    else:
        # Convert to JPEG
        base = os.path.splitext(input_path)[0]
        output_path = base + ".jpg"
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(output_path, "JPEG", quality=quality, optimize=True)
        # Remove original if extension changed
        if output_path != input_path and os.path.exists(input_path):
            os.remove(input_path)
        return os.path.basename(output_path)


def read_json(path):
    _ensure_dirs()
    if not os.path.exists(path):
        write_json(path, [])
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    """Atomic JSON write: write to temp file then rename to prevent corruption."""
    _ensure_dirs()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def timeline_path():
    return os.path.join(get_data_dir(),"timeline.json")


def append_timeline(msg):
    items = read_json(timeline_path())
    items.append(msg)
    write_json(timeline_path(), items)


def delete_timeline_entry(msg_id):
    """Remove a timeline entry by id. Also deletes associated image file.

    Returns the deleted entry, or None if not found.
    """
    items = read_json(timeline_path())
    target = None
    remaining = []
    for item in items:
        if item.get("id") == msg_id:
            target = item
        else:
            remaining.append(item)
    if target is None:
        return None
    write_json(timeline_path(), remaining)
    # Clean up uploaded image if present
    image_filename = target.get("image")
    if image_filename:
        image_path = os.path.join(_uploads_dir(), image_filename)
        if os.path.exists(image_path):
            os.remove(image_path)
    return target


# ===== Daily Review Persistence =====

def daily_reviews_path():
    return os.path.join(get_data_dir(),"daily_reviews.json")


def save_daily_review(review):
    """Save a daily review, keyed by date. Overwrites same-date review."""
    reviews = read_json(daily_reviews_path())
    date = review.get("date", "")
    found = False
    for i, r in enumerate(reviews):
        if r.get("date") == date:
            reviews[i] = review
            found = True
            break
    if not found:
        reviews.append(review)
    reviews.sort(key=lambda r: r.get("date", ""), reverse=True)
    write_json(daily_reviews_path(), reviews)


def get_all_daily_reviews():
    return read_json(daily_reviews_path())


def get_daily_review(date_str):
    reviews = read_json(daily_reviews_path())
    for r in reviews:
        if r.get("date") == date_str:
            return r
    return None


# ===== Tomorrow Plan Persistence =====

def tomorrow_plan_path():
    return os.path.join(get_data_dir(),"tomorrow_plan.json")


def tomorrow_plans_path():
    return os.path.join(get_data_dir(),"tomorrow_plans.json")


def save_tomorrow_plan(plan):
    _ensure_dirs()
    plan["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Save to single-file (for reminder system compatibility)
    with open(tomorrow_plan_path(), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    # Also save to history list (keyed by date)
    plans = read_json(tomorrow_plans_path())
    date = plan.get("date", "")
    found = False
    for i, p in enumerate(plans):
        if p.get("date") == date:
            plans[i] = plan
            found = True
            break
    if not found:
        plans.append(plan)
    plans.sort(key=lambda p: p.get("date", ""), reverse=True)
    write_json(tomorrow_plans_path(), plans)


def load_tomorrow_plan():
    path = tomorrow_plan_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_tomorrow_plans():
    return read_json(tomorrow_plans_path())


def get_tomorrow_plan(date_str):
    plans = read_json(tomorrow_plans_path())
    for p in plans:
        if p.get("date") == date_str:
            return p
    return None


# ===== Daily Reminder Plan =====

def daily_reminder_plan_path():
    return os.path.join(get_data_dir(),"daily_reminder_plan.json")


def save_daily_reminder_plan(plan):
    _ensure_dirs()
    with open(daily_reminder_plan_path(), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def load_daily_reminder_plan():
    path = daily_reminder_plan_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ===== Push Subscriptions =====

def push_subscriptions_path():
    return os.path.join(get_data_dir(),"push_subscriptions.json")


# ===== Reminder Dedup Tracking =====

def reminders_path():
    return os.path.join(get_data_dir(),"reminders.json")


def get_sent_reminder_keys(date_str):
    _ensure_dirs()
    path = reminders_path()
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {r["key"] for r in data if r.get("date") == date_str}


def get_reminders_for_date(date_str):
    _ensure_dirs()
    path = reminders_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [r for r in data if r.get("date") == date_str]


def count_today_reminder_images(date_str):
    """Count how many reminders with images were generated today."""
    path = reminders_path()
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sum(1 for r in data if r.get("date") == date_str and r.get("image_filename"))


# ===== Chat History Persistence =====

def chat_history_path():
    return os.path.join(get_data_dir(),"chat_history.json")


def append_chat_message(msg):
    history = read_json(chat_history_path())
    history.append(msg)
    write_json(chat_history_path(), history)


def get_chat_history(limit=50):
    history = read_json(chat_history_path())
    return history[-limit:] if len(history) > limit else history


def get_chat_messages_in_range(start_date: str, end_date: str, *,
                                include_archive: bool = True) -> list:
    """Return chat messages whose *time* falls in [start_date, end_date] (inclusive).

    Unlike `get_chat_history`, this is NOT bounded by a recency limit — all
    messages in the date range are returned. Used by sleep-inference which
    needs the full window regardless of total message count.

    *include_archive*: also scan archived monthly files overlapping the range.
    Dates are "YYYY-MM-DD" strings.
    """
    if start_date > end_date:
        return []

    out: list = []
    history = read_json(chat_history_path()) or []
    for m in history:
        t = m.get("time", "")
        if len(t) < 10:
            continue
        if start_date <= t[:10] <= end_date:
            out.append(m)

    if include_archive:
        from datetime import datetime as _dt
        try:
            s = _dt.strptime(start_date, "%Y-%m-%d")
            e = _dt.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            s = e = None
        if s and e:
            months_needed = set()
            cur = s.replace(day=1)
            while cur <= e:
                months_needed.add(cur.strftime("%Y-%m"))
                if cur.month == 12:
                    cur = cur.replace(year=cur.year + 1, month=1)
                else:
                    cur = cur.replace(month=cur.month + 1)
            seen_times = {m.get("time", "") for m in out}
            for month_key in months_needed:
                for m in get_archived_chat_history(month_key):
                    t = m.get("time", "")
                    if len(t) < 10:
                        continue
                    if start_date <= t[:10] <= end_date and t not in seen_times:
                        out.append(m)

    out.sort(key=lambda m: m.get("time", ""))
    return out


# ===== Pending Notifications (for offline device push) =====

def pending_notifications_path():
    return os.path.join(get_data_dir(),"pending_notifications.json")


def add_pending_notification(msg_id: str, text: str, msg_type: str = "proactive"):
    """Add a notification for offline devices to pick up."""
    _ensure_dirs()
    notifs = read_json(pending_notifications_path())
    notifs.append({
        "id": msg_id,
        "text": text,
        "type": msg_type,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "read_by": [],  # device_ids that have fetched this
    })
    # Keep only last 50 notifications
    if len(notifs) > 50:
        notifs = notifs[-50:]
    write_json(pending_notifications_path(), notifs)


def get_pending_notifications(device_id: str, mark_read: bool = True):
    """Return unread notifications for a device, optionally marking them read."""
    notifs = read_json(pending_notifications_path())
    unread = [n for n in notifs if device_id not in n.get("read_by", [])]
    if mark_read and unread:
        for n in notifs:
            if device_id not in n.get("read_by", []):
                n["read_by"].append(device_id)
        write_json(pending_notifications_path(), notifs)
    return unread


CHAT_ARCHIVE_KEEP_RECENT = 500  # messages to keep in active file


def archive_old_chat_messages(keep_recent=CHAT_ARCHIVE_KEEP_RECENT):
    """Move old chat messages to monthly archive files.

    Keeps the most recent `keep_recent` messages in chat_history.json.
    Older messages are appended to archive/chat_history_YYYY-MM.json by month.

    Returns: {"archived": int, "remaining": int, "files": [str]}
    """
    _ensure_dirs()
    path = chat_history_path()
    if not os.path.exists(path):
        return {"archived": 0, "remaining": 0, "files": []}

    history = read_json(path)
    total = len(history)
    if total <= keep_recent:
        return {"archived": 0, "remaining": total, "files": []}

    # Split: old messages to archive, recent to keep
    to_archive = history[:-keep_recent]
    to_keep = history[-keep_recent:]

    # Group old messages by month (from their "time" field)
    by_month = {}
    for msg in to_archive:
        time_str = msg.get("time", "")[:7]  # "YYYY-MM"
        if not time_str or len(time_str) < 7:
            time_str = "unknown"
        if time_str not in by_month:
            by_month[time_str] = []
        by_month[time_str].append(msg)

    # Write each month's messages to archive file
    archive_files = []
    for month_key, msgs in sorted(by_month.items()):
        archive_file = os.path.join(_archive_dir(), f"chat_history_{month_key}.json")
        # Append to existing archive if it exists
        existing = []
        if os.path.exists(archive_file):
            try:
                with open(archive_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                existing = []
        existing.extend(msgs)
        write_json(archive_file, existing)
        archive_files.append(f"chat_history_{month_key}.json")

    # Overwrite active file with only recent messages
    write_json(path, to_keep)

    archived_count = len(to_archive)
    print(f"[Storage] Archived {archived_count} chat messages to {len(archive_files)} file(s), "
          f"kept {len(to_keep)} recent")

    return {"archived": archived_count, "remaining": len(to_keep), "files": archive_files}


def get_archived_chat_months():
    """List available archived chat history months.

    Returns: ["2026-01", "2026-02", ...] sorted chronologically.
    """
    _ensure_dirs()
    months = []
    for f in os.listdir(_archive_dir()):
        if f.startswith("chat_history_") and f.endswith(".json"):
            month = f[len("chat_history_"):-len(".json")]
            months.append(month)
    return sorted(months)


def get_archived_chat_history(month_key):
    """Read archived chat messages for a specific month.

    Args:
        month_key: "YYYY-MM" format, e.g. "2026-03"

    Returns: list of message dicts, or empty list if archive doesn't exist.
    """
    _ensure_dirs()
    archive_file = os.path.join(_archive_dir(), f"chat_history_{month_key}.json")
    if not os.path.exists(archive_file):
        return []
    try:
        with open(archive_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def get_today_schedule():
    """Read the schedule block from tomorrow_plan for today's date."""
    plan = load_tomorrow_plan()
    if not plan:
        return []
    today_str = datetime.now().strftime("%Y-%m-%d")
    if plan.get("date") != today_str:
        return []
    return plan.get("schedule", [])


def record_reminder(key, date_str, text, image_filename=None, scene_description="",
                    skip_chat=False):
    _ensure_dirs()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Store in reminders.json (for dedup tracking)
    path = reminders_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []
    entry = {
        "key": key,
        "date": date_str,
        "text": text,
        "image_filename": image_filename,
        "created_at": now_str,
    }
    if scene_description:
        entry["scene_description"] = scene_description
    data.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Also persist into timeline.json (backward compat)
    reminder_id = key + "_" + now_str.replace(" ", "_").replace(":", "")
    timeline_entry = {
        "id": reminder_id,
        "reminder_key": key,
        "type": "reminder",
        "text": text,
        "image": image_filename,
        "time": now_str,
        "status": "confirmed",
    }
    if scene_description:
        timeline_entry["scene_description"] = scene_description
    append_timeline(timeline_entry)

    # Also persist into chat_history.json (companion chat channel)
    # skip_chat=True when caller already appended to chat (avoids duplicate bubbles)
    if not skip_chat and text:
        chat_entry = {
            "id": reminder_id,
            "role": "assistant",
            "type": "reminder",
            "text": text,
            "image": image_filename,
            "time": now_str,
        }
        if scene_description:
            chat_entry["scene_description"] = scene_description
        append_chat_message(chat_entry)


# ===== Relationship Metadata =====
# #193-D: The canonical store for first_meet_date is now
# ``miru_emotion.relationship_meter.first_meet_date``. These functions remain
# for backward compatibility and delegate to MiruEmotion, which handles
# migration from the legacy relationship_meta.json file transparently.

def relationship_meta_path():
    """Legacy path — kept for tests / migration scripts that still touch it."""
    return os.path.join(get_data_dir(), "relationship_meta.json")


def get_relationship_meta():
    """Return {first_meet_date: ...} from MiruEmotion. Empty dict if not set yet.

    Delegator preserved so core.py and other callers don't need rewiring.
    """
    import miru_emotion  # deferred: miru_emotion imports storage
    try:
        fmd = miru_emotion.get_instance().get_first_meet_date()
    except Exception:
        fmd = ""
    return {"first_meet_date": fmd} if fmd else {}


def save_relationship_meta(meta):
    """Legacy writer — routed to MiruEmotion for the single field it matters for.

    Anything else in ``meta`` is silently dropped (we no longer maintain the
    separate file). Kept to avoid breaking any external scripts.
    """
    import miru_emotion
    fmd = (meta or {}).get("first_meet_date", "")
    if not fmd:
        return
    emo = miru_emotion.get_instance()
    with emo._lock:  # noqa: SLF001 — intentional for one-shot write
        state = emo._load()  # noqa: SLF001
        meter = state.setdefault("relationship_meter", {})
        meter["first_meet_date"] = fmd
        emo._save()  # noqa: SLF001


def ensure_first_meet_date():
    """Ensure first_meet_date exists. Returns {"first_meet_date": "YYYY-MM-DD"}.

    Delegates to ``miru_emotion.MiruEmotion.ensure_first_meet_date``, which
    migrates from the legacy ``relationship_meta.json`` or initializes to today.
    """
    import miru_emotion
    try:
        fmd = miru_emotion.get_instance().ensure_first_meet_date()
    except Exception:
        # Never raise from a helper called at bootstrap — fall back to today.
        fmd = datetime.now().strftime("%Y-%m-%d")
    return {"first_meet_date": fmd}


# ===== Daily Greeting Tracking =====

def daily_greeting_date_path():
    return os.path.join(get_data_dir(),".daily_greeting_date")


def get_last_greeting_date():
    path = daily_greeting_date_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def set_last_greeting_date(date_str):
    with open(daily_greeting_date_path(), "w") as f:
        f.write(date_str)



# ===== Emotion Log =====

import threading as _threading
_emotion_log_lock = _threading.Lock()


def emotion_log_path():
    return os.path.join(get_data_dir(),"emotion_log.json")


def _load_emotion_log():
    path = emotion_log_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _save_emotion_log(data):
    _ensure_dirs()
    write_json(emotion_log_path(), data)


def append_emotion_log(entry):
    """Append an emotion entry to today's log. Thread-safe."""
    with _emotion_log_lock:
        data = _load_emotion_log()
        date_key = entry.get("timestamp", "")[:10]
        if not date_key:
            from datetime import datetime
            date_key = datetime.now().strftime("%Y-%m-%d")
        if date_key not in data:
            data[date_key] = []
        data[date_key].append(entry)
        _save_emotion_log(data)
    # Notify clients that emotion data changed
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
        if uid:
            import sse
            sse.broadcast("data_changed", {"scope": "emotion"}, user_id=uid)
    except Exception:
        pass


def upsert_emotion_log_entry(entry: dict):
    """Insert or update one emotion segment in today's log.

    AttentionEngine now writes user affect as state segments rather than one
    row per tick.  Rows carry a stable ``id`` while the state continues, so the
    visible emotion river stays compact and the segment's duration/tick_count
    can grow in place.
    """
    if not isinstance(entry, dict):
        return
    with _emotion_log_lock:
        data = _load_emotion_log()
        date_key = (
            (entry.get("started_at") or entry.get("timestamp") or "")[:10]
        )
        if not date_key:
            from datetime import datetime
            date_key = datetime.now().strftime("%Y-%m-%d")
        items = data.get(date_key)
        if not isinstance(items, list):
            items = []
        seg_id = entry.get("id") or entry.get("segment_id")
        replaced = False
        if seg_id:
            for idx, item in enumerate(items):
                if isinstance(item, dict) and (item.get("id") or item.get("segment_id")) == seg_id:
                    merged = {**item, **entry}
                    items[idx] = merged
                    replaced = True
                    break
        if not replaced:
            items.append(entry)
        data[date_key] = items
        _save_emotion_log(data)
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
        if uid:
            import sse
            sse.broadcast("data_changed", {"scope": "emotion"}, user_id=uid)
    except Exception:
        pass


def get_today_emotion_log():
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_emotion_log()
    return data.get(today, [])


def get_emotion_log_by_date(date_str):
    data = _load_emotion_log()
    return data.get(date_str, [])


def get_emotion_log_month(month_str):
    """Return per-day emotion summary for a month (YYYY-MM).

    Returns {
      "2026-04-01": {
        "count": 5,
        "dominant_mood": "happy",
        "avg_valence": 0.42,
        "sources": {"chat": 3, "auto_screenshot": 2},
        "devices": {"phone-abc": 3, "desktop-xyz": 2},
      },
      ...
    }

    The ``sources`` and ``devices`` breakdowns let the UI attribute each
    day to N devices (no hardcoded dual-device assumption).
    """
    data = _load_emotion_log()
    result = {}
    for date_key, entries in data.items():
        if not date_key.startswith(month_str):
            continue
        if not isinstance(entries, list) or not entries:
            continue
        moods = {}
        valences = []
        sources = {}
        devices = {}
        for e in entries:
            m = e.get("mood", "neutral")
            moods[m] = moods.get(m, 0) + 1
            v = e.get("valence")
            if isinstance(v, (int, float)):
                valences.append(v)
            src = e.get("source_type") or "chat"
            sources[src] = sources.get(src, 0) + 1
            dev = e.get("device_id") or ""
            if dev:
                devices[dev] = devices.get(dev, 0) + 1
        dominant = max(moods, key=moods.get) if moods else "neutral"
        avg_v = round(sum(valences) / len(valences), 2) if valences else 0
        result[date_key] = {
            "count": len(entries),
            "dominant_mood": dominant,
            "avg_valence": avg_v,
            "sources": sources,
            "devices": devices,
        }
    return result


def get_recent_emotion_entries(hours=3):
    """Get emotion entries from the last N hours."""
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = _load_emotion_log()
    entries = []
    for d in [yesterday, today]:
        for e in data.get(d, []):
            if e.get("timestamp", "") >= cutoff_str:
                entries.append(e)
    return entries


def get_recent_user_messages(minutes=30):
    """Get user messages from the last N minutes with timestamps."""
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(minutes=minutes)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    history = read_json(chat_history_path())
    result = []
    for msg in reversed(history):
        if msg.get("time", "") < cutoff_str:
            break
        if msg.get("role") == "user":
            result.append(msg)
    result.reverse()
    return result


# ===== Screenshot Activity Log =====
# Lightweight append-only log: {date: [{t: iso_timestamp, d: device_id}, ...]}
# Records EVERY screenshot (regardless of content), used by sleep_inference
# to detect user wake/sleep times. Per-user isolated via get_data_dir().
# Rolling 30-day window, no content stored (only timestamps + device IDs).

_screenshot_log_lock = _threading.Lock()
_screen_semantic_gate_log_lock = _threading.Lock()
_SCREENSHOT_LOG_RETENTION_DAYS = 30


def screenshot_log_path():
    return os.path.join(get_data_dir(), "screenshot_log.json")


def screen_semantic_gate_log_path():
    return os.path.join(get_data_dir(), "screen_semantic_gate_log.json")


def _load_screenshot_log():
    path = screenshot_log_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return {}


def _load_screen_semantic_gate_log():
    path = screen_semantic_gate_log_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return {}


def append_screenshot_log(device_id: str, *, captured_at: str | None = None):
    """Record a screenshot event (timestamp + device_id only, no content).

    *captured_at*: ISO timestamp string (YYYY-MM-DDTHH:MM:SS) of the actual
    capture moment.  When supplied (e.g. from a queued offline upload), this
    is used instead of ``datetime.now()``.

    Thread-safe. Prunes entries older than _SCREENSHOT_LOG_RETENTION_DAYS.
    Silently no-ops if called outside a user context (get_data_dir raises).
    """
    try:
        data_dir = get_data_dir()
    except Exception:
        return  # no user context; skip
    if not data_dir:
        return
    from datetime import datetime, timedelta
    now = datetime.now()  # always available for pruning, even when captured_at is used
    with _screenshot_log_lock:
        data = _load_screenshot_log()
        # Use captured_at if provided and valid, else fall back to now
        ts_str = None
        date_key = None
        if captured_at:
            try:
                parsed = datetime.fromisoformat(captured_at)
                ts_str = parsed.strftime("%Y-%m-%dT%H:%M:%S")
                date_key = parsed.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        if ts_str is None:
            ts_str = now.strftime("%Y-%m-%dT%H:%M:%S")
            date_key = now.strftime("%Y-%m-%d")
        data.setdefault(date_key, []).append({
            "t": ts_str,
            "d": device_id or "unknown",
        })
        # Prune old day-keys
        cutoff = (now - timedelta(days=_SCREENSHOT_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
        for k in list(data.keys()):
            if k < cutoff:
                del data[k]
        try:
            _ensure_dirs()
            write_json(screenshot_log_path(), data)
        except Exception as e:
            print(f"[Storage] screenshot_log append failed: {e}")


def load_screenshot_log():
    """Return full log dict {date: [entries]}. Read-only snapshot."""
    return _load_screenshot_log()


def append_screen_semantic_gate_log(
    *,
    observation: str,
    significance: int,
    should_continue: bool,
    device_id: str = "",
    captured_at: str | None = None,
    status: str = "",
    error: str = "",
) -> str:
    """Record one ScreenSemanticGate decision for the current user.

    Disk entries keep audit fields (id/time/device), but prompt builders must
    render only observation/sig/passed_gate to the LLM.
    """
    try:
        data_dir = get_data_dir()
    except Exception:
        return ""
    if not data_dir:
        return ""
    from datetime import datetime, timedelta
    now = datetime.now()
    ts_str = None
    date_key = None
    if captured_at:
        try:
            parsed = datetime.fromisoformat(captured_at.replace("Z", ""))
            ts_str = parsed.strftime("%Y-%m-%dT%H:%M:%S")
            date_key = parsed.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    if ts_str is None:
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%S")
        date_key = now.strftime("%Y-%m-%d")
    import hashlib as _hashlib
    digest = _hashlib.sha1(
        f"{ts_str}|{device_id}|{observation}".encode("utf-8")
    ).hexdigest()[:8]
    entry_id = f"{ts_str.replace('-', '').replace(':', '').replace('T', 'T')}_{digest}"
    entry = {
        "id": entry_id,
        "t": ts_str,
        "d": device_id or "unknown",
        "sig": int(significance or 0),
        "observation": (observation or "").strip()[:1500],
        "should_continue": bool(should_continue),
        "passed_gate": bool(should_continue),
        "status": status or ("passed" if should_continue else "skipped"),
    }
    if error:
        entry["error"] = str(error)[:500]
    with _screen_semantic_gate_log_lock:
        data = _load_screen_semantic_gate_log()
        data.setdefault(date_key, []).append(entry)
        cutoff = (now - timedelta(days=_SCREENSHOT_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
        for key in list(data.keys()):
            if key < cutoff:
                del data[key]
        try:
            _ensure_dirs()
            write_json(screen_semantic_gate_log_path(), data)
        except Exception as exc:
            print(f"[Storage] screen_semantic_gate_log append failed: {exc}")
            return ""
    return entry_id


def update_screen_semantic_gate_log(entry_id: str, **fields) -> bool:
    """Patch one gate-log entry by id."""
    if not entry_id:
        return False
    allowed = {"status", "writer", "error"}
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not patch:
        return False
    with _screen_semantic_gate_log_lock:
        data = _load_screen_semantic_gate_log()
        changed = False
        for entries in data.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("id") == entry_id:
                    entry.update(patch)
                    changed = True
                    break
            if changed:
                break
        if not changed:
            return False
        try:
            _ensure_dirs()
            write_json(screen_semantic_gate_log_path(), data)
            return True
        except Exception as exc:
            print(f"[Storage] screen_semantic_gate_log update failed: {exc}")
            return False


def get_recent_screen_semantic_gate_entries(limit: int = 30) -> list[dict]:
    """Return recent gate decisions newest first.

    Callers that build LLM prompts should strip audit fields before rendering.
    """
    data = _load_screen_semantic_gate_log()
    rows = []
    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("observation"):
                rows.append(dict(entry))
    rows.sort(key=lambda e: e.get("t", ""), reverse=True)
    return rows[:max(1, int(limit or 30))]


def get_screenshot_timestamps(start_date: str, end_date: str) -> list:
    """Return sorted list of screenshot timestamps (datetime) within [start_date, end_date].

    Both dates are inclusive, format 'YYYY-MM-DD'.
    """
    from datetime import datetime as _dt
    data = _load_screenshot_log()
    out = []
    for date_key, entries in data.items():
        if date_key < start_date or date_key > end_date:
            continue
        for entry in entries or []:
            ts = entry.get("t", "")
            try:
                out.append(_dt.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                continue
    out.sort()
    return out


def get_screenshot_stats() -> dict:
    """Aggregate passed ScreenSemanticGate events by device_id.

    Returns {device_id: {"name": str, "total": int, "today": int, "last_time": str|None}}.

    The front-end shows `name || device_id`. Orphan device_ids (records
    pointing at a physical device whose registry entry was merged away
    before the screenshot_log rewrite logic existed) get an empty name
    and surface as the raw ID — that's a one-shot data migration we
    handled separately. Going forward `get_devices()` rewrites stale
    references automatically.
    """
    from datetime import datetime as _dt
    data = _load_screen_semantic_gate_log()
    today_key = _dt.now().strftime("%Y-%m-%d")

    # Build device_id → name map from current registry (post-cleanup).
    id_to_name: dict[str, str] = {}
    try:
        import device_manager
        for d in device_manager.get_devices():
            did = d.get("device_id")
            if did:
                id_to_name[did] = d.get("name") or did
    except Exception:
        pass

    stats: dict[str, dict] = {}
    for date_key, entries in data.items():
        is_today = date_key == today_key
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("passed_gate"):
                continue
            did = entry.get("d", "unknown")
            if did not in stats:
                stats[did] = {
                    "name": id_to_name.get(did, ""),
                    "total": 0, "today": 0, "last_time": None,
                }
            s = stats[did]
            s["total"] += 1
            if is_today:
                s["today"] += 1
            t = entry.get("t")
            if t and (s["last_time"] is None or t > s["last_time"]):
                s["last_time"] = t
    return stats


# ===== CareEngine v3 evaluation log =====
#
# Every CareEngine evaluation appends one entry. Used for (a) feeding the
# previous "thought" back into the next snapshot and (b) post-hoc tuning of
# the read-the-air heuristics. Capped at 30 days to keep file size bounded.

_care_log_lock = _threading.Lock()


def care_log_path():
    return os.path.join(get_data_dir(), "care_log.json")


def append_care_log(entry: dict):
    """Append one care evaluation entry. Auto-prunes entries older than 30 days
    on each write to keep the file under control.

    Entry shape: {
        "ts": "2026-04-27 15:23:14",
        "trigger": "screenshot|chat_in|chat_out|state|wait|heartbeat|catchup",
        "decision": "speak|wait|silent",
        "wait_until": "30min" | null,
        "message": "..." | null,
        "thought": "...",
        "snapshot_size": 1234   # tokens approx, for cost analysis
    }
    """
    from datetime import datetime, timedelta
    with _care_log_lock:
        _ensure_dirs()
        path = care_log_path()
        log = read_json(path) if os.path.exists(path) else []
        if not isinstance(log, list):
            log = []
        log.append(entry)
        # Prune > 30 days
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        log = [e for e in log if isinstance(e, dict) and e.get("ts", "") >= cutoff]
        write_json(path, log)


def get_recent_care_log(limit: int = 20) -> list:
    """Return the most recent care evaluation entries, newest last."""
    path = care_log_path()
    if not os.path.exists(path):
        return []
    log = read_json(path) or []
    if not isinstance(log, list):
        return []
    return log[-limit:]


# ===== AttentionEngine state/log =====
#
# AttentionEngine supersedes CareEngine as Miru's continuous inner presence:
# it records what Miru noticed, how she interpreted the user/Miru emotions,
# and whether there is a speak_intent.  Phase 1 deliberately does not deliver
# proactive chat messages; the log/state files are the observable output.

_attention_log_lock = _threading.Lock()
_ATTENTION_LOG_RETENTION_DAYS = 30


def attention_log_path():
    return os.path.join(get_data_dir(), "attention_log.json")


def attention_state_path():
    return os.path.join(get_data_dir(), "attention_state.json")


def attention_intent_queue_path():
    return os.path.join(get_data_dir(), "attention_intent_queue.json")


def attention_delivery_log_path():
    return os.path.join(get_data_dir(), "attention_delivery_log.json")


def append_attention_log(entry: dict):
    """Append or update one AttentionEngine segment.

    ``attention_log.json`` used to be a tick log.  The live AttentionEngine
    now writes compact first-person segments: if an entry has a stable ``id``
    we update that segment in place while it continues; a new segment is only
    appended when the LLM says the inner state materially shifted.
    """
    from datetime import datetime, timedelta
    with _attention_log_lock:
        _ensure_dirs()
        path = attention_log_path()
        log = read_json(path) if os.path.exists(path) else []
        if not isinstance(log, list):
            log = []
        seg_id = entry.get("id") if isinstance(entry, dict) else ""
        replaced = False
        if seg_id:
            for idx, item in enumerate(log):
                if isinstance(item, dict) and item.get("id") == seg_id:
                    log[idx] = {**item, **entry}
                    replaced = True
                    break
        if not replaced:
            log.append(entry)
        cutoff = (datetime.now() - timedelta(days=_ATTENTION_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        log = [
            e for e in log
            if isinstance(e, dict)
            and (e.get("updated_at") or e.get("ended_at") or e.get("ts") or "") >= cutoff
        ]
        write_json(path, log)


def get_recent_attention_log(limit: int = 20) -> list:
    """Return recent AttentionEngine ticks, newest last."""
    path = attention_log_path()
    if not os.path.exists(path):
        return []
    log = read_json(path) or []
    if not isinstance(log, list):
        return []
    return log[-limit:]


def load_attention_state() -> dict:
    path = attention_state_path()
    if not os.path.exists(path):
        return {}
    data = read_json(path) or {}
    return data if isinstance(data, dict) else {}


def save_attention_state(data: dict):
    _ensure_dirs()
    write_json(attention_state_path(), data if isinstance(data, dict) else {})
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
        if uid:
            import sse
            sse.broadcast("data_changed", {"scope": "attention"}, user_id=uid)
    except Exception:
        pass


def load_attention_intent_queue() -> list:
    path = attention_intent_queue_path()
    if not os.path.exists(path):
        return []
    data = read_json(path) or []
    return data if isinstance(data, list) else []


def save_attention_intent_queue(items: list):
    _ensure_dirs()
    if not isinstance(items, list):
        items = []
    write_json(attention_intent_queue_path(), items[-80:])
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
        if uid:
            import sse
            sse.broadcast("data_changed", {"scope": "attention_intents"}, user_id=uid)
    except Exception:
        pass


def append_attention_delivery_log(entry: dict):
    from datetime import datetime, timedelta
    with _attention_log_lock:
        _ensure_dirs()
        path = attention_delivery_log_path()
        log = read_json(path) if os.path.exists(path) else []
        if not isinstance(log, list):
            log = []
        log.append(entry if isinstance(entry, dict) else {})
        cutoff = (datetime.now() - timedelta(days=_ATTENTION_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        log = [e for e in log if isinstance(e, dict) and e.get("ts", "") >= cutoff]
        write_json(path, log[-300:])


def get_recent_attention_delivery_log(limit: int = 20) -> list:
    path = attention_delivery_log_path()
    if not os.path.exists(path):
        return []
    log = read_json(path) or []
    if not isinstance(log, list):
        return []
    return log[-limit:]


# ===== Greeting Meta =====

def greeting_meta_path():
    return os.path.join(get_data_dir(),"greeting_meta.json")


def get_greeting_meta():
    path = greeting_meta_path()
    if not os.path.exists(path):
        return {}
    return read_json(path) or {}


def save_greeting_meta(meta):
    _ensure_dirs()
    write_json(greeting_meta_path(), meta)


# ===== Onboarding State =====

def onboarding_meta_path():
    return os.path.join(get_data_dir(), "onboarding_meta.json")


def get_onboarding_meta():
    path = onboarding_meta_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_onboarding_meta(meta):
    _ensure_dirs()
    write_json(onboarding_meta_path(), meta if isinstance(meta, dict) else {})


def mark_onboarding_completed(answers: dict | None = None):
    """Persist that the current user has handled the first-meet flow.

    This is intentionally separate from core_memory.human: users may skip the
    questionnaire, and an empty human block should not make the app ask the
    same first-meet questions on every restart.
    """
    meta = get_onboarding_meta()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["completed"] = True
    meta.setdefault("completed_at", now)
    meta["updated_at"] = now
    supplied = []
    if isinstance(answers, dict):
        supplied = sorted([str(k) for k, v in answers.items() if str(v or "").strip()])
    meta["answered_keys"] = supplied
    meta["skipped"] = len(supplied) == 0
    save_onboarding_meta(meta)
    return meta


def is_onboarding_completed() -> bool:
    return bool(get_onboarding_meta().get("completed"))


# ===== Miru Emotion State =====

_miru_emotion_lock = _threading.Lock()


def miru_emotion_path():
    return os.path.join(get_data_dir(),"miru_emotion.json")


def load_miru_emotion():
    """Load Miru's emotion state. Thread-safe."""
    with _miru_emotion_lock:
        path = miru_emotion_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, FileNotFoundError):
            return {}


def save_miru_emotion(data):
    """Save Miru's emotion state. Thread-safe."""
    with _miru_emotion_lock:
        _ensure_dirs()
        write_json(miru_emotion_path(), data)


# ===== Proactive Message Timing =====

def proactive_meta_path():
    return os.path.join(get_data_dir(),"proactive_meta.json")


def get_last_proactive_time():
    """Return epoch timestamp of the last proactive message, or 0."""
    path = proactive_meta_path()
    if not os.path.exists(path):
        return 0.0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("last_proactive_time", 0))
    except (json.JSONDecodeError, FileNotFoundError, ValueError):
        return 0.0


def set_last_proactive_time(ts=None):
    """Record the current time as the last proactive message timestamp."""
    import time as _time
    _ensure_dirs()
    data = {}
    path = proactive_meta_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            data = {}
    data["last_proactive_time"] = ts or _time.time()
    write_json(path, data)
