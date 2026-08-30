"""Memory Router — slot-based memory routing (chat path).

Single entry point: route_with_slot_write(slot_write, source_context)
  invoked by Sleep Agent v3 (sleep_agent._process_batch) per slot_write from
  Slot Writer. Old route_and_write / Pass 1-3 deprecated in v3.1 (2026-05-13).

Pipeline (per slot_write):
    kind="new"   → no LLM, body = content_to_integrate, summary = meta.summary
    kind="match" → Pass 4 AppendEditor (add one timestamped entry)

Slot storage:
    memory/_slots/{domain}.json     — slot metadata (list of slots)
    memory/{domain}/{slot_id}/main.md — slot content

Status transitions (handled by daily_slot_audit, NOT by router):
    active → 30d no last_active → paused → 90d no last_active → archived
    archived slots excluded from match candidates
    pinned slots exempt from auto-downgrade

Key non-router logic preserved:
    - commitments/active.md : chat agent / sleep_agent auto-complete writes here
    - patterns/sleep.md     : daily_patterns / sleep_inference writes here, NOT router
    - journal/{date}.{json,md}: journal.py writes here, NOT router
    - core_memory.json      : core_memory module + Persona Writer manage, NOT here
"""

from __future__ import annotations

import json
import os
import re
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Any, Optional

import memory
from memory_prompts_v2 import (
    call_legacy_slot_merge_rewrite,
    call_pass4_append_slot,
    call_slot_daily_compactor,
)


# ===========================================================================
# Constants
# ===========================================================================

DOMAINS = ("project", "person", "topic", "self")

# Auto-downgrade thresholds — set to 10 years (effectively disabled).
# Architecture preserves the three-state machine (active/paused/archived)
# but auto-transition is a no-op: only manual archive via UI changes status.
# To re-enable real auto-archive later, drop these to e.g. 30 / 90 again.
PAUSED_AFTER_DAYS = 365 * 10
ARCHIVED_AFTER_DAYS = 365 * 10

# Reserved slot IDs in self/ that are NOT slot-managed (固定文件供 core_memory 读)
SELF_RESERVED_FILES = {"profile.md", "preferences.md", "about.md"}

_slot_write_lock = threading.RLock()


# ===========================================================================
# Path helpers
# ===========================================================================

def _memory_dir() -> str:
    return memory._memory_dir()  # type: ignore[attr-defined]


def _slots_dir() -> str:
    return os.path.join(_memory_dir(), "_slots")


def _slots_file(domain: str) -> str:
    return os.path.join(_slots_dir(), f"{domain}.json")


def _slot_main_file_rel(domain: str, slot_id: str) -> str:
    """Return path relative to memory/ root, suitable for memory.write_file."""
    # Map domain to directory name (project → projects, etc.)
    dir_name = _domain_to_dir(domain)
    return f"{dir_name}/{slot_id}/main.md"


def _domain_to_dir(domain: str) -> str:
    """Domain (singular) → directory name (plural)."""
    mapping = {
        "project": "projects",
        "person": "people",
        "topic": "topics",
        "self": "self",
    }
    return mapping.get(domain, domain)


def _dir_to_domain(dir_name: str) -> Optional[str]:
    """Directory name (plural) → domain (singular). None if not slot-managed."""
    mapping = {
        "projects": "project",
        "people": "person",
        "topics": "topic",
        "self": "self",
    }
    return mapping.get(dir_name)


# ===========================================================================
# Slot CRUD (atomic JSON read/write)
# ===========================================================================

def _ensure_slots_dir():
    os.makedirs(_slots_dir(), exist_ok=True)


def _empty_slot_table() -> dict:
    return {"version": 1, "slots": []}


def load_all_slots(domain: str) -> list[dict]:
    """Load full slot metadata list (all states: active/paused/archived).

    Returns empty list if file doesn't exist yet.
    """
    if domain not in DOMAINS:
        return []
    path = _slots_file(domain)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        return data.get("slots", [])
    except (json.JSONDecodeError, OSError):
        return []


def load_active_slots(domain: str) -> list[dict]:
    """Load slots that should be considered for routing (active + paused, not archived).

    Sorted by last_active descending.
    """
    slots = load_all_slots(domain)
    candidates = [s for s in slots if s.get("status", "active") in ("active", "paused")]
    candidates.sort(key=lambda s: s.get("last_active", ""), reverse=True)
    return candidates


def get_slot(domain: str, slot_id: str) -> Optional[dict]:
    """Find a slot by id (regardless of status). Returns None if not found."""
    for s in load_all_slots(domain):
        if s.get("id") == slot_id:
            return s
    return None


def _save_all_slots_atomic(domain: str, slots: list[dict]) -> bool:
    """Write the full slot list atomically. Returns True on success."""
    if domain not in DOMAINS:
        return False
    _ensure_slots_dir()
    path = _slots_file(domain)
    tmp = path + ".tmp"
    payload = {"version": 1, "slots": slots}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"[memory_router] save slots failed: {e}")
        return False


def _notify_curator_change(domain: str) -> None:
    """Notify curator that a slot in this domain changed.

    Best-effort: failure is non-fatal. Imported lazily to avoid circular import
    (curator imports memory_router).
    """
    try:
        import curator as _curator
        _curator.track_slot_change(domain)
    except Exception:
        pass


def upsert_slot(domain: str, slot: dict) -> bool:
    """Insert or update a slot in _slots/{domain}.json. Atomic + locked."""
    if domain not in DOMAINS:
        return False
    if "id" not in slot:
        return False
    with _slot_write_lock:
        slots = load_all_slots(domain)
        sid = slot["id"]
        replaced = False
        for i, s in enumerate(slots):
            if s.get("id") == sid:
                slots[i] = slot
                replaced = True
                break
        if not replaced:
            slots.append(slot)
        ok = _save_all_slots_atomic(domain, slots)
    if ok:
        _notify_curator_change(domain)
    return ok


def delete_slot(domain: str, slot_id: str, *, delete_file: bool = False,
                ledger_terminal_reason: str | None = "slot_deleted",
                ledger_terminal_target_slot_id: str | None = None) -> bool:
    """Remove slot from registry. Optionally delete main.md file."""
    if domain not in DOMAINS:
        return False
    removed = False
    with _slot_write_lock:
        slots = load_all_slots(domain)
        new_slots = [s for s in slots if s.get("id") != slot_id]
        if len(new_slots) == len(slots):
            return False
        if not _save_all_slots_atomic(domain, new_slots):
            return False
        removed = True
    if delete_file:
        rel = _slot_main_file_rel(domain, slot_id)
        # Use memory module's safe path
        full = os.path.join(_memory_dir(), rel)
        try:
            if os.path.exists(full):
                os.remove(full)
            # Also try removing parent directory if empty
            parent = os.path.dirname(full)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
    if removed:
        if ledger_terminal_reason:
            _terminalize_slot_appends(
                domain,
                slot_id,
                reason=ledger_terminal_reason,
                target_slot_id=ledger_terminal_target_slot_id,
            )
        _notify_curator_change(domain)
    return True


# ===========================================================================
# Helpers used during routing
# ===========================================================================

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_id_collision(proposed: str, existing_ids: list[str], existing_aliases: list[str]) -> str:
    """If proposed id conflicts with existing ids/aliases, append _2, _3..."""
    used = set(existing_ids) | set(existing_aliases)
    if proposed not in used:
        return proposed
    for n in range(2, 100):
        candidate = f"{proposed}_{n}"
        if candidate not in used:
            return candidate
    raise RuntimeError(f"无法解决 id 冲突: {proposed}")


_NGRAM_SIZE = 3


def _content_overlap_ratio(new_content: str, existing_main_md: str) -> float:
    """Approximate Jaccard overlap on character 3-grams.

    Returns 0.0-1.0. Used to skip writes when new content is mostly already
    present in existing main.md.
    """
    if not new_content.strip() or not existing_main_md.strip():
        return 0.0
    a = _ngrams(new_content, _NGRAM_SIZE)
    b = _ngrams(existing_main_md, _NGRAM_SIZE)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _ngrams(s: str, n: int) -> set:
    """Return set of character n-grams (lowercased)."""
    s = re.sub(r"\s+", " ", s.lower())
    if len(s) < n:
        return {s}
    return {s[i:i+n] for i in range(len(s) - n + 1)}


def _strip_title_header(main_md: str, title: str) -> str:
    """Strip the leading '# title' header from main.md so memory writers see only body.

    main.md is laid out as ``# {title}\\n\\n{body}\\n``. Tolerant of empty input,
    different header text, and missing blank line after the heading.
    """
    if not main_md:
        return ""
    text = main_md.lstrip("﻿")  # BOM-safe
    lines = text.split("\n")
    # Skip a single leading H1 line if present
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        # Skip one immediately-following blank line if any
        if lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


def _detect_source_type(source_context: str) -> str:
    ctx_lc = (source_context or "").lower()
    if "screenshot" in ctx_lc:
        return "screenshot"
    return "chat"


def _append_slot_body_entry(current_body: str, append_entry: str,
                            current_time: str) -> str:
    """Append one timestamped record without asking the LLM to rewrite body."""
    body = (current_body or "").strip()
    entry = re.sub(r"\s+", " ", (append_entry or "").strip())
    if not entry:
        return body
    if entry in body:
        return body

    ts = (current_time or _now_iso()).replace("T", " ")[:16]
    line = f"- [{ts}] {entry}"
    if not body:
        return f"## 近期记录\n\n{line}"
    if "## 近期记录" in body:
        return f"{body.rstrip()}\n{line}"
    return f"{body.rstrip()}\n\n## 近期记录\n\n{line}"


# ===========================================================================
# Index regeneration
# ===========================================================================

def regenerate_index_md() -> bool:
    """Rebuild memory/index.md from current slot tables + non-slot dirs."""
    try:
        memory.ensure_dirs()
        lines = ["# Memory Index", ""]
        lines.append("_由 Miru 自动维护. 列出所有 slot 与非 slot 文件的索引._")
        lines.append("")

        # Slot domains (按 domain 顺序展示)
        domain_titles = {
            "project": "Projects 项目",
            "person":  "People 人物",
            "topic":   "Topics 话题",
            "self":    "Self 自己",
        }
        for domain in DOMAINS:
            slots = load_all_slots(domain)
            active = [s for s in slots if s.get("status", "active") == "active"]
            paused = [s for s in slots if s.get("status") == "paused"]
            archived = [s for s in slots if s.get("status") == "archived"]
            lines.append(f"## {domain_titles[domain]} ({len(active) + len(paused)} 活跃, {len(archived)} 归档)")
            lines.append("")

            # Pinned + active first, then paused
            visible = sorted(
                active + paused,
                key=lambda s: (
                    not s.get("pinned", False),
                    {"active": 0, "paused": 1}.get(s.get("status", "active"), 2),
                    -_iso_to_ts(s.get("last_active", "")),
                ),
            )
            for s in visible:
                icon = s.get("icon", "📄")
                title = s.get("title", s.get("id", "?"))
                summary = s.get("summary", "")
                pin = "📌 " if s.get("pinned") else ""
                state = "" if s.get("status") == "active" else f" ({s.get('status')})"
                rel = s.get("main_file") or _slot_main_file_rel(domain, s["id"])
                lines.append(f"- {icon} {pin}**{title}**{state} — `{rel}`")
                if summary:
                    lines.append(f"  - {summary}")
            if archived:
                lines.append(f"- _archived: {len(archived)} slot(s) (run `archival_memory_search` to find)_")
            lines.append("")

        # Non-slot domains (commitments, patterns, journal, etc.)
        non_slot_dirs = ["commitments", "patterns", "journal"]
        for dir_name in non_slot_dirs:
            dir_path = os.path.join(_memory_dir(), dir_name)
            if not os.path.isdir(dir_path):
                continue
            files = sorted(
                [f for f in os.listdir(dir_path) if f.endswith(".md")],
                reverse=(dir_name == "journal"),
            )
            if not files:
                continue
            lines.append(f"## {dir_name.title()}")
            lines.append("")
            for f in files[:20]:  # cap at 20 to keep index short
                lines.append(f"- `{dir_name}/{f}`")
            if len(files) > 20:
                lines.append(f"- _... and {len(files) - 20} more_")
            lines.append("")

        memory.update_index("\n".join(lines))
        return True
    except Exception as e:
        print(f"[memory_router] regenerate_index_md failed: {e}")
        return False


def _daily_writes_path() -> str:
    """Per-user log of slot writes for journal, grouped by date.

    Format: {"YYYY-MM-DD": [{slot_id, domain, action, content, ...}, ...]}.
    Journal reads this file as "what changed today". For matched slots,
    content/append_entry is the actual Pass4Append entry that reached main.md;
    raw_content_to_integrate is retained only for debugging.
    """
    return os.path.join(_memory_dir(), "_slots", "daily_writes.json")


_daily_writes_lock = threading.Lock()
_daily_slot_appends_lock = threading.Lock()


def _daily_slot_appends_path() -> str:
    """Per-user append ledger consumed by SlotDailyCompactor."""
    return os.path.join(_memory_dir(), "_slots", "daily_slot_appends.json")


def _trim_date_map(data: dict, keep_days: int = 30) -> None:
    keys_sorted = sorted(data.keys(), reverse=True)
    if len(keys_sorted) > keep_days:
        for old_key in keys_sorted[keep_days:]:
            data.pop(old_key, None)


def _append_event_id(domain: str, slot_id: str, ts: str, entry: str) -> str:
    ts_part = re.sub(r"[^0-9]", "", ts or "")[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
    digest = hashlib.sha1(f"{domain}/{slot_id}/{ts}/{entry}".encode("utf-8")).hexdigest()[:6]
    return f"{ts_part}_{digest}"


def _slot_ledger_key(domain: str, slot_id: str) -> str:
    return f"{domain}/{slot_id}"


def _record_daily_write(domain: str, slot_id: str, action: str,
                         content: str, source_type: str,
                         source_context: str = "",
                         **fields: Any) -> None:
    """Append a slot write event into daily_writes.json.

    Best-effort only: memory writes must not fail because the diary ledger
    could not be updated.
    """
    try:
        now = datetime.now()
        date_key = now.strftime("%Y-%m-%d")
        with _daily_writes_lock:
            _ensure_slots_dir()
            path = _daily_writes_path()
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            entry = {
                "time": now.strftime("%H:%M"),
                "domain": domain,
                "slot_id": slot_id,
                "action": action,  # "created" | "matched"
                "source_type": source_type,
                "source_context": (source_context or "")[:300],
                "content": (content or "")[:1500],
            }
            for key, value in fields.items():
                if value is None:
                    continue
                if isinstance(value, str):
                    entry[key] = value[:4000] if key in {
                        "initial_body", "raw_content_to_integrate"
                    } else value[:1500]
                else:
                    entry[key] = value
            data.setdefault(date_key, []).append(entry)
            _trim_date_map(data)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"[memory_router] daily_writes log failed: {e}")


def _record_daily_slot_append(*, domain: str, slot_id: str, main_file: str,
                              slot_title: str, slot_summary: str,
                              append_entry: str, entry_kind: str,
                              current_time: str) -> None:
    """Record an append entry after it has reached main.md and slot metadata."""
    entry = re.sub(r"\s+", " ", (append_entry or "").strip())
    if not entry:
        return
    try:
        date_key = (current_time or _now_iso())[:10]
        with _daily_slot_appends_lock:
            _ensure_slots_dir()
            path = _daily_slot_appends_path()
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            day = data.setdefault(date_key, {})
            key = _slot_ledger_key(domain, slot_id)
            slot_bucket = day.setdefault(key, {
                "domain": domain,
                "slot_id": slot_id,
                "main_file": main_file,
                "slot_title_at_append": slot_title,
                "slot_summary_at_append": slot_summary,
                "entries": [],
                "last_appended_at": current_time,
            })
            slot_bucket.update({
                "domain": domain,
                "slot_id": slot_id,
                "main_file": main_file,
                "slot_title_at_append": slot_title,
                "slot_summary_at_append": slot_summary,
                "last_appended_at": current_time,
            })
            event_id = _append_event_id(domain, slot_id, current_time, entry)
            existing_ids = {
                str(item.get("id", ""))
                for item in slot_bucket.get("entries", [])
                if isinstance(item, dict)
            }
            if event_id not in existing_ids:
                slot_bucket.setdefault("entries", []).append({
                    "id": event_id,
                    "ts": current_time,
                    "entry": entry,
                    "entry_kind": entry_kind or "other",
                    "compacted": False,
                })
            _trim_date_map(data)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"[memory_router] daily_slot_appends log failed: {e}")


def list_daily_writes(date_str: str) -> list[dict]:
    """Read raw writes for a specific date. Used by journal.py."""
    path = _daily_writes_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data.get(date_str, []) or []
    except Exception:
        return []


def list_daily_slot_appends(date_str: str) -> dict:
    """Read uncompacted append ledger for a specific date."""
    path = _daily_slot_appends_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        day = data.get(date_str, {}) or {}
        return day if isinstance(day, dict) else {}
    except Exception:
        return {}


def _load_daily_slot_appends_all() -> dict:
    path = _daily_slot_appends_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_daily_slot_appends_all(data: dict) -> bool:
    try:
        _ensure_slots_dir()
        _trim_date_map(data)
        path = _daily_slot_appends_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[memory_router] save daily_slot_appends failed: {e}")
        return False


def _plain_char_count(markdown: str) -> int:
    """Approximate rendered char count for compactor target_mode."""
    text = re.sub(r"```.*?```", "", markdown or "", flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>#]", "", text)
    text = re.sub(r"\s+", "", text)
    return len(text)


def _slot_daily_target_mode(rendered_char_count: int) -> str:
    if rendered_char_count <= 1000:
        return "preserve"
    if rendered_char_count <= 1800:
        return "compact"
    return "heavy_compact"


def _pending_entries_for_bucket(bucket: dict) -> list[dict]:
    entries = bucket.get("entries", []) if isinstance(bucket, dict) else []
    if not isinstance(entries, list):
        return []
    return [
        e for e in entries
        if (
            isinstance(e, dict)
            and not e.get("compacted")
            and not _is_terminal_append_entry(e)
            and e.get("entry")
        )
    ]


def _is_terminal_append_entry(entry: dict) -> bool:
    return bool(
        isinstance(entry, dict)
        and (
            entry.get("terminal")
            or entry.get("terminal_at")
            or entry.get("terminal_reason")
        )
    )


def _collect_pending_slot_groups(data: dict, date_key: str) -> dict[str, dict]:
    """Group all pending append entries up to *date_key* by slot.

    A nightly run may be missed because the desktop app was asleep or closed.
    If the next day appends to the same slot, processing only the older date's
    bucket would see a stale ``last_appended_at`` and skip forever. Grouping by
    slot across all pending dates lets the next successful run compact the old
    and new append records together.
    """
    groups: dict[str, dict] = {}
    for day_key in sorted((data or {}).keys()):
        if day_key > date_key:
            continue
        day = data.get(day_key)
        if not isinstance(day, dict):
            continue
        for key, bucket in day.items():
            if not isinstance(bucket, dict):
                continue
            pending = _pending_entries_for_bucket(bucket)
            if not pending:
                continue
            domain = bucket.get("domain", "")
            slot_id = bucket.get("slot_id", "")
            group = groups.setdefault(key, {
                "domain": domain,
                "slot_id": slot_id,
                "main_file": bucket.get("main_file", ""),
                "entries": [],
                "refs": [],
                "dates": set(),
                "latest_last_appended_at": "",
            })
            group["domain"] = group.get("domain") or domain
            group["slot_id"] = group.get("slot_id") or slot_id
            group["main_file"] = group.get("main_file") or bucket.get("main_file", "")
            group["dates"].add(day_key)
            last_appended_at = str(bucket.get("last_appended_at", "") or "")
            if last_appended_at > str(group.get("latest_last_appended_at", "") or ""):
                group["latest_last_appended_at"] = last_appended_at
            for entry in pending:
                copied = dict(entry)
                group.setdefault("items", []).append({
                    "ts": str(entry.get("ts", "")),
                    "entry": copied,
                    "ref": (day_key, key, str(entry.get("id", ""))),
                })

    for group in groups.values():
        items = sorted(group.pop("items", []) or [], key=lambda item: item.get("ts", ""))
        group["entries"] = [item["entry"] for item in items if isinstance(item, dict)]
        group["refs"] = [item["ref"] for item in items if isinstance(item, dict)]
        group["dates"] = sorted(group.get("dates") or [])
    return groups


def has_uncompacted_slot_appends(date_str: str | None = None) -> bool:
    """Return whether this user has pending slot appends up to *date_str*."""
    date_key = date_str or _now_iso()[:10]
    with _daily_slot_appends_lock:
        data = _load_daily_slot_appends_all()
    return bool(_collect_pending_slot_groups(data, date_key))


def _has_uncompacted_appends(data: dict, domain: str, slot_id: str) -> bool:
    key = _slot_ledger_key(domain, slot_id)
    for day in (data or {}).values():
        if not isinstance(day, dict):
            continue
        bucket = day.get(key)
        if _pending_entries_for_bucket(bucket):
            return True
    return False


def _mark_compacted_in_bucket(bucket: dict, entry_ids: set[str],
                              compacted_at: str) -> int:
    count = 0
    for entry in bucket.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")) not in entry_ids:
            continue
        if entry.get("compacted"):
            continue
        entry["compacted"] = True
        entry["compacted_at"] = compacted_at
        count += 1
    return count


def _mark_compacted_refs(data: dict, refs: list[tuple[str, str, str]],
                         compacted_at: str) -> int:
    count = 0
    for day_key, slot_key, entry_id in refs:
        if not entry_id:
            continue
        day = data.get(day_key)
        if not isinstance(day, dict):
            continue
        bucket = day.get(slot_key)
        if not isinstance(bucket, dict):
            continue
        count += _mark_compacted_in_bucket(bucket, {entry_id}, compacted_at)
    return count


def _mark_terminal_in_bucket(bucket: dict, entry_ids: set[str],
                             terminal_at: str, reason: str,
                             target_slot_id: str | None = None, *,
                             mark_missing_id: bool = False,
                             mark_all_pending: bool = False) -> int:
    """Mark pending append entries terminal when they can never be compacted."""
    count = 0
    for entry in bucket.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("compacted") or _is_terminal_append_entry(entry):
            continue
        entry_id = str(entry.get("id", "") or "")
        should_mark = (
            mark_all_pending
            or (entry_id and entry_id in entry_ids)
            or (not entry_id and mark_missing_id)
        )
        if not should_mark:
            continue
        entry["terminal"] = True
        entry["terminal_reason"] = reason
        entry["terminal_at"] = terminal_at
        if target_slot_id:
            entry["terminal_target_slot_id"] = target_slot_id
        count += 1
    return count


def _mark_terminal_refs(data: dict, refs: list[tuple[str, str, str]],
                        terminal_at: str, reason: str,
                        target_slot_id: str | None = None) -> int:
    refs_by_bucket: dict[tuple[str, str], dict[str, Any]] = {}
    for day_key, slot_key, entry_id in refs:
        bucket_key = (day_key, slot_key)
        spec = refs_by_bucket.setdefault(bucket_key, {"ids": set(), "missing": False})
        if entry_id:
            spec["ids"].add(entry_id)
        else:
            spec["missing"] = True

    count = 0
    for (day_key, slot_key), spec in refs_by_bucket.items():
        day = data.get(day_key)
        if not isinstance(day, dict):
            continue
        bucket = day.get(slot_key)
        if not isinstance(bucket, dict):
            continue
        count += _mark_terminal_in_bucket(
            bucket,
            spec["ids"],
            terminal_at,
            reason,
            target_slot_id,
            mark_missing_id=bool(spec["missing"]),
        )
    return count


def _terminalize_refs(refs: list[tuple[str, str, str]], *,
                      reason: str,
                      target_slot_id: str | None = None,
                      terminal_at: str | None = None) -> int:
    """Best-effort terminal marker for compactor refs in daily_slot_appends."""
    if not refs:
        return 0
    terminal_at = terminal_at or _now_iso()
    with _daily_slot_appends_lock:
        data = _load_daily_slot_appends_all()
        count = _mark_terminal_refs(data, refs, terminal_at, reason, target_slot_id)
        if count:
            _save_daily_slot_appends_all(data)
        return count


def _terminalize_slot_appends(domain: str, slot_id: str, *,
                              reason: str,
                              target_slot_id: str | None = None,
                              terminal_at: str | None = None) -> int:
    """Terminalize all pending append ledger entries for a removed slot."""
    terminal_at = terminal_at or _now_iso()
    slot_key = _slot_ledger_key(domain, slot_id)
    with _daily_slot_appends_lock:
        data = _load_daily_slot_appends_all()
        count = 0
        for day in (data or {}).values():
            if not isinstance(day, dict):
                continue
            bucket = day.get(slot_key)
            if not isinstance(bucket, dict):
                continue
            count += _mark_terminal_in_bucket(
                bucket,
                set(),
                terminal_at,
                reason,
                target_slot_id,
                mark_all_pending=True,
            )
        if count:
            _save_daily_slot_appends_all(data)
        return count


def run_slot_daily_compactor(date_str: str | None = None, *,
                             max_slots: int = 20,
                             tier: str = "memory",
                             reasoning: bool = False) -> dict:
    """Compact dirty append-only slots up to one date.

    Reads daily_slot_appends.json, groups uncompacted entries by slot across
    all dates up to ``date_str``, asks the LLM to rewrite each affected slot
    body, then marks only successfully applied entries as compacted. Best
    effort: failures leave the ledger pending so a later run can retry.
    """
    now = _now_iso()
    date_key = date_str or now[:10]
    result = {
        "ok": True,
        "date": date_key,
        "checked": 0,
        "compacted": 0,
        "entries_compacted": 0,
        "skipped": [],
        "failed": [],
    }

    with _daily_slot_appends_lock:
        snapshot = _load_daily_slot_appends_all()
    groups = _collect_pending_slot_groups(snapshot, date_key)
    if not groups:
        return result

    def _skip_terminal(slot_key: str, group_data: dict, reason: str) -> None:
        terminalized = _terminalize_refs(
            list(group_data.get("refs", []) or []),
            reason=reason,
            terminal_at=now,
        )
        item = {"slot": slot_key, "reason": reason}
        if terminalized:
            item["terminalized"] = terminalized
        result["skipped"].append(item)

    for key, group in list(groups.items()):
        if result["checked"] >= max_slots:
            result["skipped"].append({"slot": key, "reason": "max_slots_reached"})
            break
        pending = list(group.get("entries", []) or [])
        if not pending:
            continue
        result["checked"] += 1

        domain = group.get("domain", "")
        slot_id = group.get("slot_id", "")
        if domain not in DOMAINS or not slot_id:
            _skip_terminal(key, group, "invalid_bucket")
            continue

        slot = get_slot(domain, slot_id)
        if not slot:
            _skip_terminal(key, group, "missing_slot")
            continue
        snapshot_last_appended_at = str(slot.get("last_appended_at", "") or "")
        latest_ledger_last_appended_at = str(group.get("latest_last_appended_at", "") or "")
        if snapshot_last_appended_at != latest_ledger_last_appended_at:
            result["skipped"].append({"slot": key, "reason": "last_appended_at_changed"})
            continue

        main_file = slot.get("main_file") or _slot_main_file_rel(domain, slot_id)
        current_md = memory.read_file(main_file) or ""
        current_body = _strip_title_header(current_md, slot.get("title", ""))
        markdown_count = len(current_body)
        rendered_count = _plain_char_count(current_body)
        target_mode = _slot_daily_target_mode(rendered_count)

        try:
            compacted = call_slot_daily_compactor(
                domain=domain,
                slot_id=slot_id,
                current_title=slot.get("title", ""),
                current_summary=slot.get("summary", ""),
                current_body=current_body,
                current_aliases=slot.get("aliases", []) or [],
                append_dirty_since=slot.get("append_dirty_since", ""),
                append_count_since_compact=int(slot.get("append_count_since_compact") or 0),
                last_appended_at=slot.get("last_appended_at", ""),
                append_entries=pending,
                markdown_char_count=markdown_count,
                rendered_char_count=rendered_count,
                target_mode=target_mode,
                current_time=now,
                tier=tier,
                reasoning=reasoning,
                max_tokens=6000,
                reasoning_budget=0,
            )
        except Exception as e:
            result["failed"].append({"slot": key, "reason": f"llm_error: {str(e)[:160]}"})
            continue

        if not compacted:
            result["failed"].append({"slot": key, "reason": "llm_retry_exhausted"})
            continue

        new_title = (getattr(compacted, "title", "") or "").strip()
        new_summary = (getattr(compacted, "summary", "") or "").strip()
        new_body = _strip_title_header(
            (getattr(compacted, "body", "") or "").strip(), new_title
        )
        if not new_title or not new_summary or not new_body:
            result["failed"].append({"slot": key, "reason": "empty_compactor_output"})
            continue

        entry_ids = {str(e.get("id", "")) for e in pending if e.get("id")}
        if not entry_ids:
            _skip_terminal(key, group, "missing_entry_ids")
            continue

        with _slot_write_lock:
            latest_slot = get_slot(domain, slot_id)
            if not latest_slot:
                _skip_terminal(key, group, "missing_slot_after_llm")
                continue
            with _daily_slot_appends_lock:
                latest_data = _load_daily_slot_appends_all()
                latest_groups = _collect_pending_slot_groups(latest_data, date_key)
                latest_group = latest_groups.get(key)
                latest_pending = list((latest_group or {}).get("entries", []) or [])
                latest_pending_ids = {
                    str(e.get("id", "")) for e in latest_pending if e.get("id")
                }
                entry_ids = {str(e.get("id", "")) for e in pending if e.get("id")}
                if (
                    not latest_group
                    or latest_group.get("latest_last_appended_at") != latest_ledger_last_appended_at
                    or latest_slot.get("last_appended_at") != snapshot_last_appended_at
                    or latest_pending_ids != entry_ids
                ):
                    result["skipped"].append({"slot": key, "reason": "ledger_changed_after_llm"})
                    continue

                latest_slot = dict(latest_slot)
                latest_slot["title"] = new_title
                latest_slot["summary"] = new_summary
                latest_slot["main_file"] = main_file
                latest_slot["last_compacted_at"] = now
                latest_slot["append_count_since_compact"] = 0

                marked = _mark_compacted_refs(
                    latest_data, list(latest_group.get("refs", []) or []), now
                )
                if marked <= 0:
                    result["skipped"].append({"slot": key, "reason": "nothing_marked"})
                    continue
                if not _has_uncompacted_appends(latest_data, domain, slot_id):
                    latest_slot.pop("append_dirty_since", None)

                new_main_md = f"# {new_title}\n\n{new_body.strip()}\n"
                ok = memory.write_file(main_file, new_main_md)
                if not ok:
                    result["failed"].append({"slot": key, "reason": "main.md write failed"})
                    continue
                upsert_slot(domain, latest_slot)
                _save_daily_slot_appends_all(latest_data)

        result["compacted"] += 1
        result["entries_compacted"] += len(entry_ids)

    if result["compacted"]:
        try:
            regenerate_index_md()
        except Exception as e:
            print(f"[SlotDailyCompactor] index regen failed: {e}")
    return result


def _iso_to_ts(iso: str) -> float:
    """ISO date string → unix ts (or 0 on parse failure)."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ===========================================================================
# Daily slot audit (Phase C — auto status transitions)
# ===========================================================================

def daily_slot_audit() -> dict:
    """Walk all slots and apply status transitions based on last_active age.

    Rules:
    - active → paused if last_active >= 30 days
    - paused → archived if last_active >= 90 days
    - pinned slots are exempt from auto-downgrade

    Returns: {domain: {paused: int, archived: int, total: int}}
    """
    result = {}
    now = datetime.now()
    for domain in DOMAINS:
        slots = load_all_slots(domain)
        if not slots:
            continue
        paused_count = 0
        archived_count = 0
        changed = False
        for s in slots:
            if s.get("pinned"):
                continue
            last = s.get("last_active", "")
            try:
                last_dt = datetime.fromisoformat(last)
            except (ValueError, TypeError):
                continue
            age_days = (now - last_dt).days
            cur_status = s.get("status", "active")
            if cur_status == "active" and age_days >= PAUSED_AFTER_DAYS:
                s["status"] = "paused"
                paused_count += 1
                changed = True
            elif cur_status == "paused" and age_days >= ARCHIVED_AFTER_DAYS:
                s["status"] = "archived"
                archived_count += 1
                changed = True
        if changed:
            with _slot_write_lock:
                _save_all_slots_atomic(domain, slots)
        result[domain] = {
            "paused": paused_count,
            "archived": archived_count,
            "total": len(slots),
        }
    return result


# ===========================================================================
# Sleep Agent v3: route_with_slot_write — main router for Slot Writer output
# ===========================================================================
#
# Slot Writer v3 already decided domain + match/new + (for new) full metadata.
# This entry point just:
#   1. Validate slot exists (match) or resolve id collision (new)
#   2. Call Pass 4 AppendEditor to add one timestamped memory record
#   3. Upsert slot meta
#
# Same write semantics as the bottom of _route_one_domain (Pass 4 +
# atomic write + daily_writes log + index regen) — extracted so we can
# call it without going through Pass 1/2/3.

def route_with_slot_write(
    slot_write,
    source_context: str = "",
    *,
    pass4_tier: str = "memory",
    pass4_reasoning: bool = False,
    pass4_max_tokens: int = 1000,
    pass4_reasoning_budget: int = 0,
) -> dict:
    """Route a single Slot Writer v3 output to Pass 4 + storage.

    Args:
        slot_write: a SlotWrite Pydantic instance (memory_prompts_v3.SlotWrite)
                    OR a dict with the same shape.
        source_context: original chat batch transcript (for Pass 4 context).

    Returns same shape as route_and_write:
        {ok, action, domain, slot_id, reason, body_len_before, body_len_after}
    """
    import identity
    # call_pass4_append_slot is imported at module top — reuse it so
    # patch.object(memory_router, ...) works correctly in tests.

    # Normalize to dict-like access regardless of input form
    def _get(obj, key, default=None):
        if hasattr(obj, key):
            return getattr(obj, key)
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default

    kind = _get(slot_write, "kind")
    domain = _get(slot_write, "domain")
    if domain not in DOMAINS:
        return {"ok": False, "action": "failed",
                "reason": f"invalid domain: {domain}"}

    integration = _get(slot_write, "integration")
    if integration is None:
        return {"ok": False, "action": "failed",
                "reason": "missing integration spec"}
    content = _get(integration, "content_to_integrate", "")

    if not content or not content.strip():
        return {"ok": False, "action": "failed",
                "reason": "empty content_to_integrate"}

    detected_source = _detect_source_type(source_context)

    # ===== Branch by kind =====
    # new path: skip Pass 4, use Slot Writer outputs directly as initial body +
    # summary. match path: unified Pass 4 appends one timestamped entry; whole
    # slot rewriting is reserved for nightly compaction/curation.
    if kind == "new":
        new_meta = _get(slot_write, "new_slot_meta")
        if new_meta is None:
            return {"ok": False, "action": "failed",
                    "reason": "new requires new_slot_meta", "domain": domain}

        existing_ids = [s["id"] for s in load_all_slots(domain)]
        existing_aliases = []
        for s in load_all_slots(domain):
            existing_aliases.extend(s.get("aliases", []))
        proposed_id = _get(new_meta, "id", "")
        new_id = _resolve_id_collision(proposed_id, existing_ids, existing_aliases)

        # Safety: don't allow self/identity to be created via this path
        if domain == "self" and new_id == "identity":
            return {"ok": False, "action": "failed",
                    "reason": "self/identity is managed by onboarding cascade",
                    "domain": domain}

        new_title = _get(new_meta, "title", "")
        new_summary = _get(new_meta, "summary", "")
        new_aliases = list(_get(new_meta, "aliases", []) or [])

        # Identity safety: drop alias equal to user's name
        user_name = (identity.get_user_name() or "").strip().lower()
        if user_name and domain != "self":
            new_aliases = [a for a in new_aliases
                            if a and a.strip().lower() != user_name]

        target_slot = {
            "id": new_id,
            "title": new_title,
            "icon": _get(new_meta, "icon", "📄"),
            "status": "active",
            "pinned": False,
            "summary": new_summary,
            "aliases": new_aliases,
            "main_file": _slot_main_file_rel(domain, new_id),
            "last_active": _now_iso(),
            "created": _now_iso(),
        }

        # body = Slot Writer 出的 content_to_integrate (1-600 字).
        # 后续 match 只追加条目；夜间 compactor 再统一整理正文.
        new_body = content.strip()
        new_main_md = f"# {new_title}\n\n{new_body}\n"

        try:
            with _slot_write_lock:
                ok = memory.write_file(target_slot["main_file"], new_main_md)
                if not ok:
                    return {"ok": False, "action": "failed",
                            "reason": "main.md write failed",
                            "domain": domain, "slot_id": new_id}
                upsert_slot(domain, target_slot)
            try:
                regenerate_index_md()
            except Exception as e:
                print(f"[router_v3] index regen failed: {e}")
        except Exception as e:
            return {"ok": False, "action": "failed",
                    "reason": f"write error: {e}",
                    "domain": domain, "slot_id": new_id}

        _record_daily_write(
            domain=domain, slot_id=new_id, action="created",
            content=content, source_type=detected_source,
            source_context=source_context,
            slot_title=new_title,
            slot_summary=new_summary,
            initial_body=content,
            raw_content_to_integrate=content,
            main_file=target_slot["main_file"],
        )

        return {
            "ok": True, "action": "created",
            "domain": domain, "slot_id": new_id,
            "reason": "slot_writer_v3_new (no Pass 4)",
            "body_len_before": 0,
            "body_len_after": len(new_body),
        }

    elif kind == "match":
        slot_id = _get(slot_write, "slot_id")
        if not slot_id:
            return {"ok": False, "action": "failed",
                    "reason": "match requires slot_id", "domain": domain}
        full_slot = get_slot(domain, slot_id)
        if full_slot is None:
            print(f"[router_v3] slot_id_not_exist: domain={domain} slot_id={slot_id}")
            return {"ok": False, "action": "failed",
                    "reason": f"slot_id_not_exist: {slot_id}",
                    "domain": domain, "slot_id": slot_id}

        target_slot = dict(full_slot)
        target_slot["main_file"] = (target_slot.get("main_file")
                                     or _slot_main_file_rel(domain, slot_id))
        existing_main_md = memory.read_file(target_slot["main_file"]) or ""
        # Resurrect paused slot — matched again means it's active
        if target_slot.get("status") == "paused":
            target_slot["status"] = "active"

        # Identity safety net (J5): self/identity never via router
        if domain == "self" and target_slot["id"] == "identity":
            return {"ok": False, "action": "failed",
                    "reason": "self/identity managed via cascade_identity",
                    "domain": domain, "slot_id": target_slot["id"]}

        # Pass 4: append one timestamped memory record. It never rewrites body.
        ground_truth = identity.compose_ground_truth_block()
        current_body = _strip_title_header(existing_main_md, target_slot["title"])

        current_time = _now_iso()
        edit_result = call_pass4_append_slot(
            domain=domain,
            current_title=target_slot["title"],
            current_summary=target_slot.get("summary", ""),
            current_body=current_body,
            current_aliases=target_slot.get("aliases", []),
            new_content=content,
            source_type=detected_source,
            source_context=source_context,
            ground_truth_block=ground_truth,
            current_time=current_time,
            tier=pass4_tier,
            reasoning=False,
            max_tokens=min(int(pass4_max_tokens or 1000), 1000),
            reasoning_budget=0,
        )
        if edit_result is None:
            return {"ok": False, "action": "failed",
                    "reason": "pass4 append retry exhausted",
                    "domain": domain, "slot_id": target_slot["id"]}
        if not edit_result.get("should_append") or not edit_result.get("append_entry"):
            return {
                "ok": True, "action": "skipped",
                "domain": domain, "slot_id": target_slot["id"],
                "reason": f"pass4 append skipped: {edit_result.get('skip_reason', '')}",
                "body_len_before": len(current_body),
                "body_len_after": len(current_body),
            }

        body_before = current_body
        title_update = (edit_result.get("title_update") or "").strip()
        summary_update = (edit_result.get("summary_update") or "").strip()
        new_title = title_update or target_slot["title"]
        new_summary = summary_update or target_slot.get("summary", "")
        new_body = _append_slot_body_entry(
            current_body, edit_result["append_entry"], current_time
        )
        if new_body == current_body:
            return {
                "ok": True, "action": "skipped",
                "domain": domain, "slot_id": target_slot["id"],
                "reason": "pass4 append skipped: append entry already present",
                "body_len_before": len(current_body),
                "body_len_after": len(current_body),
            }
        route_reason = "slot_writer_v3_match_append"
        target_slot["title"] = new_title
        target_slot["summary"] = new_summary
        if not target_slot.get("append_dirty_since"):
            target_slot["append_dirty_since"] = current_time
        target_slot["last_appended_at"] = current_time
        target_slot["append_count_since_compact"] = int(
            target_slot.get("append_count_since_compact") or 0
        ) + 1

        # Identity safety: drop alias equal to user's name (J5)
        user_name = (identity.get_user_name() or "").strip().lower()
        existing_aliases = list(target_slot.get("aliases", []) or [])
        for a in edit_result.get("alias_additions", []) or []:
            if not a:
                continue
            if user_name and domain != "self" and a.strip().lower() == user_name:
                print(f"[router_v3] dropped alias '{a}' on {domain}/{target_slot['id']} "
                      f"— equals user's name")
                continue
            if a not in existing_aliases:
                existing_aliases.append(a)
        target_slot["aliases"] = existing_aliases

        new_main_md = f"# {new_title}\n\n{new_body}\n"

        try:
            with _slot_write_lock:
                ok = memory.write_file(target_slot["main_file"], new_main_md)
                if not ok:
                    return {"ok": False, "action": "failed",
                            "reason": "main.md write failed",
                            "domain": domain, "slot_id": target_slot["id"]}
                target_slot["last_active"] = _now_iso()
                upsert_slot(domain, target_slot)
            try:
                regenerate_index_md()
            except Exception as e:
                print(f"[router_v3] index regen failed: {e}")
        except Exception as e:
            return {"ok": False, "action": "failed",
                    "reason": f"write error: {e}",
                    "domain": domain, "slot_id": target_slot["id"]}

        append_entry = edit_result["append_entry"]
        entry_kind = edit_result.get("entry_kind") or "other"
        _record_daily_write(
            domain=domain, slot_id=target_slot["id"], action="matched",
            content=append_entry, source_type=detected_source,
            source_context=source_context,
            slot_title=new_title,
            slot_summary=new_summary,
            append_entry=append_entry,
            entry_kind=entry_kind,
            raw_content_to_integrate=content,
            main_file=target_slot["main_file"],
        )
        _record_daily_slot_append(
            domain=domain,
            slot_id=target_slot["id"],
            main_file=target_slot["main_file"],
            slot_title=new_title,
            slot_summary=new_summary,
            append_entry=append_entry,
            entry_kind=entry_kind,
            current_time=current_time,
        )

        return {
            "ok": True, "action": "matched",
            "domain": domain, "slot_id": target_slot["id"],
            "reason": route_reason,
            "body_len_before": len(body_before),
            "body_len_after": len(new_body),
        }

    else:
        return {"ok": False, "action": "failed",
                "reason": f"invalid kind: {kind}", "domain": domain}


# ===========================================================================
# Deprecated entry points (kept as no-op stubs)
# ===========================================================================
#
# trigger_route_async / route_and_write / route_fragments served the old
# fragments → Pass 1-4 pipeline. After the Sleep Agent v3 refactor those
# Pass 1-3 LLMs were deleted (chat sleep agent now outputs slot_writes that
# go directly to Pass 4 via route_with_slot_write).
#
# Screenshot path callers (screen_analyzer / screen_sleep_agent) still
# invoke trigger_route_async during the transition window. We keep these
# as no-op stubs so imports do not break; they log a single warning and
# return. See docs/SCREENSHOT_REFACTOR_TODO.md for the migration plan.

def trigger_route_async(content: str, source_type: str = "chat",
                        source_context: str = "",
                        broadcast: bool = True) -> None:
    """DEPRECATED: no-op stub. Screenshot pipeline pending v3 migration."""  # noqa: D401
    if content and content.strip():
        print(f"[router] trigger_route_async called but pipeline removed; "
              f"source_type={source_type} content_len={len(content)} (no-op)")

def route_and_write(domain_hint=None, content: str = "", *,
                    source_type: str = "chat",
                    source_context: str = "") -> dict:
    """DEPRECATED: no-op stub. Use route_with_slot_write instead."""
    return {"ok": False, "action": "failed",
            "reason": "route_and_write removed in v3; use route_with_slot_write",
            "domain": domain_hint}

def route_fragments(fragments: list) -> list:
    """DEPRECATED: no-op stub."""
    return [{"ok": False, "action": "failed",
             "reason": "route_fragments removed in v3"}
            for _ in (fragments or [])]


# ===========================================================================
# Slot manual operations (used by UI: edit / merge / archive / pin / delete)
# ===========================================================================

def update_slot_metadata(domain: str, slot_id: str, updates: dict) -> Optional[dict]:
    """Apply user-edited fields to a slot. Allowed fields:
        title, icon, status, pinned, summary, aliases

    Returns updated slot dict or None if not found.
    """
    if domain not in DOMAINS:
        return None
    allowed = {"title", "icon", "status", "pinned", "summary", "aliases"}
    with _slot_write_lock:
        slots = load_all_slots(domain)
        target = None
        for s in slots:
            if s.get("id") == slot_id:
                target = s
                break
        if target is None:
            return None
        for k, v in (updates or {}).items():
            if k in allowed:
                target[k] = v
        if not _save_all_slots_atomic(domain, slots):
            return None
    _notify_curator_change(domain)
    try:
        regenerate_index_md()
    except Exception:
        pass
    return target


def merge_slots(domain: str, source_id: str, target_id: str) -> Optional[dict]:
    """Merge source slot into target slot.

    Process:
    1. Read both main.md files
    2. Use legacy slot rewrite helper to integrate source content into target
    3. Write merged main.md to target's location
    4. Delete source slot (registry + main.md file)
    5. Merge target's aliases (union)
    """
    if domain not in DOMAINS:
        return None
    if source_id == target_id:
        return None
    # Identity safety: never merge into or out of self/identity. That slot
    # is reserved for the cascade_identity path (onboarding / settings).
    if domain == "self" and (source_id == "identity" or target_id == "identity"):
        return None
    src = get_slot(domain, source_id)
    dst = get_slot(domain, target_id)
    if src is None or dst is None:
        return None

    src_md = memory.read_file(src.get("main_file") or _slot_main_file_rel(domain, source_id)) or ""
    dst_md = memory.read_file(dst.get("main_file") or _slot_main_file_rel(domain, target_id)) or ""

    if not src_md.strip():
        # Just delete source, no integration needed
        delete_slot(
            domain,
            source_id,
            delete_file=True,
            ledger_terminal_reason="merged_into",
            ledger_terminal_target_slot_id=target_id,
        )
        try:
            regenerate_index_md()
        except Exception:
            pass
        return dst

    # Manual merge still needs a whole-slot rewrite helper; this is not the
    # daily Pass 4 append path used by chat/screenshot match writes.
    edit_result = call_legacy_slot_merge_rewrite(
        domain=domain,
        current_title=dst["title"],
        current_summary=dst.get("summary", ""),
        current_body=dst_md,
        current_aliases=dst.get("aliases", []),
        new_content=(
            f"以下内容来自被合并的 slot '{src.get('title', source_id)}', "
            f"请整合进当前 slot:\n\n{src_md}"
        ),
        source_type="mixed",
        source_context=f"slot merge: {source_id} → {target_id}",
    )
    if edit_result is None:
        return None

    new_title = edit_result.get("title", dst["title"])
    new_summary = edit_result.get("summary", dst.get("summary", ""))
    new_body = edit_result.get("body", dst_md)
    alias_additions = edit_result.get("alias_additions", []) or []

    # Write merged body to target
    memory.write_file(
        dst.get("main_file") or _slot_main_file_rel(domain, target_id),
        new_body,
    )

    # Update target slot metadata: merge aliases (existing + src + Pass4 additions)
    new_aliases = list(set(
        dst.get("aliases", []) + src.get("aliases", []) + list(alias_additions)
    ))[:8]
    dst["title"] = new_title
    dst["summary"] = new_summary
    dst["aliases"] = new_aliases
    dst["last_active"] = _now_iso()
    upsert_slot(domain, dst)

    # Delete source
    delete_slot(
        domain,
        source_id,
        delete_file=True,
        ledger_terminal_reason="merged_into",
        ledger_terminal_target_slot_id=target_id,
    )

    try:
        regenerate_index_md()
    except Exception:
        pass

    return dst


def archive_slot(domain: str, slot_id: str) -> Optional[dict]:
    """Manually archive a slot."""
    return update_slot_metadata(domain, slot_id, {"status": "archived"})


def pin_slot(domain: str, slot_id: str, pinned: bool = True) -> Optional[dict]:
    return update_slot_metadata(domain, slot_id, {"pinned": pinned})


def hard_delete_slot(domain: str, slot_id: str) -> bool:
    """Delete slot record + main.md file. Used by UI 🗑 button."""
    ok = delete_slot(domain, slot_id, delete_file=True)
    if ok:
        try:
            regenerate_index_md()
        except Exception:
            pass
    return ok
