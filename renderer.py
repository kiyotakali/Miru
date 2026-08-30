"""Slot Renderer — single source of truth for all slot views (v3).

Every consumer (UI cards, chat-context overview, identity ground-truth
injection) goes through this module.

The slot data model (v3, append-first) is:
    · slot.json (registry):  id / title / icon / status / pinned / summary
                              / aliases / last_active / created
    · main.md (body):        created by Slot Writer; daily match writes append
                              timestamped records; maintenance jobs compact
                              them back into a coherent narrative.

There is no fact list anymore. Confidence/source-tagged facts have been
replaced by a free-text body plus append records that later compaction can
fold into prose.

Modes:
    "card"          → structured dict for UI slot cards
                      (title, summary, body, aliases, status, ...)
    "markdown"      → human-readable Markdown (the main.md content)
    "overview"      → 1-2 line summary for chat agent context
    "ground_truth"  → ground-truth block for prompt injection,
                      sourced from slots/self/identity body or
                      identity.json fallback

The dual-target principle still holds:

    User reads on screen ≡ Miru reads from prompt

Both go through render_slot(); they cannot diverge.
"""

from __future__ import annotations

from typing import Optional


def _slot_meta(domain: str, slot_id: str) -> Optional[dict]:
    """Load slot meta from the registry (memory_router._slots/<domain>.json).

    Returns None if not found. Avoids a circular import at module load by
    importing memory_router lazily.
    """
    try:
        import memory_router
        slots = memory_router.load_all_slots(domain)
    except Exception:
        return None
    for s in slots:
        if s.get("id") == slot_id:
            return s
    return None


def _read_body(domain: str, slot_id: str, meta: Optional[dict]) -> str:
    """Read the slot body (main.md content sans the leading '# title' header).

    Returns empty string if the file does not exist yet.
    """
    try:
        import memory
        import memory_router
        rel = (meta.get("main_file") if meta else None) \
            or memory_router._slot_main_file_rel(domain, slot_id)
        raw = memory.read_file(rel) or ""
        return memory_router._strip_title_header(raw, (meta or {}).get("title", ""))
    except Exception:
        return ""


def render_slot(domain: str, slot_id: str, *,
                mode: str = "card",
                title_override: Optional[str] = None) -> dict | str:
    """Render a slot in one of four modes.

    Args:
        domain:       project / person / topic / self
        slot_id:      slot id within that domain
        mode:         "card" | "markdown" | "overview" | "ground_truth"
        title_override: optional title; falls back to slot meta then slot_id

    Returns:
        - mode=card: dict (see _render_card)
        - mode=markdown / overview / ground_truth: str
    """
    meta = _slot_meta(domain, slot_id)
    title = title_override or (meta.get("title") if meta else "") or slot_id
    icon = (meta.get("icon") if meta else "") or ""
    summary = (meta.get("summary") if meta else "") or ""
    pinned = bool(meta.get("pinned")) if meta else False

    body = _read_body(domain, slot_id, meta)

    if mode == "card":
        return _render_card(domain, slot_id, title, icon, summary,
                            pinned, body, meta)
    if mode == "markdown":
        return _render_markdown(title, summary, body)
    if mode == "overview":
        return _render_overview(title, summary, body)
    if mode == "ground_truth":
        return _render_ground_truth(domain, slot_id, body)
    raise ValueError(f"unknown render mode: {mode}")


# ─────────────────────────────────────────────────────────────────────
# Mode implementations
# ─────────────────────────────────────────────────────────────────────

def _render_card(domain: str, slot_id: str, title: str, icon: str,
                 summary: str, pinned: bool, body: str,
                 meta: Optional[dict]) -> dict:
    """Structured data for UI cards. UI is free to style it any way."""
    return {
        "domain": domain,
        "slot_id": slot_id,
        "title": title,
        "icon": icon,
        "summary": summary,
        "body": body,
        "body_len": len(body),
        "pinned": pinned,
        "status": (meta.get("status") if meta else "active"),
        "aliases": list((meta.get("aliases") if meta else []) or []),
        "last_active": (meta.get("last_active") if meta else "") or "",
        "created": (meta.get("created") if meta else "") or "",
    }


def _render_markdown(title: str, summary: str, body: str) -> str:
    """Full markdown — used for archival_memory_search results / file reads.

    Format::

        # {title}

        > {summary}

        {body}
    """
    lines = [f"# {title}"]
    if summary:
        lines.append("")
        lines.append(f"> {summary}")
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines) + "\n"


def _render_overview(title: str, summary: str, body: str) -> str:
    """1-2 lines for chat-context overview block. Optimized for token budget.

    Falls through summary → first sentence of body → '(暂无信息)'.
    """
    if summary:
        head = summary
    elif body:
        # First non-empty line/sentence of body, truncated
        first_chunk = body.strip().split("\n", 1)[0].strip()
        head = first_chunk[:80] + ("…" if len(first_chunk) > 80 else "")
    else:
        head = "(暂无信息)"
    return f"- **{title}**: {head}"


def _render_ground_truth(domain: str, slot_id: str, body: str) -> str:
    """Ground-truth block for LLM prompt injection.

    Special case: if rendering self/identity, the body IS the
    ground-truth block (it was written by cascade_identity from the
    user's onboarding/settings answers). For any other slot, ground
    truth makes no sense — return empty string.

    Falls back to identity.compose_ground_truth_block if the slot's
    body is empty (e.g. brand-new account that hasn't run cascade yet).
    """
    if domain != "self" or slot_id != "identity":
        return ""

    if body.strip():
        # Wrap with the standard header / warning tag for prompt clarity
        header = "# 用户已声明的事实（不可质疑，必须遵循）"
        warning = (
            "⚠️ 任何与上述事实冲突的内容，必须按上述事实写。"
            "VLM 对 OCR / 手写 / 模糊文字的识别可能出错，"
            "若疑似涉及姓名等已声明字段，直接采用上述声明值。"
        )
        return "\n".join([header, "", body.strip(), "", warning])

    # Fallback: read from identity.json directly (pre-cascade users)
    try:
        import identity
        return identity.compose_ground_truth_block(header=True)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Convenience: render every slot in a domain (for index page / search)
# ─────────────────────────────────────────────────────────────────────

def render_domain_overview(domain: str) -> list[str]:
    """Return [overview_line] for every active slot in a domain.

    Used to build the chat-context block listing what slots exist.
    """
    try:
        import memory_router
        slots = memory_router.load_active_slots(domain)
    except Exception:
        return []
    out = []
    for s in slots:
        if s.get("status") == "archived":
            continue
        line = render_slot(domain, s["id"], mode="overview",
                           title_override=s.get("title"))
        if isinstance(line, str) and line:
            out.append(line)
    return out
