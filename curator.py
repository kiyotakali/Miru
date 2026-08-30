"""Curator (v3 stub) — identity cascade only.

After the slot-based memory migration, the only remaining write path that needs
a curator is the onboarding/settings → slots/self/identity cascade.
Everything else (chat / screenshot) goes through ``memory_router.route_and_write``.

cascade_identity translates the authoritative identity dict from
identity.py into:
    1. a structured body in slots/self/identity/main.md
       (read by renderer.render_slot for ground_truth injection AND UI display)
    2. slot meta (title / icon / summary / pinned)
       in memory/_slots/self.json

There is NO atomic-fact pipeline anymore. Confidence scores, supersede
logic, and fact_resolver/fact_store have been removed because the
slot-writing LLM stack does that semantic work in the prompt rather than in
brittle rule-based Python.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import memory
import memory_router


# ─────────────────────────────────────────────────────────────────────
# Identity body composition (single canonical layout)
# ─────────────────────────────────────────────────────────────────────

# Order matters — drives reading order in prompt + UI display.
# 2026-05-08: "aliases" inserted between style and notes — multi-platform
# usernames the user goes by, joined as "alex, 小明A, ..." in the rendered card.
_IDENTITY_FIELD_ORDER = ("name", "role", "occupation", "schedule", "style", "aliases", "notes")

_IDENTITY_LABELS = {
    "name":       "姓名",
    "role":       "身份",
    "occupation": "职业 / 研究方向",
    "schedule":   "作息",
    "style":      "偏好的说话风格",
    "aliases":    "别名",
    "notes":      "用户备注",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compose_identity_body(authoritative: dict) -> str:
    """Render the identity dict into the canonical body layout.

    Returns a string that becomes the body of slots/self/identity/main.md.
    Empty fields are skipped — we only write what the user has told us.

    List-valued fields (currently only "aliases") are joined with ", "
    so they read naturally on the card: "- 别名: alex, 小明A".
    """
    lines = []
    for key in _IDENTITY_FIELD_ORDER:
        v = authoritative.get(key, "")
        if isinstance(v, list):
            cleaned = [str(x).strip() for x in v if str(x).strip()]
            if not cleaned:
                continue
            v = ", ".join(cleaned)
        elif isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        else:
            continue
        label = _IDENTITY_LABELS.get(key, key)
        lines.append(f"- {label}: {v}")
    return "\n".join(lines)


def _ensure_identity_slot(meta_summary: str = "") -> dict:
    """Make sure slots/self/identity exists with the right meta. Return the slot."""
    existing = memory_router.get_slot("self", "identity")
    now = _now_iso()
    if existing:
        slot = dict(existing)
        slot["last_active"] = now
        if meta_summary:
            slot["summary"] = meta_summary
        # Ensure pinned + active for identity
        slot["pinned"] = True
        slot["status"] = "active"
        memory_router.upsert_slot("self", slot)
        return slot

    slot = {
        "id": "identity",
        "title": "用户身份",
        "icon": "🪪",
        "status": "active",
        "pinned": True,
        "summary": meta_summary or "由 onboarding / 设置页直接维护的权威身份信息",
        "aliases": [],
        "main_file": memory_router._slot_main_file_rel("self", "identity"),
        "last_active": now,
        "created": now,
    }
    memory_router.upsert_slot("self", slot)
    return slot


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def cascade_identity(authoritative: dict) -> dict:
    """Write the authoritative identity dict into slots/self/identity.

    `authoritative` is the canonical-form dict from identity.py:
        {"name": "<user>", "role": "<role>", "occupation": "<focus>", ...}

    We compose a body, write it to main.md atomically, and ensure the
    slot meta is up to date. This is the only path that touches
    self/identity — chat / screenshot routes are blocked by the J5
    safety net in memory_router.

    Returns:
        {"ok": bool, "no_op": bool (if nothing changed), "body": str, ...}
    """
    if not authoritative or not isinstance(authoritative, dict):
        return {"ok": True, "no_op": True}

    body = _compose_identity_body(authoritative)
    if not body:
        return {"ok": True, "no_op": True}

    # 1-line summary for the slot card (truncated)
    summary_bits = []
    for key in ("name", "role", "occupation"):
        v = (authoritative.get(key) or "").strip() if isinstance(authoritative.get(key), str) else ""
        if v:
            summary_bits.append(v)
    meta_summary = " · ".join(summary_bits) if summary_bits else "用户身份"
    if len(meta_summary) > 80:
        meta_summary = meta_summary[:78] + "…"

    slot = _ensure_identity_slot(meta_summary=meta_summary)
    title = slot.get("title", "用户身份")

    main_md = f"# {title}\n\n{body}\n"
    ok = memory.write_file(slot["main_file"], main_md)
    if not ok:
        return {"ok": False, "error": "main.md write failed"}

    # Bump last_active + persist updated summary
    slot["last_active"] = _now_iso()
    slot["summary"] = meta_summary
    memory_router.upsert_slot("self", slot)
    try:
        memory_router.regenerate_index_md()
    except Exception:
        pass

    return {
        "ok": True,
        "domain": "self",
        "slot_id": "identity",
        "title": title,
        "summary": meta_summary,
        "body": body,
    }


# ═════════════════════════════════════════════════════════════════════
# Auto-Curator (2026-05-08) — 三层 LLM 后台清理 slot 内存
# ═════════════════════════════════════════════════════════════════════
#
# 触发条件 (两条同时满足才跑):
#   1. 距上次跑 ≥ 600 秒 (10 min 冷却)
#   2. 自上次跑后, 该 domain 累计 ≥3 次 slot 写入变化
#      (route_and_write 创建/匹配 / UI 编辑 / merge / archive / pin / 用户改 main.md)
#
# 两层流水线 (per domain, v3.5 2026-05-14):
#   Layer 1 Planner  : sees all slot summaries in the domain, emits ≤3 ops
#                      + reason. Does NOT produce new metadata — it only
#                      sees摘要级 info, so title/summary/icon/aliases are
#                      decided by the Executor after reading full body.
#   Layer 2 Executor : each op = 1 LLM call that BOTH (a) re-checks
#                      Planner's diagnosis against the full main.md, and
#                      (b) produces the result. should_skip=true means
#                      复核未过, skip this op without modifying any slot.
#     merge  → call_curator_execute_merge   (new_slot_meta + merged_body)
#     edit   → call_curator_execute_edit    (new_slot_meta + optional new_body)
#     delete → call_curator_execute_delete  (decision only; physical move
#                                            to archived/ happens in caller)
#
# No separate Validator stage. Merge Metadata + attach_as_subtopic ops
# were removed — Planner outputs only 3 op types, and Merge Executor
# produces metadata directly. See memory_prompts_v2 docstring.
#
# 状态文件 memory/_slots/curator_meta.json:
#   {
#     "last_run_ts":   {"project": iso, "person": iso, ...},
#     "change_counter": {"project": 5, "person": 0, "topic": 12, "self": 1}
#   }

import json as _json
import os as _os
import threading as _threading
import time as _time
from typing import Any as _Any

from memory_prompts_v2 import (
    call_curator_planner,
    call_curator_execute_merge,
    call_curator_execute_edit,
    call_curator_execute_delete,
)


def _load_identity() -> tuple[str, str]:
    """Pull per-user identity for prompt injection.

    Returns (user_name, ground_truth_block). On any error returns ("", "")
    so the prompts fall back to "(尚未声明)" — never propagates exceptions
    into the curator loop. See: people/qinglong incident 2026-05-17.
    """
    try:
        import identity as _id
        return _id.get_user_name() or "", _id.compose_ground_truth_block(header=False) or ""
    except Exception:
        return "", ""


CURATOR_COOLDOWN_SECONDS = 3600         # Normal trigger: max once per hour
CURATOR_CHANGE_THRESHOLD = 3            # Normal trigger: 3+ slot changes
CURATOR_SOFT_TRIGGER_SECONDS = 3600     # Soft trigger (G-safety net 2026-05-12):
                                        # If a domain had ANY change in the last
                                        # hour (counter ≥1) and the cooldown
                                        # passed, run anyway. Catches users who
                                        # only get 1-2 slot changes per hour.
CURATOR_IDLE_RESET_SECONDS = 3600       # If a domain has 0 changes for 1h,
                                        # don't run LLM, but bump last_run_ts so
                                        # the soft trigger logic stays clean.
CURATOR_LOOP_TICK_SECONDS = 60
CURATOR_DOMAINS = ("project", "person", "topic", "self")

_curator_meta_lock = _threading.Lock()
_curator_loop_lock = _threading.Lock()
# Per-user daemon thread + matching stop Event. Both keyed by user_id.
# Mirroring care_engine._instances + inst._stop_event so suspend_user /
# 7-day evict / delete_user can actually terminate the loop instead of
# just orphaning it.
_curator_user_threads: dict = {}
_curator_user_stop_events: dict = {}


def _curator_meta_path() -> str:
    return _os.path.join(memory._memory_dir(), "_slots", "curator_meta.json")


def _curator_empty_meta() -> dict:
    return {
        "last_run_ts":    {d: "" for d in CURATOR_DOMAINS},
        "change_counter": {d: 0  for d in CURATOR_DOMAINS},
    }


def _curator_load_meta() -> dict:
    path = _curator_meta_path()
    if not _os.path.exists(path):
        return _curator_empty_meta()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f) or {}
        m = _curator_empty_meta()
        if isinstance(data.get("last_run_ts"), dict):
            for d in CURATOR_DOMAINS:
                v = data["last_run_ts"].get(d)
                if isinstance(v, str):
                    m["last_run_ts"][d] = v
        if isinstance(data.get("change_counter"), dict):
            for d in CURATOR_DOMAINS:
                v = data["change_counter"].get(d)
                if isinstance(v, int):
                    m["change_counter"][d] = v
        return m
    except (_json.JSONDecodeError, OSError):
        return _curator_empty_meta()


def _curator_save_meta(meta: dict) -> bool:
    path = _curator_meta_path()
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"[curator] save meta failed: {e}")
        return False


def track_slot_change(domain: str) -> None:
    """Hook: any slot write should call this so Curator knows the domain changed.

    Best-effort: failure to record is non-fatal (counter just resets next cron).
    """
    if domain not in CURATOR_DOMAINS:
        return
    try:
        with _curator_meta_lock:
            m = _curator_load_meta()
            m["change_counter"][domain] = m["change_counter"].get(domain, 0) + 1
            _curator_save_meta(m)
    except Exception as e:
        print(f"[curator] track_slot_change failed: {e}")


def _decide_action(domain: str, meta: dict) -> str:
    """Three-way trigger decision for one domain.

    Returns one of:
      "run"        — invoke LLM Planner now
      "idle_reset" — domain has 0 changes in last hour; don't run LLM, but
                     bump last_run_ts so the soft-trigger window resets
      "skip"       — do nothing this tick

    Trigger rules (2026-05-12 user-requested safety net):
      • Normal: counter ≥ 3 AND age ≥ 10min → run
      • Soft:   counter ≥ 1 AND age ≥ 60min → run (catch slow-moving domains)
      • Idle reset: counter = 0 AND age ≥ 60min → idle_reset (no LLM, just
        update last_run_ts so the soft trigger doesn't accumulate forever)
      • Otherwise → skip
    """
    counter = meta["change_counter"].get(domain, 0)
    last_ts_str = meta["last_run_ts"].get(domain, "")
    if not last_ts_str:
        age = float("inf")  # never run → infinitely old, immediately eligible
    else:
        try:
            last_dt = datetime.fromisoformat(last_ts_str)
            age = (datetime.now() - last_dt).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")

    # 1. Normal trigger
    if counter >= CURATOR_CHANGE_THRESHOLD and age >= CURATOR_COOLDOWN_SECONDS:
        return "run"
    # 2. Soft trigger — at least one change accumulated over an hour
    if counter >= 1 and age >= CURATOR_SOFT_TRIGGER_SECONDS:
        return "run"
    # 3. Idle reset — totally quiet domain. Don't run LLM, but mark so the
    #    soft trigger logic isn't permanently armed by ancient timestamps.
    if counter == 0 and age >= CURATOR_IDLE_RESET_SECONDS:
        return "idle_reset"
    return "skip"


# Legacy alias preserved for any external callers / tests still on old name.
def _should_run(domain: str, meta: dict) -> bool:
    return _decide_action(domain, meta) == "run"


def _mark_run(domain: str) -> None:
    """Called after a Planner LLM invocation. Resets counter + timestamps."""
    with _curator_meta_lock:
        m = _curator_load_meta()
        m["last_run_ts"][domain] = _now_iso()
        m["change_counter"][domain] = 0
        _curator_save_meta(m)


def _mark_idle_reset(domain: str) -> None:
    """Called when a domain was idle for an hour — bump timestamp without
    touching counter (it's already 0). Prevents the soft trigger from firing
    immediately on the next minimal change."""
    with _curator_meta_lock:
        m = _curator_load_meta()
        m["last_run_ts"][domain] = _now_iso()
        # counter already 0 in this branch, but be defensive
        m["change_counter"][domain] = 0
        _curator_save_meta(m)


# ─────────────────────────────────────────────────────────────────────
# Layer 2: Executors (v3.5) — self-check + execute in one LLM call
# ─────────────────────────────────────────────────────────────────────

def _execute_merge(domain: str, op) -> bool:
    """v3.5: combine source slots into target. Executor LLM does self-check
    against full main.md and produces new metadata + merged body, or
    returns should_skip=true."""
    tgt_id = op.target_slot_id
    src_ids = list(op.source_slot_ids or [])
    if not tgt_id or not src_ids:
        print(f"[curator] merge skip: missing target/sources tgt={tgt_id} src={src_ids}")
        return False

    tgt = memory_router.get_slot(domain, tgt_id)
    if tgt is None:
        print(f"[curator] merge skip: missing target {tgt_id}")
        return False
    if tgt.get("pinned"):
        print(f"[curator] merge skip: target pinned")
        return False
    if domain == "self" and tgt_id == "identity":
        print(f"[curator] merge skip: self/identity reserved")
        return False

    sources = []
    for sid in src_ids:
        s = memory_router.get_slot(domain, sid)
        if s is None:
            print(f"[curator] merge skip: missing source {sid}")
            return False
        if s.get("pinned"):
            print(f"[curator] merge skip: source {sid} pinned")
            return False
        if domain == "self" and sid == "identity":
            print(f"[curator] merge skip: self/identity reserved")
            return False
        s_md = memory.read_file(s.get("main_file") or
                                memory_router._slot_main_file_rel(domain, sid)) or ""
        sources.append({"slot": s, "main_md": s_md})

    tgt_md = memory.read_file(tgt.get("main_file") or
                              memory_router._slot_main_file_rel(domain, tgt_id)) or ""

    out = call_curator_execute_merge(
        target_slot=tgt,
        target_main_md=tgt_md,
        source_slots_with_body=sources,
        reason_from_planner=op.reason,
    )
    if out is None:
        print(f"[curator] merge executor retry exhausted")
        return False
    if out.should_skip:
        print(f"[curator] merge SKIPPED: {out.skip_reason}")
        return False
    if out.new_slot_meta is None or not out.merged_body:
        print(f"[curator] merge executor returned incomplete result; skip")
        return False

    meta = out.new_slot_meta
    # Code-layer guard — Planner contracts target_slot_id; Executor should
    # echo it. If the Executor produced a different id, fall back to
    # target_slot_id (Executor prompt is explicit about this).
    new_id = tgt_id

    merged_aliases = list(meta.aliases or [])

    new_slot = {
        "id":           new_id,
        "title":        meta.title,
        "icon":         meta.icon,
        "status":       "active",
        "pinned":       False,
        "summary":      meta.summary,
        "aliases":      merged_aliases,
        "main_file":    memory_router._slot_main_file_rel(domain, new_id),
        "last_active":  _now_iso(),
        "created":      min(
            tgt.get("created", _now_iso()),
            *[s["slot"].get("created", _now_iso()) for s in sources]
        ),
    }

    new_main_md = f"# {meta.title}\n\n{out.merged_body.strip()}\n"
    if not memory.write_file(new_slot["main_file"], new_main_md):
        print(f"[curator] merge write failed")
        return False

    memory_router.upsert_slot(domain, new_slot)
    for sid in src_ids:
        if sid != new_id:
            memory_router.delete_slot(
                domain,
                sid,
                delete_file=True,
                ledger_terminal_reason="merged_into",
                ledger_terminal_target_slot_id=new_id,
            )

    try:
        memory_router.regenerate_index_md()
    except Exception:
        pass

    print(f"[curator] merge {src_ids} → {new_id} OK (self_check: {out.self_check})")
    return True


def _execute_delete(domain: str, op) -> bool:
    """v3.5: Executor LLM self-checks against full main.md to decide
    whether the delete is justified (classification fix / worthless), then
    physically moves the slot to archived/."""
    slot_id = op.slot_id
    slot = memory_router.get_slot(domain, slot_id)
    if slot is None:
        return False
    if slot.get("pinned"):
        return False
    if domain == "self" and slot_id == "identity":
        return False

    main_md = memory.read_file(slot.get("main_file") or
                               memory_router._slot_main_file_rel(domain, slot_id)) or ""

    _u_name, _id_block = _load_identity()
    out = call_curator_execute_delete(
        domain=domain, slot=slot, main_md=main_md,
        reason_from_planner=op.reason,
        user_name=_u_name, identity_ground_truth=_id_block,
    )
    if out is None:
        print(f"[curator] delete executor retry exhausted")
        return False
    if out.should_skip:
        print(f"[curator] delete SKIPPED: {out.skip_reason}")
        return False

    if memory_router.delete_slot(domain, slot_id, delete_file=True):
        try:
            memory_router.regenerate_index_md()
        except Exception:
            pass
        print(f"[curator] delete {slot_id} OK (self_check: {out.self_check})")
        return True
    return False


def _execute_edit(domain: str, op) -> bool:
    """v3.5: Executor LLM self-checks against full main.md, then produces
    a full new_slot_meta (5 fields, unchanged ones回填) and optional
    new_body."""
    slot_id = op.slot_id
    slot = memory_router.get_slot(domain, slot_id)
    if slot is None:
        return False
    if slot.get("pinned"):
        return False
    if domain == "self" and slot_id == "identity":
        return False

    main_md = memory.read_file(slot.get("main_file") or
                               memory_router._slot_main_file_rel(domain, slot_id)) or ""

    out = call_curator_execute_edit(
        slot=slot, main_md=main_md, reason_from_planner=op.reason,
    )
    if out is None:
        print(f"[curator] edit executor retry exhausted")
        return False
    if out.should_skip:
        print(f"[curator] edit SKIPPED: {out.skip_reason}")
        return False
    if out.new_slot_meta is None:
        print(f"[curator] edit executor returned no meta; skip")
        return False

    meta = out.new_slot_meta
    # Code-layer guard: Executor should NOT change the slot id. If it did,
    # ignore the new id and keep slot_id (Executor prompt is explicit).
    if meta.id != slot_id:
        print(f"[curator] edit: executor changed id {meta.id} (expected {slot_id}); ignoring id change")

    # Body: empty new_body means body untouched; non-empty means replace.
    if out.new_body and out.new_body.strip():
        new_body = out.new_body.strip()
    else:
        new_body = memory_router._strip_title_header(main_md, slot.get("title", "")).strip()
        if not new_body:
            new_body = "(空)"

    new_main_md = f"# {meta.title}\n\n{new_body}\n"
    if not memory.write_file(
        slot.get("main_file") or memory_router._slot_main_file_rel(domain, slot_id),
        new_main_md,
    ):
        return False

    memory_router.update_slot_metadata(domain, slot_id, {
        "title":   meta.title,
        "icon":    meta.icon,
        "summary": meta.summary,
        "aliases": list(meta.aliases or []),
    })

    print(f"[curator] edit {slot_id} OK (self_check: {out.self_check})")
    return True


# ─────────────────────────────────────────────────────────────────────
# Main cycle
# ─────────────────────────────────────────────────────────────────────

def _planner_candidates(domain: str, all_slots: list[dict]) -> list[dict]:
    """Filter slots before sending to the planner.

    Excluded:
      - status=archived
      - pinned (user said "don't touch")
      - self/identity (cascade-only)
      - last_active < 1h ago (user might still be writing) — UNLESS this slot
        collides with another (same normalized title OR overlapping alias).
        Without the collision override, duplicate slots that the user is
        actively chatting with never enter candidates and the duplicate
        never gets merged. See `weijiazhe`/`weijiazhe_2` (2026-05-11): both
        had last_active < 1h every time Curator ran, so neither was ever
        visible to the Planner.

    Returns trimmed dicts (only the fields the planner needs).
    """
    now_ts = datetime.now().timestamp()

    # Build a "collides with another slot" set first — slots whose normalized
    # title matches another slot's title, or whose aliases overlap. These
    # *bypass* the 1h cooldown because cooling them down hides the duplicate.
    def _norm(s: str) -> str:
        return "".join((s or "").lower().split())  # whitespace + case insensitive

    # G1 (2026-05-12): "title prefix family" detection.
    # If ≥2 slots share a long normalized prefix (3+ alphanumeric chars, or
    # 3+ CJK chars), they're likely sub-slots of the same project family —
    # e.g. "AgiBot 动作冗余分析" / "AgiBotWorld 数据管线" / "Agibot 快速 Scale".
    # These should bypass the 10-min cooldown so the Planner sees them all
    # together and can decide whether to merge them.
    #
    # Stop words ("项目", "学习", "笔记" ...) shouldn't count as a meaningful
    # prefix (would collide with everything). Strip them before comparison.
    _STOP_PREFIXES = (
        "项目", "笔记", "学习", "记录", "the ", "my ", "我的",
    )

    def _norm_for_prefix(s: str) -> str:
        v = _norm(s)
        for sw in _STOP_PREFIXES:
            if v.startswith(sw):
                v = v[len(sw):]
        return v

    def _title_prefix_key(title: str) -> str:
        """Return the 3-char prefix used for family detection, or '' if too short."""
        v = _norm_for_prefix(title)
        # Take first 3 chars; CJK and Latin both treated as 1 char each in
        # Python str. Empty / too short titles can't anchor a family.
        return v[:3] if len(v) >= 3 else ""

    collision_ids: set[str] = set()

    # 1. Title prefix family — group slots by 3-char prefix, families ≥2 collide
    prefix_groups: dict = {}
    for s in all_slots:
        if s.get("status") == "archived" or s.get("pinned"):
            continue
        key = _title_prefix_key(s.get("title", ""))
        if not key:
            continue
        prefix_groups.setdefault(key, []).append(s.get("id") or "")
    for key, ids in prefix_groups.items():
        if len(ids) >= 2:
            collision_ids.update(ids)

    # 2. Exact title match + alias overlap (existing logic)
    for i, a in enumerate(all_slots):
        if a.get("status") == "archived" or a.get("pinned"):
            continue
        a_id = a.get("id") or ""
        a_title_norm = _norm(a.get("title", ""))
        a_aliases = {_norm(x) for x in (a.get("aliases") or []) if x}
        for j, b in enumerate(all_slots):
            if i >= j:
                continue
            if b.get("status") == "archived" or b.get("pinned"):
                continue
            b_id = b.get("id") or ""
            # Same normalized title is a near-certain duplicate signal
            if a_title_norm and a_title_norm == _norm(b.get("title", "")):
                collision_ids.add(a_id)
                collision_ids.add(b_id)
                continue
            # Alias overlap (≥ 1 shared non-empty alias) is also strong
            b_aliases = {_norm(x) for x in (b.get("aliases") or []) if x}
            if a_aliases and b_aliases and (a_aliases & b_aliases):
                collision_ids.add(a_id)
                collision_ids.add(b_id)

    out = []
    for s in all_slots:
        if s.get("status") == "archived":
            continue
        if s.get("pinned"):
            continue
        if domain == "self" and s.get("id") == "identity":
            continue
        # Slot-level cooldown (10 min — was 1h, but that locked out freshly-
        # created mis-routed slots from Curator for an hour, defeating the
        # whole point of background cleanup. 10 min still protects slots
        # the user is actively editing). UNLESS this slot collides with
        # another (collision overrides cooldown — duplicate is urgent).
        if s.get("id") not in collision_ids:
            try:
                la_ts = datetime.fromisoformat(s.get("last_active", "")).timestamp()
                if (now_ts - la_ts) < 600:
                    continue
            except (ValueError, TypeError):
                pass
        out.append({
            "id":           s.get("id"),
            "title":        s.get("title"),
            "summary":      s.get("summary"),
            "aliases":      s.get("aliases", []),
            "status":       s.get("status", "active"),
            "pinned":       s.get("pinned", False),
            "last_active":  s.get("last_active", ""),
        })
    return out


def run_domain(domain: str) -> dict:
    """One Curator pass on a single domain."""
    if domain not in CURATOR_DOMAINS:
        return {"error": f"invalid domain {domain}"}

    all_slots = memory_router.load_all_slots(domain)
    if not all_slots:
        return {"domain": domain, "skipped": "no slots"}

    candidates = _planner_candidates(domain, all_slots)
    if len(candidates) < 2:
        return {"domain": domain, "actions": 0, "executed": 0,
                "planner_reasoning": f"候选 slot 不足 ({len(candidates)} 个), 跳过"}

    _u_name, _id_block = _load_identity()
    plan = call_curator_planner(
        domain=domain, slots=candidates,
        user_name=_u_name, identity_ground_truth=_id_block,
    )
    if plan is None:
        return {"domain": domain, "error": "planner retry exhausted"}

    if not plan.ops:
        return {"domain": domain, "ops": 0, "executed": 0,
                "planner_reasoning": plan.reasoning}

    print(f"[curator] domain={domain} planner: {plan.reasoning}")
    print(f"[curator] domain={domain} ops: "
          f"{[op.op + ':' + (op.slot_id or op.target_slot_id or '') for op in plan.ops]}")

    executed = rejected = failed = 0
    for op in plan.ops:
        try:
            if op.op == "merge":
                ok = _execute_merge(domain, op)
            elif op.op == "delete":
                ok = _execute_delete(domain, op)
            elif op.op == "edit":
                ok = _execute_edit(domain, op)
            else:
                ok = False
        except Exception as e:
            print(f"[curator] op {op.op} exception: {e}")
            failed += 1
            continue
        if ok:
            executed += 1
        else:
            rejected += 1

    return {
        "domain":             domain,
        "ops":                len(plan.ops),
        "executed":           executed,
        "rejected":           rejected,
        "failed":             failed,
        "planner_reasoning":  plan.reasoning,
    }


def run_cycle() -> dict:
    """Per-domain: dispatch on _decide_action (run / idle_reset / skip).

    Caller must have a Flask app/request context pushed (g.user_data_dir).
    """
    meta = _curator_load_meta()
    results = {}
    for domain in CURATOR_DOMAINS:
        action = _decide_action(domain, meta)
        if action == "skip":
            continue
        if action == "idle_reset":
            _mark_idle_reset(domain)
            results[domain] = {"action": "idle_reset", "note": "no slot changes for 1h"}
            continue
        # action == "run"
        try:
            r = run_domain(domain)
            results[domain] = r
            _mark_run(domain)
        except Exception as e:
            print(f"[curator] domain {domain} exception: {e}")
            results[domain] = {"error": str(e)}
    return results


# ─────────────────────────────────────────────────────────────────────
# Background loop (per-user)
# ─────────────────────────────────────────────────────────────────────

def _curator_loop(user_id: str, user_data_dir: str, stop_event: _threading.Event):
    """Long-running daemon: every 60s, push Flask ctx + run_cycle().

    Exits promptly when ``stop_event`` is set (admin suspend / user delete /
    7-day evict). The Event-based wait is interruptible — unlike a plain
    sleep(60) which would orphan the thread for up to a minute.
    """
    while not stop_event.is_set():
        # Interruptible wait. Returns True if event was set during the wait,
        # in which case we exit immediately without running another tick.
        if stop_event.wait(timeout=CURATOR_LOOP_TICK_SECONDS):
            break
        try:
            import app as _app
            ctx = _app.app.app_context()
            ctx.push()
            try:
                from flask import g as _g
                _g.user_id = user_id
                _g.user_data_dir = user_data_dir
                _g.is_admin = (user_id == "_admin")
                run_cycle()
            finally:
                try:
                    ctx.pop()
                except Exception:
                    pass
        except Exception as e:
            print(f"[curator_loop user={user_id}] tick exception: {e}")
    print(f"[curator] background loop stopped for user={user_id}")


def start_loop_for_user(user_id: str, user_data_dir: str) -> None:
    """Idempotent: start one daemon thread per user.

    A second call for the same user is a no-op as long as the thread is
    still alive. If the user was previously stopped (event set + thread
    exited), we transparently spawn a fresh thread — same shape as
    CareEngine.start() being callable after stop().
    """
    with _curator_loop_lock:
        existing = _curator_user_threads.get(user_id)
        if existing and existing.is_alive():
            return
        # Drop a stale stop event from the previous lifecycle, if any
        _curator_user_stop_events.pop(user_id, None)
        stop_event = _threading.Event()
        _curator_user_stop_events[user_id] = stop_event
        t = _threading.Thread(
            target=_curator_loop,
            args=(user_id, user_data_dir, stop_event),
            daemon=True,
            name=f"curator_loop_{user_id}",
        )
        t.start()
        _curator_user_threads[user_id] = t
        print(f"[curator] background loop started for user={user_id}")


def stop_loop_for_user(user_id: str) -> None:
    """Set the stop event AND drop the thread reference.

    The daemon will see the event on its next ``wait()`` (within
    ``CURATOR_LOOP_TICK_SECONDS`` worst case, immediate if currently
    sleeping) and break out of the loop. Idempotent — calling on an
    unknown user is a no-op.
    """
    with _curator_loop_lock:
        ev = _curator_user_stop_events.pop(user_id, None)
        thread = _curator_user_threads.pop(user_id, None)
    if ev is not None:
        ev.set()
    if thread is not None and thread.is_alive():
        # Best-effort join with short timeout. Daemon will be reaped at
        # process exit anyway, but waiting briefly here means callers
        # like auth.suspend_user can know the thread is really gone
        # before they return.
        thread.join(timeout=2.0)
