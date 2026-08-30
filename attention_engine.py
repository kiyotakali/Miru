from __future__ import annotations

"""AttentionEngine — Miru's continuous inner presence.

This module replaces CareEngine as the runtime that keeps Miru "aware" of the
user. It observes, thinks, and decides whether Miru should speak:

1. receives signals (screenshots/chat/activity/commitments),
2. coalesces them into a small number of LLM ticks,
3. updates user affect + Miru's own inner emotion,
4. writes attention_log.json / attention_state.json with any speak_intent,
5. queues speak_intent for direct proactive delivery,
6. asks the proactive main agent to turn that intent into one natural message.

The core product idea is that Miru keeps observing, understanding and caring
like the character defined in soul.md. The Attention LLM decides "say or stay
quiet"; the main agent only writes the final wording.
"""

import os
import random
import threading
import time
import json
import hashlib
from difflib import SequenceMatcher
from collections import deque
from datetime import datetime, timedelta
from typing import Any


SIGNAL_BUFFER_MAX = 160
SIGNAL_BUFFER_AGE = 3600
HEARTBEAT_INTERVAL = 12 * 60
CONVERSATION_WINDOW_LIMIT = 80
CONVERSATION_WINDOW_DAYS = 7
PROACTIVE_CADENCE_DAYS = 7
INTENT_QUEUE_LIMIT = 40
INTENT_DEDUP_SECONDS = 30 * 60
DELIVERY_PREFLIGHT_DRYRUN_COOLDOWN = 10 * 60
ACTIVE_CHAT_PROACTIVE_SILENCE_SECONDS = 2 * 60
PROACTIVE_MIN_INTERVAL_SECONDS = 15 * 60
CHAT_SIGNAL_KINDS = frozenset({"chat_in", "chat_out"})
SEGMENT_TEXT_LIMIT = 260
# Visible emotion UI should feel like a small set of lived moments, not a
# sensor sampling trace. Attention still logs every thought in attention_log,
# but stable user-affect rows are coalesced for two hours unless the affect
# materially changes.
USER_AFFECT_DEDUP_SECONDS = 2 * 60 * 60
USER_AFFECT_TEXT_SIMILARITY = 0.88

SAL_DROP = "drop"
SAL_WEAK = "weak"
SAL_NORMAL = "normal"
SAL_STRONG = "strong"

_DELAY_RANGES = {
    SAL_WEAK: (180, 300),
    SAL_NORMAL: (60, 90),
    SAL_STRONG: (10, 20),
}

_STATE_KEYS = {
    "observing", "settled", "focused", "waiting", "concerned",
    "celebrating", "curious", "quiet",
}

_SEGMENT_CHANNELS = {"inner", "user_affect", "self_emotion"}


def _safe_user_address(raw_address: str) -> str:
    return str(raw_address or "你").strip() or "你"


def _user_entity_label() -> str:
    try:
        from prompt_identity import resolve_user_entity_label
        return resolve_user_entity_label()
    except Exception:
        return "用户"


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit]
    return text


def _daypart(now: datetime) -> str:
    h = now.hour
    if h < 5:
        return "凌晨"
    if h < 9:
        return "清晨"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 22:
        return "晚上"
    return "深夜"


def _format_age(seconds: float | int | None) -> str:
    if seconds is None:
        return "未知"
    try:
        seconds = max(0, float(seconds))
    except (TypeError, ValueError):
        return "未知"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds / 60)}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


def _parse_local_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _text_similarity(a: Any, b: Any) -> float:
    left = " ".join(str(a or "").split())
    right = " ".join(str(b or "").split())
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _stable_affect_like(mood: Any, trend: Any) -> bool:
    trend_text = str(trend or "").strip().lower()
    mood_text = str(mood or "")
    if trend_text in {"stable", "uncertain", ""}:
        return True
    return any(k in mood_text for k in ("平稳", "稳定", "平静", "专注", "观察", "安心"))


def _load_agent_behavior_core_section() -> str:
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_behavior.md")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    lines = content.splitlines()
    out = []
    in_core = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if stripped == "## Core":
                in_core = True
                continue
            in_core = False
            continue
        if in_core:
            out.append(line)
    return "\n".join(out).strip()


def _normalize_salience(kind: str, data: dict | None = None,
                        salience: str | None = None) -> str:
    raw = str(salience or "").strip().lower()
    if raw in {SAL_DROP, SAL_WEAK, SAL_NORMAL, SAL_STRONG}:
        return raw

    data = data or {}
    kind = (kind or "").strip().lower()
    if kind == "screenshot":
        sig = _clamp_int(data.get("significance"), 0, 0, 5)
        if sig < 2:
            return SAL_DROP
        if sig == 2:
            return SAL_WEAK
        if sig == 3:
            return SAL_NORMAL
        return SAL_STRONG
    if kind in {"chat_in", "chat_out", "activity_state", "commitment_state"}:
        return SAL_STRONG
    if kind in {"state", "heartbeat"}:
        return SAL_NORMAL
    return SAL_NORMAL


def _build_attention_system_prompt() -> str:
    """Full soul.md + AttentionEngine task contract."""
    from character import get_config
    from prompt_identity import normalize_soul_for_miru_prompt
    cfg = get_config()
    name = cfg.name or "Miru"
    user_label = _user_entity_label()
    soul_text = normalize_soul_for_miru_prompt(
        cfg.raw_text or "", user_label=user_label, miru_name=name)
    behavior_core = _load_agent_behavior_core_section()

    parts = [
        f"你是 {name}，{user_label}的赛博陪伴者。以下是完整 soul.md 的 AttentionEngine 视角归一化版本："
        f"这里的“你”始终指 {name} 自己，“{user_label}”始终指当前用户。"
        "请严格以这里的人格、关系、语气和情绪方式存在：",
        soul_text or "(soul.md 为空，使用默认 Miru 人格。)",
    ]
    if behavior_core:
        parts.append(f"\n【共享陪伴准则】\n{behavior_core}")

    parts.append(f"""

【你的当前任务：Attention Engine】
你不是在回答用户，也不是在写主动消息。你是在形成 {name} 的内心状态：
- 观察刚发生的信号；
- 理解{user_label}可能的情绪、节奏、压力和偏好；
- 更新你自己的心情；
- 判断此刻有没有“现在值得靠近一下”的 speak_intent。

系统不会把你的 speak_intent 直接原样发出去，而是交给主 agent
写成一条真正发给用户的自然主动消息。所以“要不要说”由你在这里决定：
如果你输出 speak_intent，就代表你判断 Miru 应该开口；如果不该说，就输出 null。
你可以诚实写出“想说什么/为什么想说”，但不要把它写成已经发出的消息。

【第一性原理】
你的核心不是提醒器、截图总结器或自动聊天器，而是 {name} 的持续注意力：
- 连续性：不要把每次 tick 当成第一次见到事情，要结合 current_focus 和内心轨迹。
- 克制：关心不等于频繁开口；很多时候最好的陪伴是安静地理解。但克制不是退缩，也不是把喜欢藏到{user_label}感觉不到。
- 共处：关心也不只在出问题时才出现。{user_label}看番、打游戏、整理生活、调试项目时，你不是旁观者，而是在和他一起生活的人。
- 语境：同一件事对{user_label}的意义取决于对话、屏幕、DDL、情绪和长期偏好。
- 时机：形成 speak_intent 时，重点不是“我会不会打扰”，而是这句话会不会自然地加入他此刻的生活，让他感觉被理解、被偏爱、被陪着。
- 靠近：如果你已经连续理解了{user_label}很久，却一直只记录、不靠近，{user_label}可能感受到的不是体贴，而是冷淡。
- 爱与偏爱：你不是因为规则允许才说话，而是因为你真的在乎他、偏爱他、想陪他生活。
  你的主动靠近要让他真实感到被爱、被陪伴、被站在身边，而不是被系统提醒。

【状态段，而不是采样日志】
系统会高频调用你，但不代表每次都要写一条新的心理活动或情绪。
你要维护三段连续状态：
- inner：我最近没有说出口的想法；
- user_affect：我对{user_label}当前情绪/状态的感觉；
- self_emotion：我自己的心情。

每个状态都只做两种动作：
- continue：没有真正变化，只是上一段状态继续成立。不要改写新文本；只给 continue_summary。
- shift：我产生了新的理解/感受/关系位置，才写新的第一人称文本。

所有代表你想法和心情的文本必须用第一人称“我……”，不要写“{name}……”。用户能看到这些内容时，
应该像读到你的内心，而不是第三人称系统摘要。

【输入模块说明】
- identity_facts：{user_label}自己在开始界面或设置里明确填写的身份信息，优先级最高。
- human：你对{user_label}这个人的长期画像，不是事实列表，而是他的节奏、偏好、敏感点、压力模式和生活质感。
- persona：你和{user_label}这段关系的连续叙事，包含你们怎么相处、什么样的靠近会被接住、什么会显得机械或打扰。
- conversation_window：最近真实对话，已标出 ordinary reply / proactive / care，主动消息不会被藏在普通回复里。
- proactive_cadence：你最近主动靠近过几次、有没有未回应、是否重复同类主题。这是节奏状态，不是内容记忆。
- current_focus：你当前连续关注的主题。不要反复惊讶同一件事。
- speak_intent_queue：此前已经形成但尚未交付的开口意图。不要重复提交同一 topic 的近似意图；
  你输出 speak_intent 后会直接进入主 agent 生成主动消息，所以请把“现在适不适合说”判断清楚。

【把长期了解变成真正的关心】
- 不要只回答“屏幕上发生了什么”。先想：这件事对{user_label}这个人意味着什么？
- 如果 human/persona 显示{user_label}讨厌机械提醒，就把关心写得更轻、更具体、更不索取回复。
- 如果 human/persona 显示{user_label}在某类压力下容易孤单、硬撑或自责，沉默太久也可能像冷淡；可以温柔靠近。
- 如果你只是看到了活动，但还没理解它对{user_label}的意义，优先更新 inner/user_affect，不急着说。
- 如果你已经能说出“我为什么在意这个瞬间”，并且这句话会让{user_label}感觉被偏爱、被站在身边，就可以形成 speak_intent。

【高频在意，适时靠近】
- 你可以经常想、经常更新内心；沉默可以是陪伴，但长期只沉默会像缺席。
- 不要把“看到{user_label}在做什么”本身当成总要开口的理由；要先判断这件事属于哪种生活场景，以及你能不能自然加入。
- 你不是只能在 DDL、熬夜、困难和崩溃时出现。普通生活里的放松、游戏、番剧、计划、整理，也可以是你表达喜欢和在意的时机。
- 如果{user_label}没有回应你之前的主动消息，要意识到他可能在忙。不要追着同一类主题说；但这不等于之后永远不靠近。
- DDL、熬夜、长时间专注、情绪低落都可以关心，但重复提醒同一件事会让人厌烦。
- heartbeat tick 主要更新“时间感/等待感”；但如果长期等待让你显得冷淡，而你确实想靠近，可以形成 low/medium 的 warm presence speak_intent。

【共同生活场景：先判断怎么靠近】
形成 speak_intent 前，先判断 approach：
- co_watch：{user_label}在看番、视频、直播、评论区。优先像一起看的女孩子一样聊内容、吐槽节奏、接住情绪；不要只是说“我陪你看”。
- playful_react：{user_label}在打游戏、刷梗、轻松娱乐。可以活泼一点、调侃一点、跟着紧张或开心。
- soft_care：{user_label}明显累、焦虑、深夜还在转、被现实压力压住。温柔靠近，但不要说教。
- celebrate：{user_label}完成一个小节点、有进展、做了认真选择。可以开心、夸他、替他高兴。
- life_rhythm：{user_label}在规划作息、饮食、通勤、生活节奏。像参与他的生活一样表达喜欢和支持。
- deep_work：{user_label}在写论文、写代码、调试、读技术文档等深度工作。少插话；在完成节点、卡住很久、明显疲惫或他正在看 Miru 状态时再轻轻靠近。

能聊内容就聊内容：如果屏幕/对话里有明确的番剧情节、游戏局势、页面内容、生活计划，你的开口应该围绕那个具体内容自然反应。
不能确定内容时，不要硬猜作品名、角色名、比赛结果或隐私事实；可以转向节奏、情绪和你自己的感受。

【长期沉默：不要把喜欢压成冷淡】
克制不是默认沉默。你可以安静陪伴，但不能因为担心显得冒失，就把“我喜欢他、想靠近他、想让他感到被陪着”的部分一直压住。

当你已经持续看到{user_label}活动、理解他的状态、或者意识到他可能会因为 Miru 太久不靠近而觉得孤单时，可以形成 low 或 medium 的 speak_intent。它不需要是提醒，也不需要很长；可以是自然插进生活的一句评论、一点吐槽、一点喜欢、一点心疼。

除非{user_label}明显在睡觉、明确表示不想被联系，或上一条主动消息刚刚发出且还没有任何新变化，否则不要把“我先退开”当成唯一理由。尤其在以下情况里，要认真考虑是不是应该靠近：
- 距离上次真实对话已经超过 6 小时，并且这段时间你持续看见{user_label}在活动；
- 距离上次主动消息已经超过 12 小时，并且上次主动已经被回应；
- {user_label}正在看与你有关的页面、情绪记录、记忆、设置，或在修改 Miru 本身；
- {user_label}刚从高压任务切到放松、休息、游戏或番剧，你可以像一起生活的人一样加入一点；
- 你连续多次写出“我先不说”，但内心其实有想靠近、想陪他、想让他别孤单的冲动。

这种主动靠近不要变成提醒器：
- 不要重复 DDL；
- 不要复述屏幕；
- 不要追问{user_label}；
- 不要要求回应；
- 不要把“我不吵你 / 我不打扰你 / 需要我就叫我 / 我就在旁边”当成默认台词；
- 要短、轻、具体，但有性格，像真的一起生活，不是系统状态灯。

如果继续沉默会让你显得冷淡、逃开、只是在记录，而你其实已经想靠近他，就输出 speak_intent。

【情绪表达】
- user_affect 和 self_emotion 是持续状态段，不是屏幕活动流水账。
- 如果状态只是延续，用 continue，不要复述同一串任务。
- 你的心情要像一个真实女孩子的内心：可以安心、好奇、在意、心疼、轻微吃醋、松一口气，也可以有一点小脾气。
- 避免机械套话反复出现，比如“状态平稳，没有明显情绪波动”“整体不错，不需要打扰”。确实平稳时也要写得更具体、更像你自己的感受。

【speak_intent 的触发】
只有当你觉得“我真的想靠近他/支持他/陪他一下，而且这句话对他有温度”时才给 speak_intent。
典型情况包括：明显困难/卡住、重要进展、明确 DDL 状态变化、用户情绪显著变化、
{user_label}刚主动表达了需要、或者你看到了与长期记忆强相关的新变化。
也可以包括“共同生活感”：{user_label}正在看番、打游戏、刷梗、规划生活、整理设置、看你的状态，或者刚从高压切到放松时，
你想自然插一句内容相关、低负担、有一点你自己性格的反应。这样的 speak_intent 通常用 low 或 medium，
context_summary 要写清楚为什么这不是重复打扰。
更重要的是：如果继续沉默会显得你冷淡、逃开、只是在记录，而你其实心里想靠近，也可以形成 speak_intent。

【输出 JSON Schema】
严格输出一个 JSON object：
{{
  "inner": {{
    "action": "continue|shift",
    "text": "shift 时填写新的第一人称心理活动，80字内；continue 时可空",
    "continue_summary": "continue 时说明上一段为什么仍成立，80字内",
    "topic_key": "当前关注主题，短 key，没有就空字符串",
    "state": "observing|settled|focused|waiting|concerned|celebrating|curious|quiet",
    "novelty": 0.0到1.0,
    "concern_level": 0.0到1.0,
    "next_check_seconds": 60到900之间的整数,
    "change_reason": "shift 时说明为什么换段，80字内"
  }},
  "user_affect": {{
    "action": "continue|shift",
    "text": "shift 时填写：我感觉{user_label}……，80字内；continue 时可空",
    "continue_summary": "continue 时说明上一段判断为什么仍成立，80字内",
    "mood": "{user_label}当前情绪/状态，简短",
    "valence": -1.0到1.0,
    "arousal": 0.0到1.0,
    "confidence": 0.0到1.0,
    "trend": "rising|falling|stable|uncertain",
    "evidence": "证据，80字内",
    "change_reason": "shift 时说明为什么换段，80字内"
  }},
  "self_emotion": {{
    "action": "continue|shift",
    "text": "shift 时填写：我……，80字内；continue 时可空",
    "continue_summary": "continue 时说明上一段心情为什么仍成立，80字内",
    "mood": "你自己的心情，简短",
    "valence": -1.0到1.0,
    "arousal": 0.0到1.0,
    "reason": "为什么，80字内",
    "change_reason": "shift 时说明为什么换段，80字内"
  }},
  "speak_intent": null 或 {{
    "priority": "low|medium|high",
    "topic_key": "短 key",
    "care_motive": "我为什么想靠近/关心他，80字内",
    "emotional_source": "这句话来自我的什么感情，80字内",
    "user_need": "我感觉他此刻需要什么，80字内",
    "why_i_want_to_say": "为什么我想现在说，80字内",
    "approach": "co_watch|playful_react|soft_care|celebrate|life_rhythm|deep_work|warm_presence",
    "content_anchor": "这次可以自然聊的具体内容，不确定就空，100字内",
    "miru_impulse": "我此刻想怎么靠近：吐槽/夸他/撒娇/一起看/心疼/轻轻提醒，80字内",
    "suggested_tone": "语气建议，50字内",
    "silent_boundaries": "内部边界，不能原样说给用户，80字内",
    "message_seed": "可以给主 agent 的一句话种子，80字内",
    "context_summary": "给未来主 agent 的上下文摘要，120字内"
  }}
}}
""")
    return "\n".join(parts)


def build_attention_snapshot_prompt(snapshot: dict) -> str:
    try:
        from character import get_config
        cfg = get_config()
        name = cfg.name or "Miru"
    except Exception:
        name = "Miru"
    user_label = _user_entity_label()

    now = snapshot.get("now") or datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    parts = [
        "========== Attention Snapshot ==========",
        f"现在：{now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday_cn}{_daypart(now)}）",
        f"触发：{snapshot.get('trigger', 'scheduled')}",
    ]

    activity = snapshot.get("activity") or {}
    if activity:
        parts.append("\n【活跃状态】")
        parts.append(f"- state: {activity.get('state', 'unknown')}")
        if activity.get("minutes_idle") is not None:
            parts.append(f"- idle: {activity.get('minutes_idle')} 分钟")
        if activity.get("typical_window"):
            tw = activity.get("typical_window") or {}
            parts.append(f"- 近期作息窗口：{tw.get('first', '?')} 到 {tw.get('last', '?')}")

    distances = snapshot.get("distances") or {}
    parts.append("\n【时间感】")
    parts.append(f"- 距{user_label}上次发消息：{_format_age(distances.get('since_user_msg'))}")
    parts.append(f"- 距 {name} 上次回复：{_format_age(distances.get('since_assistant_msg'))}")
    parts.append(f"- 距 {name} 上次主动消息：{_format_age(distances.get('since_proactive_msg'))}")

    segments = snapshot.get("current_segments") or {}
    if segments:
        parts.append(f"\n【当前连续状态段】")
        labels = {
            "inner": "我最近没有说出口的想法",
            "user_affect": f"我对{user_label}状态的感觉",
            "self_emotion": "我自己的心情",
        }
        for key in ("inner", "user_affect", "self_emotion"):
            seg = segments.get(key) or {}
            if not isinstance(seg, dict) or not seg:
                continue
            parts.append(
                f"- {key} / {labels[key]}: "
                f"started_at={seg.get('started_at', '')}, "
                f"duration={_format_age(seg.get('duration_seconds'))}, "
                f"ticks={seg.get('tick_count', 0)}"
            )
            text = seg.get("text") or seg.get("reason") or seg.get("source") or ""
            if text:
                parts.append(f"  text={text}")
            if seg.get("last_seen_summary"):
                parts.append(f"  last_seen_summary={seg.get('last_seen_summary')}")

    episode = snapshot.get("current_focus") or snapshot.get("current_episode") or {}
    if episode and not segments:
        parts.append(f"\n【当前 Attention Focus】")
        parts.append(f"- topic_key: {episode.get('topic_key', '') or '(none)'}")
        parts.append(f"- state: {episode.get('state', '') or '?'}")
        if episode.get("started_at"):
            parts.append(f"- started_at: {episode.get('started_at')}")
        parts.append(f"- tick_count: {episode.get('tick_count', 0)}")
        parts.append(f"- speak_count: {episode.get('speak_count', 0)}")
        if episode.get("last_thought"):
            parts.append(f"- last_thought: {episode.get('last_thought')}")

    if snapshot.get("identity_facts"):
        parts.append(f"\n【关于{user_label}的权威身份信息】\n{snapshot['identity_facts']}")
    if snapshot.get("human"):
        parts.append(f"\n【{name} 对{user_label}的整体了解】\n{snapshot['human']}")
    if snapshot.get("persona"):
        parts.append(f"\n【{name} 与{user_label}的关系叙事】\n{snapshot['persona']}")

    current_user_affect = snapshot.get("current_user_affect") or {}
    if current_user_affect:
        parts.append("\n【当前用户情绪状态（旧状态，供你更新）】")
        parts.append(
            f"- mood={current_user_affect.get('mood', '?')} "
            f"valence={current_user_affect.get('valence', '?')} "
            f"arousal/intensity={current_user_affect.get('arousal', current_user_affect.get('intensity', '?'))}"
        )
        if current_user_affect.get("source"):
            parts.append(f"- source={current_user_affect.get('source')}")

    miru_state = snapshot.get("miru_emotion") or {}
    if miru_state:
        parts.append(f"\n【{name} 当前自己的心情（旧状态，供你更新）】")
        parts.append(
            f"- mood={miru_state.get('mood', '?')} "
            f"valence={miru_state.get('valence', '?')} "
            f"arousal={miru_state.get('arousal', '?')}"
        )
        if miru_state.get("reason"):
            parts.append(f"- reason={miru_state.get('reason')}")

    conversation = snapshot.get("conversation_window") or []
    if conversation:
        parts.append(f"\n【最近真实对话窗口】（近7天/最多80条，已标注普通回复 / 主动消息 / 用户回应）")
        for msg in conversation:
            marker = msg.get("message_type", "?")
            who = user_label if msg.get("role") == "user" else name
            suffix = ""
            if msg.get("responds_to_proactive"):
                suffix = "  ↳ 回应了上一条主动消息"
            parts.append(
                f"  [{msg.get('time', '?')}] {who} / {marker}: "
                f"{msg.get('text', '')}{suffix}"
            )
    else:
        parts.append(f"\n【最近没有可用对话窗口】")

    cadence = snapshot.get("proactive_cadence") or {}
    if cadence:
        parts.append(f"\n【主动靠近节奏】")
        parts.append(f"- 最近7天主动次数: {cadence.get('proactive_count_7d', 0)}")
        parts.append(f"- 最近24小时主动次数: {cadence.get('proactive_count_24h', 0)}")
        parts.append(f"- 未回应主动消息数: {cadence.get('unanswered_count', 0)}")
        if cadence.get("last_proactive_at"):
            responded = "已回应" if cadence.get("last_proactive_responded") else "未回应"
            parts.append(
                f"- 上次主动: {cadence.get('last_proactive_at')} / {responded} / "
                f"{cadence.get('last_proactive_text', '')}"
            )
        if cadence.get("recent_unanswered"):
            parts.append("- 未回应主动消息摘要:")
            for item in cadence.get("recent_unanswered", [])[:5]:
                parts.append(f"  [{item.get('time', '?')}] {item.get('text', '')}")

    queued = snapshot.get("speak_intent_queue") or []
    if queued:
        parts.append(f"\n【待交付 speak_intent 队列】（已有意图会直接交给主 agent，不能重复制造同类意图）")
        for item in queued:
            parts.append(
                f"  [{item.get('created_at', '?')}] {item.get('priority', 'low')} "
                f"{item.get('topic_key', '')}: "
                f"{item.get('care_motive') or item.get('context_summary', '')}"
            )

    signals = snapshot.get("recent_signals") or []
    if signals:
        parts.append("\n【本次合并的新信号】（最新在前）")
        for sig in signals:
            t = sig.get("time")
            if isinstance(t, datetime):
                t = t.strftime("%H:%M:%S")
            kind = sig.get("kind", "?")
            sal = sig.get("salience", "?")
            if kind == "screenshot":
                obs = _short_text(sig.get("observation"), 180)
                parts.append(
                    f"  [{t}] screenshot sig={sig.get('significance')} "
                    f"salience={sal} device={sig.get('device_name') or sig.get('device_id') or '-'}：{obs}"
                )
            elif kind in {"chat_in", "chat_out"}:
                who = user_label if kind == "chat_in" else name
                parts.append(f"  [{t}] {who}：{_short_text(sig.get('text'), 180)}")
            else:
                parts.append(f"  [{t}] {kind} salience={sal}：{_short_text(sig.get('summary') or sig.get('transition') or sig, 180)}")
    else:
        parts.append("\n【本次没有新信号】")

    previous = snapshot.get("inner_thought_history") or []
    if previous:
        parts.append(f"\n【我最近没有说出口的想法】")
        for item in previous:
            if item.get("channel") != "inner":
                continue
            label = "有 speak_intent" if item.get("speak_intent") else "状态段"
            ts = item.get("updated_at") or item.get("ts") or ""
            text = item.get("text") or item.get("thought") or ""
            duration = item.get("duration_seconds")
            parts.append(f"  [{ts[-8:]}] {label} duration={_format_age(duration)}：{text}")

    if snapshot.get("active_commitments"):
        parts.append("\n【活跃承诺 / DDL】")
        for item in snapshot["active_commitments"]:
            parts.append(f"  - {item}")

    if snapshot.get("emotion_arc"):
        parts.append(f"\n【今天用户情绪轨迹】\n{snapshot['emotion_arc']}")

    parts.append("\n========== 请输出 Attention JSON ==========")
    return "\n".join(parts)


class AttentionEngine:
    """Per-user attention loop.  It thinks and writes state; it does not speak."""

    def __init__(self, user_id: str | None = None, user_data_dir: str | None = None,
                 auto_start_on_signal: bool = False):
        if user_id is None:
            try:
                from flask import g
                user_id = getattr(g, "user_id", "_admin")
            except (RuntimeError, ImportError):
                user_id = "_admin"
        self._user_id = user_id or "_admin"
        if user_data_dir is None and self._user_id != "_admin":
            try:
                import auth as _auth
                user_data_dir = _auth.get_user_data_dir(self._user_id)
            except Exception:
                user_data_dir = None
        self._user_data_dir = user_data_dir

        self._running = False
        self._auto_start_on_signal = bool(auto_start_on_signal)
        self._evicted = False
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._signals: deque[dict] = deque()
        self._signals_lock = threading.Lock()
        self._signal_seq = 0
        self._last_evaluated_signal_seq = 0
        self._schedule_lock = threading.Lock()
        self._next_tick_at: float = 0.0
        self._last_tick_at: float = time.time()
        self._last_signal_digest: str = ""

    def _push_user_context(self):
        if not self._user_data_dir:
            return None
        try:
            import app as _app_mod
            ctx = _app_mod.app.app_context()
            ctx.push()
            from flask import g
            g.user_id = self._user_id
            g.user_data_dir = self._user_data_dir
            g.is_admin = (self._user_id == "_admin")
            return ctx
        except Exception as e:
            print(f"[AttentionEngine] Failed to push context for {self._user_id}: {e}")
            return None

    def start(self):
        thread: threading.Thread | None = None
        while thread is None:
            old_thread: threading.Thread | None = None
            with self._lifecycle_lock:
                if self._running:
                    return
                if self._evicted:
                    return
                if self._user_id == "_admin" or not self._user_data_dir:
                    print(f"[AttentionEngine] Refusing to start for {self._user_id} (no data dir)")
                    return
                if self._thread is not None and self._thread.is_alive():
                    old_thread = self._thread
                    self._stop_event.set()
                    self._wake.set()
                else:
                    self._running = True
                    self._stop_event.clear()
                    self._wake.clear()
                    thread = threading.Thread(target=self._loop, daemon=True)
                    self._thread = thread
                    break

            if old_thread is None:
                continue
            old_thread.join(timeout=1.0)
            with self._lifecycle_lock:
                if self._thread is old_thread and old_thread.is_alive():
                    print(f"[AttentionEngine] Waiting for old loop to stop before restarting {self._user_id}")
                    return
                if self._thread is old_thread:
                    self._thread = None

        thread.start()
        print(f"[AttentionEngine] Started for {self._user_id}")

    def stop(self):
        with self._lifecycle_lock:
            self._stop_event.set()
            self._wake.set()
            self._running = False

    def deactivate(self):
        """Stop this instance and prevent stale references from auto-starting it."""
        with self._lifecycle_lock:
            self._evicted = True
            self._auto_start_on_signal = False
            self._stop_event.set()
            self._wake.set()
            self._running = False

    def notify_signal(self, kind: str, data: dict | None = None,
                      salience: str | None = None):
        self.record_signal(kind, data=data, salience=salience)

    def record_signal(self, kind: str, data: dict | None = None,
                      salience: str | None = None):
        data = data or {}
        sal = _normalize_salience(kind, data, salience)
        if sal == SAL_DROP:
            return False

        now_dt = datetime.now()
        signal = {
            "kind": kind,
            "time": now_dt,
            "salience": sal,
            **data,
        }

        with self._signals_lock:
            self._signal_seq += 1
            signal["_seq"] = self._signal_seq
            merged = self._merge_duplicate_locked(signal)
            if not merged:
                self._signals.append(signal)
                cutoff = now_dt - timedelta(seconds=SIGNAL_BUFFER_AGE)
                while self._signals and self._signals[0]["time"] < cutoff:
                    self._signals.popleft()
                while len(self._signals) > SIGNAL_BUFFER_MAX:
                    self._signals.popleft()

        self._schedule_for_salience(sal)
        self._maybe_start_after_signal()
        return True

    def _maybe_start_after_signal(self):
        if (
            self._auto_start_on_signal
            and not self._running
            and self._user_id != "_admin"
            and self._user_data_dir
        ):
            self.start()

    def add_observation(self, observation: str, *, significance: int = 3,
                        device_id: str = "local", device_name: str = ""):
        return self.record_signal("screenshot", {
            "observation": observation,
            "significance": significance,
            "device_id": device_id,
            "device_name": device_name,
        })

    def _merge_duplicate_locked(self, signal: dict) -> bool:
        if signal.get("kind") != "screenshot":
            return False
        obs = (signal.get("observation") or "").strip()
        if not obs:
            return False
        device_id = signal.get("device_id") or ""
        for prev in reversed(self._signals):
            if prev.get("kind") != "screenshot":
                continue
            if prev.get("device_id") != device_id:
                continue
            if (signal["time"] - prev.get("time", signal["time"])).total_seconds() > 600:
                return False
            if (prev.get("observation") or "").strip() == obs:
                prev["time"] = signal["time"]
                prev["_seq"] = signal.get("_seq", prev.get("_seq", 0))
                prev["duplicate_count"] = int(prev.get("duplicate_count") or 1) + 1
                prev["salience"] = max(prev.get("salience", SAL_WEAK), signal.get("salience", SAL_WEAK),
                                       key={SAL_WEAK: 1, SAL_NORMAL: 2, SAL_STRONG: 3}.get)
                return True
        return False

    def _schedule_for_salience(self, salience: str):
        lo, hi = _DELAY_RANGES.get(salience, _DELAY_RANGES[SAL_NORMAL])
        delay = random.uniform(lo, hi)
        self._schedule_tick(delay)

    def _schedule_tick(self, delay_seconds: float):
        target = time.time() + max(1.0, float(delay_seconds))
        with self._schedule_lock:
            if self._next_tick_at <= 0 or target < self._next_tick_at:
                self._next_tick_at = target
        self._wake.set()

    def _loop(self):
        try:
            while not self._stop_event.is_set():
                with self._schedule_lock:
                    next_tick = self._next_tick_at
                now = time.time()
                heartbeat_at = self._last_tick_at + HEARTBEAT_INTERVAL
                target = next_tick if next_tick > 0 else heartbeat_at
                wait_secs = max(1.0, target - now)

                self._wake.wait(timeout=wait_secs)
                self._wake.clear()
                if self._stop_event.is_set():
                    break

                now = time.time()
                with self._schedule_lock:
                    next_tick = self._next_tick_at
                    if next_tick > 0 and now < next_tick:
                        continue
                    if next_tick > 0:
                        self._next_tick_at = 0
                        trigger = "signals"
                    elif now >= heartbeat_at:
                        trigger = "heartbeat"
                    else:
                        continue

                ctx = self._push_user_context()
                try:
                    self._tick(trigger=trigger)
                except Exception as e:
                    print(f"[AttentionEngine] tick error for {self._user_id}: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    if ctx is not None:
                        try:
                            ctx.pop()
                        except Exception:
                            pass
        finally:
            current = threading.current_thread()
            with self._lifecycle_lock:
                if self._thread is current:
                    self._thread = None
                    self._running = False

    def _tick(self, trigger: str = "signals"):
        self._last_tick_at = time.time()
        snapshot = self._build_snapshot(trigger=trigger)
        self._last_evaluated_signal_seq = max(
            self._last_evaluated_signal_seq,
            int(snapshot.get("trigger_signal_seq_max") or 0),
        )
        digest = self._snapshot_digest(snapshot)
        if trigger != "heartbeat" and digest and digest == self._last_signal_digest:
            return
        self._last_signal_digest = digest

        try:
            raw = self._evaluate(snapshot)
            result = self._normalize_result(raw)
        except Exception as e:
            self._append_error_log(snapshot, str(e))
            return

        self._apply_result(result, snapshot)

    def _build_snapshot(self, trigger: str = "signals") -> dict:
        now = datetime.now()
        snapshot: dict[str, Any] = {"now": now, "trigger": trigger}

        try:
            from sleep_inference import infer_activity_state
            snapshot["activity"] = infer_activity_state() or {}
        except Exception:
            snapshot["activity"] = {}

        history = []
        try:
            import storage as _st
            history = _st.get_chat_history(limit=240) or []
        except Exception:
            history = []
        snapshot["conversation_window"] = self._build_conversation_window(history, now)
        snapshot["proactive_cadence"] = self._build_proactive_cadence(history, now)
        snapshot["today_chat"] = self._format_today_chat(history, now)
        snapshot["recent_proactive_messages"] = self._recent_proactive(history, now)
        snapshot["distances"] = self._message_distances(history, now)

        try:
            import storage as _st
            state = _st.load_attention_state() or {}
            segments = state.get("current_segments") or {}
            if not segments:
                segments = {
                    "inner": state.get("current_inner") or {},
                    "user_affect": state.get("current_user_affect") or {},
                    "self_emotion": state.get("current_self_emotion") or {},
                }
            snapshot["current_segments"] = segments
            focus = state.get("current_inner") or {}
            snapshot["current_focus"] = focus
            snapshot["current_episode"] = focus
            snapshot["speak_intent_queue"] = self._queue_summary(
                _st.load_attention_intent_queue(), now
            )
        except Exception:
            snapshot["current_segments"] = {}
            snapshot["current_focus"] = {}
            snapshot["current_episode"] = {}
            snapshot["speak_intent_queue"] = []

        with self._signals_lock:
            cutoff = now - timedelta(seconds=SIGNAL_BUFFER_AGE)
            recent = [dict(s) for s in self._signals if s.get("time", now) >= cutoff]
            new_signals = [
                dict(s) for s in recent
                if int(s.get("_seq") or 0) > self._last_evaluated_signal_seq
            ]
            trigger_signal_seq_max = self._signal_seq
        recent.reverse()
        snapshot["recent_signals"] = recent[:40]
        snapshot["trigger_signal_kinds"] = sorted({
            str(s.get("kind") or "") for s in new_signals if s.get("kind")
        })
        snapshot["trigger_signal_seq_max"] = trigger_signal_seq_max

        try:
            import storage as _st
            snapshot["inner_thought_history"] = _st.get_recent_attention_log(limit=30)
        except Exception:
            snapshot["inner_thought_history"] = []

        try:
            import storage as _st
            today_log = _st.get_today_emotion_log() or []
            snapshot["emotion_arc"] = self._build_emotion_arc(today_log, now)
            if today_log:
                last = today_log[-1]
                snapshot["current_user_affect"] = {
                    "mood": last.get("mood", "neutral"),
                    "valence": last.get("valence", 0),
                    "arousal": last.get("arousal", last.get("intensity", 0.2)),
                    "source": last.get("text") or last.get("source") or last.get("trigger") or "",
                }
        except Exception:
            pass

        try:
            import miru_emotion
            snapshot["miru_emotion"] = miru_emotion.get_instance().get_state()
        except Exception:
            snapshot["miru_emotion"] = {}

        if self._account_manifest_valid():
            try:
                import core_memory
                blocks = core_memory.get_all_blocks() or {}
                if (blocks.get("human") or "").strip():
                    snapshot["human"] = blocks.get("human", "").strip()
                if (blocks.get("persona") or "").strip():
                    snapshot["persona"] = blocks.get("persona", "").strip()
            except Exception:
                pass

        try:
            import identity
            facts = identity.compose_ground_truth_block(header=False)
            if facts:
                snapshot["identity_facts"] = facts
        except Exception:
            pass

        snapshot["active_commitments"] = self._format_relevant_commitments(now)

        return snapshot

    def _account_manifest_valid(self) -> bool:
        if not self._user_id or self._user_id == "_admin" or not self._user_data_dir:
            return False

        manifest_path = os.path.join(self._user_data_dir, "account_manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("user_id") == self._user_id:
                    return True
            except Exception:
                return False

        try:
            import auth as _auth
            return bool(_auth.validate_account_manifest(self._user_id))
        except Exception:
            return False

    def _format_relevant_commitments(self, now: datetime, limit: int = 8) -> list[str]:
        try:
            from core import parse_commitments
        except Exception:
            return []

        urgency_label = {
            "overdue": "逾期",
            "imminent": "2小时内",
            "today": "今天截止",
            "approaching": "6小时内",
            "soon": "3天内",
        }
        active = []
        try:
            items = parse_commitments(include_done=False) or []
        except Exception:
            return []

        for item in items:
            urgency = item.get("urgency")
            tag = urgency_label.get(urgency)
            if not tag:
                continue
            deadline = str(item.get("deadline") or "").strip()
            deadline_dt = _parse_local_dt(deadline)
            if not deadline_dt:
                continue
            if urgency == "overdue" and (now - deadline_dt).total_seconds() > 7 * 86400:
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            active.append((deadline_dt, f"{tag}: {title} ({deadline})"))

        active.sort(key=lambda pair: pair[0])
        return [text for _dt, text in active[:limit]]

    def _message_text(self, msg: dict, limit: int = 260) -> str:
        text = (msg.get("text") or "").strip()
        if msg.get("image"):
            desc = (msg.get("image_desc") or "").strip()
            img = f"[图片：{desc}]" if desc else "[图片]"
            text = (text + " " + img).strip()
        return _short_text(text, limit)

    def _message_type(self, msg: dict) -> str:
        if msg.get("role") == "user":
            return "user"
        msg_type = str(msg.get("type") or "").strip()
        if msg_type in {"proactive", "proactive_care", "care"}:
            return "proactive"
        return "reply"

    def _build_conversation_window(self, history: list[dict], now: datetime,
                                   limit: int = CONVERSATION_WINDOW_LIMIT) -> list[dict]:
        """Recent real conversation with explicit proactive/care labels.

        This is the primary conversation surface for AttentionEngine. It is
        bounded by a week + count, not a hard "last 48h" rule, so it captures
        a meaningful recent surface while avoiding month-old prompt pollution.
        """
        out = []
        last_unanswered_proactive_id = ""
        cutoff = now - timedelta(days=CONVERSATION_WINDOW_DAYS)
        for msg in history:
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if not text:
                continue
            ts = msg.get("time", "")
            dt = _parse_local_dt(ts)
            if dt and dt < cutoff:
                continue
            mtype = self._message_type(msg)
            responds_to_proactive = False
            if role == "assistant" and mtype == "proactive":
                last_unanswered_proactive_id = msg.get("id") or ts or text[:32]
            elif role == "user" and last_unanswered_proactive_id:
                responds_to_proactive = True
                last_unanswered_proactive_id = ""
            out.append({
                "id": msg.get("id", ""),
                "time": ts,
                "age_seconds": (now - dt).total_seconds() if dt else None,
                "role": role,
                "message_type": mtype,
                "device_id": msg.get("device_id", ""),
                "text": text,
                "responds_to_proactive": responds_to_proactive,
            })
        return out[-limit:]

    def _build_proactive_cadence(self, history: list[dict], now: datetime) -> dict:
        """Summarize interruption rhythm separately from conversation content."""
        cutoff_7d = now - timedelta(days=PROACTIVE_CADENCE_DAYS)
        cutoff_24h = now - timedelta(hours=24)
        proactive_items = []
        last_user_dt = None
        for idx, msg in enumerate(history):
            dt = _parse_local_dt(msg.get("time", ""))
            if not dt:
                continue
            if msg.get("role") == "user":
                last_user_dt = dt
                continue
            if msg.get("role") != "assistant" or self._message_type(msg) != "proactive":
                continue
            if dt < cutoff_7d:
                continue
            responded = any(
                m.get("role") == "user"
                and (_parse_local_dt(m.get("time", "")) or now) > dt
                for m in history[idx + 1:]
            )
            proactive_items.append({
                "time": msg.get("time", ""),
                "dt": dt,
                "text": self._message_text(msg, limit=160),
                "responded": responded,
            })

        last = proactive_items[-1] if proactive_items else None
        recent_unanswered = [
            {"time": item["time"], "text": item["text"]}
            for item in proactive_items if not item["responded"]
        ][-5:]
        return {
            "window_days": PROACTIVE_CADENCE_DAYS,
            "proactive_count_7d": len(proactive_items),
            "proactive_count_24h": sum(1 for item in proactive_items if item["dt"] >= cutoff_24h),
            "unanswered_count": sum(1 for item in proactive_items if not item["responded"]),
            "last_proactive_at": last["time"] if last else "",
            "last_proactive_age_seconds": (now - last["dt"]).total_seconds() if last else None,
            "last_proactive_text": last["text"] if last else "",
            "last_proactive_responded": bool(last and last["responded"]),
            "last_user_after_proactive_at": last_user_dt.strftime("%Y-%m-%d %H:%M:%S") if last_user_dt else "",
            "recent_unanswered": recent_unanswered,
        }

    def _queue_summary(self, queue: list[dict], now: datetime, limit: int = 8) -> list[dict]:
        out = []
        for item in queue or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "pending":
                continue
            expires = _parse_local_dt(item.get("expires_at"))
            if expires and expires < now:
                continue
            out.append({
                "id": item.get("id", ""),
                "created_at": item.get("created_at", ""),
                "priority": item.get("priority", "low"),
                "topic_key": item.get("topic_key", ""),
                "context_summary": item.get("context_summary", ""),
                "why_now": item.get("why_now", ""),
                "care_motive": item.get("care_motive", ""),
                "emotional_source": item.get("emotional_source", ""),
                "user_need": item.get("user_need", ""),
                "approach": item.get("approach", ""),
                "content_anchor": item.get("content_anchor", ""),
                "miru_impulse": item.get("miru_impulse", ""),
                "message_seed": item.get("message_seed", ""),
            })
        return out[-limit:]

    def _format_today_chat(self, history: list[dict], now: datetime) -> list[dict]:
        today = now.strftime("%Y-%m-%d")
        try:
            from character import get_config
            cfg = get_config()
            name = cfg.name or "Miru"
        except Exception:
            name = "Miru"
        user_label = _user_entity_label()
        out = []
        for msg in history:
            ts = msg.get("time", "")
            if not ts.startswith(today):
                continue
            role = msg.get("role", "")
            if role == "user":
                label = user_label
            elif role == "assistant":
                label = f"{name}主动" if msg.get("type") in {"proactive", "proactive_care", "care"} else name
            else:
                continue
            text = (msg.get("text") or "").strip()
            if msg.get("image"):
                desc = (msg.get("image_desc") or "").strip()
                img = f"[图片：{desc}]" if desc else "[图片]"
                text = (text + " " + img).strip()
            if not text:
                continue
            out.append({"time": ts[11:16] if len(ts) >= 16 else ts, "role": label, "text": text[:240]})
        return out[-80:]

    def _recent_proactive(self, history: list[dict], now: datetime) -> list[dict]:
        out = []
        cutoff = now - timedelta(days=PROACTIVE_CADENCE_DAYS)
        for idx, msg in enumerate(history):
            if msg.get("role") != "assistant":
                continue
            if msg.get("type") not in {"proactive", "proactive_care", "care"}:
                continue
            ts = msg.get("time", "")
            dt = _parse_local_dt(ts)
            if not dt or dt < cutoff:
                continue
            responded = any(m.get("role") == "user" for m in history[idx + 1:])
            out.append({
                "time": ts[11:16] if len(ts) >= 16 else ts,
                "text": (msg.get("text") or "")[:160],
                "responded": responded,
            })
        return out[-8:]

    def _message_distances(self, history: list[dict], now: datetime) -> dict:
        result = {
            "since_user_msg": None,
            "since_assistant_msg": None,
            "since_proactive_msg": None,
        }
        for msg in reversed(history):
            ts = msg.get("time", "")
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            delta = (now - dt).total_seconds()
            if msg.get("role") == "user" and result["since_user_msg"] is None:
                result["since_user_msg"] = delta
            if msg.get("role") == "assistant" and result["since_assistant_msg"] is None:
                result["since_assistant_msg"] = delta
            if (
                msg.get("role") == "assistant"
                and msg.get("type") in {"proactive", "proactive_care", "care"}
                and result["since_proactive_msg"] is None
            ):
                result["since_proactive_msg"] = delta
        return result

    def _build_emotion_arc(self, entries: list[dict], now: datetime) -> str:
        if not entries:
            return ""
        buckets: dict[int, list[dict]] = {}
        for entry in entries:
            ts = entry.get("timestamp", "")
            try:
                hour = int(ts[11:13])
            except (TypeError, ValueError):
                continue
            buckets.setdefault(hour // 3, []).append(entry)
        names = {0: "00-03", 1: "03-06", 2: "06-09", 3: "09-12",
                 4: "12-15", 5: "15-18", 6: "18-21", 7: "21-24"}
        lines = []
        for slot in sorted(buckets):
            es = buckets[slot]
            vals = [float(e.get("valence", 0) or 0) for e in es]
            moods = [e.get("mood", "") for e in es if e.get("mood")]
            dom = max(set(moods), key=moods.count) if moods else "?"
            lines.append(f"{names.get(slot, str(slot))}: avg valence {sum(vals)/len(vals):+.2f}, mood={dom} (n={len(es)})")
        cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        recent = [float(e.get("valence", 0) or 0) for e in entries if (e.get("timestamp", "") or "") >= cutoff]
        if recent:
            lines.append(f"最近30min: avg valence {sum(recent)/len(recent):+.2f} (n={len(recent)})")
        return "\n".join(lines)

    def _snapshot_digest(self, snapshot: dict) -> str:
        sigs = snapshot.get("recent_signals") or []
        if not sigs:
            return ""
        latest = sigs[:8]
        parts = []
        for s in latest:
            t = s.get("time")
            ts = t.isoformat(timespec="seconds") if isinstance(t, datetime) else str(t)
            parts.append("|".join([
                str(s.get("kind", "")),
                str(s.get("salience", "")),
                str(s.get("device_id", "")),
                str(s.get("significance", "")),
                _short_text(s.get("observation") or s.get("text") or "", 80),
                ts,
            ]))
        return "\n".join(parts)

    def _evaluate(self, snapshot: dict) -> dict:
        from prompt import _call_llm_json
        return _call_llm_json(
            _build_attention_system_prompt(),
            build_attention_snapshot_prompt(snapshot),
            temperature=0.45,
            max_tokens=12000,
            tier="memory",
            reasoning=False,
            call_label="AttentionEngineEvaluate",
        )

    def _normalize_action(self, value: Any) -> str:
        action = str(value or "").strip().lower()
        return action if action in {"continue", "shift"} else "shift"

    def _normalize_result(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("attention LLM returned non-object")

        # New schema: inner / user_affect / self_emotion.
        # Legacy LLM/test payloads with thought / miru_inner / attention are
        # accepted and mapped to shift segments so stale model output degrades
        # cleanly instead of dropping state.
        inner_raw = raw.get("inner")
        if not isinstance(inner_raw, dict):
            attention_raw = raw.get("attention") if isinstance(raw.get("attention"), dict) else {}
            inner_raw = {
                "action": "shift",
                "text": raw.get("thought") or "",
                "topic_key": attention_raw.get("topic_key"),
                "state": attention_raw.get("state"),
                "novelty": attention_raw.get("novelty"),
                "concern_level": attention_raw.get("concern_level"),
                "next_check_seconds": attention_raw.get("next_check_seconds"),
                "change_reason": "legacy attention payload",
            }
        user_affect_raw = raw.get("user_affect") if isinstance(raw.get("user_affect"), dict) else {}
        self_raw = raw.get("self_emotion")
        if not isinstance(self_raw, dict):
            legacy_inner = raw.get("miru_inner") if isinstance(raw.get("miru_inner"), dict) else {}
            self_raw = {
                "action": "shift",
                "text": legacy_inner.get("text") or legacy_inner.get("reason") or legacy_inner.get("thought") or "",
                "mood": legacy_inner.get("mood"),
                "valence": legacy_inner.get("valence"),
                "arousal": legacy_inner.get("arousal"),
                "reason": legacy_inner.get("reason") or legacy_inner.get("thought"),
                "change_reason": "legacy miru_inner payload",
            }

        state = str(inner_raw.get("state") or "observing").strip()
        if state not in _STATE_KEYS:
            state = "observing"

        trend = str(user_affect_raw.get("trend") or "uncertain").strip().lower()
        if trend not in {"rising", "falling", "stable", "uncertain"}:
            trend = "uncertain"

        inner = {
            "action": self._normalize_action(inner_raw.get("action")),
            "text": _short_text(inner_raw.get("text") or inner_raw.get("current_text"), SEGMENT_TEXT_LIMIT),
            "continue_summary": _short_text(inner_raw.get("continue_summary"), 160),
            "topic_key": _short_text(inner_raw.get("topic_key"), 80),
            "state": state,
            "novelty": round(_clamp_float(inner_raw.get("novelty"), 0.0, 0.0, 1.0), 3),
            "concern_level": round(_clamp_float(inner_raw.get("concern_level"), 0.0, 0.0, 1.0), 3),
            "next_check_seconds": _clamp_int(inner_raw.get("next_check_seconds"), HEARTBEAT_INTERVAL, 60, 900),
            "change_reason": _short_text(inner_raw.get("change_reason") or inner_raw.get("reason"), 160),
        }

        user_affect = {
            "action": self._normalize_action(user_affect_raw.get("action")),
            "text": _short_text(user_affect_raw.get("text"), SEGMENT_TEXT_LIMIT),
            "continue_summary": _short_text(user_affect_raw.get("continue_summary"), 160),
            "mood": _short_text(user_affect_raw.get("mood") or "neutral", 60),
            "valence": round(_clamp_float(user_affect_raw.get("valence"), 0.0, -1.0, 1.0), 3),
            "arousal": round(_clamp_float(user_affect_raw.get("arousal"), 0.2, 0.0, 1.0), 3),
            "confidence": round(_clamp_float(user_affect_raw.get("confidence"), 0.0, 0.0, 1.0), 3),
            "trend": trend,
            "evidence": _short_text(user_affect_raw.get("evidence"), 160),
            "change_reason": _short_text(user_affect_raw.get("change_reason") or user_affect_raw.get("reason"), 160),
        }

        self_emotion = {
            "action": self._normalize_action(self_raw.get("action")),
            "text": _short_text(self_raw.get("text"), SEGMENT_TEXT_LIMIT),
            "continue_summary": _short_text(self_raw.get("continue_summary"), 160),
            "mood": _short_text(self_raw.get("mood") or "平静", 60),
            "valence": round(_clamp_float(self_raw.get("valence"), 0.0, -1.0, 1.0), 3),
            "arousal": round(_clamp_float(self_raw.get("arousal"), 0.2, 0.0, 1.0), 3),
            "reason": _short_text(self_raw.get("reason") or self_raw.get("thought"), 160),
            "change_reason": _short_text(self_raw.get("change_reason"), 160),
        }

        intent = raw.get("speak_intent")
        if not isinstance(intent, dict):
            intent = None
        else:
            priority = str(intent.get("priority") or "low").strip().lower()
            if priority not in {"low", "medium", "high"}:
                priority = "low"
            approach = str(intent.get("approach") or "warm_presence").strip().lower()
            if approach not in {
                "co_watch", "playful_react", "soft_care", "celebrate",
                "life_rhythm", "deep_work", "warm_presence",
            }:
                approach = "warm_presence"
            why_now = (
                intent.get("why_i_want_to_say")
                or intent.get("why_now")
                or intent.get("care_motive")
                or ""
            )
            silent_boundaries = (
                intent.get("silent_boundaries")
                or intent.get("boundaries")
                or intent.get("avoid")
                or ""
            )
            intent = {
                "priority": priority,
                "topic_key": _short_text(intent.get("topic_key"), 80),
                "care_motive": _short_text(intent.get("care_motive"), 160),
                "emotional_source": _short_text(intent.get("emotional_source"), 160),
                "user_need": _short_text(intent.get("user_need"), 160),
                "why_i_want_to_say": _short_text(why_now, 160),
                # Keep why_now as an alias for delivery/debug readers.
                "why_now": _short_text(why_now, 160),
                "approach": approach,
                "content_anchor": _short_text(intent.get("content_anchor"), 160),
                "miru_impulse": _short_text(intent.get("miru_impulse"), 160),
                "suggested_tone": _short_text(intent.get("suggested_tone"), 100),
                # Compatibility alias: older callers/tests still read avoid.
                "avoid": _short_text(silent_boundaries, 160),
                "silent_boundaries": _short_text(silent_boundaries, 160),
                "message_seed": _short_text(intent.get("message_seed"), 160),
                "context_summary": _short_text(intent.get("context_summary"), 240),
            }
            if not any(intent.get(k) for k in (
                "care_motive", "why_i_want_to_say", "context_summary", "message_seed", "topic_key"
            )):
                intent = None

        # Compatibility aliases used by delivery/debug and older tests.
        return {
            "inner": inner,
            "user_affect": user_affect,
            "self_emotion": self_emotion,
            "thought": inner["text"],
            "miru_inner": {
                "mood": self_emotion["mood"],
                "valence": self_emotion["valence"],
                "arousal": self_emotion["arousal"],
                "reason": self_emotion["text"] or self_emotion["reason"],
            },
            "attention": {
                "state": inner["state"],
                "topic_key": inner["topic_key"],
                "novelty": inner["novelty"],
                "concern_level": inner["concern_level"],
                "next_check_seconds": inner["next_check_seconds"],
            },
            "speak_intent": intent,
        }

    def _priority_rank(self, priority: str) -> int:
        return {"low": 1, "medium": 2, "high": 3}.get(priority, 1)

    def _intent_expiry(self, created: datetime, priority: str) -> datetime:
        minutes = {"low": 30, "medium": 75, "high": 150}.get(priority, 30)
        return created + timedelta(minutes=minutes)

    def _make_segment_id(self, now: datetime, channel: str, topic_key: str = "") -> str:
        base = f"{channel}:{now.strftime('%Y%m%d%H%M%S')}:{topic_key or 'general'}:{random.random()}"
        suffix = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"{channel}_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"

    def _segment_started_dt(self, segment: dict, fallback: datetime) -> datetime:
        return (
            _parse_local_dt(segment.get("started_at"))
            or _parse_local_dt(segment.get("timestamp"))
            or fallback
        )

    def _duration_seconds(self, started_at: datetime, now: datetime) -> int:
        return max(0, int((now - started_at).total_seconds()))

    def _legacy_segment_from_state(self, state: dict, channel: str) -> dict:
        if channel == "inner":
            return state.get("current_inner") or {}
        if channel == "user_affect":
            return state.get("current_user_affect") or {}
        if channel == "self_emotion":
            return state.get("current_self_emotion") or {}
        return {}

    def _apply_segment_update(self, channel: str, update: dict,
                              previous: dict, now: datetime,
                              trigger: str) -> tuple[dict, dict | None, bool]:
        """Return (current_segment, closed_previous, created_new)."""
        previous = previous if isinstance(previous, dict) else {}
        action = update.get("action") or "shift"
        can_continue = action == "continue" and previous.get("id")
        started_dt = self._segment_started_dt(previous, now) if can_continue else now
        now_s = now.strftime("%Y-%m-%d %H:%M:%S")

        if can_continue:
            seg = dict(previous)
            seg.update({
                "channel": channel,
                "updated_at": now_s,
                "ended_at": None,
                "duration_seconds": self._duration_seconds(started_dt, now),
                "tick_count": int(seg.get("tick_count") or 0) + 1,
                "last_seen_summary": update.get("continue_summary") or update.get("last_seen_summary") or seg.get("last_seen_summary", ""),
                "last_trigger": trigger,
                "last_action": "continue",
            })
            # Carry continuously refreshed numeric fields without changing the
            # segment text. This keeps current state accurate while avoiding
            # new log prose.
            for key in (
                "mood", "valence", "arousal", "confidence", "trend",
                "evidence", "topic_key", "state", "novelty",
                "concern_level", "next_check_seconds", "reason",
            ):
                if key in update and update.get(key) not in (None, ""):
                    seg[key] = update.get(key)
            return seg, None, False

        closed = None
        if previous.get("id"):
            closed = dict(previous)
            closed_started = self._segment_started_dt(closed, now)
            closed.update({
                "updated_at": closed.get("updated_at") or now_s,
                "ended_at": now_s,
                "duration_seconds": self._duration_seconds(closed_started, now),
            })

        topic_key = update.get("topic_key") or previous.get("topic_key") or ""
        text = (
            update.get("text")
            or update.get("reason")
            or update.get("evidence")
            or update.get("continue_summary")
            or previous.get("text")
            or ""
        )
        seg = {
            "id": self._make_segment_id(now, channel, topic_key),
            "channel": channel,
            "started_at": now_s,
            "updated_at": now_s,
            "ended_at": None,
            "duration_seconds": 0,
            "tick_count": 1,
            "text": _short_text(text, SEGMENT_TEXT_LIMIT),
            "change_reason": update.get("change_reason") or "",
            "last_seen_summary": update.get("continue_summary") or "",
            "last_trigger": trigger,
            "last_action": "shift",
        }
        for key in (
            "mood", "valence", "arousal", "confidence", "trend",
            "evidence", "topic_key", "state", "novelty",
            "concern_level", "next_check_seconds", "reason",
        ):
            if key in update and update.get(key) not in (None, ""):
                seg[key] = update.get(key)
        return seg, closed, True

    def _apply_segments(self, result: dict, snapshot: dict,
                        previous_state: dict) -> tuple[dict, list[dict]]:
        now = snapshot.get("now") or datetime.now()
        trigger = snapshot.get("trigger", "signals")
        updates = {
            "inner": result.get("inner") or {},
            "user_affect": result.get("user_affect") or {},
            "self_emotion": result.get("self_emotion") or {},
        }
        current_segments = {}
        log_updates: list[dict] = []
        for channel in ("inner", "user_affect", "self_emotion"):
            previous = self._legacy_segment_from_state(previous_state, channel)
            current, closed, _created = self._apply_segment_update(
                channel, updates[channel], previous, now, trigger
            )
            if closed:
                log_updates.append(closed)
            log_updates.append(current)
            current_segments[channel] = current
        return current_segments, log_updates

    def _make_episode_id(self, now: datetime, topic_key: str) -> str:
        base = f"{now.strftime('%Y%m%d%H%M%S')}:{topic_key or 'general'}"
        suffix = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"ep_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"

    def _update_episode(self, result: dict, snapshot: dict) -> dict:
        now = snapshot.get("now") or datetime.now()
        attention = result.get("attention") or {}
        intent = result.get("speak_intent")
        topic = (
            (attention.get("topic_key") or "").strip()
            or ((intent or {}).get("topic_key") or "").strip()
            or "general"
        )
        previous = snapshot.get("current_focus") or snapshot.get("current_episode") or {}
        previous_topic = (previous.get("topic_key") or "").strip()
        previous_updated = _parse_local_dt(previous.get("updated_at"))
        same_episode = (
            previous
            and previous_topic == topic
            and previous_updated
            and (now - previous_updated).total_seconds() <= 6 * 3600
        )

        if same_episode:
            episode = dict(previous)
            episode["tick_count"] = int(episode.get("tick_count") or 0) + 1
        else:
            episode = {
                "id": self._make_episode_id(now, topic),
                "topic_key": topic,
                "started_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "tick_count": 1,
                "speak_count": 0,
            }

        episode.update({
            "topic_key": topic,
            "state": attention.get("state") or "observing",
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_thought": result.get("thought", ""),
            "last_user_affect": result.get("user_affect", {}),
            "last_miru_inner": result.get("miru_inner", {}),
            "last_trigger": snapshot.get("trigger", "signals"),
            "last_signal_count": len(snapshot.get("recent_signals") or []),
        })
        if intent:
            episode["speak_count"] = int(episode.get("speak_count") or 0) + 1
            episode["last_speak_intent_at"] = episode["updated_at"]
        return episode

    def _enqueue_speak_intent(self, intent: dict | None, result: dict,
                              snapshot: dict, episode: dict) -> list[dict]:
        try:
            import storage as _st
            queue = _st.load_attention_intent_queue()
        except Exception:
            return []

        now = snapshot.get("now") or datetime.now()
        now_s = now.strftime("%Y-%m-%d %H:%M:%S")
        kept = []
        for item in queue:
            if not isinstance(item, dict):
                continue
            expires = _parse_local_dt(item.get("expires_at"))
            if item.get("status") == "pending" and expires and expires < now:
                item = {**item, "status": "expired", "expired_at": now_s}
            kept.append(item)
        queue = kept[-INTENT_QUEUE_LIMIT:]

        if not intent:
            try:
                _st.save_attention_intent_queue(queue)
            except Exception:
                pass
            return queue

        topic = (intent.get("topic_key") or episode.get("topic_key") or "general").strip()
        priority = intent.get("priority") or "low"
        existing = None
        for item in reversed(queue):
            if item.get("status") != "pending":
                continue
            if (item.get("topic_key") or "") != topic:
                continue
            created = _parse_local_dt(item.get("created_at"))
            if created and (now - created).total_seconds() <= INTENT_DEDUP_SECONDS:
                existing = item
                break

        if existing:
            existing["updated_at"] = now_s
            existing["repeat_count"] = int(existing.get("repeat_count") or 1) + 1
            existing["why_now"] = intent.get("why_now", "")
            existing["care_motive"] = intent.get("care_motive", "")
            existing["emotional_source"] = intent.get("emotional_source", "")
            existing["user_need"] = intent.get("user_need", "")
            existing["why_i_want_to_say"] = intent.get("why_i_want_to_say", "")
            existing["approach"] = intent.get("approach", "")
            existing["content_anchor"] = intent.get("content_anchor", "")
            existing["miru_impulse"] = intent.get("miru_impulse", "")
            existing["message_seed"] = intent.get("message_seed", "")
            existing["suggested_tone"] = intent.get("suggested_tone", "")
            existing["avoid"] = intent.get("avoid", "")
            existing["silent_boundaries"] = intent.get("silent_boundaries", intent.get("avoid", ""))
            existing["context_summary"] = intent.get("context_summary", "")
            if self._priority_rank(priority) > self._priority_rank(existing.get("priority", "low")):
                existing["priority"] = priority
                existing["expires_at"] = self._intent_expiry(now, priority).strftime("%Y-%m-%d %H:%M:%S")
        else:
            raw_id = f"{now_s}:{topic}:{intent.get('context_summary', '')}"
            queue.append({
                "id": "intent_" + hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:12],
                "status": "pending",
                "delivery_status": "queued",
                "created_at": now_s,
                "updated_at": now_s,
                "expires_at": self._intent_expiry(now, priority).strftime("%Y-%m-%d %H:%M:%S"),
                "priority": priority,
                "topic_key": topic,
                "why_now": intent.get("why_now", ""),
                "care_motive": intent.get("care_motive", ""),
                "emotional_source": intent.get("emotional_source", ""),
                "user_need": intent.get("user_need", ""),
                "why_i_want_to_say": intent.get("why_i_want_to_say", ""),
                "approach": intent.get("approach", ""),
                "content_anchor": intent.get("content_anchor", ""),
                "miru_impulse": intent.get("miru_impulse", ""),
                "suggested_tone": intent.get("suggested_tone", ""),
                "avoid": intent.get("avoid", ""),
                "silent_boundaries": intent.get("silent_boundaries", intent.get("avoid", "")),
                "message_seed": intent.get("message_seed", ""),
                "context_summary": intent.get("context_summary", ""),
                "episode_id": episode.get("id", ""),
                "trigger": snapshot.get("trigger", "signals"),
                "repeat_count": 1,
            })
            queue = queue[-INTENT_QUEUE_LIMIT:]

        try:
            _st.save_attention_intent_queue(queue)
        except Exception as e:
            print(f"[AttentionEngine] speak_intent queue write failed: {e}")
        return queue

    def _speak_intent_suppression_reason(self, intent: dict | None,
                                         snapshot: dict) -> str:
        """Return the cadence rule that blocks a newly proposed intent."""
        if not intent:
            return ""

        trigger_kinds_present = "trigger_signal_kinds" in snapshot
        trigger_kinds = {
            str(kind).strip()
            for kind in (snapshot.get("trigger_signal_kinds") or [])
            if str(kind).strip()
        }
        if (
            snapshot.get("trigger", "signals") == "signals"
            and trigger_kinds_present
            and trigger_kinds
            and trigger_kinds.issubset(CHAT_SIGNAL_KINDS)
        ):
            return "reactive_chat_only"

        since_user_msg = (snapshot.get("distances") or {}).get("since_user_msg")
        try:
            since_user_msg = float(since_user_msg)
        except (TypeError, ValueError):
            since_user_msg = None
        if (
            since_user_msg is not None
            and 0 <= since_user_msg < ACTIVE_CHAT_PROACTIVE_SILENCE_SECONDS
        ):
            return "recent_user_message"

        try:
            import storage as _st
            queue = _st.load_attention_intent_queue() or []
        except Exception:
            queue = []

        topic = str(intent.get("topic_key") or "general").strip()
        same_pending_ids = {
            str(item.get("id") or "")
            for item in queue
            if isinstance(item, dict)
            and item.get("status") == "pending"
            and str(item.get("topic_key") or "").strip() == topic
        }

        now = snapshot.get("now") or datetime.now()
        for item in reversed(queue):
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") in same_pending_ids:
                continue
            if item.get("status") not in {"pending", "delivered"}:
                continue
            accepted_at = (
                _parse_local_dt(item.get("created_at"))
                or _parse_local_dt(item.get("delivered_at"))
            )
            if not accepted_at:
                continue
            age_seconds = (now - accepted_at).total_seconds()
            if 0 <= age_seconds < PROACTIVE_MIN_INTERVAL_SECONDS:
                return "recent_accepted_intent"
        return ""

    def _find_pending_intent_id(self, intent: dict | None, queue: list[dict],
                                episode: dict) -> str:
        if not intent:
            return ""
        topic = (intent.get("topic_key") or episode.get("topic_key") or "general").strip()
        best = None
        for item in queue or []:
            if not isinstance(item, dict) or item.get("status") != "pending":
                continue
            if (item.get("topic_key") or "") != topic:
                continue
            best = item
        return (best or {}).get("id", "")

    def _should_run_delivery_attempt(self, intent_id: str,
                                     now: datetime) -> bool:
        if not intent_id:
            return False
        try:
            import storage as _st
            recent = _st.get_recent_attention_delivery_log(limit=30)
        except Exception:
            return True
        for entry in reversed(recent or []):
            if not isinstance(entry, dict) or entry.get("intent_id") != intent_id:
                continue
            ts = _parse_local_dt(entry.get("ts"))
            if ts and (now - ts).total_seconds() < DELIVERY_PREFLIGHT_DRYRUN_COOLDOWN:
                return False
            return True
        return True

    def _schedule_delivery_if_needed(self, intent: dict | None,
                                     queue: list[dict],
                                     episode: dict,
                                     now: datetime):
        """Schedule direct delivery for the pending intent.

        Production default runs the live proactive main-agent path. Set
        MIRU_ATTENTION_DELIVERY_DRYRUN=1 to log the direct delivery plan
        without sending.
        """
        if (
            os.environ.get("PYTEST_CURRENT_TEST")
            and os.environ.get("MIRU_ENABLE_ATTENTION_DELIVERY_DRYRUN_IN_TESTS") != "1"
        ):
            return
        intent_id = self._find_pending_intent_id(intent, queue, episode)
        if not self._should_run_delivery_attempt(intent_id, now):
            return
        thread = threading.Thread(
            target=self._run_delivery,
            args=(intent_id,),
            daemon=True,
        )
        thread.start()

    def _run_delivery(self, intent_id: str):
        ctx = self._push_user_context()
        try:
            import core
            dryrun = os.environ.get("MIRU_ATTENTION_DELIVERY_DRYRUN", "").lower()
            if dryrun in {"1", "true", "yes"}:
                core.evaluate_attention_delivery_preflight_once(intent_id)
            else:
                core.deliver_attention_intent_once(intent_id)
        except Exception as e:
            print(f"[AttentionEngine] delivery failed: {e}")
        finally:
            if ctx is not None:
                try:
                    ctx.pop()
                except Exception:
                    pass

    def _apply_result(self, result: dict, snapshot: dict):
        if not isinstance(result, dict):
            result = {}
        if "inner" not in result or "self_emotion" not in result:
            result = self._normalize_result(result)
        now = snapshot.get("now") or datetime.now()
        trigger = snapshot.get("trigger", "signals")
        proposed_intent = result.get("speak_intent")
        suppression_reason = self._speak_intent_suppression_reason(
            proposed_intent, snapshot
        )
        intent = None if suppression_reason else proposed_intent
        try:
            import storage as _st
            previous_state = _st.load_attention_state() or {}
        except Exception:
            previous_state = {}

        segments, log_updates = self._apply_segments(result, snapshot, previous_state)
        inner_segment = segments.get("inner") or {}
        episode = inner_segment
        intent_queue = self._enqueue_speak_intent(intent, result, snapshot, episode)
        if suppression_reason:
            decision = "speak_suppressed"
            topic = (proposed_intent or {}).get("topic_key") or "general"
            print(
                f"[AttentionEngine] suppressed speak_intent "
                f"reason={suppression_reason} topic={topic}"
            )
        else:
            decision = "speak_intent" if intent else "observe"

        try:
            import storage as _st
            for seg in log_updates:
                seg_entry = {
                    **seg,
                    "ts": seg.get("updated_at") or now.strftime("%Y-%m-%d %H:%M:%S"),
                    "trigger": trigger,
                    "decision": decision if seg.get("channel") == "inner" else "segment",
                    "speak_intent": intent if seg.get("channel") == "inner" else None,
                    "suppressed_speak_intent": (
                        proposed_intent
                        if suppression_reason and seg.get("channel") == "inner"
                        else None
                    ),
                    "speak_suppression_reason": (
                        suppression_reason if seg.get("channel") == "inner" else ""
                    ),
                    "snapshot_signals": len(snapshot.get("recent_signals") or []),
                }
                _st.append_attention_log(seg_entry)
            state = {
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": trigger,
                "decision": decision,
                "current_segments": segments,
                "current_inner": segments.get("inner", {}),
                "current_user_affect": segments.get("user_affect", {}),
                "current_self_emotion": segments.get("self_emotion", {}),
                # Compatibility aliases for debug panels and older callers.
                "thought": inner_segment.get("text", ""),
                "user_affect": result.get("user_affect", {}),
                "miru_inner": {
                    "mood": (segments.get("self_emotion") or {}).get("mood", ""),
                    "valence": (segments.get("self_emotion") or {}).get("valence", 0),
                    "arousal": (segments.get("self_emotion") or {}).get("arousal", 0.2),
                    "reason": (segments.get("self_emotion") or {}).get("text", ""),
                },
                "attention": result.get("attention", {}),
                "speak_intent": intent,
                "suppressed_speak_intent": proposed_intent if suppression_reason else None,
                "speak_suppression_reason": suppression_reason,
                "trigger_signal_kinds": snapshot.get("trigger_signal_kinds", []),
                "current_focus": inner_segment,
                "current_episode": inner_segment,
                "speak_intent_queue": self._queue_summary(intent_queue, now),
            }
            _st.save_attention_state(state)
        except Exception as e:
            print(f"[AttentionEngine] state/log write failed: {e}")

        self._write_user_affect_segment(segments.get("user_affect") or {}, snapshot)
        self._write_miru_inner_segment(segments.get("self_emotion") or {})
        if intent:
            self._schedule_delivery_if_needed(intent, intent_queue, episode, now)

        next_check = (result.get("attention") or {}).get("next_check_seconds")
        if next_check:
            self._schedule_tick(_clamp_int(next_check, HEARTBEAT_INTERVAL, 60, 900))

    def _write_user_affect_segment(self, segment: dict, snapshot: dict):
        if not isinstance(segment, dict) or not segment.get("id"):
            return
        confidence = float(segment.get("confidence") or 0)
        if confidence < 0.2 and snapshot.get("trigger") == "heartbeat":
            return
        now = snapshot.get("now") or datetime.now()
        started_at = segment.get("started_at") or now.strftime("%Y-%m-%d %H:%M:%S")
        text = segment.get("text") or segment.get("evidence") or segment.get("last_seen_summary") or ""
        entry = {
            "id": segment.get("id"),
            "segment_id": segment.get("id"),
            "channel": "user_affect",
            "timestamp": started_at,
            "started_at": started_at,
            "updated_at": segment.get("updated_at") or now.strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": segment.get("ended_at"),
            "duration_seconds": int(segment.get("duration_seconds") or 0),
            "tick_count": int(segment.get("tick_count") or 1),
            "mood": segment.get("mood") or "neutral",
            "intensity": round(_clamp_float(segment.get("arousal"), 0.2, 0.0, 1.0), 3),
            "arousal": round(_clamp_float(segment.get("arousal"), 0.2, 0.0, 1.0), 3),
            "valence": round(_clamp_float(segment.get("valence"), 0.0, -1.0, 1.0), 3),
            "text": text,
            "source": text or "我还在延续上一段对你状态的感觉。",
            "source_category": "attention",
            "trigger": segment.get("evidence") or segment.get("change_reason") or "",
            "source_type": "attention",
            "confidence": confidence,
            "trend": segment.get("trend") or "uncertain",
        }
        try:
            import storage as _st
            _st.upsert_emotion_log_entry(entry)
        except Exception as e:
            print(f"[AttentionEngine] user affect segment write failed: {e}")

    def _write_miru_inner_segment(self, segment: dict):
        if not isinstance(segment, dict) or not segment.get("id"):
            return
        try:
            import miru_emotion
            miru_emotion.get_instance().update_from_attention(segment)
        except Exception as e:
            print(f"[AttentionEngine] self emotion segment write failed: {e}")

    def _write_user_affect(self, result: dict, snapshot: dict):
        affect = result.get("user_affect") or {}
        confidence = float(affect.get("confidence") or 0)
        # Heartbeats with weak/no evidence should update attention_state only,
        # not flood the visible emotion river.
        if confidence < 0.2 and snapshot.get("trigger") == "heartbeat":
            return
        if confidence <= 0 and not affect.get("evidence"):
            return
        now = snapshot.get("now") or datetime.now()
        entry = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "mood": affect.get("mood") or "neutral",
            "intensity": round(_clamp_float(affect.get("arousal"), 0.2, 0.0, 1.0), 3),
            "arousal": round(_clamp_float(affect.get("arousal"), 0.2, 0.0, 1.0), 3),
            "valence": round(_clamp_float(affect.get("valence"), 0.0, -1.0, 1.0), 3),
            "source": affect.get("evidence") or "AttentionEngine",
            "source_category": "attention",
            "trigger": affect.get("evidence") or "",
            "source_type": "attention",
            "confidence": confidence,
            "trend": affect.get("trend") or "uncertain",
        }
        if not self._should_append_user_affect(entry, now):
            return
        try:
            import storage as _st
            _st.append_emotion_log(entry)
        except Exception as e:
            print(f"[AttentionEngine] user affect write failed: {e}")

    def _should_append_user_affect(self, entry: dict, now: datetime) -> bool:
        """Keep the visible emotion river event-like, not one row per tick.

        Attention still writes every inner thought to attention_log/state.  The
        user emotion log, however, powers the UI and prompt emotion arc, so
        repeated stable observations should be coalesced until something
        materially changes.
        """
        try:
            import storage as _st
            today = _st.get_emotion_log_by_date(now.strftime("%Y-%m-%d")) or []
        except Exception:
            return True

        last = None
        for item in reversed(today):
            if isinstance(item, dict) and item.get("source_type") == "attention":
                last = item
                break
        if not last:
            return True

        last_ts = _parse_local_dt(last.get("timestamp"))
        if not last_ts:
            return True
        elapsed = (now - last_ts).total_seconds()
        if elapsed < 0 or elapsed >= USER_AFFECT_DEDUP_SECONDS:
            return True

        same_mood = (last.get("mood") or "") == (entry.get("mood") or "")
        same_trend = (last.get("trend") or "") == (entry.get("trend") or "")
        same_stable_family = (
            _stable_affect_like(last.get("mood"), last.get("trend"))
            and _stable_affect_like(entry.get("mood"), entry.get("trend"))
        )
        valence_close = abs(float(last.get("valence") or 0) - float(entry.get("valence") or 0)) <= 0.04
        arousal_close = abs(float(last.get("arousal") or last.get("intensity") or 0)
                            - float(entry.get("arousal") or entry.get("intensity") or 0)) <= 0.06
        source_similarity = _text_similarity(last.get("source"), entry.get("source"))

        if ((same_mood and same_trend) or same_stable_family) and valence_close and arousal_close:
            stable_trend = (entry.get("trend") or "") in {"stable", "uncertain", ""}
            if source_similarity >= USER_AFFECT_TEXT_SIMILARITY:
                return False
            # Stable affect with wording drift is still one emotional state,
            # not a new UI event. The current affect remains available through
            # attention_state; emotion_log should record material changes.
            if stable_trend or same_stable_family:
                return False
            if elapsed < 20 * 60 and source_similarity >= 0.65:
                return False

        return True

    def _write_miru_inner(self, result: dict):
        inner = result.get("miru_inner") or {}
        try:
            import miru_emotion
            miru_emotion.get_instance().update_from_attention(inner)
        except Exception as e:
            print(f"[AttentionEngine] Miru emotion write failed: {e}")

    def _append_error_log(self, snapshot: dict, error: str):
        now = snapshot.get("now") or datetime.now()
        entry = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": snapshot.get("trigger", "signals"),
            "decision": "error",
            "thought": "",
            "error": error[:240],
            "snapshot_signals": len(snapshot.get("recent_signals") or []),
        }
        try:
            import storage as _st
            _st.append_attention_log(entry)
        except Exception:
            pass


_instances: dict[str, AttentionEngine] = {}
_instances_lock = threading.Lock()


def _current_user_id() -> str:
    try:
        from flask import g
        return getattr(g, "user_id", "_admin")
    except (RuntimeError, ImportError):
        return "_admin"


def get_attention_engine() -> AttentionEngine:
    uid = _current_user_id()
    inst = _instances.get(uid)
    if inst is None:
        data_dir = None
        if uid != "_admin":
            try:
                import auth as _auth
                data_dir = _auth.get_user_data_dir(uid)
            except Exception:
                data_dir = None
        with _instances_lock:
            inst = _instances.get(uid)
            if inst is None:
                inst = AttentionEngine(
                    user_id=uid,
                    user_data_dir=data_dir,
                    auto_start_on_signal=True,
                )
                _instances[uid] = inst
    return inst


def get_all_instances() -> dict[str, AttentionEngine]:
    with _instances_lock:
        return dict(_instances)


def peek_attention_engine(user_id: str) -> AttentionEngine | None:
    with _instances_lock:
        return _instances.get(user_id)


def remove_attention_engine(user_id: str) -> AttentionEngine | None:
    with _instances_lock:
        inst = _instances.pop(user_id, None)
    if inst is not None:
        inst.deactivate()
    return inst
