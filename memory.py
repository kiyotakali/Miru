"""Filesystem-based memory manager for Miru.

Memory is stored as markdown files under data/memory/, organized into
topic directories. An always-loaded index.md (<200 lines) maps all files.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
INDEX_PATH = os.path.join(MEMORY_DIR, "index.md")

# File-level lock to prevent concurrent writes from Summarizer + Sleep-time Agent
_write_lock = threading.Lock()

# In-memory cache for list_tree_rich() — keyed by data_dir for multi-tenant
_tree_cache: dict = {}  # {data_dir: (data, signature)}


def _get_data_dir() -> str:
    """Multi-tenant aware data dir.

    Prefer storage.get_data_dir() whenever a Flask user context exists so
    stale deleted/suspended user contexts cannot recreate data/users/<uid>.
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
        import storage
        return storage.get_data_dir()
    return DATA_DIR


def _memory_dir() -> str:
    return os.path.join(_get_data_dir(), "memory")


def _index_path() -> str:
    return os.path.join(_memory_dir(), "index.md")

# Default directory structure
DEFAULT_DIRS = [
    "people",
    "commitments",
    "journal",
    "patterns",
    "self",
    "projects",
    "topics",
]


def ensure_dirs():
    """Create memory directory tree if it doesn't exist."""
    os.makedirs(_memory_dir(), exist_ok=True)
    for d in DEFAULT_DIRS:
        os.makedirs(os.path.join(_memory_dir(), d), exist_ok=True)
    if not os.path.exists(_index_path()):
        _write_default_index()


def _write_default_index():
    lines = [
        "# Memory Index",
        "",
        "_Auto-maintained by Miru. Maps all memory files. Keep under 200 lines._",
        "",
        "## People",
        "",
        "## Commitments",
        "",
        "## Journal",
        "",
        "## Patterns",
        "",
        "## Self",
        "",
        "## Projects",
        "",
        "## Topics",
        "",
    ]
    with open(_index_path(), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_index() -> str:
    """Return the full index.md content (always loaded into chat context)."""
    ensure_dirs()
    with open(_index_path(), "r", encoding="utf-8") as f:
        return f.read()


def read_file(rel_path: str) -> Optional[str]:
    """Read a memory file by relative path (e.g. 'people/alice.md').

    Returns None if file doesn't exist.
    """
    safe = _safe_path(rel_path)
    if safe is None:
        return None
    if not os.path.exists(safe):
        return None
    with open(safe, "r", encoding="utf-8") as f:
        return f.read()


def list_journal_entries() -> list[dict]:
    """Return journal entries sorted by date descending with rich metadata.

    Each entry includes: date, file, size, sections (list of ## headings),
    diary_preview (first Miru diary paragraph), observation_count,
    has_diary (bool — has 晨间记录/每日回顾 sections).
    """
    ensure_dirs()
    journal_dir = os.path.join(_memory_dir(), "journal")
    if not os.path.isdir(journal_dir):
        return []
    entries = []
    for fname in os.listdir(journal_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(journal_dir, fname)
        date_str = fname.replace(".md", "")
        sections = []
        diary_preview = ""
        observation_count = 0
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for line in content.split("\n"):
                if line.startswith("## "):
                    sections.append(line[3:].strip())
                if line.startswith("- ["):
                    observation_count += 1
            # Extract diary preview: first paragraph from 晨间记录 or 每日回顾
            for marker in ("## 每日回顾", "## 晨间记录"):
                idx = content.find(marker)
                if idx >= 0:
                    after = content[idx + len(marker):].strip()
                    # Take first non-empty, non-heading, non-list line
                    for pline in after.split("\n"):
                        pline = pline.strip()
                        if pline and not pline.startswith("#") and not pline.startswith("- ["):
                            diary_preview = pline[:120]
                            break
                    if diary_preview:
                        break
            # Fallback: first Miru 碎碎念
            if not diary_preview:
                for line in content.split("\n"):
                    if "Miru的碎碎念" in line or "Miru的想法" in line:
                        text = line.lstrip("- ").strip()
                        # Remove the （Miru的碎碎念：）wrapper
                        text = re.sub(r"^[（(]Miru的(?:碎碎念|想法)[：:]\s*", "", text)
                        text = re.sub(r"[）)]$", "", text)
                        diary_preview = text[:120]
                        break
        except OSError:
            pass
        diary_sections = {"晨间记录", "每日回顾"}
        entries.append({
            "date": date_str,
            "file": f"journal/{fname}",
            "size": os.path.getsize(fpath),
            "sections": sections,
            "observation_count": observation_count,
            "has_diary": bool(diary_sections & set(sections)),
            "diary_preview": diary_preview,
        })
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_file(rel_path: str, content: str) -> bool:
    """Write content to a memory file. Creates parent dirs if needed.

    Thread-safe via _write_lock. Returns True on success.
    """
    safe = _safe_path(rel_path)
    if safe is None:
        return False
    with _write_lock:
        os.makedirs(os.path.dirname(safe), exist_ok=True)
        with open(safe, "w", encoding="utf-8") as f:
            f.write(content)
    # Notify clients: memory or journal changed
    try:
        from flask import g
        uid = getattr(g, "user_id", None)
        if uid:
            import sse
            scope = "journal" if rel_path.startswith("journal/") else "memory"
            sse.broadcast("data_changed", {"scope": scope}, user_id=uid)
    except Exception:
        pass
    return True


def append_to_file(rel_path: str, content: str) -> bool:
    """Append content to a memory file. Creates if it doesn't exist.

    Thread-safe via _write_lock.
    """
    safe = _safe_path(rel_path)
    if safe is None:
        return False
    with _write_lock:
        os.makedirs(os.path.dirname(safe), exist_ok=True)
        with open(safe, "a", encoding="utf-8") as f:
            f.write(content)
    return True


def update_index(new_content: str) -> bool:
    """Overwrite index.md with new content. Truncates to 200 lines.

    Thread-safe via _write_lock.
    """
    ensure_dirs()
    lines = new_content.split("\n")
    if len(lines) > 200:
        lines = lines[:200]
        lines.append("<!-- truncated to 200 lines -->")
    with _write_lock:
        with open(_index_path(), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return True


# ---------------------------------------------------------------------------
# Index Consolidation — pure algorithmic, no LLM
# ---------------------------------------------------------------------------

# Regex to extract file path from index entry line like:
# - [file.md](people/file.md) — description
_INDEX_ENTRY_RE = re.compile(r'-\s*\[.*?\]\(([^)]+)\)')
# Regex to extract date from journal entry path like journal/2026-03-30.md
_JOURNAL_DATE_RE = re.compile(r'journal/(\d{4}-\d{2}-\d{2})\.md')

JOURNAL_KEEP_DAYS = 30  # keep journal index entries for this many days


def consolidate_index(keep_journal_days: int = JOURNAL_KEEP_DAYS) -> dict:
    """Smart consolidation of index.md — pure algorithmic, no LLM.

    Three rules:
    1. Deduplicate: if the same file path appears multiple times in a section,
       keep only the last (most recent) entry.
    2. Journal pruning: remove journal index entries older than keep_journal_days.
       (The journal files still exist on disk and are searchable.)
    3. Clean formatting: normalize blank lines between sections.

    Returns: {"before": int, "after": int, "deduped": int, "journal_pruned": int}
    """
    ensure_dirs()
    content = read_index()
    lines = content.split("\n")

    today = datetime.now()
    cutoff_date = (today - __import__("datetime").timedelta(days=keep_journal_days)).strftime("%Y-%m-%d")

    # Parse into sections: list of (header_line, [entry_lines])
    sections = []
    current_header = None
    current_entries = []

    # Header/preamble lines before first ## section
    preamble = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if current_header is not None:
                sections.append((current_header, current_entries))
            else:
                # Save preamble (lines before first section)
                pass
            current_header = stripped
            current_entries = []
        elif current_header is None:
            preamble.append(line)
        else:
            current_entries.append(line)

    # Don't forget the last section
    if current_header is not None:
        sections.append((current_header, current_entries))

    deduped_count = 0
    journal_pruned_count = 0

    new_sections = []
    for header, entries in sections:
        is_journal = header.strip().lower() in ("## journal",)

        # Step 1: Deduplicate by file path (keep last occurrence)
        seen_paths = {}
        deduped_entries = []
        for entry in entries:
            stripped = entry.strip()
            if not stripped or not stripped.startswith("-"):
                # Keep non-entry lines (blank lines, comments)
                deduped_entries.append(entry)
                continue

            match = _INDEX_ENTRY_RE.search(stripped)
            if match:
                fpath = match.group(1)
                if fpath in seen_paths:
                    # Remove the earlier duplicate
                    old_idx = seen_paths[fpath]
                    if old_idx < len(deduped_entries) and deduped_entries[old_idx] is not None:
                        deduped_entries[old_idx] = None  # mark for removal
                        deduped_count += 1
                seen_paths[fpath] = len(deduped_entries)

            deduped_entries.append(entry)

        # Remove None markers
        deduped_entries = [e for e in deduped_entries if e is not None]

        # Step 2: Prune old journal entries
        if is_journal:
            pruned_entries = []
            for entry in deduped_entries:
                stripped = entry.strip()
                if not stripped or not stripped.startswith("-"):
                    pruned_entries.append(entry)
                    continue

                date_match = _JOURNAL_DATE_RE.search(stripped)
                if date_match:
                    entry_date = date_match.group(1)
                    if entry_date < cutoff_date:
                        journal_pruned_count += 1
                        continue  # skip old entry
                pruned_entries.append(entry)
            deduped_entries = pruned_entries

        new_sections.append((header, deduped_entries))

    # Step 3: Rebuild with clean formatting
    # Collapse consecutive blank lines in preamble
    result_lines = []
    for line in preamble:
        if not line.strip() and (not result_lines or not result_lines[-1].strip()):
            continue
        result_lines.append(line)
    # Remove trailing blank lines from preamble
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    for header, entries in new_sections:
        result_lines.append("")  # blank line before section
        result_lines.append(header)

        # Clean up entries: remove leading/trailing blank lines within section,
        # collapse consecutive blank lines to at most one
        clean = []
        for e in entries:
            if not e.strip():
                # Skip leading blanks and consecutive blanks
                if not clean or not clean[-1].strip():
                    continue
            clean.append(e)
        # Remove trailing blanks
        while clean and not clean[-1].strip():
            clean.pop()

        if clean:
            result_lines.append("")  # one blank line after header
            result_lines.extend(clean)

    # Final newline
    result_lines.append("")

    before_count = len(lines)
    after_count = len(result_lines)

    if deduped_count > 0 or journal_pruned_count > 0:
        update_index("\n".join(result_lines))
        print(f"[Memory] Index consolidated: {before_count} → {after_count} lines "
              f"(deduped={deduped_count}, journal_pruned={journal_pruned_count})")

    return {
        "before": before_count,
        "after": after_count,
        "deduped": deduped_count,
        "journal_pruned": journal_pruned_count,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(query: str, max_results: int = 10) -> list[dict]:
    """Simple keyword search across all memory files.

    Returns list of {path, title, snippet, modified} sorted by relevance.
    """
    ensure_dirs()
    query_lower = query.lower()
    keywords = [k.strip() for k in query_lower.split() if k.strip()]
    if not keywords:
        return []

    results = []
    for root, _dirs, files in os.walk(_memory_dir()):
        for fname in files:
            if not fname.endswith(".md") or fname == "index.md":
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, _memory_dir())
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            content_lower = content.lower()
            score = sum(content_lower.count(kw) for kw in keywords)
            if score == 0:
                continue

            title = _extract_title(content, fname)
            snippet = _extract_snippet(content, keywords[0])
            mtime = os.path.getmtime(full)

            results.append({
                "path": rel,
                "title": title,
                "snippet": snippet,
                "modified": datetime.fromtimestamp(mtime).isoformat()[:19],
                "score": score,
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    for r in results:
        del r["score"]
    return results[:max_results]


# ---------------------------------------------------------------------------
# List tree
# ---------------------------------------------------------------------------

def list_tree() -> dict:
    """Return the memory directory tree as a nested dict.

    Format: {dir_name: [file1.md, file2.md, ...], ...}
    """
    ensure_dirs()
    tree = {}
    for d in sorted(os.listdir(_memory_dir())):
        full = os.path.join(_memory_dir(), d)
        if os.path.isdir(full):
            files = sorted(
                f for f in os.listdir(full) if f.endswith(".md")
            )
            tree[d] = files
    return tree


def _tree_cache_signature() -> tuple:
    """Build a signature from directory mtimes + file counts for cache invalidation."""
    ensure_dirs()
    sig = []
    try:
        for d in sorted(os.listdir(_memory_dir())):
            dirpath = os.path.join(_memory_dir(), d)
            if os.path.isdir(dirpath):
                sig.append((d, os.path.getmtime(dirpath)))
    except OSError:
        pass
    return tuple(sig)


def list_tree_rich() -> dict:
    """Return memory tree with per-file metadata (title, preview, modified).

    Format: {dir_name: [{name, path, title, preview, modified, size}, ...]}

    Memory v2 changes: slot domains (projects/people/topics/self) now use
    `{domain}/{slot_id}/main.md` structure. We recursively descend ONE level
    inside slot domain dirs to surface those main.md files. Non-slot dirs
    (commitments/, patterns/, journal/) keep flat one-level listing.

    Uses in-memory cache invalidated by directory mtime changes.
    """
    cache_key = _get_data_dir()
    cached = _tree_cache.get(cache_key)
    sig = _tree_cache_signature()
    if cached and cached[1] == sig:
        return cached[0]

    ensure_dirs()
    SLOT_DIRS = {"projects", "people", "topics", "self"}
    tree = {}
    for d in sorted(os.listdir(_memory_dir())):
        dirpath = os.path.join(_memory_dir(), d)
        if not os.path.isdir(dirpath):
            continue
        if d.startswith("_"):
            # _slots/ etc. — internal, skip from rich tree
            continue
        files = []
        for entry in sorted(os.listdir(dirpath)):
            entry_path = os.path.join(dirpath, entry)
            # Case 1: direct .md file (legacy / non-slot dirs)
            if os.path.isfile(entry_path) and entry.endswith(".md"):
                _append_file_meta(files, entry_path, f"{d}/{entry}", entry)
                continue
            # Case 2: slot subdir — descend to find main.md
            if os.path.isdir(entry_path) and d in SLOT_DIRS:
                main_md = os.path.join(entry_path, "main.md")
                if os.path.isfile(main_md):
                    _append_file_meta(
                        files, main_md, f"{d}/{entry}/main.md",
                        f"{entry}/main.md",
                    )
        # Sort by modified time descending within each dir
        files.sort(key=lambda f: f["modified"], reverse=True)
        tree[d] = files

    _tree_cache[cache_key] = (tree, sig)
    return tree


def _append_file_meta(files: list, fpath: str, rel_path: str, name: str):
    """Helper: read a .md file and append its metadata to `files`."""
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return
    title = _strip_markdown_inline(_extract_title(content, name))
    preview = ""
    for line in content.split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            preview = _strip_markdown_inline(line)[:100]
            break
    mtime = os.path.getmtime(fpath)
    files.append({
        "name": name,
        "path": rel_path,
        "title": title,
        "preview": preview,
        "modified": datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M"),
        "size": os.path.getsize(fpath),
    })


def list_slots_grouped() -> dict:
    """New API for Memory v2 UI: return slot data grouped by domain + status.

    Format:
        {
          "project": {
            "active":   [{slot dict + extras}, ...],
            "paused":   [...],
            "archived": [...]
          },
          "person": {...}, "topic": {...}, "self": {...}
        }

    Each slot dict includes slot metadata + main_file_size + main_file_mtime
    + main_file_preview (first 100 chars from main.md).
    """
    try:
        from memory_router import load_all_slots, DOMAINS, _slot_main_file_rel
    except ImportError:
        return {}

    result = {}
    for domain in DOMAINS:
        slots = load_all_slots(domain)
        grouped = {"active": [], "paused": [], "archived": []}
        for s in slots:
            status = s.get("status", "active")
            if status not in grouped:
                grouped["active"].append(s)
                continue
            # Hydrate with main.md preview/size/mtime
            try:
                rel = s.get("main_file") or _slot_main_file_rel(domain, s["id"])
                full = _safe_path(rel)
                if full and os.path.isfile(full):
                    main_preview = ""
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            content = f.read()
                        for line in content.split("\n"):
                            line = line.strip()
                            if line and not line.startswith("#"):
                                main_preview = _strip_markdown_inline(line)[:120]
                                break
                    except OSError:
                        pass
                    s_with_meta = dict(s)
                    s_with_meta["main_file_preview"] = main_preview
                    s_with_meta["main_file_size"] = os.path.getsize(full)
                    s_with_meta["main_file_mtime"] = datetime.fromtimestamp(
                        os.path.getmtime(full)).strftime("%m-%d %H:%M")
                    grouped[status].append(s_with_meta)
                else:
                    grouped[status].append(dict(s))
            except Exception:
                grouped[status].append(dict(s))
        # Sort within each status: pinned first, then by last_active desc
        for st in grouped:
            grouped[st].sort(
                key=lambda x: (
                    not x.get("pinned", False),
                    -_iso_to_ts_local(x.get("last_active", "")),
                ),
            )
        result[domain] = grouped
    return result


def _iso_to_ts_local(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_path(rel_path: str) -> Optional[str]:
    """Resolve a relative path under _memory_dir(), preventing traversal."""
    if not rel_path:
        return None
    # Block obvious traversal
    if ".." in rel_path or rel_path.startswith("/"):
        return None
    full = os.path.normpath(os.path.join(_memory_dir(), rel_path))
    if not full.startswith(_memory_dir()):
        return None
    return full


def _strip_markdown_inline(text: str) -> str:
    """Strip common inline markdown syntax for plain-text display."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)         # __bold__
    text = re.sub(r"\*(.+?)\*", r"\1", text)         # *italic*
    text = re.sub(r"_(.+?)_", r"\1", text)           # _italic_
    text = re.sub(r"~~(.+?)~~", r"\1", text)         # ~~strike~~
    text = re.sub(r"`(.+?)`", r"\1", text)           # `code`
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)  # [link](url)
    return text


def _extract_title(content: str, fallback: str) -> str:
    """Extract first heading or use filename as title."""
    for line in content.split("\n")[:5]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return os.path.splitext(fallback)[0]


def _extract_snippet(content: str, keyword: str, context_chars: int = 80) -> str:
    """Extract a snippet around the first occurrence of keyword."""
    lower = content.lower()
    idx = lower.find(keyword.lower())
    if idx == -1:
        return content[:context_chars * 2].replace("\n", " ").strip()
    start = max(0, idx - context_chars)
    end = min(len(content), idx + len(keyword) + context_chars)
    snippet = content[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet
