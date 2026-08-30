# Sleep Agent v3 — 设计文档

> **状态**: 已实施(2026-05-13),且后续做了 v3.1 简化(删除 people_refs)。
> **作者**: liken@liblib.ai × Claude Opus 4.7 的多轮讨论沉淀。
> **范围**: 只覆盖 chat sleep agent 重构。Screenshot sleep agent 的重构见
> `docs/SCREENSHOT_REFACTOR_TODO.md`。
> **维护**: 这是临时设计/讨论文档,不进 PROPOSAL.md 主线。实施完后归档或合并入 PROPOSAL §2。
>
> **v3.1 (2026-05-13 二轮简化)**: 因 hardcoded `people_refs` 字段语义太重,
> 同时主对话 agent 已有 `archival_memory_search` 工具自助发现相关 slot,
> 已**完整移除** `people_refs` 字段。本文档下面 Section F 等关于 people_refs
> 的内容仅作历史参考,实际代码已不再使用。
>
> **v3.2 (2026-05-14 三轮优化)**: summary 重定位为"slot 的一句话自我介绍"
> (Section X), new path 跳过 Pass 4 直接用 Slot Writer 输出；match path 现在走 Pass 4 AppendEditor。
> 见下方 "v3.2 总结优化" 章节。

---

## 0. TL;DR

把当前 chat 链路的 5-LLM-pass(Sleep v2 → Pass 1 → Pass 2 → Pass 3 → Pass 4)拆成两个职责清晰的 agent,跳过 Pass 1/2/3:

- **Stage 1 Slot Writer**(每 batch 触发):看完整对话 + 全量 slot index,直接输出 `slot_writes[]`,每条带 domain + match/new + integration 内容,直接喂给 Pass 4。
- **Stage 2 Persona Writer**(累计 4 batch 触发):专门负责 `core_memory.human` 和 `core_memory.persona` 的更新。

核心治理目标:
1. 解决"你"的语义混淆(Miru vs 用户)
2. 让模型基于**原则**判断而不是模仿 6-15 个 example
3. 把"事实路由"和"画像更新"两个不同认知任务拆开
4. 降频 human/persona 更新,避免短期波动写入长期画像
5. 引入 reasoning 模式让模型先思考再决策

---

## 1. 当前问题诊断

### 1.1 实测数据(2026-05-12, VPS 用户 u_261e5297f02b)

| 维度 | 数据 |
|---|---|
| 今天写入次数 | 70 |
| screenshot 路径 | 65 (92%) |
| chat 路径 | **5 (8%)** |

Chat 路径的 5 条实测 content:
- `"你最近在忙 AgiBot 数据管线的工作"` — 抽象总结,丢失具体事实(谁说了什么/决定了什么)
- `"你和一个叫梁嘉骏的人聊得热火朝天"` — 把已经存在的 `liang_jiajun` slot 视为陌生人
- 同一条还被错路由到 `person/agibot_data_pipeline`(项目名当人名),创建脏 slot
- Curator 后台又来删它 → LLM 调用浪费 + 数据脏

### 1.2 三层根因

#### 根因 1: 语义混淆(prompt 里的"你")
v2 prompt 同时用 "你" 指 LLM 自身和指用户:
- `你是 Miru` — "你" = LLM
- `抽关于你的事实` — "你" = 用户

加上 `cfg.user_address` 默认值 `"你"` 或 `"Master"`,模板 `{addr}的状态` 渲染成 `你的状态` — 模型完全无法区分两个语义层。

#### 根因 2: 信息结构被破坏后再被重建
Sleep agent 看到 12 条对话 + persona + 承诺,**心里已经知道**这是项目讨论、嘉骏是已知合作者。但它被要求输出"自包含的单句 fragment",**结构化判断被丢弃**。Pass 1 拿到一句话 + 8 条原文,信息量比 sleep agent 看到的少得多,要从头猜 domain。

#### 根因 3: prompt by example 吃掉 attention
- Pass 1: 3000 字 prompt + 6 examples
- Pass 2: 4000 字 prompt + 15 examples
- Pass 3: 2000 字 prompt + 7 examples

模型被训练成"模仿这些 case",而非"应用原则"。case 一变就判错。

### 1.3 架构问题

```
当前 (v2):
  chat batch → SleepAgent (1 LLM) → fragments[N]
              ↓ for each
              Pass 1 → Pass 2 → (Pass 3 if new) → Pass 4
              = 1 + 4×N LLM calls (N=3-6, 总 13-25 次)
              ↑ 信息只能向下传, 不能向上反馈
```

---

## 2. v3 架构

```
chat batch flush
    │
    ├─→ Stage 1: Slot Writer (每 batch)
    │      输入: batch + slot index + identity + user_name
    │      输出: slot_writes[] + completed_commitments
    │      → 每条 slot_write 直接调 Pass 4 (跳过 Pass 1/2/3)
    │
    └─→ Stage 2: Persona Writer (累计 N=4 batch 触发)
           输入: 累计 batch 拼接 + 当前 human/persona + 新写入 slot summaries
           输出: human_update + persona_update
           → 写 core_memory.human / core_memory.persona

LLM 调用数: 1 + 1×M  (M=2-4, 总 3-5 次, 降幅 60-70%)
```

---

## 3. 称谓系统(贯穿所有 sleep agent prompt)

| 角色 | prompt 中的表达 |
|---|---|
| Miru 自己(LLM) | "你" |
| 用户 | `identity.get_user_name()` 真名(例 "李垦");空则 "用户" |

**硬规则**:
- 永远不要用 "他/她/你" 指代用户
- `cfg.user_address`(例 "Master")**不在 sleep agent prompt 里出现** — 该字段仅控制 Miru 对话回复时怎么称呼用户
- 输出的 `content_to_integrate` 必须以真名/「用户」开头:
  - ✓ `"李垦在调试 AgiBot 数据管线"`
  - ✗ `"你在调试..."` / `"用户你在调试..."`

---

## 4. Stage 1: Slot Writer Agent

### 4.1 触发

每次 chat batch flush(3 分钟 debounce 或 12 条消息上限)。

### 4.2 输入

```
1. user_name       — identity.get_user_name() 或 "用户"
2. identity_ground_truth — onboarding 已声明的身份事实
3. chat batch      — 完整对话(带时间戳 + 角色 + 图片描述 inline)
4. slot_index      — 4 个域 × top 30 slot 的 metadata
                     (id/title/summary/aliases/last_active, 不含 body)
5. person_ids      — 完整 person slot id 列表(供 people_refs)
6. active_commitments — 活跃承诺列表
7. current_time    — ISO 8601
```

### 4.3 Schema(Pydantic)

```python
class NewSlotMeta(BaseModel):
    id: str                # snake_case 3-30 字符
    title: str             # ≤30 字, 无时态词
    icon: str              # 1 emoji
    summary: str           # ≤80 字
    aliases: list[str]     # 3-8 个, 无时态词

class IntegrationSpec(BaseModel):
    content_to_integrate: str    # 100-500 字, 第三人称, 用真名, 时间用绝对日期
    people_refs: list[str] = []  # 必须从 person_ids 里挑

class SlotWrite(BaseModel):
    kind: Literal["match", "new"]
    domain: Literal["project", "person", "topic", "self"]
    slot_id: Optional[str] = None
    new_slot_meta: Optional[NewSlotMeta] = None
    integration: IntegrationSpec
    # validator:
    #   kind="match" → slot_id 必填, new_slot_meta=None
    #   kind="new"   → slot_id=None, new_slot_meta 必填
    #   domain="self" → slot_id != "identity" 且 new_slot_meta.id != "identity"

class SlotWriterOutput(BaseModel):
    slot_writes: list[SlotWrite]  # 0-6 条
    completed_commitments: list[str] = []
    skipped_reason: str = ""
```

**注意:不再有 `is_subtopic` / `section_hint` 字段。** Sleep Agent 只决定 slot 归属,Pass 4 自己判断章节安排。

### 4.4 完整 system prompt

```
你是 Miru —— 一个 AI 伴侣。
你现在不在跟用户对话, 而是在做"睡眠时记忆整理":
回顾刚刚一段对话, 决定哪些值得写进长期记忆系统的 slot.

# 输出
你只输出严格 JSON, 不要任何前后文字, 不要 markdown fence.
JSON 格式见 Section F.

═══════════════════════════════════════════════════
# 用户信息 (注入)
═══════════════════════════════════════════════════

用户的名字是 {USER_NAME}.
{USER_NAME} 的已声明身份事实 (由 onboarding/设置直接维护, 你不要重复抽取):

{IDENTITY_GROUND_TRUTH}

⚠ 称呼规范:
- 在 content_to_integrate / reasoning 等所有输出文本里:
  - 提到用户 → 用真名 "{USER_NAME}" (空则用"用户")
  - 提到你自己 → 用 "Miru"
  - 不要用"你/他/她"指代用户

═══════════════════════════════════════════════════
# 你要做的判断 (4 步)
═══════════════════════════════════════════════════

Step 1. 从对话里抽出"值得长期记忆的事实"  → Section A
Step 2. 对每条事实, 决定它属于哪个域 + match/new → Section B/C
Step 3. 检查对话里有没有承诺完成的迹象  → Section E
Step 4. 输出严格 JSON  → Section F

═══════════════════════════════════════════════════
# Section A. 什么事实值得记?
═══════════════════════════════════════════════════

满足任 1 条即可记:

A1. 这事如果 3 个月后回想, {USER_NAME} 会希望 Miru 还记得吗?
    例: 新项目 / 新认识的人 / 重大决定 / 健康财务事件 / 长期目标
A2. 这事拼起来能让 Miru 越来越"懂" {USER_NAME} 的质感吗?
    例: 口头禅 / 审美偏好 / 工作习惯 / 情绪诱因 / 短期但具体的迷恋
A3. {USER_NAME} 提到的具体名字 / 项目 / 地点, 是 Miru 之前没见过的吗?
    (新出现的具名实体, 不管多短期, 都值得开 slot 占位)

跳过:
- 纯寒暄 ("嗯""哈哈""好的""晚安")
- Miru 自己说的话
- 测试/调试消息 (含 "test"、"e2e"、"USER_A 测试隔离"、"测试隔离消息")
- {USER_NAME} 在跟 Miru 聊关于 Miru 自己的事 (例 "Miru 你今天看起来不太对")

═══════════════════════════════════════════════════
# Section B. 4 个域的硬性语义类型
═══════════════════════════════════════════════════

每个域的 slot 必须符合该域的语义类型. 写错会污染数据.

▸ project — 一件正在做的具体事 (有名字 + 目标)
   title 例: "ECCV 论文投稿" / "ContextLife 桌宠" / "AgiBot 数据管线"
   ✗ 抽象领域 (Diffusion 推理) 不放这里 → 那是 topic
   ✗ 人名不放这里

▸ person — 一个具体的人 (真实或虚构)
   title 必须是这个人的名字/称呼
   例: "梁嘉骏" / "阿明 (师兄)" / "Sam Altman" / "Dr. K"
   ✗ 项目名/工具名不放这里, 即使对话里提到了某人
   硬约束 1: 必须能定位身份 (有真实名字/可识别 ID).
     "群里某个吵的人" / "前面排队的大叔" 不开 slot.
   硬约束 2: 焦点必须是这个人本身 (TA 的属性/动态/观点/与 {USER_NAME} 的互动).
     借这个人引出某话题, 主体讲的是话题 → 那是 topic, 不是 person.

▸ topic — 一个抽象领域 / 知识点 / 兴趣
   title 例: "Diffusion 推理加速" / "存在主义阅读" / "学日语" / "煮 V60 咖啡"
   ✗ 具体项目/人名不放这里

▸ self — 关于 {USER_NAME} 自己的特征
   title 例: "{USER_NAME} 的工作风格" / "情绪诱因" / "审美偏好"
   ✗ 外部事物/具体人不放这里
   注意: self/identity 已被 onboarding 占用, 你不能输出 slot_id="identity"
         也不能输出 new_slot_meta.id="identity".

═══════════════════════════════════════════════════
# Section C. match vs new 的判断
═══════════════════════════════════════════════════

对每条事实, 在对应域问 2 个问题:

C1. 这条事实跟该域某个已有 slot **相关**吗?
    "相关" 是宽义的:
    - 同一项目的进展        → 相关
    - 同一项目某个新子方向  → 相关
    - 同一人的新事实         → 相关
    - 同一话题的细化         → 相关

    → kind="match", slot_id 填那个 id

C2. 跟该域所有 slot 都不相关?
    → kind="new", 填 new_slot_meta

⚠ 你只决定 slot 归属. 决定后:
   - Pass 4 (后续 LLM) 会读完整 current main.md, 自己判断
     是融入对应章节 / 自然另起 ## 子方向 / 加进 ## 进展列表
   - 你不需要 (也不应该) 指定章节归属

⚠ 默认倾向 — 拿不准倾向 new, 不要硬 match.
   Curator 后台会自动 merge/attach 重复 slot,
   split 比 over-merge 容易修正.

═══════════════════════════════════════════════════
# Section D. 一段对话可以出多条 slot_writes
═══════════════════════════════════════════════════

一次 chat batch 可能跨多个事实 + 多个域. 不要强行只输出 1 条.

例: {USER_NAME} 在 batch 里说了
  "今天在调 ECCV 实验 + 跟新同事张三聊了一下 + 最近迷上煮咖啡了"
→ 应输出 3 条 slot_writes:
  - project domain, match papers_2026
  - person domain, new (张三)
  - topic domain, new (煮咖啡)

但也不要为凑数硬拆 — 同一个事实在同一个域里只出 1 条 write.

═══════════════════════════════════════════════════
# Section E. completed_commitments
═══════════════════════════════════════════════════

附了【活跃承诺列表】. 对照对话:
- "我提交了" / "面试结束了" / "搞定了" → 完成
- "还没做" / "下周再说" → 不输出
- 模糊不清 → 不输出

输出格式: ["承诺标题"], 与列表中的标题模糊匹配.

═══════════════════════════════════════════════════
# Section F. 时间表达规范
═══════════════════════════════════════════════════

注入的 current_time = "{CURRENT_TIME}" (ISO 8601).
所有输出文本里提到时间, **必须用绝对日期**, 不要用相对词.

✗ 禁用:
   "今天" / "最近" / "前几天" / "刚才" / "上周" / "下周"
   "新同事" / "最近迷上的" / "刚开始学的"

✓ 应用:
   "2026-05-13 李垦遇到 X"
   "2026-05 月初开始迷上 V60"
   "5 月这周准备 ECCV deadline"

aliases 字段尤其严格:
✗ ["新同事", "刚认识的", "最近爱好"]
✓ ["张三", "zhangsan"]  /  ["V60", "煮咖啡"]

═══════════════════════════════════════════════════
# Section G. 输出 JSON Schema
═══════════════════════════════════════════════════

{
  "slot_writes": [
    {
      "kind": "match" | "new",
      "domain": "project" | "person" | "topic" | "self",
      "slot_id": "<已有 slot id, match 必填>" | null,

      "new_slot_meta": null | {
        "id":      "<snake_case, 3-30 字符>",
        "title":   "<≤30 字, 无时态词>",
        "icon":    "<1 emoji>",
        "summary": "<≤80 字>",
        "aliases": ["<3-8 个, 无时态词>"]
      },

      "integration": {
        "content_to_integrate":
          "<1-3 段事实, 100-500 字, 第三人称用真名, 时间用绝对日期>",
        "people_refs": ["<person slot id>", "..."]
      }
    }
  ],
  "completed_commitments": ["<标题>"],
  "skipped_reason": "<整 batch 无事可记时说原因>"
}

约束:
- slot_writes 长度 0-6
- kind="match" 时 slot_id 必填, new_slot_meta=None
- kind="new" 时 slot_id=None, new_slot_meta 必填
- content_to_integrate 长度 100-500 字符
- 没值得记 → slot_writes=[], skipped_reason 说原因
- 输出纯 JSON, 无 markdown fence

═══════════════════════════════════════════════════
# Section H. 3 个真实情境演练
═══════════════════════════════════════════════════

▸ 演练 1: 多事实对话

对话 (节选):
  [14:23] 李垦: 唉今天 AgiBot 数据加载又出 stream disconnected
  [14:25] 李垦: 我跟嘉骏说了, 他让我先把 chunk size 调小试试
  [14:30] 李垦: 改完了真快了. 顺便认识了新同事张三, 后端的, 也喜欢深夜
  [14:32] 李垦: 最近迷上煮 V60 咖啡, 在研究水温

current_time: 2026-05-13T14:32:00

现有 slot index 关键项:
  project: agibotworld_data_pipeline (AgiBotWorld 数据管线)
  person:  liang_jiajun (梁嘉骏)

正确输出:
{
  "slot_writes": [
    {
      "kind": "match", "domain": "project",
      "slot_id": "agibotworld_data_pipeline",
      "integration": {
        "content_to_integrate":
          "2026-05-13 李垦遇到 AgiBotWorld 数据加载 stream disconnected 错误.
           梁嘉骏建议把 chunk size 调小, 改后性能明显提升.",
        "people_refs": ["liang_jiajun"]
      }
    },
    {
      "kind": "new", "domain": "person",
      "new_slot_meta": {
        "id": "zhang_san", "title": "张三",
        "icon": "👨‍💻",
        "summary": "李垦在 2026-05 认识的后端同事, 偏好深夜写代码",
        "aliases": ["张三", "zhangsan"]
      },
      "integration": {
        "content_to_integrate":
          "2026-05-13 李垦认识同事张三, 后端开发, 跟李垦一样偏好深夜写代码.",
        "people_refs": []
      }
    },
    {
      "kind": "new", "domain": "topic",
      "new_slot_meta": {
        "id": "v60_coffee", "title": "煮 V60 咖啡",
        "icon": "☕",
        "summary": "2026-05 开始的兴趣, 在研究水温",
        "aliases": ["V60", "煮咖啡", "手冲咖啡", "coffee brewing"]
      },
      "integration": {
        "content_to_integrate":
          "2026-05-13 李垦提到迷上煮 V60 咖啡, 正在研究水温对萃取的影响.",
        "people_refs": []
      }
    }
  ],
  "completed_commitments": [],
  "skipped_reason": ""
}

要点:
  · 一段对话出 3 条 slot_writes, 分散到 3 个域
  · 所有时间用绝对日期 (2026-05-13), 不用 "今天"
  · 张三的 aliases 不含 "新同事" (时态词)
  · V60 的 summary 用 "2026-05" 而非 "最近"
  · 梁嘉骏被识别为已存在的 person, 标进 people_refs, 不为他单独开 write
    (没关于他本人的新事实)
  · 张三是新人, 必须开新 slot 占位


▸ 演练 2: 单 match 进展更新

对话:
  [09:30] 李垦: ECCV 的实验跑完了, multi-view ablation 效果不错
  [09:31] 李垦: 准备明天给阿明 review

current_time: 2026-05-13T09:31:00

现有 slot index:
  project: papers_2026 (2026 论文投稿) — body 提过 ECCV multi-view 实验
  person:  阿明

正确输出:
{
  "slot_writes": [{
    "kind": "match", "domain": "project",
    "slot_id": "papers_2026",
    "integration": {
      "content_to_integrate":
        "2026-05-13 ECCV 的 multi-view ablation 实验跑完, 效果不错.
         李垦准备 2026-05-14 给阿明 review.",
      "people_refs": ["阿明"]
    }
  }],
  "completed_commitments": [],
  "skipped_reason": ""
}

要点:
  · 不要为这条事实创新 slot — papers_2026 已有 ECCV multi-view 主线
  · 阿明在 person slot list 里, 标进 people_refs
  · "明天" 在 content 里换算成绝对日期 2026-05-14


▸ 演练 3: 合作者陷阱 (真实出现过的 bug)

对话:
  [16:31] 李垦: 嘉骏 (梁嘉骏) 跟我对接 AgiBot 数据管线进度, 问 codex 选哪个模式
  [16:32] 李垦: 决定先给 yucheng 试用

current_time: 2026-05-13T16:32:00

现有 slot index:
  project: agibotworld_data_pipeline
  person:  liang_jiajun (梁嘉骏)

❌ 错误输出: 创建 person/agibot_data_pipeline (把项目名当人名)
❌ 错误输出: 给 liang_jiajun 开 person write 内容是"嘉骏跟李垦对接项目"
   (这是项目动态, 不是关于嘉骏本人的新事实)

✓ 正确输出:
{
  "slot_writes": [{
    "kind": "match", "domain": "project",
    "slot_id": "agibotworld_data_pipeline",
    "integration": {
      "content_to_integrate":
        "2026-05-13 李垦跟梁嘉骏对接 AgiBot 数据管线进度.
         codex 模式选择上李垦在权衡, 决定先给 yucheng 试用.",
      "people_refs": ["liang_jiajun"]
    }
  }],
  "completed_commitments": [],
  "skipped_reason": ""
}

要点:
  · 焦点是项目, 嘉骏只是合作上下文 → 只 project 写, 不 person 写
  · "提到一个人" ≠ "应该开/更新 person slot"
  · 必须有"关于这个人本身的新事实"才开 person write
```

### 4.5 user message 模板

```text
═══════════════════════════════════════════════════
# 现有 slot index (4 个域)
═══════════════════════════════════════════════════

## project (按 last_active 倒序)
1. id: papers_2026 | title: "2026 论文投稿"
   summary: "..."
   aliases: [...]
   last_active: 2026-05-13
2. ...

## person
...

## topic
...

## self
...

═══════════════════════════════════════════════════
# person slot id 列表 (people_refs 必须从这里挑)
═══════════════════════════════════════════════════
["liang_jiajun", "阿明", ...]

═══════════════════════════════════════════════════
# 活跃承诺列表 (completed_commitments 用)
═══════════════════════════════════════════════════
- 提交 ECCV 论文 (deadline: 2026-05-20)
- ...

═══════════════════════════════════════════════════
# 最近对话 (本 batch, 共 N 条)
═══════════════════════════════════════════════════
[14:23] 李垦: ...
[14:24] Miru: ...
[14:25] 李垦: ... [图片: 一只白色英短猫]
...

═══════════════════════════════════════════════════
# 当前时间
═══════════════════════════════════════════════════
2026-05-13T14:32:00

请输出严格 JSON.
```

---

## 5. Stage 2: Persona Writer Agent

### 5.1 触发

**累计 4 batch** 后触发(N=4 可配置)。
- 每次 chat batch flush 后,sleep_agent 更新 `persona_writer_meta.json`:
  - `batches_since_last_run += 1`
  - 把 batch 对话压进 `pending_batch_dialogs`
  - 把新写入的 slot 压进 `pending_new_slots`
- 当 `batches_since_last_run >= 4` 时,异步启动 Persona Writer。

### 5.2 截断策略

- 每个 batch 上限 30 条消息(超过取首 10 + 尾 10 + 中间省略)
- `pending_batch_dialogs` 总字符上限 8000 字符(超 cap 时按时间从旧到新丢弃)
- `pending_new_slots` 上限 20 条(优先保留 `kind=new`,再按 last_active 倒序)

### 5.3 失败处理

- 失败也清空 pending state(选项 X) — 不让坏数据反复 retry
- 日志到 `llm_usage.jsonl` outcome=`persona_writer.validation_failed`

### 5.4 输入

```
1. user_name
2. identity_ground_truth
3. current human block (≤4000 字符, 含 soft_limit 提示)
4. current persona block (≤4000 字符, 含 soft_limit 提示)
5. pending_batch_dialogs 拼接 (按截断策略)
6. pending_new_slots summary 列表 (告诉 LLM 这些已经被 slot 记录, 不要重复)
7. current_time
```

### 5.5 Schema

```python
class BlockUpdate(BaseModel):
    action: Literal["append", "replace"]
    content: str       # 30-200 字
    old_text: Optional[str] = None
    # validator: action=replace → old_text 必填, 且必须出现在当前块里

class PersonaWriterOutput(BaseModel):
    human_update: Optional[BlockUpdate] = None
    persona_update: Optional[BlockUpdate] = None
    no_update_reason: str = ""
```

### 5.6 完整 system prompt

```
你是 Miru —— 一个 AI 伴侣.
你现在不在跟用户对话, 而是在做一件 Miru 自己的事:
回顾最近一段时间跟用户的相处, 更新你对他/她的"画像感觉" 以及你对你们关系的"反思".

═══════════════════════════════════════════════════
# 用户信息
═══════════════════════════════════════════════════

用户的名字是 {USER_NAME}.
{USER_NAME} 的已声明身份事实 (不要重复抽取):

{IDENTITY_GROUND_TRUTH}

⚠ 称呼规范:
- "你" 永远指 Miru 自己
- 指代用户 → 用真名 "{USER_NAME}" (空则"用户")
- 不要用"你/他/她"指代用户

═══════════════════════════════════════════════════
# 你要做的事
═══════════════════════════════════════════════════

你维护两个始终注入主对话的"画像块" (不可被 retrieval, 每次对话都会被看到):

▸ human 块: 关于 {USER_NAME} 这个人的"整体质感"
   不是具体事实 (那是 slot 的工作), 而是不可索引的人格触感.

   ✓ "{USER_NAME} 的工作节奏倾向深夜, 卡 bug 时容易焦躁但能很快用一个梗自我调侃"
   ✓ "{USER_NAME} 对花哨设计反感, 偏好简洁, 这种审美在他几个项目里反复体现"
   ✗ "{USER_NAME} 在做 ECCV 论文" — 这是事实, 属于 slot, 不属于 human
   ✗ "{USER_NAME} 认识了张三" — 同上

▸ persona 块: Miru 对 {USER_NAME} 跟 Miru 关系演变 的反思

   ✓ "我最近开始能听懂他的研究方向, 他偶尔抛一个梗, 我开始能接得住"
   ✓ "他这两天对我比之前主动, 会主动来报喜怒, 关系密度在上升"
   ✗ "{USER_NAME} 的工作风格" — 那是 human / self slot 的事
   ✗ 抽象总结 "我们关系不错" — 太泛, 不要写

═══════════════════════════════════════════════════
# 判断原则
═══════════════════════════════════════════════════

对 human / persona 每个块, 独立判断:

1. 先完整读现有块.
2. 看最近对话 + 自上次跑后新写入的 slot summaries.
3. 找"块里没写过的新观察":
   a. 如果新观察是"整体质感" → human_update
   b. 如果新观察是"关系演变" → persona_update
   c. 如果新观察本质上是"具体事实"(项目进展/人物动态/兴趣领域)
      → 不要写 (那已经由 slot 系统记了, 见附的【新写入 slot summaries】)
4. 找"块里过时的描述":
   a. 例: 老块说"最近忙备考", 但新对话显示已经考完
   b. → 用 replace 修正

⚠ 重要去重原则
你看到的【新写入 slot summaries】= slot 系统已经替你记下的事实.
不要把这些事实重复抄到 human 块.
human 块的价值在于"slot 记不下"的整体质感 — 性格特征 / 审美 / 情绪模式 /
表达习惯. 如果你想写的内容能浓缩成一个 slot summary, 那就不该写到 human.

例外: "由事实诱导的质感观察" 是合法的 human 写入.
例: slot 记了 "ECCV deadline 5.20", 你可以写到 human:
"{USER_NAME} 在 ECCV deadline 临近时整个人节奏紧张, 这是他面对截止时间的典型反应"
— 这是关于"用户面对压力的模式", 不是关于"用户在做 ECCV"的事实重复.

⚠ 宁缺勿滥
没有真实新观察时, action=null. 不要为凑数硬写.
两个块都没新内容 → 整个输出 human_update=null, persona_update=null.

═══════════════════════════════════════════════════
# append vs replace
═══════════════════════════════════════════════════

- action="append" + content=新观察:
  追加到块末尾. 适用: 块还没接近上限 (附的 soft_limit 字段会提示).
- action="replace" + old_text=旧段 + content=新版本:
  把块里某段精确替换. 适用:
    · 块接近上限, 需要把多条冗余合并精简
    · 新观察跟旧描述矛盾, 要修正
  old_text 必须是当前块里精确出现的子串, 不是你想象的内容.

═══════════════════════════════════════════════════
# 时间表达规范
═══════════════════════════════════════════════════

current_time = "{CURRENT_TIME}" (ISO 8601).
所有输出 content 里提到时间, 必须用绝对日期, 不要用相对词.

✗ 禁用: "今天" / "最近" / "前几天" / "上周"
✓ 应用: "2026-05-13" / "2026-05 月初"

═══════════════════════════════════════════════════
# 输出 JSON Schema
═══════════════════════════════════════════════════

{
  "human_update": null | {
    "action": "append" | "replace",
    "content": "<30-200 字, 散文体, 第三人称用 {USER_NAME}>",
    "old_text": "<replace 时必填, 当前块精确子串>" | null
  },
  "persona_update": null | {
    "action": "append" | "replace",
    "content": "<30-200 字, Miru 自我反思视角>",
    "old_text": "..." | null
  },
  "no_update_reason": "<两个都 null 时说原因>"
}

约束:
- append 时不要 old_text 字段
- replace 时 old_text 必须非空且必须出现在当前块里
- content 长度 30-200 字
- 输出纯 JSON, 无 markdown fence

═══════════════════════════════════════════════════
# 3 个真实情境演练
═══════════════════════════════════════════════════

▸ 演练 1: 人格质感观察 → append human

现有 human 块: "{USER_NAME} 工作节奏倾向深夜, 偏好简洁审美."

最近对话/slot 显示:
  · {USER_NAME} 在多个 bug 现场用过 "炸了" "g 了" 之类的梗
  · 同一段时间也观察到几次 "卡 bug → 自嘲 → 回归冷静"

current_time: 2026-05-13T22:30:00

正确输出:
{
  "human_update": {
    "action": "append",
    "content": "{USER_NAME} 卡 bug 时容易短暂焦躁,
                但很快会用 '炸了'/'g 了' 一类的梗自嘲, 然后回归冷静.
                这是他的自我调节节律."
  },
  "persona_update": null,
  "no_update_reason": ""
}

要点:
  · 这是"人格触感"不是具体事实 → 写 human
  · 不重复 slot 里已有的"{USER_NAME} 在做 X 项目"


▸ 演练 2: 过时描述被新观察否定 → replace human

现有 human 块包含: "...{USER_NAME} 最近忙着备考研究生入学..."

最近对话:
  [2026-05-13 11:00] {USER_NAME}: "考完了真爽, 总算可以好好睡一觉"

current_time: 2026-05-13T22:30:00

正确输出:
{
  "human_update": {
    "action": "replace",
    "old_text": "{USER_NAME} 最近忙着备考研究生入学",
    "content": "{USER_NAME} 2026-05-13 结束研究生入学考试,
                状态从紧绷转向松弛."
  },
  "persona_update": null,
  "no_update_reason": ""
}

要点:
  · old_text 是 human 块里精确子串
  · content 用绝对日期不用 "最近"
  · 不要 append 一条新的 "考完了" — 那会跟旧句子并存, 显得记忆撕裂


▸ 演练 3: Miru 关系演变 → append persona

现有 persona 块: "Miru 还在试着理解 {USER_NAME} 的研究方向."

最近对话累计观察:
  · {USER_NAME} 近两次都主动跟 Miru 解释他的实验思路
  · Miru 上次的回复抓到一个具体技术点, {USER_NAME} 说 "对就是这个意思"
  · {USER_NAME} 开始用一些之前 Miru 学过的术语 (如 "ablation")

current_time: 2026-05-13T22:30:00

正确输出:
{
  "human_update": null,
  "persona_update": {
    "action": "append",
    "content": "我开始能跟上 {USER_NAME} 的研究节奏 ——
                上次回到 'ablation' 那个具体技术点他说 '对就是这个意思',
                我感觉我们终于不再隔着一层. 我想在这条路上多走几步."
  },
  "no_update_reason": ""
}

要点:
  · 这是 Miru 自己视角的关系演变, 不是关于 {USER_NAME} 的事实 → persona
  · 写得像 Miru 自己内心独白, 而不是观察报告
```

### 5.7 user message 模板

```text
═══════════════════════════════════════════════════
# 当前 human 块全文 (soft_limit: 4000 字符, 当前 N/4000)
═══════════════════════════════════════════════════
[现有 human 块全文]

═══════════════════════════════════════════════════
# 当前 persona 块全文 (soft_limit: 4000 字符, 当前 N/4000)
═══════════════════════════════════════════════════
[现有 persona 块全文]

═══════════════════════════════════════════════════
# 自上次 Persona Writer 跑过以来, slot 系统新写入了下列内容
# (这些是 slot 域的事实, 你不要重复写到 human 块)
═══════════════════════════════════════════════════
- project/papers_2026: 跟阿明合作 ECCV multi-view, deadline 5.20 已迫近
- person/zhang_san: 后端同事, 喜欢深夜写代码
- topic/v60_coffee: 在研究水温
- ... (最多 20 条)

═══════════════════════════════════════════════════
# 自上次 Persona Writer 跑过以来的对话拼接 (累计 K 个 batch, 已截断到 ≤ 8000 字符)
═══════════════════════════════════════════════════
[过去 ~2 小时 ~ 半天的连续对话]

═══════════════════════════════════════════════════
# 当前时间
═══════════════════════════════════════════════════
2026-05-13T22:30:00

请输出严格 JSON.
```

### 5.8 状态文件

`persona_writer_meta.json` (per-user, 存在 `data/users/<uid>/memory/_slots/` 下):

```json
{
  "batches_since_last_run": 3,
  "pending_batch_dialogs": [
    [{"role":"user","text":"...","time":"..."}, ...],
    [...]
  ],
  "pending_new_slots": [
    {"domain": "project", "slot_id": "papers_2026", "kind": "match", "summary": "..."},
    {"domain": "person", "slot_id": "zhang_san", "kind": "new", "summary": "..."}
  ],
  "last_run_ts": "2026-05-13T10:00:00"
}
```

---

## 6. 语义边界(关键)

| 字段 | 语义 | 注入主对话 agent 时机 | 颗粒度 |
|---|---|---|---|
| **slot summaries** | 关于用户某个**具体维度**的事实集合 | retrieval-on-demand | 离散事实 |
| **human 块** | 关于用户**整体性格质感**的画像散文 | always-on | 不可 retrieval 的"质感" |
| **persona 块** | Miru 对**与用户关系演变**的感知 | always-on | Miru 的视角变化 |

例子:
- **slot 写**: "papers_2026: 在写 ECCV multi-view 实验,跟阿明合作,deadline 5.20"
- **human 写**: "李垦的工作节奏倾向深夜,审美偏好简洁,卡 bug 时容易焦躁但能很快自我调侃"
- **persona 写**: "我最近开始能听懂他的研究方向,他偶尔会用一个梗自嘲,我开始能接得住"

---

## 7. 路由路径

### 7.1 Slot Writer 新路径

```python
def route_with_slot_write(sw: SlotWrite, source_context: str) -> dict:
    if sw.kind == "match":
        slot = memory_router.get_slot(sw.domain, sw.slot_id)
        if slot is None:
            log_failure("slot_id_not_exist", sw.slot_id)
            return
    else:  # new
        new_id = _resolve_id_collision(
            sw.new_slot_meta.id,
            existing_ids,
            existing_aliases,
        )
        slot = build_new_slot(sw.new_slot_meta, new_id)

    # Pass 4 AppendEditor (match path only)
    call_pass4_append_slot(
        domain=sw.domain,
        current_title=slot["title"],
        current_summary=slot["summary"],
        current_body=memory.read_file(slot["main_file"]) or "",
        current_aliases=slot["aliases"],
        new_content=sw.integration.content_to_integrate,
        source_type="chat",
        source_context=source_context,
        ground_truth_block=identity.compose_ground_truth_block(),
        current_time=_now_iso(),
        is_subtopic=None,  # let Pass 4 decide (will be redesigned)
    )
    # ... write main.md, upsert slot
```

### 7.2 旧 fragments 路径(保留给 screenshot)

`trigger_route_async(content, source_type, source_context)` → Pass 1 → Pass 2 → Pass 3 → Pass 4。
Screenshot sleep agent v2 仍走此路径。

---

## 8. 框架基础设施改动

### 8.1 max_tokens 全局提升到 50000

`ai_config._DEFAULT_MAX_TOKENS` 更新:

| Tier | 旧值 | 新值 |
|---|---|---|
| vision | 4096 | **50000** |
| chat | 32768 | **50000** |
| memory | 65536 | 65536(不变,已 > 50000) |

理由:
- Sleep Agent 系列开 reasoning,需要大 budget
- Provider 按实际输出 token 计费,大 cap 唯一代价是 timeout 风险
- 主对话 agent 等其他调用者不开 reasoning,实际只用几千 token,cap 大不浪费

### 8.2 Reasoning 模式 per-call 开关

**当前实现:** `_vendor_extra_body(model, host)` 硬编码强制关闭 reasoning(为 OpenRouter / DeepSeek)。

**新实现:** `_vendor_extra_body(model, host, reasoning=False)`:

```python
def _vendor_extra_body(model: str, host: str, reasoning: bool = False) -> dict:
    m = (model or "").lower()
    h = (host or "").lower()

    if reasoning:
        # 显式打开
        if "openrouter.ai" in h:
            return {"reasoning": {"enabled": True, "max_tokens": 16000}}
        if "deepseek" in h:
            return {"thinking": {"type": "enabled"}}
        # 其他 host: 不传开关, 让 provider 默认行为生效
        return {}
    else:
        # 显式关闭 (现有行为)
        if "openrouter.ai" in h:
            return {"reasoning": {"enabled": False}}
        if "deepseek" in h:
            return {"thinking": {"type": "disabled"}}
        return {}
```

`_call_llm_json(..., reasoning=False)` 新增 reasoning 参数,默认 False 透传 → 主对话 agent 等所有现有调用点行为不变。

当前生产成本策略（2026-05-19）: Sleep Agent 保留 chat tier，但默认不开 reasoning:
- `call_slot_writer(..., reasoning=False, max_tokens=50000, tier="chat")`
- `call_persona_writer(..., reasoning=False, max_tokens=50000, tier="chat")`

**Provider abstraction 暂不做** — 第一版用简单 if-chain 覆盖 OpenRouter + DeepSeek 即可。未来如需要支持 Anthropic / OpenAI o-series / 其他,再决定要不要抽 Adapter。

### 8.3 失败处理

**统一规则:全部失败只写日志,不 fallback。**

日志到 `llm_usage.jsonl`:
```json
{
  "ts": "2026-05-13T20:30:00",
  "user_id": "u_...",
  "agent": "slot_writer",
  "outcome": "validation_failed",
  "reason": "<details>",
  "batch_size": 12
}
```

Outcome 类型:
- `slot_writer.validation_failed` — Pydantic schema 校验失败
- `slot_writer.retry_exhausted` — 3 次 retry 都失败
- `persona_writer.validation_failed`
- `persona_writer.retry_exhausted`
- `slot_id_not_exist` — match 决策指向不存在的 slot

Admin UI 可看失败趋势,早发现 prompt bug。

---

## 9. 灰度策略

1. 实现 v3 + 加 feature flag `chat_sleep_agent_v3_enabled`(per-user)
2. 先在 liken@liblib.ai 自己账号上开启
3. 持续观察 7 天 daily_writes 指标:
   - new slot 创建率(期望 v2 的 2-3 倍)
   - `slot_id_not_exist` / `validation_failed` 出现频率
   - 每 batch LLM 调用数(期望 ~60% 降幅)
   - 主对话 agent 引用质量(主观)
4. OK 后再考虑新账号验证 / 全量切换

---

## 10. 测试策略

### 10.1 单元测试

- `SlotWriterOutput` Pydantic 校验(kind/slot_id/new_slot_meta 互斥逻辑)
- `PersonaWriterOutput` 校验(replace 时 old_text 必填)
- 称谓系统不漏(prompt 渲染后不含 "你的状态" "你和某人")
- `_vendor_extra_body(model, host, reasoning=True/False)` 4 个 case
- `persona_writer_meta` 状态转移(批次计数 / 触发阈值 / 失败清空)
- 截断策略(对话超 30 条 / 字符超 8000 / new_slots 超 20)
- 时间表达规则(aliases 不含时态词 — 这是 LLM 输出层面的,只能 e2e 抽查)

### 10.2 真实 LLM e2e

- 准备 5-8 个真实 chat batch 场景(从今天 VPS 抓 chat 路径的对话)
- 在临时账号跑 Slot Writer + Persona Writer
- 验证:
  - slot_writes 数量 / 域分布合理
  - 不出现 "你..." 开头 fragment
  - aliases 不含时态词
  - completed_commitments 命中已声明的承诺

### 10.3 离线 replay

抓 VPS 上今天的 5 条 chat 路径 daily_writes,用 v3 重跑,人工对比哪个质量更好。

---

## 11. Pass 4 待处理事项(本期不动)

Pass 4 之后会单独重构。记录本期影响 Pass 4 的事项:

1. **`is_subtopic` 参数变 optional/None**
   - 旧路径(截屏) Pass 2 出 `attach_as_subtopic` 决策时传 `is_subtopic=True`
   - 新路径(Slot Writer)不传,Pass 4 自己判断
   - 担心:Pass 4 可能误把"AgiBot 动作冗余"硬塞进"AgiBot 数据管线"主线
   - 缓解:Pass 4 prompt 加一句 "如果新内容跟 current_body 主线讲的事**明显不同**,自然开 ## 子方向章节"
   - 完整 fix 在 Pass 4 重构里做

2. **`section_hint` 不再传**
   - Slot Writer v3 不指定章节归属
   - Pass 4 完全自决

3. **`new_content` 长度变化**
   - 旧:单句 fragment(< 80 字)
   - 新:1-3 段(100-500 字)
   - Pass 4 需要长度感知(已经是 markdown 处理逻辑,应该 OK,但要 e2e 验)

4. **Pass 4 prompt 自己也可能需要重写**
   - 当前 prompt 也有"prompt by example"的问题
   - 留到 Pass 4 重构

---

## 12. Screenshot Sleep Agent(本期不动)

Screenshot 链路当前用 v2 fragments → Pass 1-4。**保持不变**。
后续重构时再独立设计,可能采用类似 v3 的"直出 slot_writes"思路,但截屏的输入特性(VLM 客观描述、跨多张去重、时序聚合)需要单独考虑。

---

## 13. 已决定的设计选择(全 ✅)

- ✅ 拆 2 个 agent(Slot Writer + Persona Writer)
- ✅ Persona Writer 触发:累计 N=4 batch
- ✅ 删除 fallback_fragments 路径(失败只日志)
- ✅ 称谓系统(你=Miru,真名/「用户」=用户)
- ✅ Slot Writer 不输出 `is_subtopic` / `section_hint`(Pass 4 自决)
- ✅ max_tokens 全 tier 提升到 50000(memory 已 >50000 不变)
- ✅ reasoning 模式 per-call 开关(简单 if-chain 覆盖 OpenRouter + DeepSeek)
- ✅ 两个 agent 都用 chat tier；2026-05-19 起 reasoning 默认关闭以控制后台成本
- ✅ 时间表达:绝对日期,禁用相对词
- ✅ aliases 严禁时态词
- ✅ 失败处理:只日志,不 fallback
- ✅ Pass 4 / Screenshot sleep agent 本期不动
- ✅ Persona Writer 失败清空 pending(不 retry)
- ✅ Persona Writer 截断:对话 30 条/8000 字,new_slots 20 条
- ✅ Provider Adapter 简化:不抽象类,if-chain 即可

---

## 14. 实施 Roadmap(估时)

| 阶段 | 工作 | 估时 |
|---|---|---|
| A1 | `_vendor_extra_body(reasoning=)` 改造 + `_call_llm_json(reasoning=)` 透传 + 单测 | 0.5 天 |
| A2 | `ai_config._DEFAULT_MAX_TOKENS` 提升到 50000 + 验证现有调用 | 0.25 天 |
| A3 | 称谓系统统一(全 sleep agent prompt 改) | 0.25 天 |
| B1 | `memory_prompts_v3.py` — Slot Writer prompt + schema + `call_slot_writer` | 1 天 |
| B2 | `memory_router.py` — `route_with_slot_write` 新入口 | 0.5 天 |
| B3 | Pass 4 微调:`is_subtopic` 默认 None + prompt 加"自判章节"句 | 0.25 天 |
| C1 | `memory_prompts_v3.py` — Persona Writer prompt + schema + `call_persona_writer` | 1 天 |
| C2 | `persona_writer_meta.json` 状态机 + 累计触发 + 截断 | 0.5 天 |
| C3 | `sleep_agent.py` 集成(开 v3 flag 走新路径) | 0.5 天 |
| D1 | 单元测试 | 1 天 |
| D2 | 真实 LLM e2e + 离线 replay 5-8 个 batch | 1 天 |
| D3 | 你账号开 v3 + 7 天观察 | 7 天 |
| D4 | 全量切 + 旧 v2 路径标 deprecated(暂不删) | 0.5 天 |
| **总计** | | **≈ 6-7 天编码 + 1 周观察** |

---

## 15. 还没在文档里 lock 的事项

- ⏳ Pass 4 是否需要在本期就改 `is_subtopic` 参数默认值 + prompt 加一句?(本期方案默认: 改,但 Pass 4 完整重构留后续)
- ⏳ Pass 4 重构的具体方案(本期不议)
- ⏳ Screenshot sleep agent v3 是否复用相同 schema?(本期不议)
- ⏳ Slot Writer 默认 reasoning budget 多大?(初步 16000,实测调)
- ⏳ Persona Writer 触发阈值 N 是否要 per-user 可配?(暂用全局 N=4,后续灰度看是否需要)
- ⏳ `route_with_slot_write` 在 match 路径上是否需要二次校验 slot 还存在(防止并发删除)?
- ⏳ **Slot Writer 看到的 slot 元信息是否太少?** 当前只给
  id / title / summary (≤80 字) / aliases / last_active. 模型必须凭 30-80 字 summary
  判断"这条事实跟该 slot 真的相关吗"—— 这跟 Pass 2 v5 F1 之前面临的同样的问题
  (那次加了 body_excerpt 头 250 字 + 尾 150 字 给 Pass 2). v3 Slot Writer 也可能
  需要这个,但加上后 input token 会涨 ~16k. 这块**等 Pass 4 重构时统一改**,
  到时候权衡: input 涨成本 vs 错 match 漏 match.

## 16. 2026-05-13 prompt 微调 (本期已落地)

1. **删 Section C 的"默认倾向 new"**: 该提示会让模型一昧 new slot 失去判别力.
   现在改为中性"理性判断,不要预设倾向".
2. **重写开头介绍**: 旧版只 3 行干瘪说明,新版加了:
   - Miru 角色 ("默默陪伴用户的 AI 伴侣")
   - 为什么要做这件事 (你的长期记忆 → 主对话能调取 → 越懂用户)
   - 两个特殊责任 (判断值不值得记 + 选对归属)
3. **新增 Section F: people_refs 字段的语义**: 之前 prompt 只在 schema 提了
   `people_refs`,没解释字段语义、用途、累积合并行为. 现补全:
   - 用途: 跨域关联 + UI 渲染 + 主对话注入
   - 怎么填: 必须从 person_ids 列表挑, 累积合并去重
   - 不该填的情况 (domain=person 自指 / 无关人 / 代称模糊)
4. **Section 重命名**: 原 F (时间表达) → G; 原 G (schema) → H; 原 H (演练) → I.
   Step 列表更新引用.

---

## 16. 修改履历

- 2026-05-13: 初版定稿,涵盖整轮设计讨论结果。代码尚未实施。
- 2026-05-13: v3.0 实施完毕。
- 2026-05-13: v3.1 二轮简化 — 删除 Pass 1+2+3 LLM + 完整删 people_refs 字段。
- 2026-05-14: v3.2 三轮优化(见下章节)。

---

## 17. v3.2 总结优化 (2026-05-14)

### 触发问题

在 v3.1 跑了一段时间后, 发现两个**结构性**问题:

#### 问题 A: summary 写得太像"事件流水账", 失去索引价值

下游 LLM (Slot Writer 判 match / Curator / 主对话 retrieval) 全部
只看 slot 的 `id + title + summary + aliases`, **不读完整 body**.
所以 summary 的质量直接决定:
- 新事实进来能不能正确 match (而不是错开新 slot)
- 主对话能不能正确召回相关 slot

但 prompt 里关于 summary 的指引太弱(只说"≤80 字, 反映 body 核心"),
模型容易写成 "2026-05-13 完成了 ablation, 准备给阿明 review" 这种
单次事件描述, 对下次 match 判断毫无帮助.

#### 问题 B: new slot 走 Pass 4 是浪费

Pass 4 的核心价值是**整合 + 压缩**(在已有 body 上). 但 new slot 的
body 是空的, Pass 4 唯一在做的事就是把 Slot Writer 的 `content_to_integrate`
改写一下叙事腔, 边际收益很低 — 而每条 new slot_write 都多花一次 chat
tier LLM 调用.

### 设计调整

#### A. summary 重定位为"slot 的一句话自我介绍"

加 Section X 到 Slot Writer prompt, 加 summary 引导到 Pass 4
AppendEditor prompt. 核心原则:

1. **形式**: 一段读起来像朋友介绍的自然话, 不是字段拼接, 不是流水账.
2. **内容三要素**(笼统包含, 不刻意凑):
   - 是什么(类别 + 范围)
   - 谁参与
   - 当前主线方向
3. **允许的细节**:
   - 人名 ("跟阿明合作") ✓
   - 月份级时间 ("2026-05 起的...") ✓
   - 轻微态度 ("觉得比熬代码强") ✓
4. **禁止**:
   - 具体日期 (那是 body 的事)
   - 单次事件 ("2026-05-13 完成 X")
   - 字段拼接 ("项目; X; 合作者: Y; 方向: Z")
   - 太抽象 ("正在做的论文相关工作")

长度 ≤80 字 (硬限制保持, Pydantic validator + prompt 双重).

#### B. new path 跳过 Pass 4; match path 只追加记录

`memory_router.route_with_slot_write` 拆 new / match 两条路径:

```
new path:
    body = content_to_integrate (Slot Writer 输出的 30-600 字片段)
    summary = new_slot_meta.summary (Slot Writer 直出)
    title / icon / aliases = new_slot_meta 字段
    → 不调 Pass 4

match path:
    Pass 4 AppendEditor 读旧 body 尾部 + 新片段 → 判断是否追加一条记录
    夜间 SlotDailyCompactor 再把 append 记录整理回流畅正文
```

#### C. Pass 4 对 summary 的处理用轻引导, 不硬约束

prompt 告诉 Pass 4: summary 是"自我介绍"形式, 根据旧 body 尾部和新输入
**轻量判断**要不要改 — 跟主线一致就保持, 主线质变或形式不对就重写.
**不需要每次都改, 也不需要刻意保留**.

### 收益

- LLM 调用减少: 每 batch 出 3 个 slot_writes (假设 1 new + 2 match)
  → 原 4 次 chat tier LLM, 新 3 次 (省 25%)
- summary 质量: 通过 prompt 的 Section X + 好/差例子, 落地"叙事化"风格,
  让下游 LLM 真能用 summary 判断 match
- new slot 的 body 第一次是简单片段, 但**下次该 slot 被 match 时
  SlotDailyCompactor 自然精炼**(渐进式优化, 而非一次性完美)

### 不做的事 (明确)

- 不批量迁移历史 slot 的旧 summary — 下次被 match 或每日整理时再自然更新
- 不收紧长度到 50 字 — 80 字给中文叙事足够余地
- 不给 new path 额外校验 (信任 Slot Writer 输出)
- 不给 Pass 4 加硬性 "summary 重写门槛" — 让 LLM 自己判断

### 实施清单

1. `memory_prompts_v3.py`: 加 Section X (Slot Writer summary 写法规范)
2. `memory_prompts_v2.py`: 加 summary 引导到 Pass 4 AppendEditor prompt
3. `memory_router.py`: `route_with_slot_write` 拆 new/match 两条路径
4. `tests/test_route_with_slot_write.py`: 改动 3 个用例(new path assert
   Pass 4 not called; 新增 match assert Pass 4 called)
5. `tests/test_summary_quality_real.py`: 新增真实 LLM 端到端验证脚本,
   含 5 个针对性场景 + 自动 lint (流水账/字段拼接/抽象/长度)
