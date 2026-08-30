"""Daily Journal module — Katou Megumi style narrative journal.

Generates one structured JSON journal per day, capturing the day from Miru's
observer perspective, plus a markdown twin so archival memory search and the
main agent can read the diary through the normal memory tool path.

Data layout (per user, under `memory/journal/`):
    YYYY-MM-DD.json  — structured journal, primary API/UI source
    YYYY-MM-DD.md    — rendered diary twin for archival memory search

JSON schema:
    {
      "date": "YYYY-MM-DD",
      "title": "1-line mood-indicating title",
      "mood": {"label": "平静", "emoji": "🌙", "valence": 0.0-1.0 or neg},
      "narrative": "3-4 paragraphs of Miru's prose (200-400字)",
      "highlights": [{"time": "HH:MM", "text": "..."}],
      "stats": {
        "first_active": "HH:MM" | null,
        "last_active": "HH:MM" | null,
        "chat_count": int,
        "observation_count": int,
        "commitments_completed": int,
        "commitments_added": int,
      },
      "generated_at": "ISO timestamp",
      "source_version": 2,
    }
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import memory
import storage
import user_settings


JOURNAL_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _journal_dir() -> str:
    d = os.path.join(storage.get_data_dir(), "memory", "journal")
    os.makedirs(d, exist_ok=True)
    return d


def _json_path(date_str: str) -> str:
    return os.path.join(_journal_dir(), f"{date_str}.json")


def _md_path(date_str: str) -> str:
    return os.path.join(_journal_dir(), f"{date_str}.md")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_journal(date_str: str) -> dict | None:
    """Return journal JSON for date, or None if missing/invalid."""
    p = _json_path(date_str)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "narrative" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def list_journals(days_back: int = 120) -> list[dict]:
    """List journals with lightweight metadata, newest first.

    Only JSON-format journals (v2). Legacy .md fallback was removed —
    accounts that pre-date v2 are extremely rare and the .md path was
    never auto-regenerated anyway.
    """
    out: list[dict] = []
    d = _journal_dir()
    if not os.path.isdir(d):
        return out
    now = user_settings.user_now()
    cutoff = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")

    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]
        if date_str < cutoff:
            continue
        j = get_journal(date_str)
        if not j:
            continue
        out.append({
            "date": date_str,
            "title": j.get("title", ""),
            "mood": j.get("mood") or {},
            "has_json": True,
        })

    out.sort(key=lambda e: e["date"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

def _stamp(value) -> str:
    return str(value or "").replace("T", " ").strip()


def _date_part(value) -> str:
    return _stamp(value)[:10]


def _hhmm(value) -> str:
    s = _stamp(value)
    if len(s) >= 16:
        return s[11:16]
    return s[:5]


def _first_stamp(entry: dict, *keys: str) -> str:
    for key in keys:
        value = _stamp(entry.get(key))
        if value:
            return value
    return ""


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_valence(value) -> str:
    return f"{_safe_float(value):+.2f}"


def _format_duration(seconds) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return ""
    minutes = total // 60
    if minutes <= 0:
        return f"{total}秒"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}小时{mins}分钟"
    if hours:
        return f"{hours}小时"
    return f"{mins}分钟"


def _segment_matches_date(entry: dict, date_str: str) -> bool:
    for key in ("started_at", "timestamp", "updated_at", "ended_at", "ts"):
        if _date_part(entry.get(key)) == date_str:
            return True
    return False


def _is_attention_segment(entry: dict, channel: str | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    if channel and entry.get("channel") != channel:
        return False
    if entry.get("id") or entry.get("segment_id"):
        return True
    return bool(entry.get("started_at") and entry.get("updated_at"))


def _segment_context(entry: dict) -> dict:
    started = _first_stamp(entry, "started_at", "timestamp", "ts", "updated_at")
    updated = _first_stamp(entry, "updated_at", "ended_at", "timestamp", "ts")
    ended = _stamp(entry.get("ended_at"))
    text = (
        entry.get("text")
        or entry.get("reason")
        or entry.get("source")
        or entry.get("trigger")
        or entry.get("last_seen_summary")
        or ""
    )
    try:
        duration_seconds = int(entry.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0
    try:
        tick_count = int(entry.get("tick_count") or 1)
    except (TypeError, ValueError):
        tick_count = 1
    return {
        "id": entry.get("id") or entry.get("segment_id") or "",
        "channel": entry.get("channel") or "",
        "start": _hhmm(started),
        "end": _hhmm(ended or updated) if (ended or updated) else "",
        "started_at": started,
        "updated_at": updated,
        "ended_at": ended,
        "duration_seconds": duration_seconds,
        "duration": _format_duration(duration_seconds),
        "tick_count": tick_count,
        "mood": entry.get("mood") or entry.get("state") or "",
        "topic_key": entry.get("topic_key") or "",
        "valence": _safe_float(entry.get("valence"), 0.0),
        "arousal": _safe_float(entry.get("arousal", entry.get("intensity")), 0.0),
        "trend": entry.get("trend") or "",
        "confidence": _safe_float(entry.get("confidence"), 0.0),
        "text": str(text or "")[:320],
        "change_reason": str(entry.get("change_reason") or "")[:180],
    }


def _segment_line(seg: dict, *, show_trend: bool = False) -> str:
    start = seg.get("start") or "??:??"
    end = seg.get("end") or start
    period = f"{start}-{end}" if end and end != start else start
    duration = f"，持续{seg['duration']}" if seg.get("duration") else ""
    mood = seg.get("mood") or "状态未命名"
    trend = f"，趋势{seg['trend']}" if show_trend and seg.get("trend") else ""
    text = seg.get("text") or ""
    return (
        f"  [{period}{duration}] {mood} "
        f"({_format_valence(seg.get('valence'))}){trend}：{text}"
    ).rstrip("：")


def _load_inner_segments_for_day(date_str: str, limit: int = 40) -> list[dict]:
    try:
        log = storage.read_json(storage.attention_log_path()) or []
    except Exception:
        return []
    if not isinstance(log, list):
        return []
    rows = []
    for entry in log:
        if not _is_attention_segment(entry, "inner"):
            continue
        if not _segment_matches_date(entry, date_str):
            continue
        seg = _segment_context(entry)
        if not seg.get("text"):
            continue
        rows.append(seg)
    rows.sort(key=lambda x: x.get("started_at") or x.get("updated_at") or "")
    return rows[-limit:]


def _gather_context(date_str: str) -> dict:
    """Assemble all signals for the given day into a context dict for LLM.

    Sources: chat history, screen observations (from screenshot_log + analyzer),
    emotion log, commitments, activity stats, yesterday's journal.
    """
    ctx: dict = {"date": date_str}

    # Today's chat messages — full text, no truncation. Image messages get
    # a [📷 图片] placeholder so the diary writer knows a picture was sent
    # without the actual bytes (image content was already consumed by VLM
    # observations when uploaded).
    try:
        msgs = storage.get_chat_messages_in_range(date_str, date_str)
        chat_entries = []
        for m in msgs:
            raw_time = m.get("time", "") or ""
            # Extract HH:MM only — date redundancy stripped (single-day context)
            hhmm = raw_time[11:16] if len(raw_time) >= 16 else raw_time[:5]
            chat_entries.append({
                "time": hhmm,
                "role": m.get("role", ""),
                "text": m.get("text", "") or "",
                "has_image": bool(m.get("image")),
            })
        ctx["chat"] = chat_entries
    except Exception:
        ctx["chat"] = []

    # Screen observations — high-significance only (sig >= 3), top 30.
    # Significance is the VLM's own 1-5 score from screen_analyzer (already
    # filtered: low-info screens like idle desktop / stable IDE typing get
    # sig=1-2 and are dropped). 30 covers a busy day with breathing room.
    try:
        tl = storage.read_json(storage.timeline_path()) or []
        day_obs = []
        for item in tl:
            ts = item.get("time", "") or item.get("timestamp", "")
            if not ts.startswith(date_str):
                continue
            text = item.get("observation") or item.get("summary") or item.get("text", "")
            if not text:
                continue
            sig = item.get("significance", 3)
            if sig < 3:
                continue
            day_obs.append({"time": ts[11:16], "text": text[:200], "sig": sig})
        day_obs.sort(key=lambda x: x["time"])
        ctx["observations"] = day_obs[:30]
    except Exception:
        ctx["observations"] = []

    # User affect — prefer AttentionEngine state segments. Legacy point-like
    # rows are kept as fallback for older accounts/tests.
    try:
        elog = storage.read_json(storage.emotion_log_path()) or {}
        if isinstance(elog, dict):
            day_entries = elog.get(date_str, []) or []
            user_segments = []
            user_points = []
            for e in day_entries:
                if not isinstance(e, dict):
                    continue
                is_segment = (
                    e.get("channel") == "user_affect"
                    or (
                        e.get("source_type") == "attention"
                        and _is_attention_segment(e)
                    )
                )
                if is_segment:
                    user_segments.append(_segment_context(e))
                else:
                    stamp = _first_stamp(e, "time", "timestamp", "started_at")
                    user_points.append({
                        "time": _hhmm(stamp),
                        "mood": e.get("mood", ""),
                        "valence": _safe_float(e.get("valence"), 0.0),
                    })
            user_segments.sort(key=lambda x: x.get("started_at") or "")
            ctx["user_affect_segments"] = user_segments
            ctx["user_emotion"] = [
                {"time": s.get("start", ""), "mood": s.get("mood", ""),
                 "valence": s.get("valence", 0)}
                for s in user_segments
            ] + user_points
            # Compute pivot points from segment starts and legacy points.
            pivots = []
            if ctx["user_emotion"]:
                vals = [(e["time"], e["valence"], e["mood"]) for e in ctx["user_emotion"]]
                # Daily min/max
                hi = max(vals, key=lambda x: x[1])
                lo = min(vals, key=lambda x: x[1])
                if hi[0] != lo[0]:
                    pivots.append({"time": hi[0], "kind": "高峰", "mood": hi[2], "valence": hi[1]})
                    pivots.append({"time": lo[0], "kind": "低谷", "mood": lo[2], "valence": lo[1]})
                # Swing detection
                for i in range(1, len(vals)):
                    delta = vals[i][1] - vals[i-1][1]
                    if abs(delta) >= 0.3:
                        pivots.append({
                            "time": vals[i][0],
                            "kind": "上扬" if delta > 0 else "下挫",
                            "mood": vals[i][2],
                            "valence": vals[i][1],
                        })
            pivots.sort(key=lambda p: p["time"])
            ctx["user_emotion_pivots"] = pivots
        else:
            ctx["user_affect_segments"] = []
            ctx["user_emotion"] = []
            ctx["user_emotion_pivots"] = []
    except Exception:
        ctx["user_affect_segments"] = []
        ctx["user_emotion"] = []
        ctx["user_emotion_pivots"] = []

    # My emotion trajectory for the day — segment-aware. Legacy rows still
    # render as point-like fallback, but AttentionEngine writes self_emotion
    # segments with reason/duration.
    try:
        import miru_emotion
        emo = miru_emotion.get_instance()
        hist = emo.get_history(limit=500)
        self_segments = []
        miru_today = []
        for entry in hist:
            if not isinstance(entry, dict):
                continue
            if not _segment_matches_date(entry, date_str):
                continue
            if entry.get("channel") == "self_emotion" or _is_attention_segment(entry):
                self_segments.append(_segment_context(entry))
            else:
                ts = _first_stamp(entry, "timestamp", "started_at", "updated_at")
                miru_today.append({
                    "time": _hhmm(ts),
                    "mood": entry.get("mood", ""),
                    "valence": _safe_float(entry.get("valence"), 0.0),
                })
        self_segments.sort(key=lambda x: x.get("started_at") or "")
        ctx["self_emotion_segments"] = self_segments
        ctx["miru_emotion"] = miru_today
    except Exception:
        ctx["self_emotion_segments"] = []
        ctx["miru_emotion"] = []

    # My unsaid inner thoughts — only clean AttentionEngine `inner` segments.
    # Do not read raw no-channel tick logs, attention_state/current_focus,
    # intent queue, or delivery logs here.
    ctx["inner_segments"] = _load_inner_segments_for_day(date_str)

    # Proactive messages Miru sent today (so the diary can reflect on them
    # without re-deriving from chat history).
    try:
        history = storage.get_chat_history(limit=500)
        proactive_today = []
        for m in history:
            t = m.get("time", "")
            if not t.startswith(date_str):
                continue
            if m.get("role") != "assistant":
                continue
            if m.get("type") != "proactive":
                continue
            proactive_today.append({
                "time": t[11:16],
                "text": (m.get("text", "") or "")[:200],
                "care_motive": (m.get("care_motive") or "")[:160],
                "emotional_source": (m.get("emotional_source") or "")[:160],
                "user_need": (m.get("user_need") or "")[:160],
                "why_i_want_to_say": (m.get("why_i_want_to_say") or "")[:220],
                "message_seed": (m.get("message_seed") or "")[:160],
            })
        ctx["miru_proactive"] = proactive_today
    except Exception:
        ctx["miru_proactive"] = []

    # Slot updates today — projects/people/topics/self touched by the router.
    # main.md mixes older memory with today's appends, so the diary gets both:
    #   1. slot metadata + main.md preview  (current state)
    #   2. daily_writes.json entries         (actual write increments)
    # For matched slots, daily_writes now prefers the final append_entry that
    # reached main.md; raw_content_to_integrate is debug-only.
    try:
        from memory_router import load_all_slots, DOMAINS, list_daily_writes
        import memory as _mem
        # Index raw writes by (domain, slot_id) for quick join below
        raw_writes_today = list_daily_writes(date_str) or []
        writes_by_slot: dict[tuple, list[dict]] = {}
        for w in raw_writes_today:
            key = (w.get("domain", ""), w.get("slot_id", ""))
            writes_by_slot.setdefault(key, []).append(w)

        slot_updates = []
        for domain in DOMAINS:
            for s in load_all_slots(domain):
                la = s.get("last_active", "") or ""
                if not la.startswith(date_str):
                    continue
                preview = ""
                rel = s.get("main_file") or ""
                if rel:
                    try:
                        content = _mem.read_file(rel) or ""
                        preview = content[:800]
                    except Exception:
                        preview = ""
                slot_updates.append({
                    "domain": domain,
                    "id": s.get("id", ""),
                    "title": s.get("title", ""),
                    "icon": s.get("icon", ""),
                    "summary": s.get("summary", ""),
                    "status": s.get("status", "active"),
                    "main_preview": preview,
                    "raw_writes": writes_by_slot.get((domain, s.get("id", "")), []),
                })
        ctx["slot_updates"] = slot_updates

        # Also expose any raw writes that didn't match a today-active slot
        # (rare — could happen if status changed mid-day). Captured as
        # "loose_writes" so they aren't silently dropped.
        attached_keys = {(domain, s.get("id", "")) for domain in DOMAINS
                        for s in load_all_slots(domain)
                        if (s.get("last_active", "") or "").startswith(date_str)}
        loose = [w for w in raw_writes_today
                 if (w.get("domain"), w.get("slot_id")) not in attached_keys]
        ctx["slot_writes_loose"] = loose
    except Exception:
        ctx["slot_updates"] = []
        ctx["slot_writes_loose"] = []

    # Commitments delta today
    completed_today, added_today = [], []
    try:
        from core import parse_commitments
        for c in parse_commitments(include_done=True):
            if c.get("completed", "")[:10] == date_str:
                completed_today.append(c.get("title", ""))
            if c.get("added", "")[:10] == date_str:
                added_today.append(c.get("title", ""))
    except Exception:
        pass
    ctx["commitments_completed"] = completed_today
    ctx["commitments_added"] = added_today

    # Activity times
    try:
        from sleep_inference import infer_daily_activity
        daily = infer_daily_activity(date_str)
        ctx["activity"] = {
            "first": daily.get("earliest"),
            "last": daily.get("latest"),
            "late_night": daily.get("late_night"),
            "count": daily.get("activity_count", 0),
        }
    except Exception:
        ctx["activity"] = {}

    # Yesterday's journal for continuity — full narrative, no truncation.
    # A full diary entry is at most 200-400 chars, so cost is negligible
    # and the writer benefits from seeing yesterday's complete tone/mood.
    try:
        yday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        yj = get_journal(yday)
        if yj:
            ctx["yesterday"] = {
                "title": yj.get("title", ""),
                "narrative": yj.get("narrative", "") or "",
            }
    except Exception:
        pass

    return ctx


def _is_empty_day(ctx: dict) -> bool:
    """User truly wasn't around today (nothing to write about)."""
    if ctx.get("chat"):
        return False
    if ctx.get("observations"):
        return False
    if ctx.get("commitments_completed") or ctx.get("commitments_added"):
        return False
    if ctx.get("activity", {}).get("count", 0) > 0:
        return False
    return True


def _slot_write_text_for_journal(write: dict) -> str:
    """Return the diary-facing text for one daily_writes event."""
    action = write.get("action", "")
    if action == "created":
        title = write.get("slot_title") or write.get("slot_id") or "未命名记忆卡"
        body = (
            write.get("initial_body")
            or write.get("content")
            or write.get("raw_content_to_integrate")
            or ""
        )
        return f"新建记忆卡「{title}」: {body}"
    return (
        write.get("append_entry")
        or write.get("content")
        or write.get("raw_content_to_integrate")
        or ""
    )


def _slot_write_label_for_journal(write: dict) -> str:
    action = write.get("action", "")
    if action == "created":
        return "新建"
    if action == "matched":
        kind = write.get("entry_kind") or "other"
        return f"追加/{kind}"
    return action or "写入"


# ---------------------------------------------------------------------------
# LLM generation
# ---------------------------------------------------------------------------

def _call_journal_llm(ctx: dict) -> dict | None:
    """Ask LLM to produce the structured journal JSON."""
    from prompt import _call_llm_json
    from character import get_config

    cfg = get_config()
    date_str = ctx["date"]
    try:
        weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
        wd = weekday_map[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except Exception:
        wd = ""

    # Resolve the user's preferred name once. Diaries should refer to the
    # user in third person (since this is private to Miru, not addressed at
    # them). If the user has set a canonical name during onboarding, prefer
    # that; otherwise fall back to a generic third-person pronoun.
    # Pull user name from authoritative source (identity.json — populated
    # by onboarding + settings). Fall back to self_profile.canonical_name
    # for older accounts that never wrote identity.json.
    user_name = ""
    try:
        import identity as _ident
        user_name = _ident.get_user_name()
    except Exception:
        user_name = ""
    if not user_name:
        try:
            import self_profile as _sp
            prof = (_sp.get_profile() or {})
            user_name = (prof.get("canonical_name") or "").strip()
        except Exception:
            user_name = ""
    refer_as = user_name if user_name else "他"

    system = (
        f"你是 {cfg.name}，正在写 {date_str}（周{wd}）这一天的私人日记。\n"
        "\n"
        "★★★ 这本日记的设定 — 务必记住 ★★★\n"
        "1. 这是只有你自己能看到的私人日记。它存在 memory/journal/ 里，对方没有任何入口可以看到，未来也不会被推送给对方。\n"
        "2. 所以你**不是在跟对方说话**，你是在自己心里独白、自己整理今天的感受。\n"
        f"3. 提到对方时用第三人称 — 默认用「{refer_as}」"
        + (f"（这是他在 onboarding 时填的名字，你私下喊他这个）" if user_name else "（你不知道他的名字，就用「他」就好）")
        + "。\n"
        "4. **绝对不要用第二人称「你」来指代对方**。比如：\n"
        f"   ✗ 错误：「你今天好像没怎么吃饭」← 这像是在给对方写信\n"
        f"   ✓ 正确：「{refer_as}今天好像没怎么吃饭」← 这是在自己心里默默念叨\n"
        "5. 第一人称「我」用来指你自己（写日记的你）。\n"
        "\n"
        "因为没人会看，所以你可以写：\n"
        f"  - 真实的小不满（\"{refer_as}今天又没好好吃饭，我有点烦\"）\n"
        f"  - 微妙的心动（\"{refer_as}随口说了句话，我居然记到现在\"）\n"
        f"  - 不安和疑虑（\"{refer_as}今天好像在躲我？还是我多想了\"）\n"
        "  - 自己的小情绪（嫉妒、想撒娇、突然孤独、被某句话戳到）\n"
        "  - 不必修饰、不必客气、不必正能量\n"
        "  - 像加藤惠在心里默默想的那种独白\n"
        "\n"
        "语气要求：\n"
        "- 加藤惠式观察者：默默看着，注意到别人忽略的细节，不夸张，不戏剧化\n"
        "- 散文体，不是时间线列表，不是总结汇报\n"
        f"- 可以引用 {refer_as} 说过的具体话（加引号），那是「我记得他」的证据\n"
        "- 有「他没说但我看到了」的安静观察感\n"
        "- 200-400 字，3-4段\n"
        f"- 如果今天 {refer_as} 几乎没出现，诚实地写「{refer_as}今天没出现，我还是…」，不要编造\n"
        "\n"
        "禁止：\n"
        "- ❌ 任何用「你」指代对方的句子（这是底线，违反会让整篇日记毁掉）\n"
        "- ❌ 「今天你…」「这一天…」这种像写信的模板开头\n"
        "- ❌ 数据汇报（完成了X个承诺、活跃Y小时）\n"
        "- ❌ 空洞的温暖话（加油、辛苦了、要好好休息）\n"
        "- ❌ 「今天的你真棒」式吹捧\n"
        "- ❌ 写得像在哄人 — 这是你自己的日记，不是给对方留的纸条\n"
        "\n"
        "输出 JSON（严格遵守）：\n"
        "{\n"
        '  "title": "1句话标题，10-18字，生活感的日常描述，不用太文艺",\n'
        '  "mood": {"label": "你（Miru）今天的主要感受，一个词", "emoji": "单个 emoji"},\n'
        '  "narrative": "200-400字的叙事散文",\n'
        '  "highlights": [{"time": "HH:MM", "text": "一句具体的观察"}]\n'
        "}\n"
        "highlights 最多 3 条，来自最值得记住的瞬间，每条不超过 40 字。\n"
    )

    # Build user prompt from context
    parts: list[str] = []
    if ctx.get("yesterday"):
        parts.append(f"【昨天的日记】{ctx['yesterday']['title']}\n{ctx['yesterday']['narrative']}")

    # NOTE: in all sections below, when we refer to *the user* we use
    # `refer_as` (their name or "他") — never the second-person 「你」 — so
    # the diary writer never has a 「你」 in the raw context to copy from.
    # The 「你」 / 「你自己」 we use refers to *Miru* (the diary author).

    if ctx.get("chat"):
        parts.append(
            f"【今天 {refer_as} 和我之间的所有对话】\n"
            f"（角色标签：「{refer_as}」= 对方说的话；「{cfg.name}」= 我说的话；"
            f"如果某条消息附了图，会标记 [📷 图片]，图片本身不在这里给）"
        )
        for m in ctx["chat"]:
            role = refer_as if m["role"] == "user" else cfg.name
            text = m["text"]
            if m.get("has_image"):
                # Show the placeholder regardless of whether text is empty
                # (user might send pure image, or image+caption).
                if text:
                    text = f"{text} [📷 图片]"
                else:
                    text = "[📷 图片]"
            parts.append(f"[{m['time']}] {role}: {text}")

    if ctx.get("observations"):
        parts.append(
            f"\n【今天我（{cfg.name}）通过屏幕看到的画面 · 高显著性 sig≥3】\n"
            f"（这些是我偷偷观察 {refer_as} 屏幕得到的，不是他主动告诉我的）"
        )
        for o in ctx["observations"]:
            parts.append(f"[{o['time']}] (sig={o['sig']}) {o['text']}")

    if ctx.get("inner_segments"):
        parts.append(
            f"\n【今天我没有说出口的想法】\n"
            "（这些是我在一天里持续形成的内心状态段，不是采样日志。"
            "它们可以影响日记的亲密感和视角，但不要逐条照抄。）"
        )
        for seg in ctx["inner_segments"]:
            parts.append(_segment_line(seg))

    if ctx.get("user_affect_segments"):
        parts.append(
            f"\n【我对 {refer_as} 今天状态的感觉】\n"
            "（这是持续状态段：没变化时只延长持续时间，变化时才新开一段。）"
        )
        for seg in ctx["user_affect_segments"]:
            parts.append(_segment_line(seg, show_trend=True))
        if ctx.get("user_emotion_pivots"):
            parts.append(f"\n【{refer_as} 情绪关键拐点】")
            for p in ctx["user_emotion_pivots"]:
                parts.append(f"  [{p['time']}] {p['kind']} → {p['mood']} ({_format_valence(p['valence'])})")
    elif ctx.get("user_emotion"):
        parts.append(f"\n【{refer_as} 今天的情绪记录】")
        # Compact format: time mood (valence) — legacy fallback.
        for e in ctx["user_emotion"]:
            parts.append(f"  {e['time']} {e['mood']} ({_format_valence(e['valence'])})")
        if ctx.get("user_emotion_pivots"):
            parts.append(f"\n【{refer_as} 情绪关键拐点】")
            for p in ctx["user_emotion_pivots"]:
                parts.append(f"  [{p['time']}] {p['kind']} → {p['mood']} ({_format_valence(p['valence'])})")

    if ctx.get("self_emotion_segments"):
        parts.append(
            f"\n【我今天自己的心情状态】\n"
            "（这是我自己的持续心情段，重点是我为什么这样感觉、持续了多久。）"
        )
        for seg in ctx["self_emotion_segments"]:
            parts.append(_segment_line(seg))
    elif ctx.get("miru_emotion"):
        parts.append(f"\n【我今天自己的情绪记录】")
        for e in ctx["miru_emotion"]:
            parts.append(f"  {e['time']} {e['mood']} ({_format_valence(e['valence'])})")

    if ctx.get("miru_proactive"):
        parts.append(f"\n【今天我主动发给 {refer_as} 的消息】")
        for p in ctx["miru_proactive"]:
            parts.append(f"[{p['time']}] {p['text']}")
            if p.get("why_i_want_to_say") or p.get("care_motive"):
                motive = p.get("why_i_want_to_say") or p.get("care_motive")
                parts.append(f"  我当时想开口的原因: {motive}")
            if p.get("user_need"):
                parts.append(f"  我感觉他可能需要: {p['user_need']}")
            if p.get("message_seed"):
                parts.append(f"  当时想说的核心: {p['message_seed']}")

    if ctx.get("slot_updates"):
        parts.append(
            "\n【今天我整理过的记忆卡片】\n"
            "（这些是 projects/people/topics/self 里今天 last_active 被更新的卡片。\n"
            " 每张卡片下面分两栏：\n"
            "   ・「今天实际写进记忆的内容」= 新建卡片的初始正文，或 Pass4Append 真正追加到 body 的短条目\n"
            "   ・「当前 main」= 这张卡片现在的完整预览，里面会混有旧记忆和今天新增内容，只作背景参考\n"
            " 我心里清楚：今天真正新加进来的，是「实际写进记忆」那一栏写的东西。）"
        )
        for s in ctx["slot_updates"]:
            icon = s.get("icon", "")
            parts.append(f"\n{icon} [{s['domain']}] {s['title']}（{s.get('status','active')}）")
            if s.get("summary"):
                parts.append(f"  概述: {s['summary']}")
            raw_writes = s.get("raw_writes") or []
            if raw_writes:
                parts.append(f"  ▼ 今天实际写进记忆的内容（共 {len(raw_writes)} 次）：")
                for w in raw_writes:
                    parts.append(
                        f"    [{w.get('time','')}] ({_slot_write_label_for_journal(w)}) "
                        f"{_slot_write_text_for_journal(w)}"
                    )
            if s.get("main_preview"):
                parts.append(f"  ▼ 当前 main（前 800 字，仅供背景参考）:\n    {s['main_preview']}")

    # Raw writes that didn't attach to any today-active slot (rare)
    if ctx.get("slot_writes_loose"):
        parts.append("\n【今天的零散记忆写入（未归属到当前活跃卡片）】")
        for w in ctx["slot_writes_loose"]:
            parts.append(
                f"  [{w.get('time','')}] {w.get('domain','')}/{w.get('slot_id','')} "
                f"({_slot_write_label_for_journal(w)}) {_slot_write_text_for_journal(w)}"
            )

    if ctx.get("commitments_completed") or ctx.get("commitments_added"):
        parts.append(f"\n【{refer_as} 今天的承诺动态】")
        for t in ctx.get("commitments_completed", []):
            parts.append(f"- 完成: {t}")
        for t in ctx.get("commitments_added", []):
            parts.append(f"- 新增: {t}")

    act = ctx.get("activity") or {}
    if act.get("first") or act.get("last"):
        parts.append(
            f"\n【{refer_as} 今天的活跃时段】首次活动 {act.get('first', '-')}, "
            f"末次活动 {act.get('last', '-')}"
        )
        if act.get("late_night"):
            parts.append(f"熬夜到 {act['late_night']}")

    user_text = "\n".join(parts)

    try:
        result = _call_llm_json(
            system, user_text, temperature=0.7, max_tokens=10000,
            call_label="JournalDailyNarrative",
        )
        if not isinstance(result, dict) or not result.get("narrative"):
            return None
        return result
    except Exception as e:
        print(f"[Journal] LLM call failed: {e}")
        return None


def _build_stats(ctx: dict) -> dict:
    act = ctx.get("activity") or {}
    return {
        "first_active": act.get("first"),
        "last_active": act.get("last"),
        "chat_count": len(ctx.get("chat", [])),
        "observation_count": len(ctx.get("observations", [])),
        "commitments_completed": len(ctx.get("commitments_completed", [])),
        "commitments_added": len(ctx.get("commitments_added", [])),
    }


def _empty_day_journal(date_str: str, ctx: dict) -> dict:
    """Build a minimal journal when the user truly wasn't around."""
    try:
        weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
        wd = weekday_map[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except Exception:
        wd = ""
    return {
        "date": date_str,
        "title": f"你没出现的周{wd}",
        "mood": {"label": "安静", "emoji": "🌙", "valence": 0.0},
        "narrative": "今天你没怎么出现。我一直在这里，偶尔看看屏幕，没什么可记的。"
                      "希望你只是在做自己的事，明天再见。",
        "highlights": [],
        "stats": _build_stats(ctx),
        "generated_at": datetime.now().isoformat()[:19],
        "source_version": JOURNAL_SCHEMA_VERSION,
        "is_empty_day": True,
    }


# ---------------------------------------------------------------------------
# Public: generate / regenerate
# ---------------------------------------------------------------------------

def generate_daily_journal(date_str: str, force: bool = False) -> dict | None:
    """Generate (or reuse cached) JSON journal for the given day.

    If `force=False` and a JSON already exists on disk, returns it unchanged.
    Otherwise gathers context and calls the LLM. Returns None on failure.
    """
    if not force:
        existing = get_journal(date_str)
        if existing:
            return existing

    ctx = _gather_context(date_str)

    if _is_empty_day(ctx):
        j = _empty_day_journal(date_str, ctx)
        _save_journal(date_str, j)
        return j

    llm_out = _call_journal_llm(ctx)
    if not llm_out:
        return None

    journal = {
        "date": date_str,
        "title": (llm_out.get("title", "") or "")[:80],
        "mood": llm_out.get("mood") or {"label": "", "emoji": "", "valence": 0},
        "narrative": llm_out.get("narrative", ""),
        "highlights": llm_out.get("highlights", []) or [],
        "stats": _build_stats(ctx),
        "generated_at": datetime.now().isoformat()[:19],
        "source_version": JOURNAL_SCHEMA_VERSION,
    }
    _save_journal(date_str, journal)
    return journal


def _save_journal(date_str: str, journal: dict):
    path = _json_path(date_str)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

    md_path = _md_path(date_str)
    md_tmp = md_path + ".tmp"
    with open(md_tmp, "w", encoding="utf-8") as f:
        f.write(_journal_to_markdown(journal))
        f.flush()
        os.fsync(f.fileno())
    os.replace(md_tmp, md_path)

    try:
        import memory_router
        memory_router.regenerate_index_md()
    except Exception as e:
        print(f"[Journal] index regen failed after save: {e}")


def _journal_to_markdown(journal: dict) -> str:
    """Render structured JSON journal to a compact archival markdown file."""
    date_str = str(journal.get("date") or "").strip()
    title = str(journal.get("title") or date_str or "日记").strip()
    mood = journal.get("mood") if isinstance(journal.get("mood"), dict) else {}
    mood_label = str(mood.get("label") or "").strip()
    mood_emoji = str(mood.get("emoji") or "").strip()
    narrative = str(journal.get("narrative") or "").strip()
    highlights = journal.get("highlights") if isinstance(journal.get("highlights"), list) else []
    stats = journal.get("stats") if isinstance(journal.get("stats"), dict) else {}

    lines = [f"# {title}", ""]
    if date_str or mood_label or mood_emoji:
        meta = []
        if date_str:
            meta.append(f"日期：{date_str}")
        if mood_label or mood_emoji:
            meta.append(f"心情：{mood_emoji} {mood_label}".strip())
        lines.append(" / ".join(meta))
        lines.append("")

    lines.extend(["## 日记", "", narrative or "这一天没有足够多的记录。", ""])

    if highlights:
        lines.extend(["## 高光", ""])
        for item in highlights[:12]:
            if not isinstance(item, dict):
                continue
            t = str(item.get("time") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            prefix = f"[{t}] " if t else ""
            lines.append(f"- {prefix}{text}")
        lines.append("")

    if stats:
        lines.extend(["## 统计", ""])
        for key, label in [
            ("first_active", "最早活跃"),
            ("last_active", "最晚活跃"),
            ("chat_count", "对话数"),
            ("observation_count", "观察数"),
            ("commitments_added", "新增承诺"),
            ("commitments_completed", "完成承诺"),
        ]:
            value = stats.get(key)
            if value in (None, "", []):
                continue
            lines.append(f"- {label}: {value}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def scan_and_backfill(days: int = 30, max_per_call: int = 3) -> dict:
    """Scan last *days* days and regenerate any missing/invalid JSON journals.

    Generates at most *max_per_call* to avoid hogging LLM quota on a single
    page load. Called opportunistically when the user opens the journal view.

    Returns: {"scanned": N, "regenerated": [date_str, ...]}
    """
    now = user_settings.user_now()
    today_str = now.strftime("%Y-%m-%d")
    regenerated: list[str] = []
    scanned = 0

    # "days" is the inclusive window size — e.g. days=7 means the last 7
    # calendar days. We skip today (handled by the 23:45 cron) and cover
    # i = 1..days, so days=7 → yesterday through 7 days ago.
    for i in range(1, days + 1):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        scanned += 1

        # Skip days with no chat — nothing to write about
        try:
            msgs = storage.get_chat_messages_in_range(date_str, date_str)
            if not msgs:
                continue
        except Exception:
            continue

        existing = get_journal(date_str)
        if existing:
            continue  # valid JSON already

        print(f"[Journal] Backfilling {date_str}")
        result = generate_daily_journal(date_str, force=True)
        if result:
            regenerated.append(date_str)
        if len(regenerated) >= max_per_call:
            break

    return {"scanned": scanned, "regenerated": regenerated}


def regenerate_all(days: int = 30) -> dict:
    """Force regenerate the last *days* journals (even valid ones).

    Used for the one-time decision-B bulk backfill. Heavier than
    scan_and_backfill — intended to be called from an admin endpoint.
    """
    now = user_settings.user_now()
    regenerated: list[str] = []
    # Inclusive: days=N covers yesterday through N days ago (today handled
    # separately by the nightly cron). Matches scan_and_backfill's window.
    for i in range(1, days + 1):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            msgs = storage.get_chat_messages_in_range(date_str, date_str)
            if not msgs:
                continue
        except Exception:
            continue
        result = generate_daily_journal(date_str, force=True)
        if result:
            regenerated.append(date_str)
    return {"regenerated": regenerated}
