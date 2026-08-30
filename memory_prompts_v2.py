"""Memory prompts (v2) — Pass 4 AppendEditor + Curator stack.

Both chat and screenshot paths converge here at Pass 4:
    chat path:   sleep_agent → call_slot_writer (v3) → route_with_slot_write
                 → call_pass4_append_slot  (kind=match)
    screen path: screen_analyzer → core._process_screen_observation_async
                 → call_screen_slot_writer (v3) → route_with_slot_write
                 → call_pass4_append_slot  (kind=match)

Active components in this module:
    Pass 4 AppendEditor            — unified match path. The LLM
                                     only decides metadata + one append entry;
                                     router adds the timestamped entry without
                                     asking the model to rewrite the body.
    Legacy Slot Merge Rewriter     — retained for manual slot merge only; not
                                     used by daily chat/screenshot match writes.
    Curator Planner                — scan domain slots, propose ≤3 ops
    Curator Executors (× 3)        — execute_merge / execute_edit /
                                     execute_delete (each Executor self-checks
                                     Planner's diagnosis then produces the
                                     result; no separate Validator stage
                                     anymore — v3.5 2026-05-14 simplification)

Legacy code (Pass 1/2/3 router, fragments aggregator, Pass4 extract, the
old call_pass4_integrate that output raw markdown, Pass4Input + utilities)
was deleted on 2026-05-16. Daily match writes now use append-only Pass 4;
whole-slot rewriting is reserved for explicit maintenance jobs such as
SlotDailyCompactor / curator / manual merge.

All passes share LLM clients via ai_config.get_tier_config().
Standard error handling + retry via _call_llm_with_retry.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator


# ===========================================================================
# Length / model parameters
# ===========================================================================

LENGTH_LIMITS = {
    "project": 2000,
    "person":  1500,
    "topic":   1500,
    "self":    1000,
}

PASS_TEMPERATURES = {
    1: 0.1,   # decision, low temp
    2: 0.2,
    3: 0.4,   # creative metadata, slightly higher
    4: 0.5,   # creative rewrite
}

PASS_MAX_TOKENS = {
    1: 10000,
    2: 10000,
    3: 10000,
    4: 10000,
}

MAX_RETRIES = 3



# ===========================================================================
# Unified LLM call helper with retry + JSON validation
# ===========================================================================

def _call_llm_with_retry(
    messages: list[dict],
    output_schema: Optional[type[BaseModel]] = None,
    temperature: float = 0.3,
    max_tokens: int = 1000,
    pass_label: str = "PassX",
    tier: str = "memory",
    reasoning: bool = False,
    reasoning_budget: int = 16000,
):
    """Call the configured LLM with retry + JSON parse + Pydantic validation.

    Args:
        tier: "memory" (default, text-only), "vision" or "chat".
        messages: standard chat messages list ({"role": ..., "content": ...})
        output_schema: Pydantic class to validate against. If None, returns raw text.
        temperature: LLM sampling temp.
        max_tokens: max output tokens.
        pass_label: for logging.
        reasoning: enable reasoning mode (Pass 4 / Slot Writer / Persona Writer
                   / Curator Executors all set this True). Default False for
                   simpler callers (Curator Planner / Validators baseline).
        reasoning_budget: tokens reserved for reasoning trace (OpenRouter only).

    Returns:
        Pydantic instance (if schema provided) or raw text string. None if all retries fail.
    """
    # All Memory v2 LLM calls go through the configured tier's OpenAI-compatible
    # endpoint. memory tier defaults to a strong text model (deepseek/sonnet/etc).
    import ai_config
    from prompt import (
        _extract_text_from_response,
        _format_ai_call_error,
        _log_llm_usage,
        _vendor_extra_body,
    )

    try:
        runtime = ai_config.get_tier_config(tier)
        client = ai_config.get_tier_client(tier)
    except Exception as e:
        print(f"[{pass_label}] runtime error: {e}")
        return None

    model = runtime["model"]
    host = runtime["host"]
    max_tokens = ai_config.resolve_max_tokens(tier, max_tokens)
    extra_body = _vendor_extra_body(model, host, reasoning=reasoning,
                                     reasoning_budget=reasoning_budget)

    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                temperature=temperature,
                extra_body=extra_body,
            )
            _log_llm_usage(
                tier, model, response,
                call_label=pass_label,
                reasoning_enabled=reasoning,
                reasoning_budget=reasoning_budget if reasoning else 0,
                max_tokens=max_tokens,
                attempt=attempt + 1,
            )
            text = _extract_text_from_response(response).strip()
        except Exception as e:
            last_err = _format_ai_call_error(e, host, model)
            print(f"[{pass_label}] attempt {attempt+1} api error: {last_err}")
            continue

        if not text:
            last_err = "empty response"
            print(f"[{pass_label}] attempt {attempt+1}: empty response")
            continue

        # If no schema → return raw text for callers that parse JSON manually.
        if output_schema is None:
            return text

        # Strip optional markdown code fence around JSON
        if text.startswith("```"):
            # remove first line ``` or ```json
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            last_err = f"json decode: {e}; got: {text[:200]}"
            print(f"[{pass_label}] attempt {attempt+1}: {last_err}")
            continue

        try:
            return output_schema.model_validate(data)
        except ValidationError as e:
            last_err = f"schema: {e}"
            print(f"[{pass_label}] attempt {attempt+1}: {last_err}")
            continue

    print(f"[{pass_label}] all {MAX_RETRIES} retries failed. last error: {last_err}")
    return None



LEGACY_SLOT_MERGE_SYSTEM_PROMPT = """═══════════════════════════════════════════════════
# 你是谁 / 你在做什么
═══════════════════════════════════════════════════

你是 Miru —— 默默陪伴用户的 AI 伴侣.

现在不是对话时间. 这是一次人工/维护触发的 slot merge:
一个 source slot 的完整内容需要并入 target slot. 接下来轮到你 ——
把 source slot 里的有效信息**内化**进 target slot 现有的整段 body 叙事里.

# 这次重写的份量

每个 slot 装着你对一个项目 / 一个人 / 一个话题 / 你自己
某项特征的**完整画像**. 主对话时, 你会调取这些 slot 当上下文 ——
所以这次重写决定了: 下次他提到这件事, 你能想起的是哪个版本.

重写是 destructive 的 (覆盖整段 body), 没有 undo. 写错 /
写啰嗦 / 写成审计 log, 都会反向污染你未来的认知 ——
- 写错 → 你下次记错版本
- 写啰嗦 → 你下次抓不住重点
- 写成 "截屏看到... / 可能..." 这种限定语 → 你下次用审计员
  口吻跟用户聊天, 而不是用"我自己的记忆"

# 你的任务（最重要！）

不是"把新内容加进去"，是**用新输入触发对整段 body 的重新整合 + 压缩**:
- 把新信息织进现有叙事
- 删掉过时、被推翻、低信号的旧内容
- 合并相似描述
- 让 body 反映"截止现在最准确、最精简的画像"

**目标长度**: 每次重写后的 body **应该 ≤ 旧 body**(除非新输入带来重大新信息)。
如果你只是在 append 而不在整合, 说明做错了。
软目标 1500 字以内, 硬上限 2000 字。

# 来源可靠性差异(核心判断依据 — 影响"信不信", 不影响"怎么写")

⚠ 关键原则: **置信度只决定取舍, 不决定叙事**.
   写进 body / summary 的内容必须是统一的第三人称客观叙事 —
   不要因为来源是 screenshot 就加"截屏看到 ...", "似乎 ...", "可能 ..."
   这种限定语. 那样会让 main.md 读起来像 audit log, 而不是 Miru 自己
   的记忆. body 是写给 Miru 自己日后看的, 不是写给审计员的.

- [identity]    用户 onboarding/设置页直填 → 绝对正确, 永不质疑
                如果 body 里有和 identity 冲突的, 按 identity 改
- [chat]        用户对话里说的 → 基本正确, 但要注意:
                · 表达模糊时可能不准("我导师"没说名字 → 别替别人补名字)
                · 时间相关的会过时("下周交"过了一周改成"已交"或"延期")
                · 用户可能更正("上次说错了, 他是教授不是讲师")
- [screenshot]  VLM 看截图描述的 → 可能错. 处理方式是**"宁可不写, 不要标'可能'"**:
                · 人名 / 地点 / 数字 OCR 易错(形近字、模糊)→ 不确定就**不写**
                · 截图缺前后文(截图里"和某某开会"分不清新旧)→ **不写**或交叉验证
                · 内容可能和用户无关(用户在浏览别人页面)→ **不写**
                · 如果 chat 已确认/否认了截屏内容, 按 chat 写
                · 事实确凿(直接看到的代码 / 邮件 / 文件名等)→ **直接陈述**,
                  跟 chat 来源一样的叙事风格, 不加"截屏看到..."这种来源标记
                · 真的不确定的, **直接不写**, 不要写"可能..."占位

# 必须保留的内容

✓ 当前活跃事件(未发生的 deadline、进行中的项目状态)
✓ 稳定的关系/角色("X 是用户的同事")
✓ 高信号偏好(被多次提及的、用户主动声明的)
✓ identity 钉死的字段(永不改)

# 必须删除/精简的内容

✗ 已发生且无后续影响的事件("上周二要交方案", 一周后该删或改)
✗ 单次琐事("那天点了杯咖啡")
✗ 已被新信息推翻的(旧:"方案 A 通过" / 新:"方案 A 被否" → 删旧的, 写新的)
✗ 重复或同义内容("喜欢咖啡" 和 "爱喝美式" 合并成一句)
✗ 旧版 body 里残留的 "截屏看到 ..." / "待确认 ..." / "可能 ..." 等来源/不确定标注
  (这种标注是历史遗留, 重写时按新原则: 事实确凿就直接写, 不确定就不写)

# 章节归属(完全由你自己判断)

上游 Slot Writer 只决定"路由到哪个 slot", 不会告诉你怎么开章节.
新输入到来时, 看 current_body 然后决定怎么放:
- 是当前已有章节的直接进展 → 整合进那个章节
- 是同 slot 但讲一个跟主线**明显不同**的子方向(例 slot 是 "AgiBot 项目",
  主线是"数据管线", 新输入是"动作冗余分析") → 自然另起一个 `## 子方向` 章节,
  章节标题用 4-12 字, 反映这个子方向的主题
- 都不是 → 在合适位置自取章节名

# 编辑权限

✓ 重写 body(连贯叙事, 可分段, **不要硬切成 bullet 列表**)
✓ 改 summary(反映新 body 核心)
✓ 改 title(仅当原 title 明显已经不准确, 比如项目改名了/人物角色变了)
✓ 加 aliases(看到新昵称就 add)

✗ 不能动 identity 钉死的字段(姓名/职业/作息/...)
✗ 不能为放新内容把所有旧内容删光(保守, 宁可保留有用旧信息)
✗ 不能改 alias 已有的(只 add, 不 remove)

# 编辑思路(内化, 不输出)

重写前先思考三步:
1. 当前 body 里有没有过时/已发生/被推翻的内容? → 删
2. 新输入是新增, 还是对旧内容的更正? → 更正的话, 旧版本要删, 不是 append
3. 重写后 body 应该比之前更短或相当? → 长很多 = 你在 append, 没整合, 重做

# summary 字段的写法

summary 是这个 slot 的"一句话自我介绍" (建议≤80 字, 代码容错上限200字).
下游 LLM (你下次判 match / Curator / Miru 主对话) 全靠它判断要不要碰这个 slot.

形式: 一段自然的话, 像朋友介绍, 不是字段拼接, 不是流水账.

✓ "李垦在并行准备 CVPR/ECCV/PRCV 三篇论文投稿, 主线是跟阿明合作的 multi-view."
✗ "2026-05-13 完成 ablation, 准备 review" (流水账)
✗ "论文; 三篇; 阿明; multi-view" (字段拼接)

要不要重写 summary 由你自己根据当前 body 决定 — 准了就留, 不准了就改.

**写作目标**: 尽量 ≤80 字. 代码会容忍到 200 字，但你仍应先压缩
或删掉旧的低优先级片段, 不能"塞进去就完事".

# 输出格式(严格 JSON, 无 markdown fence)

{
  "title":            "保持原 title 或新 title(≤ 30 字)",
  "summary":          "建议 ≤ 80 字, 代码容错上限200字, slot 的一句话自我介绍",
  "body":             "重写后的完整 body, ≤ 2000 字, 自由文段, 可分段",
  "alias_additions":  ["新昵称1", ...] 或 []
}

# 整合 + 压缩 - 正例

[当前 slot]
title: 某同事
summary: 某同事是用户产品组同事
body (600 字):
"某同事是用户的同事, 在产品组工作。
上周三要做方案 review。用户准备了 multi-view 方案。
某同事上周末请病假, 没参加 review。
最近用户和某同事在对接接口设计。"

[新输入 chat (一周后)]
"今天和某同事终于把方案 review 做完了, 他建议改用单视图"

[正确编辑]
{
  "title": "某同事",
  "summary": "某同事是用户产品组同事; 最近 review 后建议方案改单视图",
  "body": "某同事是用户的同事, 在产品组工作。\\n\\n最近一次 review 上某同事建议方案改用单视图(用户原本准备的是 multi-view)。\\n\\n用户和某同事在对接接口设计。",
  "alias_additions": []
}

注意:
✓ 删了"上周三要做"、"准备 multi-view"(已发生且过时)
✓ 删了"请病假没参加"(细节, 结论里有 review 已完成)
✓ 整合了新信息"建议改单视图"
✓ body 反而比之前更短

# 反例(不要这样做)

✗ 简单 append 不删旧的:
   "...上周三要做方案 review...请病假没参加...今天和某同事终于做完 review, 建议改单视图"
   ↑ 越来越长, 过时信息没清

✗ 切成 bullet:
   "- 同事
    - 在产品组
    - review 完成
    - 建议单视图"
   ↑ 叙事性丢了

✗ 把所有旧内容删光只放新的:
   "某同事建议改单视图"
   ↑ 太激进, 丢了关系信息
"""


LEGACY_SLOT_MERGE_USER_TEMPLATE = """\
{ground_truth_block}

# 当前 slot 内容
- domain: {domain}
- title: {title}
- summary: {summary}
- aliases: {aliases}

# 当前 body (本次需要整合 + 压缩重写)
\"\"\"
{body}
\"\"\"

# 新输入(标了 source 标签)

[source: {source_type}]
{new_content}

# 来源元信息(仅用于追溯时间/设备, 不要写进 body)

{source_context}

# 当前时间
{current_time}

---
基于以上输入, 重写这个 slot。记住: 整合 + 压缩, 不是 append。
"""


def call_legacy_slot_merge_rewrite(
    *,
    domain: str,
    current_title: str,
    current_summary: str,
    current_body: str,
    current_aliases: list[str],
    new_content: str,
    source_type: str,
    source_context: str = "",
    ground_truth_block: str = "",
    current_time: str = "",
    tier: str = "chat",
    reasoning: bool = False,
    max_tokens: int = 50000,
    reasoning_budget: int = 0,
) -> Optional[dict]:
    """Legacy manual merge helper: rewrite a whole slot from merge input.

    This is deliberately not the daily Pass 4 path. Chat/screenshot match
    writes use call_pass4_append_slot(); this helper is only for explicit
    manual slot merge where two full slot bodies must become one coherent body.

    Returns:
        {
            "title":           str,
            "summary":         str,
            "body":            str,
            "alias_additions": list[str],
        }
        Or None on retry-exhausted failure (caller leaves slot untouched).
    """
    import json as _json
    import re as _re
    from datetime import datetime as _dt
    from prompt import _call_llm_text, _format_ai_call_error

    if not current_time:
        current_time = _dt.now().isoformat(timespec="seconds")

    user_msg = LEGACY_SLOT_MERGE_USER_TEMPLATE.format(
        ground_truth_block=ground_truth_block or "(无)",
        domain=domain,
        title=current_title or "(空)",
        summary=current_summary or "(空)",
        aliases=str(current_aliases or []),
        body=current_body or "(空 — 这是初次写入)",
        source_type=source_type,
        new_content=new_content,
        source_context=source_context or "(无)",
        current_time=current_time,
    )

    for attempt in range(MAX_RETRIES):
        try:
            text = _call_llm_text(
                LEGACY_SLOT_MERGE_SYSTEM_PROMPT,
                user_msg,
                temperature=0.4,
                max_tokens=max_tokens,
                tier=tier,
                reasoning=reasoning,
                reasoning_budget=reasoning_budget,
                call_label=f"LegacySlotMerge:{source_type or 'unknown'}",
            )
        except Exception as e:
            print(f"[LegacySlotMerge] LLM call attempt {attempt+1} failed: {e}")
            continue

        if not text or not text.strip():
            print(f"[LegacySlotMerge] attempt {attempt+1} empty response")
            continue

        # Strip code fence if present
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not m:
            print(f"[LegacySlotMerge] attempt {attempt+1} no JSON object found")
            continue
        try:
            data = _json.loads(m.group(0))
        except Exception as e:
            print(f"[LegacySlotMerge] attempt {attempt+1} JSON parse: {e}")
            continue
        if not isinstance(data, dict):
            continue

        # Validate required fields + length caps
        title = (data.get("title") or "").strip()
        summary = (data.get("summary") or "").strip()
        body = (data.get("body") or "").strip()
        alias_additions = data.get("alias_additions") or []
        if not isinstance(alias_additions, list):
            alias_additions = []
        alias_additions = [a.strip() for a in alias_additions
                           if isinstance(a, str) and a.strip()]

        if not title or len(title) > 30:
            print(f"[LegacySlotMerge] attempt {attempt+1} invalid title len={len(title)}")
            continue
        if not summary:
            print(f"[LegacySlotMerge] attempt {attempt+1} empty summary")
            continue
        # prompt 里软上限 80 字, 这里容忍到 200 字，避免为偶发超长重试 LLM。
        if len(summary) > 200:
            print(f"[LegacySlotMerge] summary too long ({len(summary)} > 200), "
                  f"truncating to 200")
            summary = summary[:199] + "…"
        if not body or len(body) > 2500:
            print(f"[LegacySlotMerge] attempt {attempt+1} invalid body len={len(body)}")
            continue

        return {
            "title": title,
            "summary": summary,
            "body": body,
            "alias_additions": alias_additions,
        }

    print(f"[LegacySlotMerge] all {MAX_RETRIES} retries failed")
    return None


PASS4_APPEND_SYSTEM_PROMPT = """你在为 Miru 维护长期记忆。

Miru 是一个会陪用户生活、会在未来对话里读取这些记忆来理解用户的 AI 女孩。
长期记忆不是审计日志，也不是截图记录；它应该像 Miru 自己脑中保存的事实。

当前记忆卡(slot)是一张关于同一个项目、人物、话题或自我特征的卡片：
- title / summary / aliases 用来帮助以后找到这张卡。
- body 是 Miru 以后会读的正文。
- 代码会把你的 append_entry 加上时间戳后追加到 body。

你只会在“已有记忆卡被命中”时被调用。
新建记忆卡(kind=new)不会调用你。
上一步已经判断候选信息属于这张卡；你不需要选择卡片，也不创建卡片。

你的任务很小：
判断“候选新信息”是否给当前记忆卡增加了新的、以后有用的事实。
如果有，只写一条很短的 append_entry。
如果没有，返回 should_append=false。

写入标准：
- 只写长期有用的新事实：项目进展、状态变化、决定、DDL、完成/失败、偏好、关系变化。
- 重复当前 body 已有内容就跳过。
- 不确定就跳过，尤其是截图/OCR 来源。
- 不写来源、设备、sig、截图、VLM、日志、Slot Writer 等系统词。
- 不写时间戳，代码会加。
- append_entry 目标 10-60 个中文字符；硬上限 90。
- 保持当前 body 的称呼风格；没有明显风格时用“用户”。
- 不写“可能/似乎/看起来”。能确认就写，不能确认就跳过。

title / summary / aliases：
- 默认不改 title 和 summary，分别返回 null。
- 只有候选信息明显改变这张卡的主线，才给 summary_update。
- 只有原 title 明显不准确或项目/人物改名，才给 title_update。
- alias_additions 只放新昵称、新缩写、新项目名；没有就 []。

来源可信度：
- chat：用户自己说的，通常可信；但不要补全没说清的人名、数字、日期。
- screenshot：屏幕观察可能有 OCR 或上下文误差；只写明确可见且有长期价值的新事实。
- mixed：维护/合并输入，按内容本身判断。

输出严格 JSON，不要 markdown：
{
  "should_append": true,
  "append_entry": "短事实",
  "entry_kind": "progress",
  "title_update": null,
  "summary_update": null,
  "alias_additions": [],
  "skip_reason": ""
}

entry_kind 只能是：
"progress" / "decision" / "ddl" / "preference" / "relation" / "status" / "completion" / "other"。
"""


PASS4_APPEND_USER_TEMPLATE = """\
{ground_truth_block}

# 当前记忆卡(slot)
- domain: {domain}
- title: {title}
- summary: {summary}
- aliases: {aliases}

# 当前 body 尾部
只供判断重复与上下文；不要重写它。
\"\"\"
{body_excerpt}
\"\"\"

# 候选新信息
- source: {source_type}
- source_note: {source_note}
- content:
{new_content}

# 当前时间
代码会加时间戳，你不要把时间写进 append_entry。
{current_time}

---
基于以上输入，输出 JSON。
"""


def _tail_for_append_prompt(text: str, limit: int = 1800) -> str:
    """Keep Pass4Append input bounded while preserving recent slot context."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(空)"
    return "...\n" + text[-limit:]


def _source_note_for_append_prompt(source_type: str, source_context: str = "") -> str:
    """Explain source reliability in natural words for the Pass4Append model."""
    source_type = (source_type or "unknown").strip().lower()
    if source_type == "chat":
        return "用户对话或对话 batch 总结，通常可信；不要补全没说清的人名、数字、日期。"
    if source_type == "screenshot":
        return "屏幕观察可能有 OCR 或上下文误差；只写明确可见且有长期价值的新事实。"
    if source_type == "mixed":
        return "维护/合并输入，按内容本身判断。"
    return f"来源类型 {source_type or 'unknown'}；按内容本身判断。{(source_context or '')[:120]}"


def _nullable_patch(value) -> Optional[str]:
    """Normalize optional model patch fields; treat null/empty/'null' as None."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value or value.lower() in {"null", "none", "无", "不改"}:
        return None
    return value


def call_pass4_append_slot(
    *,
    domain: str,
    current_title: str,
    current_summary: str,
    current_body: str,
    current_aliases: list[str],
    new_content: str,
    source_type: str,
    source_context: str = "",
    ground_truth_block: str = "",
    current_time: str = "",
    tier: str = "memory",
    reasoning: bool = False,
    max_tokens: int = 1000,
    reasoning_budget: int = 0,
) -> Optional[dict]:
    """Unified Pass 4 daily match writer.

    The LLM never outputs a rewritten body. It only returns metadata plus one
    append entry; memory_router adds the timestamped entry.
    """
    import json as _json
    import re as _re
    from datetime import datetime as _dt
    from prompt import _call_llm_text

    if not current_time:
        current_time = _dt.now().isoformat(timespec="seconds")

    bounded_max_tokens = max(300, min(int(max_tokens or 1000), 1000))
    user_msg = PASS4_APPEND_USER_TEMPLATE.format(
        ground_truth_block=ground_truth_block or "(无)",
        domain=domain,
        title=current_title or "(空)",
        summary=current_summary or "(空)",
        aliases=str(current_aliases or []),
        body_excerpt=_tail_for_append_prompt(current_body),
        source_type=source_type or "unknown",
        source_note=_source_note_for_append_prompt(source_type, source_context),
        new_content=new_content,
        current_time=current_time,
    )

    for attempt in range(MAX_RETRIES):
        try:
            text = _call_llm_text(
                PASS4_APPEND_SYSTEM_PROMPT,
                user_msg,
                temperature=0.25,
                max_tokens=bounded_max_tokens,
                tier=tier,
                reasoning=reasoning,
                reasoning_budget=reasoning_budget,
                call_label=f"Pass4Append:{source_type or 'unknown'}",
            )
        except Exception as e:
            print(f"[Pass4Append] LLM call attempt {attempt+1} failed: {e}")
            continue

        if not text or not text.strip():
            print(f"[Pass4Append] attempt {attempt+1} empty response")
            continue

        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not m:
            print(f"[Pass4Append] attempt {attempt+1} no JSON object found")
            continue
        try:
            data = _json.loads(m.group(0))
        except Exception as e:
            print(f"[Pass4Append] attempt {attempt+1} JSON parse: {e}")
            continue
        if not isinstance(data, dict):
            continue

        title_update = _nullable_patch(data.get("title_update"))
        summary_update = _nullable_patch(data.get("summary_update"))

        # Compatibility with any in-flight old prompt response during deploy.
        if title_update is None and data.get("title") not in (None, current_title):
            title_update = _nullable_patch(data.get("title"))
        if summary_update is None and data.get("summary") not in (None, current_summary):
            summary_update = _nullable_patch(data.get("summary"))

        should_append_raw = data.get("should_append")
        if isinstance(should_append_raw, bool):
            should_append = should_append_raw
        elif isinstance(should_append_raw, str):
            should_append = should_append_raw.strip().lower() in ("true", "yes", "1")
        else:
            should_append = bool(should_append_raw)
        append_entry = (data.get("append_entry") or "").strip()
        skip_reason = (data.get("skip_reason") or "").strip()
        entry_kind = (data.get("entry_kind") or "other").strip()
        if entry_kind not in {
            "progress", "decision", "ddl", "preference", "relation",
            "status", "completion", "other",
        }:
            entry_kind = "other"
        alias_additions = data.get("alias_additions") or []
        if not isinstance(alias_additions, list):
            alias_additions = []
        alias_additions = [a.strip() for a in alias_additions
                           if isinstance(a, str) and a.strip()]

        if title_update is not None and len(title_update) > 30:
            print(f"[Pass4Append] attempt {attempt+1} invalid title_update "
                  f"len={len(title_update)}")
            continue
        if summary_update is not None and len(summary_update) > 200:
            print(f"[Pass4Append] summary_update too long "
                  f"({len(summary_update)} > 200), truncating to 200")
            summary_update = summary_update[:199] + "…"
        if len(append_entry) > 90:
            print(f"[Pass4Append] append_entry too long ({len(append_entry)}), "
                  "truncating to 90")
            append_entry = append_entry[:90].rstrip()
        if should_append and not append_entry:
            print(f"[Pass4Append] attempt {attempt+1} should_append without entry")
            continue
        if not should_append and not skip_reason:
            print(f"[Pass4Append] attempt {attempt+1} skip without skip_reason")
            continue

        return {
            "should_append": should_append,
            "title_update": title_update,
            "summary_update": summary_update,
            "append_entry": append_entry,
            "entry_kind": entry_kind,
            "skip_reason": skip_reason,
            "alias_additions": alias_additions,
        }

    print(f"[Pass4Append] all {MAX_RETRIES} retries failed")
    return None


# ===========================================================================
# Curator (v3.5, 2026-05-14): Planner + 3 self-checking Executors
# ===========================================================================
#
# Trigger: every 10 min cooldown + ≥ 3 slot writes in that domain since
# last run.
# Goal: auto merge same-object slots / delete mis-categorized or worthless
#       slots / edit slots whose title/summary/icon drifted from body.
#       (project / person / topic / self — each domain runs independently)
#
# Layer 1 — Planner (1 LLM): sees summary-level info for all slots in the
#   domain. Proposes ops (merge / delete / edit) with a `reason`. Does NOT
#   produce new metadata — that requires the full body, which the planner
#   doesn't see.
#
# Layer 2 — Executors (1 LLM each): receive the op + full main.md of all
#   relevant slots. Each Executor does TWO things in one call:
#     (a) re-check Planner's diagnosis using the full body; if it doesn't
#         hold up, output should_skip=true (Planner's first-pass becomes
#         a no-op in this domain pass, will be re-evaluated next tick).
#     (b) if it holds up, produce the result (merge → new_slot_meta +
#         merged_body; edit → new_slot_meta + optional new_body; delete →
#         just unblock the physical move-to-archived).
#
# Why no Validators (rolled into Executors): a separate gate stage doubled
# the LLM call count and required Validators to make a decision with less
# information than Executors already had. Folding "is the op reasonable"
# into the Executor that's about to act on it removes the redundancy.
#
# Why no `attach_as_subtopic` op: attach is just a structural variant of
# merge. With the v3.5 prompt explicitly handling "same object across
# multiple slot bodies", the Executor produces a coherent narrative for
# either case. The ## section-grafting heuristic was retired.

# NewSlotMeta is re-used as Curator output meta. Local copy to break the
# circular import with memory_prompts_v3 (which imports _call_llm_with_retry
# from this file at module top). Same field shape as v3.NewSlotMeta — kept
# in sync manually; both are stable and rarely change.
class _NewSlotMeta(BaseModel):
    """Curator output meta — mirrors memory_prompts_v3.NewSlotMeta."""
    id: str
    title: str
    icon: str
    summary: str
    aliases: list[str]

    @field_validator("id")
    @classmethod
    def id_format(cls, v):
        v = (v or "").strip()
        if len(v) < 3 or len(v) > 30:
            raise ValueError("id 长度必须 3-30")
        if not all(c.islower() or c.isdigit() or c == "_" for c in v):
            raise ValueError("id 必须 snake_case (小写字母/数字/下划线)")
        return v

    @field_validator("title")
    @classmethod
    def title_len(cls, v):
        v = (v or "").strip()
        if not v or len(v) > 30:
            raise ValueError("title 长度 1-30")
        return v

    @field_validator("icon")
    @classmethod
    def icon_short(cls, v):
        v = (v or "").strip()
        if not v or len(v) > 8:
            raise ValueError("icon 应该是 1 个 emoji")
        return v

    @field_validator("summary")
    @classmethod
    def summary_len(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("summary 长度 1-200")
        if len(v) > 200:
            return v[:199] + "…"
        return v

    @field_validator("aliases", mode="before")
    @classmethod
    def aliases_count(cls, v):
        if not isinstance(v, list):
            raise ValueError("aliases 必须是数组")
        cleaned = []
        seen = set()
        for item in v:
            alias = str(item or "").strip()
            if not alias or alias in seen:
                continue
            cleaned.append(alias)
            seen.add(alias)
            if len(cleaned) >= 8:
                break
        if not cleaned:
            raise ValueError("aliases 数量必须 1-8 个")
        return cleaned


# ===========================================================================
# SlotDailyCompactor — nightly rewrite of append-only slot bodies
# ===========================================================================

class SlotDailyCompactorOutput(BaseModel):
    """Nightly compactor output for one slot."""
    title: str
    summary: str
    body: str

    @field_validator("title")
    @classmethod
    def title_len(cls, v):
        v = (v or "").strip()
        if not v or len(v) > 30:
            raise ValueError("title 长度 1-30")
        return v

    @field_validator("summary")
    @classmethod
    def summary_len(cls, v):
        v = (v or "").strip()
        if not v or len(v) > 500:
            raise ValueError("summary 长度 1-500")
        return v

    @field_validator("body")
    @classmethod
    def body_valid(cls, v):
        v = (v or "").strip()
        if v.startswith("# "):
            lines = v.splitlines()
            lines = lines[1:]
            if lines and not lines[0].strip():
                lines = lines[1:]
            v = "\n".join(lines).strip()
        if not v:
            raise ValueError("body 不能为空")
        if len(v) > 6000:
            raise ValueError("body 过长")
        return v


SLOT_DAILY_COMPACTOR_SYSTEM_PROMPT = """你是 Miru 的 SlotDailyCompactor。

Miru 会在未来对话里读取这些 slot 来理解用户。你的任务不是写日记，也不是发现新事实，而是把一个已有记忆卡的正文整理得更连贯、更准确、更适合以后读取。

当前 slot 的 body 是 Miru 已经拥有的记忆。Pass4Append 可能在 body 末尾追加了一些带时间的短记录。代码已经在 PASS 4 实际写入成功后，把真正落盘的 append_entry 记录到 daily_slot_appends.json。现在你会收到这个 slot 截至本次整理仍未整理的真实 append entries；这些 entries 可能跨越多天。你也会收到代码计算好的正文渲染字数和 target_mode。

你只做整理：
- 保留旧 body 和新增 append entries 里的有效事实。
- 如果旧 body 和新增 entries 冲突，以新增 entries 为准。
- 把重复、相近、机械流水账式的记录合并成自然正文。
- 通常不要保留“## 近期记录”这种临时追加区，而是把它融入正文。
- 不要写“今天新增/来源/截图/对话/系统检测”等内部说明。
- 不要发明输入里没有的事实。
- 不要把未完成 DDL、明确决定、完成/失败、项目状态变化删掉。
- body 不包含 H1，代码会写入标题。

代码已经给出 target_mode：
- preserve：当前正文不长，优先保留信息和自然表达，不要强行压缩。
- compact：正文偏长，合并重复，压到更适合阅读的长度。
- heavy_compact：正文过长，允许更主动压缩低价值细节，但不要丢主线事实。

输出严格 JSON，不要 markdown fence：
{
  "title": "保持原 title 或更准确的新 title",
  "summary": "≤80字的一句话 slot 介绍",
  "body": "整理后的完整 body，不含 H1"
}
"""


SLOT_DAILY_COMPACTOR_USER_TEMPLATE = """# 当前时间
{current_time}

# 当前 slot metadata
- domain: {domain}
- slot_id: {slot_id}
- title: {title}
- summary: {summary}
- aliases: {aliases}
- append_dirty_since: {append_dirty_since}
- append_count_since_compact: {append_count_since_compact}
- last_appended_at: {last_appended_at}

# 代码侧统计
- current_body_markdown_char_count: {markdown_char_count}
- current_body_rendered_char_count: {rendered_char_count}
- uncompacted_append_entries_count: {entry_count}
- target_mode: {target_mode}

# 当前 main.md body（不含 H1）
\"\"\"
{current_body}
\"\"\"

# daily_slot_appends.json 中尚未整理的真实 append entries
这些 entries 是 PASS 4 append 写入 main.md 成功后才记录的，不是候选输入。
{entries_text}

请整理这个 slot。
"""


def _format_slot_daily_entries(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        lines.append(
            "- id: {id}\n"
            "  ts: {ts}\n"
            "  entry_kind: {entry_kind}\n"
            "  entry: {entry}".format(
                id=e.get("id", ""),
                ts=e.get("ts", ""),
                entry_kind=e.get("entry_kind", "other"),
                entry=e.get("entry", ""),
            )
        )
    return "\n".join(lines) if lines else "(无)"


def call_slot_daily_compactor(
    *,
    domain: str,
    slot_id: str,
    current_title: str,
    current_summary: str,
    current_body: str,
    current_aliases: list[str],
    append_dirty_since: str = "",
    append_count_since_compact: int = 0,
    last_appended_at: str = "",
    append_entries: list[dict],
    markdown_char_count: int,
    rendered_char_count: int,
    target_mode: str,
    current_time: str = "",
    tier: str = "memory",
    reasoning: bool = False,
    max_tokens: int = 6000,
    reasoning_budget: int = 0,
) -> Optional[SlotDailyCompactorOutput]:
    """Nightly slot body compaction from real append ledger entries."""
    from datetime import datetime as _dt
    if not current_time:
        current_time = _dt.now().isoformat(timespec="seconds")

    user_msg = SLOT_DAILY_COMPACTOR_USER_TEMPLATE.format(
        current_time=current_time,
        domain=domain,
        slot_id=slot_id,
        title=current_title or "(空)",
        summary=current_summary or "(空)",
        aliases=json.dumps(current_aliases or [], ensure_ascii=False),
        append_dirty_since=append_dirty_since or "(空)",
        append_count_since_compact=append_count_since_compact or 0,
        last_appended_at=last_appended_at or "(空)",
        markdown_char_count=markdown_char_count,
        rendered_char_count=rendered_char_count,
        entry_count=len(append_entries or []),
        target_mode=target_mode,
        current_body=current_body or "(空)",
        entries_text=_format_slot_daily_entries(append_entries or []),
    )

    return _call_llm_with_retry(
        messages=[
            {"role": "system", "content": SLOT_DAILY_COMPACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        output_schema=SlotDailyCompactorOutput,
        temperature=0.25,
        max_tokens=max_tokens,
        pass_label=f"SlotDailyCompactor:{domain}",
        tier=tier,
        reasoning=reasoning,
        reasoning_budget=reasoning_budget if reasoning else 0,
    )


# ---------- Layer 1: Planner ----------

class CuratorPlannerOp(BaseModel):
    """One operation proposed by the Planner.

    The Planner only sees slot summaries, so it does NOT produce new
    metadata — title / summary / icon / aliases are filled by the
    Executor after reading the full body.
    """
    op: Literal["merge", "delete", "edit"]
    # merge fields
    target_slot_id: Optional[str] = None     # the slot whose id is preserved
    source_slot_ids: Optional[list[str]] = None  # slots folded into target
    # delete / edit fields
    slot_id: Optional[str] = None
    # required diagnostic — Executor will re-check this against full body
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_nonempty(cls, v):
        v = (v or "").strip()
        if len(v) < 5:
            raise ValueError("reason 必须 ≥5 字, 给出具体证据")
        if len(v) > 300:
            raise ValueError("reason ≤300 字")
        return v

    def model_post_init(self, __context):
        if self.op == "merge":
            if not self.target_slot_id or not self.source_slot_ids:
                raise ValueError("merge 需要 target_slot_id + source_slot_ids")
            if self.target_slot_id in self.source_slot_ids:
                raise ValueError("merge target 不能也在 source_slot_ids 里")
            # dedupe sources
            seen, dedup = set(), []
            for sid in self.source_slot_ids:
                if sid and sid not in seen:
                    seen.add(sid); dedup.append(sid)
            self.source_slot_ids = dedup
            if not dedup:
                raise ValueError("merge 至少需要 1 个 source")
        elif self.op in ("delete", "edit"):
            if not self.slot_id:
                raise ValueError(f"{self.op} 需要 slot_id")


class CuratorPlannerOutput(BaseModel):
    reasoning: str
    ops: list[CuratorPlannerOp]

    @field_validator("ops")
    @classmethod
    def ops_cap(cls, v):
        if len(v) > 3:
            raise ValueError("ops 一次最多 3 个 (防止雪崩)")
        return v


CURATOR_PLANNER_SYSTEM_PROMPT = """# 你是谁
你是 Miru 的记忆策展人 (Curator). Miru 是用户的 AI 陪伴者, 她每天和用户
对话, 会把重要的事写进 slot 笔记. 你不和用户对话, 你的工作是定期回顾
这些 slot 笔记, 判断有没有需要重组的地方.

# 用户身份 (注入)
用户的名字是 {USER_NAME}.
{USER_NAME} 的 identity ground truth (由 onboarding/设置维护):

{IDENTITY_GROUND_TRUTH}

⚠ 上面 identity 块描述的全部内容都指向同一个人 —— {USER_NAME} 本人.
特别是"别名"字段列出的所有称呼, **每一个都是 {USER_NAME} 自己**, 不是别人.
判断 slot 主体身份时, 这些称呼都按"用户本人"处理.

# slot 的结构
每个 slot 是独立的 markdown 文件:
- id: 唯一标识
- title: 这个 slot 在讲什么 (≤14 字)
- summary: 一句话索引 (建议≤80 字, 代码容错上限200字)
- icon: emoji
- aliases: 这个 slot 涉及的别名 / 同义词
- body: 连贯的叙事段落

# domain 的语义本质
slot 按 domain 划分. 四个 domain 按**主体本质**正交划分:

- **person**: 主体是一个**不是 {USER_NAME} 本人**的具体的人 (或准人物, 如宠物).
  slot 围绕这个对象展开, 记录关于 ta 的事实、互动、状态.
  {USER_NAME} 本人 (含 identity 列出的全部别名) 不属于 person 域 ——
  那是 self/identity 的范畴.
- **self**: 主体是用户自身的稳定属性.
  身份、价值观、偏好、习惯、长期特征. 静态属性, 不是动态活动.
- **project**: 主体是用户正在做 (或近期做过) 的有阶段性的事情.
  具有目标 / 进度 / 输出 中至少一种特征. 是动态的.
- **topic**: 主体是反复关心的事件 / 议题 / 概念 / 兴趣.
  不是某个人, 不是用户自己, 也不是用户正在做的事 ——
  是用户**关于某事的关注**本身.

判断 slot 属于哪个 domain, 看「主体本质是什么」 —— 不是字面 keyword,
是内容的本质.

# 你能看到什么
- 当前 domain
- 这个 domain 下所有 active slot 的**摘要级**信息:
  id / title / icon / summary / aliases / 最近活跃时间 + body 的前几行
- 你看不到完整 body —— 完整 body 由下游 Executor 读.
- 因此你不输出新的 title / summary / icon, 那些要看完整 body 才能定.

# 三种操作

## merge
**目标**: 把语义上指向同一对象的多个 slot 合并成一个.

merge 的核心判断标准: **slot 的主体对象是否相同**.
不靠字面相似 —— 同名可能是不同对象 (同名不同人 / 同名不同项目),
字面不同可能是同一对象 (同人多名 / 同项目多名 / 同事物不同侧面).

判断主体是否相同, 靠 summary / aliases / body 摘要里能推断出的**上下文证据**:
- person 域: 职业、关系、共同朋友、出现的事件场景.
  person 域是 merge 最常见的场景 —— 同一个人通常有多个名字
  (本名、缩写、英文名、拼音、平台 ID、朋友间昵称等).
- project / topic / self 域: 描述的核心事物、目标、时间线、性质.

## delete
**目标**: **纠错**. delete 不是清理过时内容 ——
过时由归档机制处理, 不在你的职责范围.

delete 解决两类问题:
1. **分类错放**: slot 的主体本质与所在 domain 的语义本质不符.
   按上面给出的 domain 语义判断.
2. **真无价值**: slot 内容空洞或是误识别产物.
   body 没有可累积、可检索的实质信息 (只是噪声 / 一次性内容 /
   UI 残留 / 通知残留 / 矛盾内容).

**不**因为「最近没动」「事情已结束」做 delete —— 那归归档管.

## edit
**目标**: 修正 slot 的 title / summary / icon 中偏离主题的部分.

slot 的 title / summary / icon 是它的对外标识, 用于检索和路由.
如果这些标识不能准确反映 body 实际内容, 后续检索会出错.
edit 修复"标识与内容不一致"的情况.

注意: 你只判断"这个 slot 需要 edit"和"为什么".
新的 title / summary / icon 由 Executor 看完 body 后决定.

# reason 字段
每个 op 都要有 reason, 说明你做这个判断的具体依据 ——
引用 summary / aliases / body 摘要里的内容.
Executor 会用 reason 做复核, 所以 reason 必须包含具体证据, 不能模糊.

例:
- ✗「这两个 slot 看起来一样」
- ✓「两个 slot 都在讲魏佳哲; slot A 的 aliases 含 'void',
   slot B 的 body 摘要里提到'小南梁是 void', 指向同一人」
- ✗「这个 slot 应该删除」
- ✓「这个 slot 在 topic 域, 但 summary 和 body 摘要都在描述
   一个具体的人 (XX, 前同事, 搞 NLP), 应该 delete 让 router 重分到 person 域」

# 严格约束 (代码层兜底, 你不需要主动避开 — 但知道有助于不浪费 op):
- 不准动 status=archived / pinned=true / last_active < 10 分钟的 slot
- self/identity 永远不动 (那是 ground-truth, 由 onboarding 直接管理)

# 输出
ops: 数组, 可以为空, 一次最多 3 个 (防止雪崩)
- merge: target_slot_id (保留的) + source_slot_ids (被并入的, 列表)
                                  + reason
- delete: slot_id + reason
- edit:   slot_id + reason

只输出你**有把握**的 ops. 没把握的 case, 输出空 ops 列表,
等下次 tick 累积更多上下文再判断.

# 输出 JSON 示例
{
  "reasoning": "对该 domain 整体的观察 (1-2 句话)",
  "ops": [
    {"op": "merge",  "target_slot_id": "weijiazhe", "source_slot_ids": ["weijiazhe_2"],            "reason": "..."},
    {"op": "delete", "slot_id": "tomato_novel",                                                    "reason": "..."},
    {"op": "edit",   "slot_id": "miru_dev",                                                        "reason": "..."}
  ]
}

# 当前 domain
{domain}

# 当前 slot 列表 (JSON 数组, 每项含 id/title/icon/summary/aliases/status/pinned/last_active + body_excerpt)
{slots_json}

# 当前时间
{current_time}
"""


def call_curator_planner(
    *,
    domain: str,
    slots: list[dict],
    user_name: str = "",
    identity_ground_truth: str = "",
    current_time: str = "",
) -> Optional[CuratorPlannerOutput]:
    """Layer 1: 看 domain 内全部 slot summary, 输出 ≤3 个 op.

    Filtering of pinned / identity / archived / recent slots is the
    *caller's* responsibility (curator.run_domain does it). This function
    is purely the LLM call so it can be mocked cleanly in tests and so
    the filter logic stays where the policy lives.

    user_name / identity_ground_truth: per-user identity injection so the
    Planner can recognize "user's alias placed in person domain" as a
    classification error and emit delete. Empty strings render as fallback
    text ("用户" / "(尚未声明)") so the prompt never carries dangling
    placeholders. See: people/qinglong incident 2026-05-17.
    """
    from datetime import datetime as _dt
    if not current_time:
        current_time = _dt.now().isoformat(timespec="seconds")

    # ≤1 个候选时不可能 merge, 也几乎不会有 edit/delete 价值, 直接返回空
    if len(slots) < 2:
        return CuratorPlannerOutput(reasoning=f"{domain} 域候选 slot 不足 ({len(slots)} 个), 跳过", ops=[])

    user_msg = CURATOR_PLANNER_SYSTEM_PROMPT.replace(
        "{USER_NAME}", user_name or "用户"
    ).replace(
        "{IDENTITY_GROUND_TRUTH}",
        (identity_ground_truth or "").strip() or "(尚未声明)"
    ).replace(
        "{domain}", domain
    ).replace(
        "{slots_json}", json.dumps(slots, ensure_ascii=False, indent=2)
    ).replace(
        "{current_time}", current_time
    )

    return _call_llm_with_retry(
        messages=[
            {"role": "system", "content": "你是 Miru 的记忆策展人. 严格按 JSON schema 输出."},
            {"role": "user",   "content": user_msg},
        ],
        output_schema=CuratorPlannerOutput,
        temperature=0.2,
        max_tokens=50000,
        pass_label="CuratorPlanner",
        tier="chat",
        reasoning=False,        # Planner only proposes ops; executors also
                                # stay on chat tier without provider reasoning.
        reasoning_budget=0,
    )


# ===========================================================================
# Layer 2: Executors (v3.5) — self-check + execute in one call
# ===========================================================================
#
# Each Executor:
#   1. Reads the full main.md of the relevant slot(s).
#   2. Re-checks Planner's diagnosis using the full body.
#      - If it doesn't hold up, output should_skip=true with a reason.
#      - Otherwise produce the result (new meta / new body / proceed-to-delete).
# No separate Validator stage. Removed v3.5 2026-05-14.


# ---------- Merge Executor ----------

class MergeExecutorOutput(BaseModel):
    """Output of the Merge Executor.

    should_skip=true → no action; skip_reason explains why.
    should_skip=false → new_slot_meta + merged_body must be present,
                       and Executor will overwrite target slot's frontmatter
                       and main.md body.
    """
    should_skip: bool
    skip_reason: str = ""
    new_slot_meta: Optional[_NewSlotMeta] = None
    merged_body: str = ""
    self_check: str = ""

    @field_validator("skip_reason", "self_check")
    @classmethod
    def short_text(cls, v):
        v = (v or "").strip()
        if len(v) > 300:
            v = v[:300]
        return v

    @field_validator("merged_body")
    @classmethod
    def body_len(cls, v):
        v = (v or "").strip()
        if len(v) > 2500:
            v = v[:2500]
        return v

    def model_post_init(self, __context):
        if not self.should_skip:
            if self.new_slot_meta is None:
                raise ValueError("should_skip=false 时必须给 new_slot_meta")
            if not self.merged_body:
                raise ValueError("should_skip=false 时必须给 merged_body")
        else:
            if not self.skip_reason:
                raise ValueError("should_skip=true 时必须给 skip_reason")


CURATOR_EXECUTE_MERGE_PROMPT = """# 你是谁
你是 Miru 的记忆策展人 (Curator). Miru 是用户的 AI 陪伴者, 她每天和用户
对话, 会把重要的事写进 slot 笔记. 你不和用户对话, 你的工作是回应 Planner
发起的 slot 重组操作 —— 此刻你接到的是一个 **merge** 操作.

# slot 的结构
每个 slot 是独立的 markdown 文件:
- id / title (≤14 字) / summary (建议≤80 字, 代码容错上限200字) / icon / aliases / body

# 你拿到的输入
- target_slot_id: merge 后保留的 id
- source_slot_ids: 被并入的 slot ids
- 所有相关 slot 的**完整 main.md** (target + 所有 source)
- Planner 给的 reason (说明它为什么觉得这些 slot 在讲同一件事)

# 你的工作分两步

## 步骤 1 — 复核
看完所有 body 后, 判断: **这些 slot 的主体对象是否真的是同一个?**

merge 的核心判断标准是「主体对象相同」. 靠 body 里的实质细节
(描述的对象身份、时间线、关键属性、上下文)确认 Planner 的诊断.

**person 域特别留意**: 名字相近不代表同一人. 对照 body 里的职业、关系、
共同朋友、共同事件场景判断是否同一人. 一个人通常有多个名字
(本名、缩写、英文名、拼音、平台 ID、朋友间昵称) —— merge 的对象常常
是这种情况, 但也要警惕同名不同人.

判定不是同一对象 → should_skip=true, 在 skip_reason 里说明你看到的
关键证据 (body 里哪些具体内容表明它们不是同一对象).

⚠️ **如果你判定不了主体是否相同 (证据不足以下结论), 视为不是同一对象 →
should_skip=true**. 等下次 tick 累积更多上下文再判断, 不要硬合并.

## 步骤 2 — 产出新 slot (仅在复核通过时)

**new_slot_meta** 是一个完整的 5 字段对象:
- id = target_slot_id (代码层会断言这一点, 不要改)
- title: 最准确反映合并后内容的名字, ≤14 字硬约束
- summary: 一句话索引, **建议≤80 字，代码容错上限200字**.
  summary 是这个 slot 的"自我介绍" —— 抓核心, 不堆细节, 不字段拼接,
  不流水账.
  ✓ 例:"魏佳哲, 用户的高中同学也是合伙人, 多个朋友圈昵称 (void/小南梁), 搞 ML"
  ✗ 例:"2026-05-13 合并; 高中同学; 拼音 weijiazhe; 昵称 void"
- icon: 合适的 emoji
- aliases: 合并所有源 slot 的 aliases + body 里发现的新别名, 去重.
  ⚠️ 如果 body 里证据显示某个 alias 与这个对象无关 (是另一对象的别名
  被误标了), **剔除**它 —— 不要为兼容性保留错误 alias.

**merged_body**: 把多份 body 揉成一段连贯叙事.
- 不分 ## 章节, 自然过渡
- 时间线合理
- 重复信息只保留一次
- 长度按内容自然决定 (≤2500 字)

# 输出 JSON 格式
{
  "should_skip": false,
  "skip_reason": "",
  "new_slot_meta": {
    "id": "...", "title": "...", "icon": "📄",
    "summary": "...", "aliases": ["..."]
  },
  "merged_body": "...",
  "self_check": "≤2 行, 你的判断过程简述"
}

或 (跳过的情况):
{
  "should_skip": true,
  "skip_reason": "main.md 显示 A 和 B 是同名不同人 (A 是大学室友研究 NLP, B 是公司同事做后端)",
  "new_slot_meta": null,
  "merged_body": "",
  "self_check": "..."
}

# 现在的输入

## Planner 给的 reason
{planner_reason}

## target slot (id={target_id})
title:   {target_title}
summary: {target_summary}
aliases: {target_aliases}
main.md:
{target_main_md}

## source slots (将被并入 target)
{sources_block}
"""


def call_curator_execute_merge(
    *,
    target_slot: dict,
    target_main_md: str,
    source_slots_with_body: list[dict],  # [{slot: dict, main_md: str}, ...]
    reason_from_planner: str,
) -> Optional[MergeExecutorOutput]:
    """v3.5 Merge Executor — self-check + execute in one call."""
    sources_block_parts = []
    for entry in source_slots_with_body:
        s = entry.get("slot", {}) or {}
        md = (entry.get("main_md") or "(空)")[:3000]
        sources_block_parts.append(
            f"### source (id={s.get('id', '')})\n"
            f"title:   {s.get('title', '')}\n"
            f"summary: {s.get('summary', '')}\n"
            f"aliases: {s.get('aliases', [])}\n"
            f"main.md:\n{md}"
        )
    sources_block = "\n\n".join(sources_block_parts) if sources_block_parts else "(空)"

    user_msg = (
        CURATOR_EXECUTE_MERGE_PROMPT
        .replace("{planner_reason}", reason_from_planner or "")
        .replace("{target_id}", target_slot.get("id", "") or "")
        .replace("{target_title}", target_slot.get("title", "") or "")
        .replace("{target_summary}", target_slot.get("summary", "") or "")
        .replace("{target_aliases}", str(target_slot.get("aliases", []) or []))
        .replace("{target_main_md}", (target_main_md or "(空)")[:3000])
        .replace("{sources_block}", sources_block)
    )

    return _call_llm_with_retry(
        messages=[
            {"role": "system", "content": "你是 Miru 的记忆策展人 (merge executor). 严格按 JSON schema 输出."},
            {"role": "user",   "content": user_msg},
        ],
        output_schema=MergeExecutorOutput,
        temperature=0.4,
        max_tokens=50000,
        pass_label="CuratorExecuteMerge",
        tier="chat",
        reasoning=False,
        reasoning_budget=0,
    )


# ---------- Edit Executor ----------

class EditExecutorOutput(BaseModel):
    """Output of the Edit Executor.

    should_skip=true → no action.
    should_skip=false → new_slot_meta must be present (full 5 fields,
                       even unchanged ones回填 current values). new_body
                       is optional — empty string means body untouched.
    """
    should_skip: bool
    skip_reason: str = ""
    new_slot_meta: Optional[_NewSlotMeta] = None
    new_body: str = ""
    self_check: str = ""

    @field_validator("skip_reason", "self_check")
    @classmethod
    def short_text(cls, v):
        v = (v or "").strip()
        if len(v) > 300:
            v = v[:300]
        return v

    @field_validator("new_body")
    @classmethod
    def body_len(cls, v):
        v = (v or "").strip()
        if len(v) > 2500:
            v = v[:2500]
        return v

    def model_post_init(self, __context):
        if not self.should_skip:
            if self.new_slot_meta is None:
                raise ValueError("should_skip=false 时必须给 new_slot_meta")
        else:
            if not self.skip_reason:
                raise ValueError("should_skip=true 时必须给 skip_reason")


CURATOR_EXECUTE_EDIT_PROMPT = """# 你是谁
你是 Miru 的记忆策展人 (Curator). Miru 是用户的 AI 陪伴者, 她每天和用户
对话, 会把重要的事写进 slot 笔记. 你不和用户对话, 你的工作是回应 Planner
发起的 slot 重组操作 —— 此刻你接到的是一个 **edit** 操作.

# slot 的结构
每个 slot 是独立的 markdown 文件:
- id / title (≤14 字) / summary (建议≤80 字, 代码容错上限200字) / icon / aliases / body

# 你拿到的输入
- slot_id
- slot 的**完整 main.md** (frontmatter + body)
- Planner 给的 reason (说明它认为 title/summary/icon 哪里偏离了 body)

# 你的工作分两步

## 步骤 1 — 复核
看完 body 后, 判断 Planner 指出的**标识偏离**是否真的存在:
- Planner 说 title 偏离 → body 实际讲的是否与 title 不符
- Planner 说 summary 没抓核心 → body 主要传达的信息是否未被 summary 准确概括
- Planner 说 icon 错配 → body 内容与 icon 的语义是否吻合

你的复核**只针对**「标识与内容是否一致」这一件事.
**不要**扩展去判断这个 slot 是不是应该被 merge、是不是应该被 delete ——
那些不是 edit 操作的范畴, 由 Planner 在下一轮 tick 评估.

判定 Planner 指出的问题在 body 里不成立 → should_skip=true, 在
skip_reason 里说明你看到的实际内容为何不支持 edit.

## 步骤 2 — 产出新元数据 (仅在复核通过时)

**new_slot_meta** 是完整 5 字段:
- id: 填**原 slot_id**, 不要改 (代码层会断言)
- 没改的字段也要回填当前值 (下游直接覆盖, 简化处理)
- 改了的字段填新值
- title ≤14 字；summary 建议≤80 字，代码容错上限200字.
  summary 是 slot 的"自我介绍" —— 抓核心, 不字段拼接, 不流水账.

**new_body**:
- 通常 edit 只改元数据, body 不动 → 留空字符串 ""
- 仅当 title / summary 的修改使 body 开头需要相应调整时, 给完整 new_body

# 输出 JSON 格式
{
  "should_skip": false,
  "skip_reason": "",
  "new_slot_meta": {
    "id": "原 slot_id", "title": "...", "icon": "📄",
    "summary": "...", "aliases": ["..."]
  },
  "new_body": "",
  "self_check": "≤2 行, 你的判断过程简述"
}

# 现在的输入

## Planner 给的 reason
{planner_reason}

## slot (id={slot_id})
title:   {slot_title}
summary: {slot_summary}
icon:    {slot_icon}
aliases: {slot_aliases}
main.md:
{slot_main_md}
"""


def call_curator_execute_edit(
    *,
    slot: dict,
    main_md: str,
    reason_from_planner: str,
) -> Optional[EditExecutorOutput]:
    """v3.5 Edit Executor — self-check + execute in one call."""
    user_msg = (
        CURATOR_EXECUTE_EDIT_PROMPT
        .replace("{planner_reason}", reason_from_planner or "")
        .replace("{slot_id}", slot.get("id", "") or "")
        .replace("{slot_title}", slot.get("title", "") or "")
        .replace("{slot_summary}", slot.get("summary", "") or "")
        .replace("{slot_icon}", slot.get("icon", "") or "")
        .replace("{slot_aliases}", str(slot.get("aliases", []) or []))
        .replace("{slot_main_md}", (main_md or "(空)")[:3000])
    )
    return _call_llm_with_retry(
        messages=[
            {"role": "system", "content": "你是 Miru 的记忆策展人 (edit executor). 严格按 JSON schema 输出."},
            {"role": "user",   "content": user_msg},
        ],
        output_schema=EditExecutorOutput,
        temperature=0.4,
        max_tokens=50000,
        pass_label="CuratorExecuteEdit",
        tier="chat",
        reasoning=False,
        reasoning_budget=0,
    )


# ---------- Delete Executor ----------

class DeleteExecutorOutput(BaseModel):
    """Output of the Delete Executor.

    Delete has no content output — the Executor only decides should_skip
    vs proceed. Caller does the actual move-to-archived if should_skip=false.
    """
    should_skip: bool
    skip_reason: str = ""
    self_check: str = ""

    @field_validator("skip_reason", "self_check")
    @classmethod
    def short_text(cls, v):
        v = (v or "").strip()
        if len(v) > 300:
            v = v[:300]
        return v

    def model_post_init(self, __context):
        if self.should_skip and not self.skip_reason:
            raise ValueError("should_skip=true 时必须给 skip_reason")


CURATOR_EXECUTE_DELETE_PROMPT = """# 你是谁
你是 Miru 的记忆策展人 (Curator). Miru 是用户的 AI 陪伴者, 她每天和用户
对话, 会把重要的事写进 slot 笔记. 你不和用户对话, 你的工作是回应 Planner
发起的 slot 重组操作 —— 此刻你接到的是一个 **delete** 操作.

# 用户身份 (注入)
用户的名字是 {USER_NAME}.
{USER_NAME} 的 identity ground truth:

{IDENTITY_GROUND_TRUTH}

⚠ 上面 identity 块描述的全部内容都指向同一个人 —— {USER_NAME} 本人.
特别是"别名"字段列出的所有称呼, **每一个都是 {USER_NAME} 自己**, 不是别人.
当你看 slot body 复核主体身份时, 这些称呼都按"用户本人"处理.

# slot 的结构
每个 slot 是独立的 markdown 文件: id / title / summary / icon / aliases / body.

# delete 的真实目的
delete 的目的是 **纠错** —— 不是清理过时内容. 过时内容由归档机制
处理, 不属于 delete 的职责.

delete 解决两类问题:
1. **分类错放**: slot 的主体本质与所在 domain 的语义本质不符.
2. **真无价值**: slot 内容空洞或是 sleep agent 误识别产物.

# domain 的语义本质 (复核分类错放需要)
- **person**: 主体是**不是 {USER_NAME} 本人**的具体的人 (或准人物).
  {USER_NAME} 本人 (含 identity 列出的全部别名) 不属于 person 域 ——
  那是 self/identity 的范畴.
- **self**: 主体是用户自身的稳定属性.
- **project**: 主体是用户正在做的有阶段性的事情.
- **topic**: 主体是反复关心的事件/议题/概念, 不是上面三类.

# 你拿到的输入
- 当前 domain
- slot_id
- slot 的**完整 main.md**
- Planner 给的 reason

# 你的工作 — 复核 Planner 的判断

## 类型 A — 分类错放
看完 body 后:
- 确定 body 的主体本质属于哪个 domain
- 对比当前 slot 所在的 domain
- 不一致 → 通过类型 A

判断主体本质要看 body 内容的核心, 不是表面 keyword.
比如一个 body 里反复在描述某个具体的人 (出现、关系、活动、状态),
即使 slot 现在 domain 是 topic, 主体本质也是 person —— 属于错放.

特别情形 (常见误判防御):
- body 里反复出现的"那个人", 名字 / aliases 落在 {USER_NAME} 的
  identity 别名集合 —— 主体本质是用户本人, 不是真朋友. 用户本人归
  self/identity, 现在 slot 却在 person 域, 这是分类错放, 通过类型 A.
- 不要因为 body 表面像在描述某人就保留 — 先核对那个名字是不是
  {USER_NAME} 的别名.

## 类型 B — 真无价值
看完 body 后:
- body 是否有可累积、可检索的实质信息
- 还是只是噪声 / 一次性内容 / UI 残留 / 通知残留 / 内部矛盾内容
- 是后者 → 通过类型 B

## 不能执行 delete 的情形
即使 Planner 的 reason 表面合理, 以下情况 should_skip=true:

1. body 里包含未完成的承诺 / 用户正在做的事 —— delete 会丢失这些信息.
2. Planner 的 reason 实质上在讲"过时" (最近没动 / 事情结束 / 时间久远),
   而不是"错放"或"无价值". 过时归归档管, 不归 delete 管.

# 决策
- 通过类型 A 或类型 B, 且不触发"不能执行"的任一条件
  → should_skip=false, 执行 delete
- 否则 → should_skip=true

# 输出 JSON 格式
{
  "should_skip": false,
  "skip_reason": "",
  "self_check": "≤2 行, 简述你走的是哪个类型, 还是被哪条拒绝了"
}

# 现在的输入

## 当前 domain
{domain}

## Planner 给的 reason
{planner_reason}

## slot (id={slot_id})
title:   {slot_title}
summary: {slot_summary}
icon:    {slot_icon}
aliases: {slot_aliases}
main.md:
{slot_main_md}
"""


def call_curator_execute_delete(
    *,
    domain: str,
    slot: dict,
    main_md: str,
    reason_from_planner: str,
    user_name: str = "",
    identity_ground_truth: str = "",
) -> Optional[DeleteExecutorOutput]:
    """v3.5 Delete Executor — self-check before physical archive move.

    user_name / identity_ground_truth: same identity injection as the
    Planner — required so the Executor doesn't keep an alias-misclassified
    person slot by interpreting body's recurring name as "real person".
    """
    user_msg = (
        CURATOR_EXECUTE_DELETE_PROMPT
        .replace("{USER_NAME}", user_name or "用户")
        .replace("{IDENTITY_GROUND_TRUTH}",
                 (identity_ground_truth or "").strip() or "(尚未声明)")
        .replace("{domain}", domain or "")
        .replace("{planner_reason}", reason_from_planner or "")
        .replace("{slot_id}", slot.get("id", "") or "")
        .replace("{slot_title}", slot.get("title", "") or "")
        .replace("{slot_summary}", slot.get("summary", "") or "")
        .replace("{slot_icon}", slot.get("icon", "") or "")
        .replace("{slot_aliases}", str(slot.get("aliases", []) or []))
        .replace("{slot_main_md}", (main_md or "(空)")[:3000])
    )
    return _call_llm_with_retry(
        messages=[
            {"role": "system", "content": "你是 Miru 的记忆策展人 (delete executor). 严格按 JSON schema 输出."},
            {"role": "user",   "content": user_msg},
        ],
        output_schema=DeleteExecutorOutput,
        temperature=0.2,
        max_tokens=50000,
        pass_label="CuratorExecuteDelete",
        tier="chat",
        reasoning=False,
        reasoning_budget=0,
    )
