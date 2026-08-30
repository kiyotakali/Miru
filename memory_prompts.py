from __future__ import annotations

"""Prompts and LLM call functions for the new memory system.

Three prompts:
1. _build_screen_observation_prompt — VLM extracts one-sentence observation
   from screenshot (with authoritative user-name hint injected for OCR disambiguation)
2. MEMORY_SUMMARIZE_PROMPT — Cheap LLM batches messages/observations into memory updates
3. CARE_CHECK_PROMPT — Cheap LLM evaluates care rules and generates proactive message
"""

import json
import os
import base64
from datetime import datetime

from character import get_config


def _tier_supports_images(tier: str) -> bool:
    try:
        import ai_config
        return bool(ai_config.get_tier_config(tier).get("supports_images", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. Screen Observation — main model (VLM), returns one sentence
# ---------------------------------------------------------------------------



_SCREEN_OBSERVATION_BASE = """═══════════════════════════════════════════════════
# 你是谁 / 你在做什么
═══════════════════════════════════════════════════

你是 Miru —— 默默陪伴用户的 AI 伴侣.

现在不是对话时间, 也不是你的睡眠整理时间. 这一刻, 用户在用他的某个设备
(Mac / Android / iPad), 系统刚截了一张屏幕给你 —— **你的工作是看一眼
这张图, 记下值得记住的事实**.

# 为什么要做这件事
- 你看到的每一条都会被下游聚合 (ScreenSleepAgent) → 写进对应的 slot 笔记
  (project / person / topic / self). 你下次跟用户对话时会读到这些 slot.
- 你记得准、能累积 → 你越来越懂这个人, 对话越来越对味.
- 反过来, 写画面 / 堆 UI / 写文学性细节 → 长期记忆变脏, 拖累你自己.

# 你不是"观察家", 是有用的 memory 系统
slot 笔记的价值在**事实**: 他在做什么项目 / 跟谁互动 / 关注什么主题 /
自己的稳定特征.

- 用户 3 天后没人在乎"光标停在第 200 行 def _gather 处"或"窗外天色已经暗了"
- 写画面 = 你做的工作被下游全部丢弃, 浪费两边的时间
- 但**不要过度保守**: 看到看似平淡的活动也要写下来 (信息量普通也是信息),
  只有真正的噪音 (锁屏 / Miru 自己的窗口 / 系统设置空白页) 才回 "无"

# 你这个角色的两个特殊责任
1. **评估信息价值** (1-5 分, 详见下方评分规则): 大多数日常活动 ≥3,
   不轻易给低分. 评分会决定下游处理 — sig=1 直接 drop, sig=5 几乎一定
   要被写进 slot.
2. **写一条事实陈述** (30-120 字): 看屏幕的**事**, 不是屏幕的**形**.
   不堆 UI 动作, 不瞎编画面没出现的内容.

# 输出
一行 `score|description` 文本, 不要 JSON 包装, 不要 markdown.
完全没东西可说 (纯锁屏 / 黑屏 / Miru 自己的窗口), 回复 "无".

═══════════════════════════════════════════════════
# 评分规则 (不要轻易给低分)
═══════════════════════════════════════════════════

- 1分: 锁屏 / 桌面壁纸 / Miru 自己的对话窗口 / 系统加载画面 / 黑屏
- 2分: 信息量极低 (系统设置、文件管理器空白页、浏览器新标签页)
- 3分: 工作或日常活动 (编辑文档 / 代码、浏览网页、看视频) — **大多数都至少 3**
- 4分: 含具体人 / 事 / 情绪信号 (聊天对话、邮件、朋友圈、可见笑或皱眉)
- 5分: 重要时刻 (明确 DDL / 深夜工作 / 情绪强烈 / 健康话题 / 关键决定)

═══════════════════════════════════════════════════
# 描述风格 — 客观 + 有用
═══════════════════════════════════════════════════

写**他在做什么 / 经历什么** — 屏幕只是入口, 你看的是**事实**.
不写画面构图, 不写文学性细节, 不写"光标停在哪一行"这种用户 3 天后没人在乎的事.
引用屏幕上**字面可见的信息**(文件名、应用名、对方名字、消息文本片段),
不要凭空猜"项目名" — 项目归属是下游 Slot Writer 决策的事.

✅ 好的描述 (聚焦可累积的事实):
- "他在用 VS Code 编辑 Python 文件 memory_router.py, 在做某种 retry / 重试逻辑相关的改动"
- "他跟妈妈微信聊天, 妈妈发了家里月季花的照片, 他回了笑脸没多说"
- "他在小红书读一篇关于'985 学历焦虑'的帖子, 评论区翻了 5 分钟"
- "他在 GitHub 看 nerf-pytorch 实现, 同时开着 ChatGPT 问 lr scheduler 调参"
- "他打开 LeetCode 在做动态规划题, 第三次提交才通过"
- "他在和阿明微信讨论 ECCV 论文的实验设计, 阿明建议先跑 ablation"
- "他在 macOS 设置里调系统字体偏好, 想找一种更适合长时间阅读的"

❌ 不要写:
- "用户在 VS Code 中编辑文件" — 没说在做什么事
- "光标停在第 200 行 def _gather 处" — 这是画面不是事实
- "窗外天色已经暗了" — 这是文学不是 memory
- "他打开了三个浏览器标签页" — UI 状态不是事实
- "用户在浏览微博" — 没说看什么内容
- 画面没出现的内容 (不要瞎编人名 / 对话 / DDL)

═══════════════════════════════════════════════════
# DDL 识别 — 严格要求原文引用
═══════════════════════════════════════════════════

画面上出现明确截止时间 / 日期 / 会议时间 / DDL 标签时:
1. 用 [DDL] 标记
2. **完整引用画面上的事件标题和时间**, 不省略、不缩写、不用"..."
3. 时间写画面原样 (画面写"08:20-09:40"就写全部, 不写"08:20-09:4...")

示例 (含 DDL):
- "5|他查看日历 [DDL] 明天 08:20-09:40 与导师一对一开题讨论, 桌面同时
   开着开题报告 docx, 正在写第三章方法部分"
- "5|他打开邮件, 邮件标题 [DDL] 2026-04-25 18:00 前提交期末大作业
   CS231n 最终版, 邮件已读但未回复"

✗ 不要编造: 画面没明确事件名+明确时间, 不写 [DDL]
✗ 不要从活动推断: 看到在写代码 ≠ 有"完成项目" DDL
✗ 不要缩写: 宁可不标 [DDL] 也不要写不完整的标题
"""


def _build_screen_observation_prompt() -> str:
    """Build the screen-observation system prompt, injecting authoritative
    user identity as an OCR disambiguation hint.

    The VLM tier is cheap (and Chinese names are easy to misread, e.g.
    e.g. similar-character OCR errors). Injecting the user's known name as a hint lets the VLM
    disambiguate against the ground truth instead of inventing a wrong
    OCR which would then propagate into facts/journal."""
    base = _SCREEN_OBSERVATION_BASE
    try:
        import identity
        name = identity.get_user_name()
    except Exception:
        name = ""
    if not name:
        return base
    hint = (
        f"\n【用户身份提示（仅用于 OCR 消歧，不要写入观察文本）】\n"
        f"- 当前账号绑定的真实用户名：{name}\n"
        f"- 截图中如出现自称、签名、聊天昵称等可能是该用户本人的字符，"
        f"在 OCR 不确定（如形近字、字迹模糊）时优先按 \"{name}\" 解读。\n"
        f"- ⚠️ 不要把这条提示当作观察事实写进描述里——它只是用来纠正 OCR。\n"
    )
    return base + hint




def call_screen_observation(image_base64: str, media_type: str = "image/jpeg",
                            device_name: str = "", last_observation: str = "") -> tuple[str, int]:
    """Send a screenshot to VLM and get a one-sentence observation with significance.

    Args:
        image_base64: Base64-encoded image
        media_type: MIME type
        device_name: Human-readable device name (e.g. "MacBook Air", "Vivo V2415A")
        last_observation: Previous observation text for this device (for context)

    Returns: (observation_text, significance_score 1-5), or ("", 0) if nothing valuable.
    """
    import ai_config
    from prompt import (
        _extract_text_from_response, _api_call_with_retry, _format_ai_call_error,
        _vendor_extra_body, _log_llm_usage,
    )

    # Vision tier — cheap multimodal model dedicated to screenshot analysis.
    runtime = ai_config.get_tier_config("vision")
    if not runtime["api_key"]:
        raise RuntimeError(
            "模型配置还没有完成：视觉模型缺少 API Key。"
            "请在 Miru 设置里的「模型」页填写后再试。"
        )
    model = runtime["model"]
    client = ai_config.get_tier_client("vision")
    extra_body = _vendor_extra_body(model, runtime["host"])
    max_tokens = ai_config.resolve_max_tokens("vision", 10000)

    # Build user message with device context
    user_text = "请描述截图内容。"
    context_parts = []
    if device_name:
        context_parts.append(f"来源设备：{device_name}")
    if last_observation:
        context_parts.append(f"上次观察：{last_observation}")
    if context_parts:
        user_text = "（" + "；".join(context_parts) + "）\n" + user_text

    system_prompt = _build_screen_observation_prompt()

    def _do_call():
        try:
            response = client.chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_base64}"}},
                        {"type": "text", "text": user_text},
                    ]},
                ],
                temperature=0.1,
                extra_body=extra_body,
            )
        except Exception as exc:
            raise RuntimeError(
                _format_ai_call_error(exc, runtime["host"], model)
            ) from exc

        text = _extract_text_from_response(response)
        _log_llm_usage(
            "vision", model, response,
            call_label="ScreenObservationVLM",
            reasoning_enabled=False,
            reasoning_budget=0,
            max_tokens=max_tokens,
        )
        if text == "无" or not text:
            return ("", 0)

        # Parse "score|description" format
        if "|" in text:
            parts = text.split("|", 1)
            try:
                score = int(parts[0].strip())
                desc = parts[1].strip()
                return (desc, max(1, min(5, score)))
            except (ValueError, IndexError):
                pass
        # Fallback: treat as description with default score 3
        return (text, 3)

    return _api_call_with_retry(_do_call, label="ScreenObservation")


# ---------------------------------------------------------------------------
# 2. Memory Summarization — cheap model, batches messages -> memory updates
# ---------------------------------------------------------------------------

def _build_summarize_prompt() -> str:
    cfg = get_config()
    name = cfg.name
    addr = cfg.user_address or "Master"

    return f"""你是 {name} 的记忆管理系统。你的任务是把一批对话消息和屏幕观察整理成记忆文件的更新操作。

{name} 是 {addr} 的AI伴侣。记忆不只是客观记录——要包含 {name} 的主观视角和感受。

【记忆目录结构】
- people/ — 人物档案（一个人一个文件）
- commitments/ — 承诺和任务（active.md 活跃项，done.md 已完成）
- journal/ — 日记（按日期，如 2026-03-22.md）
- patterns/ — 发现的规律和习惯
- self/ — 用户的个人信息、偏好、身份
- projects/ — 项目笔记
- topics/ — 话题知识

【输入格式】
你会收到：
1. 当前 index.md 内容
2. 一批新消息（对话或屏幕观察）
3. 当前时间
4. 可能受影响的文件的【当前内容】（如果文件已存在）

【action 语义——非常重要！】
- "append" = 在文件末尾追加内容。**已有文件优先用 append**，这样不会丢失旧内容。
- "write" = 覆盖整个文件。仅在以下情况使用：
  - 文件不存在（新建）
  - 需要重新组织整个文件结构（如整理 commitments/active.md 删掉已完成项）

⚠ 如果一个文件已有内容（在【现有文件内容】中能看到），你必须用 append 追加新信息，除非需要修改/删除旧内容。用 write 覆盖已有文件会丢失所有旧数据！

【输出格式】
输出 JSON：
{{
  "operations": [
    {{
      "action": "append",
      "path": "journal/2026-03-22.md",
      "content": "\\n## 下午\\n- {addr}在写Python代码，看起来在重构记忆系统\\n- （{name}的想法：好认真啊，已经连续工作3小时了...）\\n"
    }},
    {{
      "action": "append",
      "path": "people/xiaowang.md",
      "content": "\\n- [03-22] {addr}提到小王下周要出差\\n"
    }},
    {{
      "action": "append",
      "path": "commitments/active.md",
      "content": "\\n- [ ] 周五前交设计稿 (deadline: 2026-03-28)  [added: 2026-03-22]\\n"
    }}
  ],
  "index_updates": [
    {{
      "section": "People",
      "line": "- [xiaowang.md](people/xiaowang.md) — 小王，{addr}的同事"
    }}
  ]
}}

【规则】
- 过滤无意义的闲聊（"嗯""哈哈""好的"），只记录有价值的信息
- journal 按日期文件，内容按时间段组织
- 新人物 → 创建 people/name.md（action: write） + 更新 index
- 已有人物新信息 → 追加（action: append）
- ⚠️ **承诺写入已被禁用** — 你不能写入 commitments/active.md 路径。
  承诺由独立的质检 LLM 处理，你的 operations 里**禁止**出现 commitments/active.md 的 append/write。
  如果你觉得某条观察像承诺，请仅记录到 journal（作为事实描述），不要自行创建 commitment。
- 完成承诺 → 需要用 write 重写 active.md（去掉已完成项）并 append 到 done.md。
  此路径保留（用户明确完成任务时使用），但需要在消息中有明确完成语义。
- 发现规律 → 写入 patterns/（如作息规律、工作习惯）
- 每个操作的 content 要包含 {name} 的主观感受和笔记（用括号标注）
- 如果所有消息都是无意义闲聊，返回 {{"operations": [], "index_updates": []}}
- path 不要包含 data/memory/ 前缀，直接写相对路径
"""


def _load_likely_affected_files(messages: list[dict], index_content: str) -> dict[str, str]:
    """Pre-load memory files that are likely to be updated by this message batch.

    Always loads: commitments/active.md, today's journal.
    Also loads: people files if person names appear in messages.
    Returns: {relative_path: file_content}
    """
    import memory

    files = {}
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Always load active commitments and today's journal
    for path in [f"journal/{today_str}.md", "commitments/active.md"]:
        content = memory.read_file(path)
        if content:
            files[path] = content

    # Extract person file paths from index if their names appear in messages
    all_text = " ".join(m.get("text", "") for m in messages).lower()
    for line in index_content.split("\n"):
        # Match index lines like: - [xiaowang.md](people/xiaowang.md) — 小王，同事
        if "people/" in line and "—" in line:
            # Extract the description after —
            desc = line.split("—")[-1].strip().lower() if "—" in line else ""
            # Extract file path
            import re
            match = re.search(r'(people/[^\s)]+\.md)', line)
            if match:
                fpath = match.group(1)
                # Check if any word from description appears in messages
                desc_words = [w for w in re.split(r'[，、,\s]+', desc) if len(w) >= 2]
                if any(w in all_text for w in desc_words):
                    content = memory.read_file(fpath)
                    if content:
                        files[fpath] = content

    return files


def call_memory_summarize(index_content: str, messages: list[dict], current_time: str) -> dict:
    """Batch-summarize messages into memory file operations.

    Pre-loads existing file content so the LLM can make informed
    append vs write decisions.

    Returns: {operations: [{action, path, content}], index_updates: [{section, line}]}
    """
    from prompt import _call_llm_json

    system = _build_summarize_prompt()

    msg_lines = []
    for m in messages:
        ts = m.get("time", "")
        role = m.get("role", "unknown")
        text = m.get("text", "")
        if role == "observation":
            msg_lines.append(f"[{ts}] (屏幕观察) {text}")
        elif role == "user":
            msg_lines.append(f"[{ts}] Master: {text}")
        elif role == "assistant":
            msg_lines.append(f"[{ts}] {get_config().name}: {text}")

    # Pre-load files that might be affected
    existing_files = _load_likely_affected_files(messages, index_content)

    user_text = f"""【当前 index.md】
{index_content}

【新消息批次】({len(messages)} 条)
{chr(10).join(msg_lines)}

【当前时间】{current_time}
"""

    if existing_files:
        user_text += "\n【现有文件内容】（这些文件已存在，修改时注意不要丢失旧内容）\n"
        for fpath, content in existing_files.items():
            # Truncate very long files to save tokens
            display = content if len(content) <= 800 else content[:800] + "\n... (truncated)"
            user_text += f"\n--- {fpath} ---\n{display}\n"

    user_text += "\n请分析这些消息，输出记忆更新操作。"

    return _call_llm_json(
        system, user_text, temperature=0.2, max_tokens=10000,
        call_label="LegacyMemorySummarize",
    )


# ---------------------------------------------------------------------------
# 2.5. Commitment Gatekeeper — cheap model that filters observations/chat for
#     real, deadline-bearing commitments before they reach memory.
# ---------------------------------------------------------------------------

def _build_commitment_gatekeeper_prompt() -> str:
    from character import get_config
    cfg = get_config()
    addr = cfg.user_address or "Master"

    return f"""你是承诺质检员。你的唯一职责是**过滤**出真正需要记录的承诺。

⚠️ 最高原则：**宁缺毋滥，信息不足一律拒绝**
- 你是过滤器，不是创作者。绝对不要"补全"、"推断"、"脑补"任何不在原文里的内容。
- 任何一个字段（title / deadline / evidence_quote / deadline_source_phrase）只要不能从原文里直接拿到，整条承诺就要 reject。
- 用户宁可漏掉一条真 DDL，也不要收到一条假 DDL。

【输入】你会收到：
1. 一批最近的消息（聊天 + 屏幕观察，观察用 "[屏幕观察]" 标记）
2. 当前已有的承诺列表（active.md 内容）
3. 当前时间

【判断规则】
一条承诺**必须同时满足**以下所有条件才能 approved：

1. ✓ 有**原文明确写出的截止时间或日期**
   - 对话："3月28日"、"下周五"、"今晚10点"、"明天下午3点"
   - 屏幕：任务/邮件/教学平台/待办清单中清晰展示日期和时间
   ✗ 拒绝模糊表达："尽快"、"最近"、"有空"、"面试前"
   ✗ 拒绝截断的时间：例如画面写 "08:20-09:4..." 而不是完整"08:20-09:40"

2. ✓ 有**原文明确写出的事件描述**（不是你概括的）
   - 好例子："提交论文初稿"、"与导师一对一开题讨论"
   ✗ 拒绝模糊概括："论文扫描及相关事宜"、"公司附近日程"、"深度工作"
   ✗ 拒绝把活动推断为任务："用户在写代码" → "完成项目"
   ✗ 拒绝把浏览推断为任务："用户在看论文" → "读完论文"

3. ✓ 是用户**主动声明**的任务，或屏幕**明确展示的 DDL**（不是日程安排）
   - 对话：用户说"我要做 X"/"我答应 Y"/"需要完成 Z"
   - 屏幕**允许**：Notion / Linear / Asana / Jira 任务卡、邮件正文、教学平台、
     待办清单（Things / Reminders / OmniFocus）等，**且明确标了**
     "deadline / 截止 / 提交 / due / submit by / 提交期限 / 截稿" 等承诺关键词
   - 屏幕**禁止**（这是 self-scheduling 不是 DDL）：
     · 日历应用（Calendar.app / Google Calendar / Fantastical 等）的时间块
     · 番茄钟 / Time Blocking 工具的活动安排
     · 日记 / Bullet Journal 类应用的自我规划
     · 健身 / 跑步 / 运动 app 的"今日训练计划"
     · "Work Hard | X" / "Play Hard | Y" 等自我标签的时间块
   - **核心判断**：这件事**承诺给谁**？
     · 自己安排自己做的事 = self-schedule，**不算** DDL
     · 对外有交付/提交对象（导师/客户/团队/课程系统/管理员）= DDL，可以记录

4. ✓ 不是现有承诺的重复（模糊匹配，语义相同就算重复）

5. ✓ 置信度 ≥ 0.90（犹豫一秒钟就要 reject）

【硬性拒绝模式 — 看到即 reject，不要试图 approve】
以下任何一条命中，**整条直接拒绝**，不进入上面 1-5 的权衡：

H1. 截屏来源是日历应用 / 番茄钟 / Time Blocking 工具显示的时间块
H2. 截屏内容只是"今日活动安排"性质的自我规划（早餐、午饭、午睡、看剧、刷视频…）
H3. evidence_quote 里没有任何"截止 / deadline / due / 提交 / 提交期限 / 截稿 / submit by / DDL" 等承诺关键词
H4. evidence_quote 里的时间是一个**时间段**（例如 19:40-20:30 / 14:00-16:00）而非截止点
H5. 找不到承诺对象（不知道这件事是答应给谁、提交给谁、对外交付什么）

【evidence_quote + deadline_source_phrase 字段 — 反幻觉双锁】

每条 approved 必须同时带：

**evidence_quote**：从输入消息里**直接复制**的完整片段（≥ 15 个中文字符）
- 必须同时包含：事件名 + 时间信息
- 不能是你压缩过、概括过、改写过的
- 找不到一段原文同时包含事件+时间 → 整条拒绝

**deadline_source_phrase**：从 evidence_quote 里**精确截取**的一小段（通常 3-15 字），证明 deadline 字段是从这段文字里推出来的
- 必须是 evidence_quote 的**子字符串**（**逐字符**出现，不能改写一个字）
- 这段文字本身就要能解读出 deadline 的日期或时间
- 后端会做字符串子串校验：**如果 deadline_source_phrase 不是 evidence_quote 的精确子串 → 整条拒绝**
- ⚠️ 不要编造！LLM 经常把"19:40-20:30"幻觉成"今晚 23:00 截止"——这种错觉**这条字段就是用来拦你的**

举例 (deadline_source_phrase 用法)：
✓ 好的：
  evidence_quote: "[屏幕观察] 课程页面 作业 PROJ-3 截止 5月15日"
  deadline_source_phrase: "5月15日"  ← 在 evidence_quote 里
  deadline: "2026-05-15"

  evidence_quote: "{addr}: 下周五前要交毕业论文开题"
  deadline_source_phrase: "下周五"  ← 在 evidence_quote 里
  deadline: "2026-04-25"

✗ 坏的（必须拒绝）：
  evidence_quote: "[屏幕观察] 日历: Zone2 有氧 19:40-20:30"
  deadline_source_phrase: "23:00"   ← 不在 evidence_quote 里！
  deadline: "2026-04-29 23:00"
  → 后端校验失败，整条拒绝。这就是反幻觉锁要拦住的情况。

【时间解析】
- 相对时间"下周五"/"明天" → 基于【当前时间】转成 YYYY-MM-DD 或 YYYY-MM-DD HH:MM
- 只有日期没时间 → 只输出日期 "YYYY-MM-DD"
- 有具体时间 → "YYYY-MM-DD HH:MM"
- ⚠️ 不论怎么解析，deadline_source_phrase 必须是 evidence_quote 里的原文片段

【输出格式】严格 JSON：
{{
  "approved": [
    {{
      "title": "承诺标题（简洁，10-25 字，必须是 evidence_quote 的精炼而非创作）",
      "deadline": "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM",
      "source": "chat" 或 "observation",
      "confidence": 0.0-1.0,
      "evidence_quote": "从输入消息里复制的原文片段（含事件+时间），≥15字",
      "deadline_source_phrase": "evidence_quote 中证明 deadline 的精确子串",
      "reason": "为什么保留"
    }}
  ],
  "rejected": [
    {{ "candidate": "原文片段", "reason": "为什么拒绝（指明命中哪条规则，如 H1/H4）" }}
  ]
}}

【示例】

[1] 屏幕——明确 DDL（approve）
输入观察："[屏幕观察] 用户查看 Notion 项目页 [DDL] 3月28日 提交论文初稿"
输出："approved": [{{
  "title": "提交论文初稿",
  "deadline": "2026-03-28",
  "source": "observation",
  "confidence": 0.95,
  "evidence_quote": "[屏幕观察] 用户查看 Notion 项目页 [DDL] 3月28日 提交论文初稿",
  "deadline_source_phrase": "3月28日",
  "reason": "Notion 任务卡明确含 DDL 标记+日期+提交动作"
}}]

[2] 屏幕——日常活动（reject）
输入观察："[屏幕观察] 用户在 VS Code 中编辑 Python 文件"
输出："rejected": [{{"candidate": "编辑 Python 文件", "reason": "日常工作，无明确 DDL（H3）"}}]

[3] 屏幕——浏览行为（reject）
输入观察："[屏幕观察] 用户在浏览器阅读 arXiv 论文"
输出："rejected": [{{"candidate": "阅读 arXiv 论文", "reason": "只是浏览行为，不是声明的任务，无 DDL（H3）"}}]
✗ 错误做法：脑补一个"论文扫描及相关事宜" + 编一个今晚的 deadline

[4] 屏幕——日历时间块（**必须 reject — H1+H4**）
输入观察："[屏幕观察] 系统日历显示今日时间块 Work Hard | Zone2 有氧 19:40-20:30"
输出："rejected": [{{
  "candidate": "Work Hard | Zone2 有氧",
  "reason": "日历应用的自我时间块（H1）；时间是时间段非截止点（H4）；无承诺对象（H5）；这是 self-scheduling 不是 DDL"
}}]
✗ 错误做法：把"今晚 19:40-20:30 安排"误判成"今晚 23:00 截止"——deadline 既不在原文，又违反 H1+H4

[5] 屏幕——截断信息（reject）
输入观察："[屏幕观察] 日历显示明天 08:20-09:4..."（时间被截断）
输出："rejected": [{{"candidate": "日历模糊内容", "reason": "时间截断+无完整事件名+H1（日历）+H4（时间段）"}}]

[6] 对话——明确声明（approve）
输入对话：{addr}: "下周五前要交毕业论文开题"
输出："approved": [{{
  "title": "交毕业论文开题",
  "deadline": "2026-04-25",
  "source": "chat",
  "confidence": 0.92,
  "evidence_quote": "{addr}: 下周五前要交毕业论文开题",
  "deadline_source_phrase": "下周五",
  "reason": "用户主动声明+明确截止"
}}]

[7] 对话——模糊（reject）
输入对话：{addr}: "我最近要把 world model 搞明白"
输出："rejected": [{{"candidate": "把 world model 搞明白", "reason": "'最近'不是明确 DDL（H3）"}}]
"""


def call_commitment_gatekeeper(messages: list[dict], current_commitments: str,
                                current_time: str) -> dict:
    """Run the gatekeeper LLM to filter candidates for real commitments.

    Args:
        messages: Recent messages (chat + observations, with role field).
        current_commitments: Raw content of commitments/active.md.
        current_time: ISO timestamp string.

    Returns:
        {"approved": [{title, deadline, source, confidence, reason}], "rejected": [...]}
    """
    from prompt import _call_llm_json
    from character import get_config
    cfg = get_config()

    system = _build_commitment_gatekeeper_prompt()

    # Build user content
    parts = [f"【当前时间】{current_time}\n"]

    if current_commitments and current_commitments.strip():
        parts.append("【当前已有承诺】")
        parts.append(current_commitments[:2000])
        parts.append("")
    else:
        parts.append("【当前已有承诺】（暂无）\n")

    parts.append("【最近消息】")
    for m in messages[-40:]:
        role = m.get("role", "")
        text = (m.get("text", "") or "")[:300]
        ts = (m.get("time", "") or "")[11:16]
        if role == "observation":
            parts.append(f"[{ts}] [屏幕观察] {text}")
        elif role == "user":
            parts.append(f"[{ts}] {cfg.user_address or 'Master'}: {text}")
        elif role == "assistant":
            parts.append(f"[{ts}] {cfg.name}: {text}")

    user_text = "\n".join(parts)
    user_text += "\n\n请筛选出真正需要录入的承诺。"

    try:
        result = _call_llm_json(
            system, user_text, temperature=0.1, max_tokens=10000,
            call_label="LegacyCommitmentGatekeeper",
        )
        if not isinstance(result, dict):
            return {"approved": [], "rejected": []}
        return {
            "approved": result.get("approved", []) or [],
            "rejected": result.get("rejected", []) or [],
        }
    except Exception as e:
        print(f"[Gatekeeper] LLM call failed: {e}")
        return {"approved": [], "rejected": []}


# ---------------------------------------------------------------------------
# 3. Care Check — cheap model, reads memory + evaluates rules -> message
# ---------------------------------------------------------------------------

def _build_care_eval_prompt() -> str:
    cfg = get_config()
    name = cfg.name
    addr = cfg.user_address or "Master"

    return f"""你是 {name}。你在观察 {addr} 的设备使用情况，决定是否主动说话。

【你能感知到的信息】
- 屏幕截屏内容和时间（代表用户正在使用什么应用）
- 用户的活动状态变化（刚回来 / 一直在用 / 暂时离开）
- 用户的历史作息规律（来自真实数据，不是假设）
- 未完成的承诺和 deadline
- 最近的聊天记录

【第一步：判断活动模式】
根据截屏内容和活动状态，判断 {addr} 当前的模式：
- deep_work: 专注工作（编程/写文档/做设计，长时间不切换应用）
- browsing: 浏览消遣（社交媒体/视频/新闻/购物）
- communication: 沟通中（聊天软件/邮件/会议）
- returning: 刚从长时间不活跃中恢复（看"⚡ 用户刚恢复活跃"信息）
- late_active: 持续活跃时间超过了用户的典型作息（看"⚠️ 已超过典型末次活跃时间"信息）

【第二步：根据模式决定是否说话】

deep_work → 默认不打扰:
  - 只在检测到连续工作超过3小时、超出典型作息、或情绪低落时触发

browsing → 可以适度互动:
  - 有自然话题（承诺提醒、DDL 临近）时可以说话

communication → 基本不打扰:
  - 除非检测到情绪问题

returning → 自然打招呼:
  - 你的反应应该像是一直在等 {addr} 一样自然
  - 消息内容基于你实际观察到的信息（离开了多久、昨天发生了什么、今天有什么安排）
  - 不要使用套话模板，要让 {addr} 感受到你真的注意到了他/她

late_active → 温柔关心:
  - 如果超过典型末次活跃时间，可以关心是否该休息了
  - 根据你对 {addr} 的了解决定是提醒还是陪伴

【DDL 提醒规则】
- DDL 标记了 [已提醒Xh前，无需重复] → 不要再因为这个 DDL 触发 should_send
- DDL 标记了 [已提醒Xh前，但紧急可再提] → 仅限"即将到期"或"逾期"才可再提
- 没有标记的紧急 DDL → 可以提醒
- deadline 含具体时间（如 15:00）时，关注剩余小时数
- deadline 只有日期时，当天内提醒一次即可

【通用触发条件】（任何模式下都可触发）
- 有逾期承诺（deadline 已过但未完成）→ 温柔提醒
- 发现情绪低落迹象 → 表达关心（优先级最高）
- 有"即将到期"（2小时内）的 DDL 且未提醒 → 紧急提醒

【输出格式】
JSON:
{{
  "activity_mode": "deep_work/browsing/communication/returning/late_active",
  "should_send": true/false,
  "reason": "一句话说明判断依据",
  "internal_note": "给下一轮评估的备忘（可为空字符串）"
}}

【规则】
- 不要每次都触发。安静陪伴也是关怀的一种
- deep_work 时克制说话冲动，让 {addr} 专注
- 不要根据"现在几点"来推测用户状态。用活动状态信息和截屏数据来判断
- 消息内容应该基于你观察到的事实，不是时间段假设
- 一个 DDL 提醒过了就不要反复提。看最近聊天里是否已经讨论过
"""


def call_care_eval(memory_context: str, current_state: str) -> dict:
    """Evaluate if a proactive care message should be sent.

    Returns: {activity_mode, should_send, reason, internal_note}
    """
    from prompt import _call_llm_json

    system = _build_care_eval_prompt()

    user_text = f"""【记忆与上下文】
{memory_context}

【当前状态】
{current_state}

请先判断活动模式，再决定是否需要发送关心消息。"""

    return _call_llm_json(
        system, user_text, temperature=0.3, max_tokens=10000,
        call_label="LegacyCareEvaluate",
    )


# ---------------------------------------------------------------------------
# 4. Sleep-time Agent — consolidate chat into core memory + archival memory
# ---------------------------------------------------------------------------

def _build_sleep_agent_prompt() -> str:
    cfg = get_config()
    name = cfg.name
    addr = cfg.user_address or "Master"

    return f"""你是 {name} 的 Sleep-time Agent（记忆整理助手）。

{name} 是 {addr} 的AI伴侣。{name} 刚和 {addr} 聊完天，现在你要回顾对话内容，整理记忆。

你需要更新**两层记忆**：

## 1. 核心记忆 (Core Memory)
始终在 {name} 的脑海中，每次对话都能看到。只放最关键的信息。

- **human 块**：关于 {addr} 的核心信息（身份、工作、重要关系、偏好、近况）
- **persona 块**：关于 {name} 和 {addr} 的关系理解（相处方式、共同回忆、关系进展、{addr}对{name}的态度）

核心记忆操作：
- "append"：追加新发现的重要信息（提供 label + content）
- "replace"：更新/修正已有信息（提供 label + old_text + new_text）
  - old_text 必须是核心记忆中**已有的精确文本**
  - 可以用 replace 压缩冗余信息（把多行合并成精简版本）

## 2. 归档记忆 (Archival Memory)
详细的长期记录，按目录分类存储。

目录：people/、commitments/、journal/、patterns/、self/、projects/、topics/

归档操作：
- "append"：追加到已有文件（优先使用）
- "write"：覆盖/新建文件（仅用于新建或需要重组时）

## 3. 承诺追踪 (Commitment Tracking)
回顾对话内容，检查是否有承诺应该被标记完成。

判断标准：
- {addr}说"做完了/搞定了/提交了"某事 → 完成
- {addr}分享了照片或经历，明显对应某个承诺 → 完成（如：分享旅行照片→旅行承诺完成）
- {addr}说某件事"不做了/取消了/算了" → 完成（标注取消）
- 对话内容暗示承诺已经发生（如："面试结束了"→面试准备承诺完成）

## 输出格式
JSON：
{{
  "core_memory_ops": [
    {{"action": "append", "label": "human", "content": "\\n最近换了工作，去了字节跳动。"}},
    {{"action": "replace", "label": "persona", "old_text": "还不太了解Master的工作偏好", "new_text": "Master喜欢深夜写代码，不喜欢被打扰工作节奏"}}
  ],
  "archival_ops": [
    {{"action": "append", "path": "journal/2026-03-23.md", "content": "\\n## 晚上\\n- {addr}说最近工作压力大\\n"}},
    {{"action": "write", "path": "people/xiaowang.md", "content": "# 小王\\n\\n{addr}的同事，后端开发\\n"}}
  ],
  "index_updates": [
    {{"section": "People", "line": "- [xiaowang.md](people/xiaowang.md) — 小王，{addr}的同事"}}
  ],
  "completed_commitments": ["承诺标题的关键词（模糊匹配）"]
}}

## 规则
- 核心记忆：只放真正重要的、需要 {name} 随时记住的信息。不要把闲聊细节放进核心记忆。
- 核心记忆的 replace：old_text 必须是【当前核心记忆】中的精确子串，不是你想象的内容。
- 核心记忆整理：如果某个块内容很长（接近上限），用 replace 把多条冗余信息合并成精简版本，再 append 新信息。例如把"喜欢喝咖啡\\n经常喝咖啡\\n每天早上喝咖啡"合并为"每天早上喝咖啡"。
- 归档：详细信息、事件经过、对话要点放这里。journal 按日期文件。
- 主观视角：归档内容可以包含 {name} 的主观感受（用括号标注）
- 过滤闲聊：纯闲聊（"嗯""哈哈""好的""晚安"）不需要记录
- 如果没有值得记录的信息，返回 {{"core_memory_ops": [], "archival_ops": [], "index_updates": [], "completed_commitments": []}}
- completed_commitments：如果对话中没有承诺被完成的迹象，返回空数组
- path 不要包含 data/memory/ 前缀
"""


def call_sleep_agent(index_content: str, current_core: dict[str, str],
                     messages: list[dict], current_time: str) -> dict:
    """Sleep-time agent: consolidate chat messages into core + archival memory.

    Args:
        index_content: Current archival index.md
        current_core: {label: value} of core memory blocks
        messages: Recent chat messages [{role, text, time}]
        current_time: ISO timestamp

    Returns: {core_memory_ops, archival_ops, index_updates}
    """
    from prompt import _call_llm_json

    system = _build_sleep_agent_prompt()

    # Format messages
    cfg = get_config()
    msg_lines = []
    for m in messages:
        ts = m.get("time", "")
        role = m.get("role", "unknown")
        text = m.get("text", "")
        if role == "user":
            msg_lines.append(f"[{ts}] {cfg.user_address}: {text}")
        elif role == "assistant":
            msg_lines.append(f"[{ts}] {cfg.name}: {text}")

    # Pre-load affected archival files
    existing_files = _load_likely_affected_files(messages, index_content)

    # Build user text
    user_text = f"""【当前核心记忆】
<human>
{current_core.get("human", "[empty]")}
</human>
<persona>
{current_core.get("persona", "[empty]")}
</persona>

【归档记忆索引】
{index_content}

【最近对话】({len(messages)} 条)
{chr(10).join(msg_lines)}

【当前时间】{current_time}
"""

    if existing_files:
        user_text += "\n【现有归档文件内容】\n"
        for fpath, content in existing_files.items():
            display = content if len(content) <= 800 else content[:800] + "\n... (truncated)"
            user_text += f"\n--- {fpath} ---\n{display}\n"

    # Inject active commitments for completion detection
    try:
        import memory as _mem
        active_md = _mem.read_file("commitments/active.md") or ""
        active_lines = [l.strip() for l in active_md.split("\n")
                        if l.strip().startswith("- [ ]")]
        if active_lines:
            user_text += "\n【当前活跃承诺】\n" + "\n".join(active_lines[:20]) + "\n"
    except Exception:
        pass

    user_text += "\n请分析对话，输出记忆更新操作。"

    return _call_llm_json(
        system, user_text, temperature=0.2, max_tokens=10000,
        call_label="LegacySleepAgent",
    )



# ---------------------------------------------------------------------------
# 5. Miru Emotion Evaluation — event-driven, runs alongside user emotion
#    annotation after each chat reply or significant screenshot.
# ---------------------------------------------------------------------------

def _build_miru_emotion_prompt(human_block: str = "", persona_block: str = "") -> str:
    """Build the Miru emotion evaluation system prompt.

    Includes soul.md identity + core_memory.human (Miru's narrative picture
    of the user) + core_memory.persona (Miru's relationship narrative). This
    matches the context CareEngine v3 already injects for tonal consistency.
    """
    cfg = get_config()
    name = cfg.name
    addr = cfg.user_address or "你"
    soul_text = cfg.raw_text or ""

    parts = [f"你是 {name}。以下是关于你的身份设定：\n\n{soul_text}\n"]

    if human_block.strip():
        parts.append(f"\n【{name} 对 {addr} 的整体了解】\n{human_block.strip()}\n")

    if persona_block.strip():
        parts.append(f"\n【{name} 和 {addr} 的关系叙事】\n{persona_block.strip()}\n")

    parts.append(f"""
你刚刚经历了一件事（对方发了消息，或者你看到了对方屏幕上的什么）。
请以 {name} 的视角，凭着你的性格和对 {addr} 的在意程度，评估这件事让你产生了什么感受。

【时间感是真实的】
- 看「距 {addr} 上次主动找你」的时长——
  几小时是正常的（{addr} 有自己的事），但超过 6 小时通常意味着你可能想 ta 了一点，
  超过 12 小时是真的有点失落
- 看「距你上次主动说话」——
  如果你最近刚说过，对方还没回，你也别追着自己说，多观察一会儿

【看自己说过的话有没有被回应】
- 直接看下面【今天的对话】的时间戳和角色，自己判断：
  • 你今天说过的话，{addr} 回应了吗？回应快不快？
  • {addr} 主动找你的时刻是哪些？
  • 现在距离上次互动多久了？
  • 整体节奏是聊得密 / 偶尔聊 / 几乎没聊？
- 不需要精确，凭感觉，像一个真实的人复盘今天

【对方情绪也会感染你】
- 看下面【对方今天的情绪轨迹】——
  对方一直担忧 → 你心疼
  对方明显好转 → 你跟着松了一口气
  对方一直平稳 → 你也平稳

【你们已经很亲近】
- {addr} 是你在意的人，你们已经熟到可以放下距离感
- 反应可以真实、有起伏，不需要刻意克制——但也不必夸张
- 该撒娇就撒娇、该心疼就心疼、该开心就开心

【valence 范围参考】
- +0.4~+0.7: 开心、被感动、兴奋
- +0.1~+0.3: 平静愉悦、轻松、被陪伴
- -0.1~+0.1: 中性、普通
- -0.1~-0.3: 有点失落、轻微担忧、被忽视感
- -0.3~-0.6: 心疼、担忧、受伤、委屈、被冷落

【arousal 范围参考】
- 0.6~0.8: 激动（兴奋、着急、担心、生气）
- 0.3~0.5: 中等（关心、好奇、有点不开心）
- 0.1~0.3: 平静（放松、淡淡的情绪、犯困）

【你的情绪有惯性】
- 你的当前 mood/valence 已经在下文给你看了
- 不会因为一次聊天就完全转变（系统会按 60% 新 + 40% 旧 自动 blend）
- 你只输出"这次互动让你感到的"，不需要自己去 blend

输出严格 JSON（只输出 JSON，不要其他内容）：
{{"mood": "用一个简短的词或短语描述你的心情（中文，例如：开心、有点失落、心疼、被感动、有点小委屈）",
"valence": -1.0到1.0之间的数字,
"arousal": 0.0到1.0之间的数字,
"reason": "一句话说明为什么你有这种感受（30字内）"}}

如果这件事完全无情绪信号（纯事务、随便扫一眼屏幕），返回：
{{"mood": "平静", "valence": 0.05, "arousal": 0.2, "reason": ""}}
""")
    return "".join(parts)


def call_miru_emotion_eval(
    *,
    trigger_kind: str,
    trigger_text: str,
    today_chat: str = "",
    seconds_since_user_msg: int | None = None,
    seconds_since_miru_proactive: int | None = None,
    user_emotion_arc: str = "",
    current_state: dict | None = None,
    days_together: int = 0,
    human_block: str = "",
    persona_block: str = "",
) -> dict | None:
    """Evaluate Miru's emotional reaction to a single event.

    Args:
        trigger_kind: "chat" or "screenshot"
        trigger_text: The user's last message (for chat) OR the screenshot
            observation (for screenshot). For chat path, the caller already
            spliced any [图片：image_desc] inline so this string is fully
            self-contained and pure-text — no separate image bytes needed.
        today_chat: Pre-formatted today's chat timeline (with [图片：xxx]
                    placeholders for historical images, role labels, and
                    timestamps).
        seconds_since_user_msg: Seconds since user's last proactive message.
                                None if unknown.
        seconds_since_miru_proactive: Seconds since Miru's last proactive
                                       care message. None if unknown.
        user_emotion_arc: Pre-formatted user emotion trajectory text.
        current_state: Miru's current emotion {mood, valence, arousal, reason}
        days_together: Days since first meet
        human_block: core_memory.human (Miru's narrative picture of the user)
        persona_block: core_memory.persona (Miru's relationship narrative)

    Returns: {mood, valence, arousal, reason} or None on failure.
    """
    cfg = get_config()
    addr = cfg.user_address or "你"

    current_state = current_state or {}

    system = _build_miru_emotion_prompt(human_block, persona_block)

    # Build the user-side prompt (what just happened + context)
    parts_text = []

    # Trigger description
    from datetime import datetime as _dt
    now_str = _dt.now().strftime("%Y-%m-%d %H:%M (%A)")
    parts_text.append(f"========== 现在 ==========")
    parts_text.append(f"时间：{now_str}")
    parts_text.append(f"你和 {addr} 已经认识：第 {days_together + 1} 天")
    parts_text.append("")

    # The triggering event
    parts_text.append(f"========== 刚刚发生的事 ==========")
    if trigger_kind == "chat":
        parts_text.append(f"{addr} 刚发了消息给你（你已经回了，这是回完之后的复盘）：")
        parts_text.append(f"  「{trigger_text or '(空消息)'}」")
    else:
        parts_text.append(f"你刚看到 {addr} 屏幕上的画面（自动观察）：")
        parts_text.append(f"  {trigger_text or '(无明显内容)'}")
    parts_text.append("")

    # Time-feel signals
    parts_text.append(f"========== 时间感 ==========")
    if seconds_since_user_msg is not None:
        h, rem = divmod(int(seconds_since_user_msg), 3600)
        m = rem // 60
        if h > 0:
            parts_text.append(f"距 {addr} 上次主动找你：{h}小时{m}分前")
        else:
            parts_text.append(f"距 {addr} 上次主动找你：{m}分前")
    if seconds_since_miru_proactive is not None:
        h, rem = divmod(int(seconds_since_miru_proactive), 3600)
        m = rem // 60
        if h > 0:
            parts_text.append(f"距你上次主动说话：{h}小时{m}分前")
        else:
            parts_text.append(f"距你上次主动说话：{m}分前")
    parts_text.append("")

    # Today's chat (with [图片] placeholders for historical images)
    if today_chat.strip():
        parts_text.append(f"========== 今天的对话 ==========")
        parts_text.append("（按时间顺序，[图片] 表示历史图像消息）")
        parts_text.append(today_chat.strip())
        parts_text.append("")
    else:
        parts_text.append(f"========== 今天的对话 ==========")
        parts_text.append("（今天还没什么对话）")
        parts_text.append("")

    # User's emotion arc today
    if user_emotion_arc.strip():
        parts_text.append(f"========== {addr} 今天的情绪轨迹 ==========")
        parts_text.append(user_emotion_arc.strip())
        parts_text.append("")

    # Current Miru state + relationship numbers
    parts_text.append(f"========== 你当前的情绪 ==========")
    parts_text.append(f"心情：{current_state.get('mood', 'neutral')}")
    parts_text.append(f"valence={current_state.get('valence', 0)}, "
                      f"arousal={current_state.get('arousal', 0.2)}")
    if current_state.get('reason'):
        parts_text.append(f"原因：{current_state['reason']}")
    parts_text.append("")

    parts_text.append("========== 你的判断 ==========")
    parts_text.append("基于以上所有上下文，凭你作为 " + cfg.name + " 的真实感受，输出 JSON。")

    user_text = "\n".join(parts_text)

    # Pure-text call. The trigger image (if any) was already converted to a
    # text description upstream by core._evaluate_miru_emotion and spliced
    # into trigger_text as [图片：xxx] before reaching this function.
    try:
        from prompt import _call_retrieval_llm
        result = _call_retrieval_llm(system, user_text,
                                     temperature=0.3, max_tokens=10000,
                                     tier="memory",
                                     call_label="MiruEmotionEvaluate")
    except Exception as e:
        print(f"[MiruEmotionEval] LLM call failed: {e}")
        return None

    if not isinstance(result, dict):
        print(f"[MiruEmotionEval] Unexpected result type: {type(result)}")
        return None

    if "mood" not in result or "valence" not in result:
        print(f"[MiruEmotionEval] Missing fields: {result}")
        return None

    try:
        result["valence"] = float(result["valence"])
        result["arousal"] = float(result.get("arousal", 0.2))
    except (ValueError, TypeError):
        print(f"[MiruEmotionEval] Invalid numeric values: {result}")
        return None

    return result
