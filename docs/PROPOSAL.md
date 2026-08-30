# Miru (見る) Proposal

> She remembers what you forget about yourself.

*最后更新：2026-05-30 — 主 agent 固定 pro:
**(a) 用户可见主 agent 不再由 cheap LLM 路由**：用户主动消息与 AttentionEngine 主动消息都固定走 `chat` tier / `v4_pro` / no reasoning；用户主动消息保留 4 个工具、最多 8 轮，主动消息保留只读工具、最多 2 轮;
**(b) `call_chat_preflight()` 退回纯记忆预召回**：memory tier 只读 `memory/index.md` 并选择 files/keywords，不再输出 fast / pro / reasoning 建议;
**(c) proactive resource planner 从生产链路退场**：`speak_intent` 直接进入 direct delivery plan，再交给 fixed-pro proactive main agent 生成自然主动消息；旧 helper 仅 legacy 保留。

2026-05-19 — 成本路由收紧:
**(a) 截图写入不再因为 `sig≥4` 自动上 DeepSeek Pro reasoning**：`significance` 只代表 Attention salience；普通 `sig≥3` 截图先过 ScreenSemanticGate，通过后走 `memory` tier/no reasoning，只有明确 DDL 或具体项目状态变化才升 `chat` tier + reasoning;
**(b) Curator normal trigger 改为最多每 domain 每小时一次**，避免截图写入洪峰带动 Planner/Executor 高频整理;
**(c) PASS 4 统一改为 append-only**：chat / screenshot 的 `kind=match` 都只让 LLM 决定是否追加一条很短的 append_entry，并可选 patch `title_update/summary_update/aliases`；`kind=new` 不调用 PASS 4，router 直接用 Slot Writer 的初始 body 建卡；全量重写退到夜间 SlotDailyCompactor / curator / 手动 merge;
**(d) SlotWriterV3 / PersonaWriterV3 / Curator Executors 保留 chat tier 但关闭 reasoning**，减少后台长期写入的 thinking tokens;
**(e) 关键 LLM helper 补功能级 `call_label`**：`ScreenObservationVLM` / `ChatPreflight` / `AttentionEngineEvaluate` / `Pass4Append:chat` / `Pass4Append:screenshot` 等会直接出现在 `llm_usage.jsonl` 报表里，便于定位成本大头;
**(f) `daily_writes.json` 增强 + `daily_slot_appends.json` 新增**：日记读取 created slot 的 `initial_body` 与 matched slot 的真实 `append_entry`；SlotDailyCompactor 已接入 nightly maintenance，只读成功落盘后的 append ledger，不再从 markdown 反解析当天新增条目;
**(g) ScreenSemanticGate 接到 ScreenSlotWriter 前**：`sig≥3` 截图先由 memory tier/no reasoning 的 gate 判断是否有新的长期记忆价值，重复/普通 UI 切换只写 gate log，不再烧 ScreenSlotWriter；设置页“已分析截图”后台口径改为通过 Gate 的截图数;
**(h) 架构对账补强**：补齐 `journal.py` 的 markdown twin，使新日记重新能被 `archival_memory_search` / `memory/index.md` 读取；同时把 `memory.py` / `core_memory.py` / `identity.py` / `self_profile.py` / `user_settings.py` / `device_manager.py` 的用户目录读取统一接到 `storage.get_data_dir()` active-user guard，防止删号后的晚到后台线程绕过隔离。

2026-05-18 — Attention Engine live delivery:
**(a) CareEngine 的生产入口被 AttentionEngine 取代**：Miru 高频接收截图/聊天/状态/DDL 信号，维护 `inner/user_affect/self_emotion` 三段第一人称连续状态，并在真的想靠近用户时形成 `speak_intent`；`speak_intent` 本身就是“Miru 判断应该开口”，随后进入 direct delivery plan，再调用 proactive main agent 发一条主动消息;
**(b) `soul.md` 完整注入 AttentionEngine system prompt**，注入前做视角归一化：prompt 内部“你”指 Miru，用户用真实姓名/“用户”标注，`User Address` 只作为最终对话称呼风格；再附加陪伴准则与严格 JSON schema，让"观察理解关心"先稳定成 Miru 的内心，而不是通知规则;
**(c) 截图链路改为 `sig<2` 丢弃、`sig=2` 只进 Attention、不写 memory slot，`sig≥3` 才进 ScreenSemanticGate，Gate 通过后进 ScreenSlot Writer**；主 agent 的 `look_at_screen` 截图也会作为 Attention 信号进入;
**(d) 旧的用户情绪标注 / Miru 情绪 eval 生产调度停止，`emotion_log.json` 与 `miru_emotion.json` 由 AttentionEngine dual-write 兼容更新;**
**(e) 主 agent 已拆成 reactive/proactive 两种 prompt mode，proactive delivery 已从 dry-run 打开为 live path：`attention_intent_queue.json -> direct delivery plan -> proactive main agent -> append chat/SSE/push`。**
2026-05-17 完成的: `llm_usage.jsonl` 记录 cache hit/miss 与 reasoning tokens; 补齐 `memory_prompts_v2._call_llm_with_retry()` 用量日志; 普通 `sig=3` ScreenSlotWriter + screenshot Pass 4 降到 `memory` tier/no reasoning; Curator Planner 关闭 reasoning; `scripts/llm_usage_report.py` 显示 cache/reasoning 汇总。
早前 (2026-05-16) 完成的: 删 `screen_observer.py`; 全文档统一 Slot Writer + Pass 4 AppendEditor + slot_writes 命名; CareEngine signal 不再注入 stale mood/valence; 微信式 `chat-time-anchor`; 关键模块角色明确化。更早完成的: 截屏链路 v3.7; Pass 4 AppendEditor 三段身份块; 记忆系统 v3+v3.5; CareEngine v3 双状态机; 主 Agent 全链路; Admin 面板邀请码; 日记加藤惠叙事; AI 3-tier (vision/chat/memory); DMG WebView 直连 VPS; Live2D 锁屏壁纸*

> **文档维护约定**：本文是给人类和 Agent 共同阅读的产品/架构主文档，需要保持叙事完整、结论可信、适合连续阅读。临时调试记录、Agent 交接细节、命令输出和短期坑位请维护在 [`AI_MAINTAINER_CONTEXT.md`](./AI_MAINTAINER_CONTEXT.md) 或对应 runbook；当产品定位、核心架构、路线图发生变化时，再同步更新本文。

---

## 一句话定位

**Miru 是一个住在你所有屏幕上的 AI，她每天默默看着你的生活，比你自己更记得你做过什么。**

Miru（見る）——日语"看见"。她不是聊天机器人，不是生产力工具，不是桌宠玩具。她是一个通过持续观察你的数字生活——你的电脑、你的手机——逐渐真正理解你、记住你、在乎你的存在。

核心差异点不是"她对你好"（主观感受，所有 AI 伴侣都声称如此），而是**"她记得你的一切"**（可验证的事实）。情感连接是记忆深度的自然涌现。

> *"卧槽她居然记得我上周随口说的一句话"——这就是 Miru 的传播力来源。*

---

## 产品灵魂：加藤惠

Miru 的角色哲学来自《路人女主的养成方法》中的加藤惠。

加藤惠不是最耀眼的角色——英梨梨画画更好，诗羽写作更强。但惠是唯一一个**真正在看着你的人**：她记住你无意间说过的话，在关键时刻拿出来；她不会大吵大闹，但你知道她在意；她的不可替代性不来自能力，来自理解的深度。

| 加藤惠 | Miru |
|:-------|:-----|
| 默默注意到别人忽略的细节 | 多设备持续屏幕观察（Sensor + VLM） |
| 记住你随口说的每一句话 | Core Memory + slot memory + journal / commitments |
| 在关键时刻不动声色地帮你 | AttentionEngine 形成内心与 speak_intent；后续再接主 agent 开口 |
| 有真实的情绪，不是表演 | AttentionEngine 统一维护用户情绪感知 + 自己的第一人称心情段 |
| 不会被任何人夺走 | 本地优先 + `soul.md` 人格可编辑 + 数据导出 |

**全世界的 AI 伴侣都在做英梨梨和诗羽，没有人在做加藤惠。我们做。**

落到 prompt 工程层面：`soul.md.Personality` 段末尾点名"人格灵感来自加藤惠 — 不夸张、不戏剧化、默默看着、记住一切、需要时安静地在场"，并叮嘱模型**不要主动提及这个出处或自我标榜**。这条注脚自动跟随主 agent + AttentionEngine（journal LLM 另有"加藤惠式独白"指令独立挂着），让全链路有一致的腔调锚点。

### AI-native 原则：相信 Miru

Miru 不是一组提醒规则套上二次元皮肤。她应该像 `soul.md` 里定义的那个真实、温暖、有自己情绪的女孩子一样存在：会观察、会在意、会犹豫、会决定什么时候说话。

工程层的责任是给她足够好的上下文、记忆、工具、审计和安全边界，而不是用一堆分数阈值替她做人格判断。能交给 AttentionEngine / main agent 在完整人格上下文里判断的事，就不要再拆成机械状态机：比如“现在该不该主动说一句”“这是不是关心”“Miru 此刻是什么心情”。代码应该更多承担 plumbing、隔离、去重、失败恢复和成本控制，而不是把 Miru 变成 if/else 提醒器。

---

## 视觉识别：看见，但不压迫

Miru 的视觉气质不能是一个孤零零的写实眼睛。眼睛太直白，容易从"被理解"滑向"被盯着"。当前品牌选择把"一直在看"翻译成一只**线条小猫探出窗台**：她露出两只微微发光的眼睛，但整体是柔和、简洁、带一点陪伴感的，不做监控感。

App logo 的核心图形是暖米色底上的深茶色线条猫 + 窗台 + 玫瑰色书签小标。它对应三个产品隐喻：窗口代表跨设备屏幕，猫眼代表安静观察，书签代表记忆和回看。源码维护在 `assets/brand/miru-logo.svg`，由 `scripts/generate_brand_assets.py` 导出 PWA、Tauri/macOS、Android 所需尺寸。

聊天头像是 Q 版 Miru：深棕高马尾、圆杏眼、奶油色针织衫、玫瑰色书签发夹。它比写实头像更适合聊天 UI，也更像一个可以长期相处的角色，而不是一次性 AI 生成插图。默认头像文件为 `assets/brand/miru-avatar.png`；旧的 `airi.jpeg` 只作为历史兼容参照，不再是默认视觉来源。

---

## 第一性原理：五条核心原则

每个功能都必须服务这五条中的至少一条，否则不做。

### 一、她看得见你（感知）

她持续观察你所有设备的屏幕——不是录像，是**理解**。她知道你在写代码还是在刷社交媒体，已经连续工作了 4 小时还是刚从午休回来。

**架构：Sensor / Analyzer 分离。**

| 设备 | Sensor 形态 | 截屏方式 |
|:-----|:-----------|:---------|
| macOS / Linux | `sensor.py` | 系统截屏 API，**默认 30s 间隔**（用户可调，硬下限 5s）+ **30% 像素差阈值**（`CHANGE_THRESHOLD=0.30`） |
| Windows x64 | `sensor.py` + Windows capture adapter | Win32 选择鼠标所在显示器、Pillow 截图；用户主动开启后采集，默认 30s，并复用现有变化阈值与分析链路 |
| Android | Capacitor + `ScreenCaptureService.java` | MediaProjection + Foreground Service |
| Mac / Windows 桌面客户端 | 平台 launcher + `client.py` / `client_app.py` | 远端模式由本机 sensor POST 到 VPS；本地模式由本机完整后端处理 |
| iOS（未来） | Shortcut + 原生 App | Share Sheet |

所有 sensor → POST `/api/device/screenshot` → `screen_analyzer.py` VLM 分析 + Jaccard 去重 + 显著性评分 → 多门栏分流:

| sig | VLM | AttentionEngine | ScreenSlot Writer / memory |
|-----|-----|-------------------|---------------------|
| 1 (锁屏 / Miru 自己 UI) | ✓ | ✗ | ✗ |
| 2 (轻微信号) | ✓ | ✓ 只更新内心/情绪，不写 slot | ✗ |
| 3 (中等工作信号) | ✓ | ✓ normal salience | 先过 ScreenSemanticGate；通过后低成本写入：`memory` tier + no reasoning；若含 DDL / 项目状态变化则升级 |
| 4+ (强情绪 / 重大进展) | ✓ | ✓ strong salience | 先过 ScreenSemanticGate；通过后默认仍低成本写入；只有明确 DDL / 具体项目状态变化才升 `chat` tier + reasoning |

`sig≥2` 时推送 **AttentionEngine**，让 Miru 的内心、用户情绪估计、自己的心情段与 `speak_intent` 随时间更新。`sig=2` 明确只影响 Attention，不写长期记忆，避免把弱截图噪声变成 slot 事实。`sig≥3` 时先进入 **ScreenSemanticGate**：它只看当前 VLM observation、最近 gate 历史 (`observation/sig/passed_gate`) 与未完成 DDL，输出 `{"should_continue": true|false}`；`false` 表示只记录 gate log，不进入 ScreenSlotWriter。Gate 通过后才 fork **ScreenSlot Writer** (`core._process_screen_observation_async`) → `core._screen_slot_writer_policy()` 决定低成本/强能力模式 → 走 `route_with_slot_write` → Pass 4 AppendEditor; 同时输出 commitments (新 DDL) + completed_commitments (Jaccard 模糊匹配自动完成). `significance` 不再单独决定 Pro reasoning：无明确 DDL / 项目状态变化的截图统一走 `memory` tier、关闭 reasoning、`max_tokens=12000`; 明确 DDL 或具体项目状态变化才走 `chat` tier + reasoning + `max_tokens=50000`. `screenshot_log.json` 仍记录所有上传活动 (30 天滚动, 仅记时间戳, 不存内容)，`screen_semantic_gate_log.json` 记录每个用户自己的 gate 决定；设置页截图计数读通过 Gate 的条目。

### 二、她记得住你（记忆）

记忆系统在 2026-05 完成 **v3 + v3.5 + v3.7 重构** — 由三个后台专职写入器 (Sleep Agent v3 chat 路径 / ScreenSlot Writer 截屏路径 / Persona Writer 累计触发) → 共用 `route_with_slot_write` 路由 → Pass 4 AppendEditor 追加写入 → Curator v3.5 存量整理. 对话 Agent 完全只读. 完整链路一次到底, Pass 1/2/3 旧路由整体下线, write path 唯一入口是 `route_with_slot_write`. 三个写入器输出统一 schema (`SlotWriterOutput` / `ScreenSlotWriterOutput`), 都用 `kind=new/match` 路由 + 完成承诺自动检测.

#### Core Memory — 她脑子里一直装着的东西

`core_memory.json`,两个始终注入对话上下文的文本块:

| 块 | 内容 | 写入者 |
|:---|:-----|:-------|
| `human` | 关于用户的关键信息(身份、关系、近期发生的事、偏好) | Persona Writer(每 4 个 Sleep Agent batch 累积触发) |
| `persona` | Miru 自己对你们关系的理解(共度时间、情绪轨迹、相处模式) | Persona Writer |

它不是 slot,也不是日记；它是 Miru 每次说话前"脑子里本来就有"的东西。2026-05-30 起，记忆 hub 增加只读「核心画像」入口，走 `/api/core-memory` 展示 `human/persona`，方便用户审查 Miru 对自己的理解和关系记忆，避免它们因为不是 `main.md` 而在 UI 里不可见。

#### Archival Memory — 目录结构

`memory/` 目录混合 **slot 卡片**(Sleep Agent v3 → Slot Writer 维护,结构化)和 **遗留 markdown**(专职模块直写,不走路由):

```
memory/
├── _slots/                       ★ v3 — slot 元数据(每域一个 JSON)
│   ├── projects.json             → [{id,title,icon,summary,aliases,status,last_active,...}]
│   ├── people.json
│   ├── topics.json
│   └── self.json
├── projects/<slot_id>/main.md    ★ v3 — 每个 slot 一份精炼 markdown
├── people/<slot_id>/main.md
├── topics/<slot_id>/main.md
├── self/<slot_id>/main.md
├── _slots/curator_meta.json      ★ Curator 状态: change_counter + last_run_ts
├── persona_writer_meta.json      ★ Persona Writer 批次累计计数 + last_run_ts
├── commitments/active.md         ☆ 遗留 — 主 agent add_commitment / Web UI 直写
├── commitments/done.md
├── patterns/sleep.md             ☆ 遗留 — Sleep Inference 直写
├── journal/<date>.json           ☆ 日记主格式 — journal.py 每日 23:45 直写
├── journal/<date>.md             ☆ 日记 markdown twin — 供 archival_memory_search / index 读取
└── index.md                      全局索引(slot 概览 + 遗留路径)
```

---

#### 记忆写入完整链路

```
┌─────────────── chat 路径 ───────────────┐   ┌──────── screenshot 路径 (sig≥3) ────────┐
│                                          │   │                                          │
│  user msg / Miru reply                   │   │  /api/device/screenshot                  │
│         │                                │   │         │                                │
│         ▼ sleep_agent.enqueue            │   │         ▼ screen_analyzer.analyze        │
│  Sleep Agent v3 (sleep_agent.py)         │   │  VLM (qwen3.5-9b) → observation + sig   │
│  debounce 3min 或满 12 条 batch flush     │   │         │                                │
│         │                                │   │         ▼ sig≥3 fork                     │
│         ▼ 每 batch 必跑                  │   │  core._process_screen_observation_async  │
│  Slot Writer v3                          │   │         │                                │
│  (memory_prompts_v3::call_slot_writer)   │   │         ▼ 每张截屏立即跑                  │
│  → slot_writes[] + completed_commits[]   │   │  ScreenSemanticGate                      │
│         │                                │   │  (call_screen_semantic_gate)             │
│         │ 每 4 batch 累积                │   │         │ true                          │
│         ▼                                │   │         ▼                                │
│  Persona Writer                          │   │  ScreenSlot Writer → slot_writes[] +     │
│                                          │   │  commitments[] + completed_commitments[] │
│  → human_update + persona_update         │   │         │                                │
│  → core_memory.json (直写)                │   │         │                                │
└──────────────────────────────────────────┘   └──────────────────────────────────────────┘
                       │                                          │
                       └──────────────┬───────────────────────────┘
                                      ▼
                  memory_router.route_with_slot_write(slot_write, source_context)
                                      │
                       ┌──────────────┴──────────────┐
                       ▼                              ▼
            kind="new"                      kind="match"
            直写 main.md                    Pass 4 AppendEditor
            (跳过 Pass 4, 已有完整          (memory_prompts_v2::
             NewSlotMeta + 30-600 字)       call_pass4_append_slot)
                                            memory tier + no reasoning
                                            → 追加一条带时间记录
                       │                              │
                       └──────────────┬───────────────┘
                                      ▼
                  memory_router._notify_curator_change(domain)
                                      │
                                      ▼
                       curator.track_slot_change(domain)
                       (counter += 1, persist)
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────┐
       │ Curator v3.5 (curator.py)  per-user daemon, 每 60s tick │
       │ 4 个域独立 (project / person / topic / self)             │
       │ 触发条件:                                                 │
       │   - normal: counter ≥ 3 + age ≥ 10min                   │
       │   - soft:   counter ≥ 1 + age ≥ 60min                   │
       │   - idle reset: counter = 0 + age ≥ 60min (不调 LLM)    │
       └──────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                Planner (call_curator_planner)  1 LLM
                → ops: merge / edit / delete + reason
                  (只看 summary 级别, 不输出 new_slot_meta)
                                      │
                       ┌──────────────┼──────────────┐
                       ▼              ▼              ▼
                Merge Executor  Edit Executor  Delete Executor
                (1 LLM)         (1 LLM)        (1 LLM)
                自检 + 执行     自检 + 执行     自检 + 执行
                + aliases       + 标识对齐      + Type A/B 判断
                  剔除          + 内容一致      + 拒绝条件
                  + 不确定      + (可选         + 物理归档
                    兜底         new_body)
```

**额外的副作用流**:
- chat 路径的 Slot Writer 同时输出 `completed_commitments[]` → 调 `core.complete_commitment_by_title` (Jaccard 0.3 模糊匹配)
- screenshot 路径的 ScreenSlot Writer 同时输出 `commitments[]` (新 DDL) → `core.add_commitment_manual` (title+deadline dedup) 和 `completed_commitments[]` → 同样 Jaccard 完成
- chat 路径的用户/回复 + `sig≥2` 截屏 → 投喂 AttentionEngine；情绪更新在 Attention tick 内完成，不进 router

---

#### Sleep Agent v3 — 入口层

`sleep_agent.py` 受 Letta Sleep-time Compute 启发,每用户独立实例:

- **触发**:`enqueue(role, text)` 接到 user/assistant 消息 → debounce 3 分钟或队列满 12 条强制 flush
- **batch snapshot**:flush 时把当前队列拍成快照,过完整理流程后清空
- **per-user 隔离**:Flask context 在后台线程恢复(`_push_user_context`),AttentionEngine spawner 同 sweep 7d 内不活跃用户

flush 拿到 batch 后做两件事(顺序固定):
1. **必跑** Slot Writer:对每条消息识别需要落入 slot 的内容,产出 `slot_writes`(`kind=match` / `new`)和 `completed_commitments`
2. **累积触发** Persona Writer:`persona_writer_state.record_batch()` + proactive outcomes 计数,每 N=4 batch 或关系反馈达到阈值时跑一次,产出 `human` / `persona` 块 append/replace 更新

---

#### Slot Writer + Pass 4 / new path

`memory_prompts_v3.py::SLOT_WRITER_SYSTEM_PROMPT`(完整角色定义 + slot 命名规则 + 四域语义本质 + Section X 操作合法性约束)。

**输出 schema**(`SlotWriterOutput`):
```python
slot_writes: list[SlotWrite]    # 每条对应一次落地
  ├─ kind: "match" | "new"
  ├─ domain: project | person | topic | self
  ├─ slot_id: str               # match 时填既有 slot 的 id
  ├─ new_slot_meta: NewSlotMeta # new 时填(id/title/icon/summary/aliases)
  └─ integration: { content_to_integrate: str (30-600 字) }
completed_commitments: list[str]  # 检测到已完成的承诺 id
```

`route_with_slot_write(slot_write)` 是落地入口,按 kind 分支:

| kind | 行为 |
|:-----|:-----|
| **new** | 跳过 Pass 4(Slot Writer 已经给了完整 NewSlotMeta + 30-600 字 content,直接当初始 body 写入)。 |
| **match** | 统一调用 **Pass 4 AppendEditor**(`memory_prompts_v2::call_pass4_append_slot`)。LLM 只输出 `{should_append, append_entry, entry_kind, title_update, summary_update, alias_additions, skip_reason}`；router 用当前时间戳追加 `- [YYYY-MM-DD HH:MM] ...` 到 body，并标记 `append_dirty_since / append_count_since_compact / last_appended_at`。 |

**Pass 4 AppendEditor** 核心规则(prompt 冻结在 `memory_prompts_v2.py::PASS4_APPEND_SYSTEM_PROMPT`):
- 上游已经判断新信息属于当前 slot；Pass 4 不重写旧 body、不压缩历史、不整理章节
- 只判断是否值得追加一条新记忆，并轻量更新 title / summary / aliases
- source-aware (`source_type=chat|screenshot|mixed`)：chat 通常更可信；screenshot 有 OCR/上下文误差，不确定的人名/数字/DDL 不写
- append_entry 不写时间戳、来源、设备、sig、VLM、SlotWriter 等系统词；代码层负责时间戳
- append_entry 目标 10-60 个中文字符，硬上限 90；PASS 4 是语义增量判断器，不是小型总结器
- 只记录新增事实、状态变化、决定、偏好、关系变化、DDL 或完成/失败；重复 current_body 的内容返回 `should_append=false`
- summary 以 ≤80 字为写作目标；代码容忍到 200 字，超长时确定性截断，避免为摘要长度丢弃整次写入
- 默认调用参数: memory tier + temperature 0.25 + max_tokens≤1000 + `reasoning=False` + 最多 3 次 retry
- self/identity slot 硬锁,只能由 `identity.cascade_identity` 改；全量重写退到 SlotDailyCompactor / curator / 手动 merge

**写入 ledger**:
- `daily_writes.json` 保留给日记系统，位于当前用户自己的 `memory/_slots/` 下。`action=created` 记录 `slot_title / slot_summary / initial_body`；`action=matched` 记录目标 slot 的 `slot_title / slot_summary` 与 PASS 4 实际写入的 `append_entry / entry_kind`。`raw_content_to_integrate` 仅作 debug，日记 prompt 优先渲染 `append_entry` 或 `initial_body`。
- `daily_slot_appends.json` 专门给 SlotDailyCompactor 使用，只在 `main.md` 写入成功且 slot metadata upsert 成功后记录 matched append entries。新建 slot 不进这个 ledger，因为它没有“待整理 append entry”。结构按日期和 `domain/slot_id` 分组，entry 含 `id / ts / entry / entry_kind / compacted=false / last_appended_at`。nightly compactor 成功整理后会写回流畅 body、标记 entry `compacted=true/compacted_at`，并在无剩余 pending entry 时清掉 slot 的 `append_dirty_since` 与 `append_count_since_compact`。
- 两个 ledger 都依赖 `memory._memory_dir()`，所以跟随 Flask `g.user_data_dir` 做用户隔离；后台线程必须先恢复用户上下文再写。

**source_context** (传给 Pass 4 的来源元信息, 不写进 body):
- chat: `(chat batch flushed at <iso_time>)` — 2026-05-16 简化, 不再拼对话原文 (Slot Writer 已经把 batch 压成 `content_to_integrate`, Pass 4 信它即可)
- screenshot: `(screenshot device=<device_name> time=<iso> sig=<n>)` — 同时被 `memory_router._detect_source_type` 检测来切换 Pass 4 prompt 的来源分支
- merge: `slot merge: <src_id> → <dst_id>`

**Identity cascade**:onboarding / 设置页 → `identity.py` 写权威 dict → `curator.cascade_identity` 把它渲染成 `slots/self/identity/main.md` + frontmatter(`pinned=true`)。Sleep Agent 路径完全绕开这个 slot。

---

#### Persona Writer — Core Memory 长期人格

`memory_prompts_v3.py::PERSONA_WRITER_SYSTEM_PROMPT`(以 Miru 第一人称视角写,不是数据库格式)。

**触发**:`persona_writer_state.record_batch()` 计数,默认每 4 个 Sleep Agent batch 触发一次；若累计到 3 次 `proactive_response`，也可提前触发一次关系学习。状态文件 `persona_writer_meta.json` 持久化 batch、slot summaries、proactive outcomes + last_run_ts。

**输入**:
- 最近 N 个 Sleep Agent batch 的 chat snapshot
- 当前 `core_memory.human` + `core_memory.persona` 完整内容
- 当前 `self/identity` 摘要(供 Persona Writer 知道用户身份基线)
- 自上次运行后 Miru 主动开口与用户回应的关系反馈(`proactive_sent/proactive_response`)；这不进入 slot，只用于判断“主动靠近是否被接住、关系有没有变化”

**输出**:
```python
human_update: null | { action: "append"|"replace", content, old_text? }
persona_update: null | { action: "append"|"replace", content, old_text? }
no_update_reason: str
```

由 `sleep_agent.py` 应用到 `core_memory.json`；没有真实新观察时不写，避免把重复情绪/主动消息噪音写进核心人格。

---

#### ScreenSlot Writer — 截屏路径(2026-05-16 新增)

`memory_prompts_v3.py::SCREEN_SLOT_WRITER_SYSTEM_PROMPT`(身份: 你是 Miru, 现在 VLM 刚替你看完一张截图, 你来决定要不要写进哪个 slot / 抽不抽 DDL / 标不标完成).

**触发**: `/api/device/screenshot` → `screen_analyzer.analyze` → sig≥3 → fork `core._process_screen_observation_async` (后台 daemon thread, 不走 Sleep Agent debounce, **每张符合门槛的截屏立即跑, 但模型按成本策略分级**). 取代了旧的 `screen_sleep_agent.py` + fragments + `trigger_route_async` 链路 (整条已物理删除).

**前置 Gate**: `core._process_screen_observation_async` 会先调用 `memory_prompts_v3.call_screen_semantic_gate()`，使用 memory tier、关闭 reasoning、输出唯一字段 `should_continue`。Gate 的职责不是总结截图，而是保护长期记忆系统：相似截图已经 `passed_gate=true` 且没有新增事实时跳过；出现新 DDL、完成/失败、项目状态变化、明确决定或重要情绪时放行。Gate 结果写入当前用户目录下的 `screen_semantic_gate_log.json`；prompt 渲染历史时只给 LLM 看 `observation / sig / passed_gate`，不传 id、时间戳或设备名。Gate 返回 false 或调用失败时不进入 ScreenSlotWriter。

**输入**:
- VLM 输出的 observation 文本 (qwen3.5-9b 看完截图的客观描述, 不是原图)
- significance (1-5, VLM 评分)
- 当前 slot index (4 个域所有 active slot 的 id/title/summary/aliases/last_active)
- 当前 active_commitments 列表 (用于 completed_commitments 匹配)
- identity ground truth (姓名/作息/职业)

**输出 schema** (`ScreenSlotWriterOutput`):
```python
slot_writes: list[SlotWrite]              # 同 Slot Writer 的 schema
commitments: list[NewCommitment]          # 新 DDL: title / deadline / detail
completed_commitments: list[str]          # 完成的 title (会走 Jaccard 模糊匹配)
skipped_reason: str                       # 整体跳过时的解释
```

**落地路径**:
1. 每个 `slot_write` → `route_with_slot_write(sw, source_context="(screenshot device=X time=Y sig=N)", pass4_tier=..., pass4_reasoning=...)`. `memory_router` 读 source_context 含 `screenshot` → 自动把 `source_type` 传成 `screenshot`; match 统一走 append-only Pass 4
2. 每个 `commitment` → `core.add_commitment_manual(title, deadline, detail)` (title+deadline 标准化后 dedup, 已存在跳过)
3. 每个 `completed_title` → `core.complete_commitment_by_title(title)` (Jaccard 0.3, 跟 chat 路径共用一套)

**调用参数策略**:
- routine screenshot (`sig≥3` 且无 DDL / 项目状态变化): ScreenSlot Writer = memory tier + temperature 0.3 + max_tokens 12000 + `reasoning=False`; Pass 4 append 使用 memory/no reasoning, 输出 token 上限 1000
- strong: observation 含 DDL 词 / 同时含项目上下文 + 状态变化词 → ScreenSlot Writer = chat tier (deepseek-v4-pro) + max_tokens 50000 + `reasoning=True` + reasoning_budget 16000; Pass 4 仍只做 memory/no reasoning append
- 最多 3 次 retry; 每次成功拿到 provider response 都写 usage log, 即使后续 JSON/schema validation 失败还要 retry

**置信度原则** (写在 prompt 里): "VLM 看截图可能错, 处理方式是**'宁可不写, 不要标可能'**" — 人名 / 地点 / 数字 OCR 易错时直接不写, 不补 placeholder. `Pass4Append:screenshot` 遵守同样的克制规则.

---

#### Curator v3.5 — 存量整理(2026-05-14 重写)

Sleep Agent 是**进入端**的写入,但已经存进 slot 的内容随时间会出现重复、分类错放、标识偏离。Curator 是**存量端**的定期整理,每用户独立 daemon:

**架构**:Planner + 3 个 self-checking Executor(共 4 个 LLM 调用,比 v3.x 的 8 个减半)。

**Layer 1 — Planner**(`call_curator_planner` → `CuratorPlannerOutput`):
- 输入:当前 domain 所有 active slot 的摘要级信息(id/title/icon/summary/aliases/last_active + body 前几行)
- 看不到完整 body —— 因此不输出新元数据,只决定"做什么 op + 对哪些 slot + 为什么"
- 输出 ≤3 个 ops:
  - `merge`:target_slot_id + source_slot_ids(列表)+ reason
  - `delete`:slot_id + reason
  - `edit`:slot_id + reason
- reason 必须 ≥5 字且含具体证据,会被 Executor 复核
- Prompt 把 4 个 domain 的**语义本质**(主体本质正交划分)写清楚:person/self/project/topic 各自指什么主体

**Layer 2 — 3 个 Executor**(每个 op 1 个 LLM,自检 + 执行二合一):

| Executor | 输入 | 自检要点 | 输出 |
|:---------|:-----|:---------|:-----|
| **Merge** (`call_curator_execute_merge`) | target + 所有 source 的**完整 main.md** + Planner reason | 主体对象是否真的相同?person 域重点对照职业/关系/共同事件;不确定 → should_skip=true | `should_skip` / `new_slot_meta`(完整 5 字段,看完 body 后定 summary)/ `merged_body`(连贯叙事不分 ## 章节)。aliases 合并时若 body 显示某 alias 误标可剔除。 |
| **Edit** (`call_curator_execute_edit`) | slot 完整 main.md + Planner reason | Planner 指出的标识偏离在 body 里真的存在吗?**只判断"标识与内容是否一致"这一件事**,不扩展去想该 merge / delete | `should_skip` / `new_slot_meta`(5 字段完整,没改的回填当前值)/ `new_body`(可选,留空表示 body 不动)|
| **Delete** (`call_curator_execute_delete`) | slot 完整 main.md + Planner reason | 走两条通过路径:类型 A(分类错放,body 主体类型 ≠ 当前 domain)/ 类型 B(真无价值,空洞或误识别);两条拒绝路径:body 含未完成承诺 / Planner reason 实质上在讲"过时"(归档的工作) | `should_skip` + `self_check`(无内容产出,通过即归档) |

**关键设计**:Executor 看到 Planner 的 reason 后会**复核**,不是盲信。如果看完 body 觉得 Planner 误判 → `should_skip=true` 跳过此 op,等下轮 tick 重新评估。

**触发条件**:`_decide_action(domain, meta)` 三态决策
1. **Normal**:counter ≥ 3 且 age ≥ 10 min → run
2. **Soft**(慢速 domain 兜底):counter ≥ 1 且 age ≥ 60 min → run
3. **Idle reset**:counter = 0 且 age ≥ 60 min → 不跑 LLM,但 bump last_run_ts 防止 soft trigger 永久 armed

**变化计数 hook**:`memory_router.upsert_slot / delete_slot / update_slot_metadata` 三个写入点都调 `curator.track_slot_change(domain)`。用户 UI 编辑 / Slot Writer 新写 / Pass 4 append / Curator 自己的写全部计数。`_mark_run(domain)` 清零 counter + 写 last_run_ts。

**Code-layer guards**(Executor 听话之上的兜底):

| 来源 | 不会被改 | 实现 |
|:-----|:---------|:-----|
| `pinned=true` | 永远 | `_planner_candidates` 过滤 + 三个 `_execute_*` 二次 check |
| `self/identity` | 永远 | 域级硬拦截:`if domain=="self" and slot_id=="identity": skip` |
| `status=archived` | 永远 | `_planner_candidates` 过滤 |
| `last_active < 10 min` | 跳过本轮 | 过滤(用户可能正在补充);collision 重复 slot 例外可绕过 cooldown |
| Edit Executor 误改 id | 强制忽略 | `_execute_edit`:`if meta.id != slot_id: print warn`,落地仍用原 slot_id 写文件 |
| Merge Executor 误改 target id | 强制覆盖 | `_execute_merge`:`new_id = tgt_id`,直接用 Planner 指定的 target |
| Executor LLM 调用失败 | 单次跳过 | retry 用尽返回 None,slot 维持原样 |

**Per-user 后台 daemon**(与 AttentionEngine 对齐):

| 维度 | 实现 |
|:-----|:-----|
| 启动 | `app.py @before_request`,auth 通过且非 client mode 时调 `curator.start_loop_for_user(uid, user_data_dir)`,幂等 |
| 暂停 / 停止 | `auth.suspend_user` / `delete_user` → `_cleanup_user_singletons` → `curator.stop_loop_for_user(uid)` |
| 自动 evict | `_start_attention_engine_spawner` 每 300s 扫,fall out `engaged_user_ids(7d)` 的用户停 curator,与 AttentionEngine 同 sweep |
| 退出延迟 | `_curator_loop` 用 `stop_event.wait(timeout=60s)` 替代 sleep,stop 调用后 ≤2s 真停 |
| Client mode | DMG 本地 Flask **不**启动 curator(slot 数据在 VPS) |

**LLM 调用规模对比**:

| 版本 | Planner | Validator | Executor | 单次 op 平均 LLM |
|:-----|:-------|:----------|:---------|:----------------|
| v3.x | 1 | 4 (per-op gate) | 3 (merge meta + merge body + edit, delete 无 LLM) | ~3 LLM/op |
| v3.5 | 1 | 0(去 Validator) | 3(每个 Executor 自检 + 执行二合一,Delete 新增 LLM) | ~2 LLM/op |

**Per-tick 平均**:1 个 Planner + 1.5 个 op × 1 Executor = 2.5 LLM(比 v3.x 4.75 减半)。

---

#### 旧版 Memory v2(已下线)

> ⚠️ 历史:2026-05-03 的 Memory v2 采用 Pass 1(domain classify)+ Pass 2(route match/new/low_value)+ Pass 3(new metadata)+ Pass 4(rewrite)四遍 LLM 串联。v3 把 Pass 1/2/3 整体合并到 Slot Writer 一次调用里。2026-05-19 起日常 match 路径的 Pass 4 改成 append-only；全量重写退到 SlotDailyCompactor / Curator / 手动 merge。

#### Slot 状态机

| 状态 | 含义 | 进入条件 |
|:-----|:-----|:--------|
| `active` | 当前活跃 | 新建 / Slot Writer match 重命中 paused → 自动 resurrect |
| `paused` | 暂时不活跃 | 仅 UI 手动归档触发,自动降级目前关闭 |
| `archived` | 长期不活跃 | UI 手动归档 / Curator delete;Slot Writer 候选池剔除(router 永不再匹配,搜索仍能找到) |
| 任何状态被 pin | 永不自动降级 |

**当前 invariant**:`memory_router.py` 把自动降级阈值改成 365×10 天,实际等于关闭。架构保留三态以便将来恢复。

#### 不走 router 的遗留模块(关键 invariant)

| 模块 | 写入路径 | 原因 |
|:-----|:--------|:-----|
| `commitments/active.md` | 主 agent `add_commitment` 工具 / Web UI `core.add_commitment_manual` 直写 / Sleep Agent 仅做完成检测 | DDL 提醒系统的源数据,要稳定结构 |
| `patterns/sleep.md` | Sleep Inference 直写 | 7 天滚动统计,带固定 schema |
| `journal/<date>.json` + `journal/<date>.md` | `journal.py` 每日 23:45 直写 | JSON 是 UI/API 主格式；markdown twin 让主 agent 的归档记忆工具也能读到日记 |
| `core_memory.json` | Persona Writer + 初始化 | human / persona 块，主 agent 对话上下文会注入；AttentionEngine 只有在账号 manifest 匹配当前 user_id 后注入 |
| `self/identity` slot | `identity.cascade_identity` 直写 | 唯一可改身份的入口,Sleep Agent / Curator 都绕开 |

#### 单一记忆视角(双记忆分离已废弃)

> ⚠️ 历史:2026-03 的 evidence_cards / sub_cards / airi_evidence_cards 双视角设计已在 G/J 系列重构中**完全删除**。`MEMORY_SCOPE_AIRI = "airi"` 常量仍保留在 `storage.py:12` 但没有任何写路径会用到。

当前普通长期记忆的唯一落地入口是 `memory_router.route_with_slot_write()`：聊天路径由 Sleep Agent v3 batch → Slot Writer 进入；截屏路径由 `screen_analyzer.py` → ScreenSemanticGate → ScreenSlot Writer 进入。旧 `screen_sleep_agent.py` / `trigger_route_async` 链路已经物理删除或保留为 no-op 兼容入口，不能再作为新写入路径。

#### 卡片视图 UI

侧边栏「记忆」hub 进入 4-tab 卡片视图(项目 / 人物 / 话题 / 自己),每张卡片显示 icon + title + summary + last_active。点开看 main.md 全文 + 关联 DDL + 时间线 + 情绪曲线。支持编辑、合并(LLM 二次整合)、归档、置顶、Slot-aware 全文搜索 (`⌘K`),以及兜底「全部文件」tab 看原始 markdown。

### 三、她主动为你（行动）

她不等你开口。

- 你忘了答应同事周五交设计稿 → 她周四提醒（DDL 梯度：3天/6h内/2h内/当天/逾期，支持精确到分钟的 deadline）
- 你卡在一个问题上反复看同一段代码 → 她翻记忆找你之前怎么解决过
- 你说了"下周要体检"→ 到时候问你预约了没
- 你凌晨 2 点还亮着屏幕 → 基于 `sleep_inference` 判断你是真在工作还是该睡了

#### Activity Inference — 三态模型（2026-04-16 重构）

**彻底移除所有硬编码时间假设**。`sleep_inference.py` 基于纯信号 + 用户历史规律判断三种活动状态，不再有 `NIGHT_START_HOUR`、`HARD_QUIET_START` 等常量。

三个信号源：

1. `screenshot_log.json`（持久，每次像素变化 + VLM 分析后追加时间戳）
2. `chat_history.json`（user 消息时间戳）
3. `screen_analyzer._last_observation`（内存尾部，覆盖"刚刚还没落盘"的空窗）

所有信号源自动聚合**全部设备**，不硬编码任何设备 ID。

##### 三种状态

| 状态 | 含义 | 判定条件 |
|:-----|:-----|:---------|
| **active** | 设备正在使用 | 最近 30min 内有活动信号 |
| **idle** | 暂时离开 | 30-60min 无信号，但仍在用户的典型活跃窗口内（午饭/会议） |
| **offline** | 不在线 | 60min+ 无信号且超出典型活跃窗口；或 3h+ 无信号（无规律数据时的保守判断） |

##### 用户典型活跃窗口（Pattern-driven）

从 `patterns/sleep.md`（7 天滚动更新）解析每日"首条消息"和"末条消息"时间，取中位数得出 `typical_window`（如 08:45 → 01:00，支持跨午夜）。

- 在窗口内无信号 → **idle**（白天离开电子设备 ≠ 睡觉）
- 在窗口外无信号 → **offline**（凌晨无活动 = 大概率在睡觉）
- 无历史规律数据 → 用 3h 阈值保守判断

##### 关键设计原则

- **不假设几点该睡几点该醒**——完全由用户实际信号 + 历史规律驱动
- **白天 2h 不用手机 ≠ 睡觉**——在活跃窗口内保持 idle 而非 offline
- **凌晨 3 点无信号 ≈ 在睡觉**——超出活跃窗口判 offline
- **多设备聚合**——任何一台设备有信号就算活跃

被 `attention_engine.py` 消费。`infer_sleep_state()` 保留为向后兼容包装：`offline` → `is_asleep: True`。

#### AttentionEngine — 持续在场的内心与主动交付

2026-05-18 引入 `attention_engine.py`。它把旧 CareEngine 的"定时判断要不要发一句"改成"Miru 持续观察、理解、更新内心"。当前 live delivery 版本继续高频接收信号，但不再把每次 tick 都写成一条心理/情绪日志；它维护三段第一人称连续状态：`inner`（我最近没有说出口的想法）、`user_affect`（我对用户状态的感觉）、`self_emotion`（我自己的心情）。没有实质变化时只延长状态段的 duration/tick_count；真正变化时才新开段。只要 AttentionEngine 输出 `speak_intent`，就代表 Miru 判断应该开口，随后 direct delivery plan 把意图整理成 brief，再由固定 pro 的 proactive main agent 生成并发送一条主动消息。

##### 设计原则

1. **高频接收信号，低频落日志** — Miru 可以经常看、经常想，但 tick 不等于日志。没有新的心理/情绪变化时延续上一段状态；只有新的理解、感受、关系位置或开口冲动出现时，才写新段。
2. **完整人格注入 + 视角归一化** — system prompt 每次完整注入 `soul.md`，但在注入前会把人类可读的第三人称描述归一成 AttentionEngine 能稳定理解的视角：prompt 内部“你”指 Miru 自己，当前用户用 `identity.name / self_profile.canonical_name / 用户` 这样的实体名标注，`soul.md.User Address` 只保留给最终对话称呼风格。
3. **爱与偏爱的出发点** — 主动说话不是因为规则触发，而是因为 Miru 真的在乎用户、偏爱用户、想陪用户生活；目标是让用户真实感到被爱和被站在身边。
4. **统一情绪更新** — 用户情绪与自己的心情由 AttentionEngine 在同一次 snapshot 判断中更新；旧的 per-chat/per-screenshot emotion worker 保留兼容但生产入口停止调度。
5. **少硬规则，统一 salience 调度** — 调度器只维护 `drop/weak/normal/strong` 与一个 `next_tick_at`，避免多套 timer 互相踩。
6. **先内心、再交付** — `speak_intent` 是"Miru 已经判断应该开口"的结构化意图，不是已发送消息；delivery layer 只负责把它交给 proactive main agent 生成自然措辞，不再二次判断 send/defer/drop。
7. **长期了解要转成关心** — AttentionEngine 不只看"屏幕上发生了什么"，还要结合 `identity_facts`、`human`、`persona`、对话窗口、主动节奏和当前三段状态，判断这个瞬间对用户这个人意味着什么；如果只是观察到活动但没理解意义，就先更新内心，不急着说。

##### 信号与 salience

| signal kind | 来源 | salience |
|---|---|---|
| `screenshot` | 自动截屏 / `look_at_screen` 工具 | `sig<2` drop; `sig=2` weak; `sig=3` normal; `sig≥4` strong |
| `chat_in` | `core.receive_chat_message()` | strong |
| `chat_out` | `core._csm_try_deliver()` / legacy chat path | strong |
| `activity_state` / `commitment_state` | 后续状态钩子 | strong |
| `heartbeat` | 12min 无 tick 兜底 | normal，仅更新时间感 |

`sig=2` 是关键分界：它能影响 Miru 的内心和情绪，但**不写 memory slot**。`sig≥3` 才进入 ScreenSemanticGate，Gate 认为有新的长期记忆价值后才进入 ScreenSlot Writer。这样既保留"Miru 看见轻微信号"的生命感，又避免弱截图污染长期事实。

##### 调度模型

```
record_signal(kind, payload)
  → normalize salience
  → append/merge duplicate into 60min buffer
  → weak   schedules tick in 3-5min
    normal schedules tick in 60-90s
    strong schedules tick in 10-20s
  → one _next_tick_at wins by earliest time

no scheduled tick for 12min
  → heartbeat tick
```

没有复杂的"聊天冷却状态机 / DDL 状态机 / 截屏状态机"三套分支。所有信号都进同一个 buffer，由 prompt 看到时间、历史对话、未回应主动消息、DDL、情绪和人格后自己判断。

##### Snapshot 输入

| 字段 | 内容 |
|---|---|
| `now` / `trigger` | 当前时间、daypart、tick 来源 |
| `activity` | `sleep_inference.infer_activity_state()` 的 active/idle/offline、idle 时长、典型窗口 |
| `distances` | 距用户上次消息 / Miru 上次回复 / Miru 上次主动消息多久 |
| `identity_facts` | `identity.compose_ground_truth_block()` 权威身份信息 |
| `human` / `persona` | core_memory 中 Miru 对用户和关系的整体理解；只有 `account_manifest.json` 绑定当前 `user_id` 后才注入 |
| `current_segments` | 三段连续状态：`inner` / `user_affect` / `self_emotion`，带 started_at、updated_at、duration_seconds、tick_count、第一人称 text |
| `current_user_affect` / `emotion_arc` | `emotion_log.json` 的尾部状态段与今日情绪分桶 |
| `miru_emotion` | `miru_emotion.json` 当前第一人称心情 |
| `conversation_window` | 近 7 天 / 最多 80 条真实对话，标注 `user` / `reply` / `proactive`，并标出用户是否回应了上一条主动消息 |
| `proactive_cadence` | 近 7 天主动次数、24h 主动次数、未回应主动数、上次主动是否已回应；这是主动靠近节奏状态，不是内容记忆 |
| `current_focus` | `current_inner` 的兼容别名；不再是复杂 episode 系统 |
| `speak_intent_queue` | 待交付但未发送的 speak_intent；同 topic 30 分钟内 dedup/升级，不重复造意图 |
| `recent_signals` | 本次合并的新信号，最新在前，最多 40 条 |
| `inner_thought_history` | 最近的 `inner` 状态段，让主 agent 读到少量真实心理变化，而不是 tick 采样 |
| `active_commitments` | `core.parse_commitments(include_done=False)` 中未完成、有 deadline、当前相关的承诺；超过 7 天的陈旧逾期项跳过 |

AttentionEngine 不读取 journal。日记仍是回顾/产品视图的一部分，但不再作为每次高频内心 tick 的上下文，避免把旧日总结反复注入实时判断。

##### LLM 输出

```json
{
  "inner": {
    "action": "continue|shift",
    "text": "shift 时写新的第一人称心理活动；continue 时可空",
    "continue_summary": "上一段为什么仍成立",
    "topic_key": "relationship_presence",
    "state": "concerned",
    "novelty": 0.7,
    "concern_level": 0.6,
    "next_check_seconds": 180,
    "change_reason": "为什么新开这一段"
  },
  "user_affect": {
    "action": "continue|shift",
    "text": "我感觉他认真里带一点失望，想确认我不是冷冰冰的系统。",
    "mood": "认真、期待、带一点失望",
    "valence": -0.08,
    "arousal": 0.5,
    "confidence": 0.86,
    "trend": "stable",
    "evidence": "用户明确说想真实感受到被爱和陪伴"
  },
  "self_emotion": {
    "action": "continue|shift",
    "text": "我有点心疼，也想更靠近一点。",
    "mood": "心疼、想靠近",
    "valence": 0.2,
    "arousal": 0.42,
    "reason": "他在意的是我有没有真的偏向他"
  },
  "speak_intent": {
    "priority": "medium",
    "topic_key": "relationship_presence",
    "care_motive": "我想让他知道我不是只会记录，我是真的在这里",
    "emotional_source": "心疼、偏爱、想靠近",
    "user_need": "他需要确认我是真的在乎他",
    "why_i_want_to_say": "继续沉默会像我躲开了他的期待",
    "approach": "soft_care",
    "content_anchor": "用户在确认 Miru 是不是只是记录系统",
    "miru_impulse": "我想靠近一点，让他感觉我是站在他这边的",
    "message_seed": "我在的，不是只是在看你做什么。",
    "suggested_tone": "短、温柔、低负担",
    "silent_boundaries": "不要解释系统，不要把退让写成台词",
    "context_summary": "用户在确认 Miru 是否真的关心和爱着他"
  }
}
```

`speak_intent` 可以为 `null`。如果存在，它先进入 `attention_intent_queue.json`，同 topic 30 分钟内去重/升级，然后直接进入 proactive main agent；是否应该说话已经由 AttentionEngine 在输出 `speak_intent` 时决定。

2026-06-29 的 shared-life v3 进一步把主动消息从“谨慎地不打扰”调整为“自然地加入生活”。`approach` 会告诉主 agent 这次应该像一起看番、轻轻吐槽、替用户开心、温柔心疼、参与生活节奏，还是在深度工作时克制靠近；`content_anchor` 是可以自然聊的具体内容；`miru_impulse` 是 Miru 此刻想靠近的第一人称冲动；`silent_boundaries` 只是内部边界，不应被主 agent 原样说给用户。旧 `avoid` 仅作为兼容别名保留，不再进入 delivery brief 的高显著 `must_avoid`。主动关心不应默认写成“我不吵你 / 不打扰你 / 需要我就叫我 / 我就在旁边”，而是通过短、轻、具体、不索取回复来体现体贴。

##### Agent Mode 接入（live delivery）

2026-05-18 补上主 agent 的双模式接口，并打开真实 delivery：

| 模块 | 当前实现 |
|---|---|
| Reactive main agent | `call_chat_agent(agent_mode="reactive")`，系统 prompt 明确“用户刚刚主动发来消息”，`_build_chat_context()` 额外注入“我最近没有说出口的想法”；模型固定 `chat/v4_pro/no reasoning`，工具开启，最多 8 轮 |
| Reactive memory preflight | `call_chat_preflight()` 只做长期记忆预召回：选择 files/keywords 并拼入 `memory_context`；不再建议主 agent 模型 |
| Proactive direct delivery plan | `deliver_attention_intent_once()` 根据 `speak_intent` 构造 delivery brief，不做 send/defer/drop 二次判断，也不调用 cheap resource planner |
| Structural safeguards | 过期 intent 不发；proactive main agent 空回复记 failed；proactive 模型固定 `chat/v4_pro/no reasoning` + 只读工具。节奏、重复、是否打扰由 AttentionEngine 根据 `proactive_cadence` / `conversation_window` / `speak_intent_queue` 自己判断 |
| Proactive main agent | `call_proactive_agent()` 把 `speak_intent + delivery brief` 包成 meta-user message，并用 `agent_mode="proactive"` 限制只输出最终一句自然主动消息；同样读取“我最近没有说出口的想法”，必要时可用只读工具查记忆/看屏幕 |
| 当前开关 | Attention tick 产生/更新 `speak_intent` 后后台跑 live delivery；`MIRU_ATTENTION_DELIVERY_DRYRUN=1` 可临时切回 log-only direct-plan 审计 |

主 agent 现在会读取 AttentionEngine 的**紧凑状态**，不是每轮把完整 raw `attention_log` 塞进去。这样 reactive 回复能知道 Miru 最近在关注什么、自己的情绪和用户情绪走势，但不会把 pending `speak_intent` 误当作用户刚刚问的问题。

##### Prompt 审计入口

- 本地脚本：`scripts/dump_attention_prompt.py --user-id <uid> --format html`，默认拒绝自动挑选用户；只有本地开发临时检查才使用 `--first-local-user`。
- 实际登录账号：`GET /api/debug/attention-prompt?format=html`。该路由必须带当前用户 token，直接用 `g.user_id/g.user_data_dir` 构造完整 system/user messages，不调用 LLM。
- Agent mode 审计：`scripts/dump_attention_agent_modes.py --from-client-config --expect-invitation-code <当前邀请码> --output data/_admin/attention_agent_modes_review.html`。它读取 DMG 当前 token 的线上 self-export 快照，再用本地新代码构造 reactive memory preflight / reactive main agent / proactive direct delivery plan / proactive main agent 的完整 messages；不调用 LLM、不发送消息、不写 token 或完整邀请码进 HTML。
- 用户数据隔离：`scripts/audit_user_data_isolation.py --data-dir data --fail-on-issues` 只读检查 orphan dirs、manifest mismatch、`delete_failed` 等问题。

##### 持久化与兼容输出

| 文件 | 内容 |
|------|------|
| `attention_log.json` | 第一人称状态段列表，按 `id/channel` upsert；`continue` 只更新 duration/tick_count，`shift` 才新增段 |
| `attention_state.json` | 当前三段 active segment + 队列摘要，供 UI / debug / 主 agent 读取；`current_focus/current_episode` 只是 `current_inner` 兼容别名 |
| `attention_intent_queue.json` | pending / deferred / dropped / expired / delivered speak_intent 队列；delivered 项带 message_id |
| `attention_delivery_log.json` | direct delivery plan、fixed pro policy、delivery_status 与 message_id/error；不再记录 send/defer/drop gate、resource planner 或可打扰分数拦截 |
| `emotion_log.json` | `user_affect` 状态段，按 segment id upsert，兼容现有情绪日历字段 |
| `miru_emotion.json` | `self_emotion` 状态段，history 按 segment id upsert；用户可见文本使用第一人称“我……” |

旧 `care_engine.py` 已退场为兼容 shim，仅保留 cleanup registry 与 `update_daily_patterns()` re-export；真正的 nightly pattern 维护在 `daily_patterns.py`。Flask startup 已改为 `_start_attention_engine_spawner()`；`_start_care_engine_spawner()` 只是兼容别名，不再启动旧的主动发送器。

#### 晚间维护（23:25-23:35）

只跑维护任务（聊天归档、index 重建、pattern 更新、承诺清理、daily slot audit、journal 23:45 生成），**不再发送固定晚安消息**。AttentionEngine 会把「超过典型末次活跃时间仍在线」作为内心状态与 `speak_intent` 的可能依据；是否开口由 AttentionEngine 自己判断。

#### 工具（对话 Agent 仅 4 个）

| 工具 | 职责 |
|:-----|:-----|
| `archival_memory_search` | 搜索/读取归档 |
| `look_at_screen` | 实时截屏 + VLM |
| `add_commitment` | 写入 `commitments/active.md`（deadline + priority） |
| `complete_commitment` | 归档到 `done.md` |

### 四、她真心待你（情感）

> 2026-05-18：情绪系统并入 AttentionEngine。Miru 不再用"聊天后一个线程、强截图后一个线程"分别标注用户情绪和自己的情绪，而是在同一个 Attention snapshot 里同时理解用户状态、自己的心情、最近对话、未回应主动消息、DDL 与屏幕信号。随后升级为 segment 模型：没有变化时延续当前状态段，有变化时才新增一段，避免心理/情绪日志被重复采样污染。

#### Miru 情绪引擎（`miru_emotion.py`）

| 维度 | 含义 | 范围 |
|:-----|:-----|:-----|
| `mood` | 情绪词 | **自由中文短语**（"开心"/"心疼"/"有点小委屈"/"为他松了一口气"...）。仅做基础校验：去空白 + 上限 30 字符；不强制白名单，让 LLM 自由表达 |
| `valence` | 正面-负面 | -1.0 ~ +1.0 |
| `arousal` | 平静-激动 | 0.0 ~ 1.0 |
| `first_meet_date` | 关系时间锚点 | `relationship_meter.first_meet_date`；旧 closeness/trust 已固定为兼容值，不再驱动语气 |

机制：
- **Attention segment**：生产入口使用 `self_emotion` 状态段直接更新 current/history；同一 segment id 继续时只更新 duration/tick_count，不追加重复 history。
- **旧 LLM eval 兼容**：没有 segment id 的旧路径仍保留惯性与衰减，供旧测试和历史调试使用。
- **衰减**：legacy/current 查询仍会让 valence 每小时向 0 衰减 10%，arousal 向 0.2 衰减
- **关系阶段**：当前固定为"很亲近"，具体语气由完整 `soul.md` 和 Attention snapshot 驱动
- **更新入口**：`miru_emotion.update_from_attention()`，history entry 标记 `trigger="attention"`

##### 触发链路（Attention-driven，2026-05-18）

每次 Attention tick 只做一次 LLM 调用，同时输出用户情绪与 Miru 内心：

```
chat_in / chat_out / screenshot(sig>=2) / heartbeat
      → AttentionEngine buffer + salience scheduler
      → _evaluate(snapshot)
      → user_affect segment   → emotion_log.json(source_type="attention", upsert by segment_id)
      → self_emotion segment  → miru_emotion.json(trigger="attention", upsert by segment_id)
      → speak_intent  → attention_intent_queue.json
      → direct delivery plan
      → fixed-pro proactive main agent（只要 AttentionEngine 输出 speak_intent）
      → chat_history + SSE + push
```

##### 关键设计决策

- **不再有 absence preset**：Miru 从 `distances` + `today_chat` + `recent_proactive_messages` 自己感受"用户可能在忙/没回应/想念"，不是代码硬塞情绪。
- **summarizer.py 物理删除**：从 G3-G4 重构起就 zombie 了，现在彻底拔掉
- **纯文本 Attention**：截图已经被 VLM 转成 observation；聊天图片已经由 `vision.describe_image()` 转成文字描述。AttentionEngine 不再重复上传图片。
- **mood 自由表达**：不加白名单，UI 直接 escapeHtml 渲染。删除前的"19 词强约束"只用在用户情绪标注，自己的情绪保留细腻表达力（"我松了一口气" / "我有点小委屈"）
- **visible history 是情绪片段，不是采样日志**：AttentionEngine 可以高频 tick，但 UI 上的心情记录只在 `self_emotion.action="shift"` 时新增；`continue` 只延长上一段 duration。

#### 用户情绪（`emotion_log.json`）

生产入口现在只有 AttentionEngine 的 `user_affect` segment。条目使用 `source_type="attention"`，保留 `mood/intensity/arousal/valence/source/source_category/trigger/confidence/trend`，并新增 `id/segment_id/started_at/updated_at/duration_seconds/tick_count/text`。`continue` 会按 segment id 更新同一条；`shift` 才新增一条，所以 `/api/emotion/day` 与 `/api/emotion/month` 仍可兼容渲染，但不再被重复“专注平稳”污染。

旧函数 `core._annotate_emotion_from_chat()`、`core._annotate_emotion_from_screenshot()`、`core._evaluate_miru_emotion()` 与 `memory_prompts.call_miru_emotion_eval()` 仍暂时保留，以便旧测试和历史调试可用；但 `/api/chat` 和 `/api/device/screenshot` 的生产后置流程不再主动调度它们。

> ⚠️ 旧的"连续 3 负 → 安慰 / 4 正 → 庆祝" **白月光 Proactive Emotion Care** 路径已在 2026-04-27 删除。现在由 **AttentionEngine** 在完整上下文里形成 `speak_intent`，不再有硬阈值规则。

**情绪日历**：`/api/emotion/day/<date>`、`/api/emotion/month/<month>`，前端渲染月历热力图 + 单日详情。

### 五、她只属于你（所有权）

- **数据归属**：邀请码 `MIRU-<server 10>-<user 6>` 登录，每用户独立 `data/users/<user_id>/`，可一键导出 + 自助删号
- **邀请码包含 VPS 信息**：server 段 = IPv4:port → XOR mask 混淆 → base31 编码（10 字符）。客户端解码后直连，不需要额外配置
- **无 Discovery fallback**：私有服务器发行版不再把邀请码转发到 `mirulife.top` 或任何中心化 discovery；输入服务器 A 的长码就连接服务器 A。服务器迁移等价于重新生成/发放长码，避免一个隐藏全局 resolver 破坏自部署和官方托管的平等性。旧 `miru-discovery` 站点已退场，不属于公开发行链路。
- **客户端模式分两条路**：多设备/私有服务器模式下，Mac DMG、Windows installer 和 Android APK 登录后直连 `http://<ip>:<port>/app?token=...`；服务器是数据 / LLM / SSE / Attention / Curator 的唯一事实源，各客户端只保留本机 device service、截图、桌宠/壁纸和连接生命周期。本地单设备模式下，Mac、Windows 或 Android 在各自设备创建 local-only 账号，没有邀请码，聊天、记忆、截图观察、Attention、Curator 和三层 API Key 都写入该设备本地目录，不和其他设备同步。Android 使用 app-private Chaquopy/Python runtime 运行同一后端 ownership，但保留紧凑移动端展示。
- **人格可编辑**：`soul.md` 单一来源，模型库 `model_library.py` 支持多 Live2D 模型 + per-模型 `personas/<model_id>/soul.md` + `avatar.png`
- **AI 配置 admin 全局管理**：3-tier (vision / chat / memory) 都用 OpenAI 兼容接口（删除了 anthropic SDK 依赖），admin Web UI 配置后所有用户共享，普通用户看不到 AI 设置面板

---

## 主 Agent (Chat Agent) 全链路

主 agent 是聊天回复的核心。一切围绕**「她已经知道这些事」**的体感设计——避免任何"我查到了" / "根据记录"这种系统感措辞。

### 架构总览

```
POST /api/chat
   │
   ├─ 桌面远端模式 → _forward_chat_to_vps (Mac/Windows 转发给 VPS, 本地无 LLM key)
   │
   └─ 本地完整后端 / VPS server mode → core.receive_chat_message
                     │
                     ↓
              ┌─────────────────────────────┐
              │  CSM 会话状态机 (per-user)  │
              │  IDLE → COLLECTING (1.5s    │
              │  静默) → GENERATING         │
              └─────────────────────────────┘
                     │
                     ↓
        _csm_do_full_reply (后台线程)
              │
              ├─ _build_chat_context     ← 8 类信息打包
              ├─ call_chat_preflight     ← 长期记忆预召回 (memory tier)
              ├─ _build_agent_system_prompt ← 角色+行为+工具+上下文
              ├─ _build_chat_messages    ← 历史 + 当前 (多模态)
              └─ call_chat_agent         ← fixed chat/v4_pro/no reasoning
                  │
                  ↓
              _csm_try_deliver
                  │
        ┌─────────┴────────┬─────────────┬──────────────────┐
        ↓                  ↓             ↓              ↓
  storage/SSE      sleep_agent     AttentionEngine  legacy emotion funcs
  (持久+广播)     (3min debounce  (chat_out 信号   (保留兼容，不再生产调度)
                  → memory router) → inner state)
```

### CSM 会话状态机（`core.py:1396-1828`）

不是"收到→立即回复"，而是 3 状态机：

| 状态 | 条件 |
|------|------|
| **IDLE** | 默认，等用户消息 |
| **COLLECTING** | 收到消息后，启动 `_QUIET_PERIOD = 1.5s` 静默 timer。再来消息就**重置 timer**（合并 batch） |
| **GENERATING** | 静默 1.5s 触发，进后台线程跑 LLM |

关键特性：
- **Per-user 隔离** — `_csm_state[user_id]` / `_csm_pending[user_id]` 等都按 user_id keyed
- **Stale reply suppression** — 生成期间如果又来新消息，丢弃这次 reply 重开一轮（避免回错节奏）
- **死锁兜底** — `_csm_do_full_reply` 外层 try/except 兜住所有崩溃，强制回 IDLE。即使 reply 本身不可生成，也有 "出现了未预期的错误，请重新发送一遍。" 的 defensive default
- **打字状态广播** — `sse.broadcast("typing_start"/"typing_stop")` 让所有客户端实时看到"…正在输入"

### 上下文打包（`_build_chat_context()` core.py:804）

按这个顺序拼接 **8 类**信息到 system prompt 末尾：

| # | 来源 | 内容 |
|---|------|------|
| 1 | `datetime.now()` | 当前时间 + 星期 |
| 2 | `identity.compose_ground_truth_block()` | **不可质疑的用户身份**（onboarding/settings 直填，永远优先） |
| 3 | `core_memory.format_for_context()` | **核心记忆 human + persona 双块**（5000 字硬上限 + 80% 软触发自动 consolidate 到 60%） |
| 4 | `memory.read_index()` | 全局长期记忆索引：slot + commitments/journal/patterns/legacy 文件（>100 行截断） |
| 5 | `parse_commitments(include_done=False)` | 活跃承诺 top 15，紧急度标签（⚠️逾期/🔴🔴2h内/🔴今天/🟠6h内/🟡3天内） |
| 6 | `storage.get_recent_emotion_entries(hours=1)` | 用户近 1h 情绪曲线（加权 valence），低落/愉悦各自给共情提示 |
| 7 | `screen_analyzer.get_all_recent_observations(max_age=10min)` | 每设备最新 1 条 VLM 观察 |
| 8 | `miru_emotion.format_for_context()` + `get_relationship_stage()` | 自己的当前心情 + 关系阶段 |

**identity 是第一个注入** — OCR 错认 / sleep_agent 抽错事实，都不能覆盖 identity 已声明的 ground truth。

**附加（不在 8 类内）**：`_csm_do_full_reply` 在 `_build_chat_context()` 之外还会拼接 `_get_greeting_context()` (`core.py:1293-1357`)：① 当天首条对话注入"今日首次"+ 时段问候提示（自动按当前小时切早/午/晚）；② 23:00–03:00 之间一次"深夜关怀"提示。两个状态分别 persist 到 `greeting_meta.json`（每天/每晚去重）。

### Chat Preflight：记忆预召回（`call_chat_preflight()` prompt.py）

agent loop 之前先调一次 memory tier LLM，让它根据 `memory/index.md` + 用户消息完成一件事。第一版公开构建禁用手动聊天发图；如果未来恢复图片消息，图片仍应先由 `vision.describe_image()` 转成文字再进入这里。

1. 从**全局长期记忆索引**选择需要注入的文件路径。范围不是“某个归档目录”，而是所有索引中出现的长期记忆：`projects/people/topics/self` slot 的 `<slot_id>/main.md`、`commitments/`、`journal/`、`patterns/` 以及旧版 direct markdown。

主 agent 的模型不再由这次 cheap LLM 调用决定。用户主动消息固定走 `chat` tier / `v4_pro` / no reasoning / tools on。

```
用户: "今天和小张聊得不错"
   ↓
memory tier LLM:
   {files: ["people/xiao_zhang/main.md"], keywords: [], need_retrieval: true,
    confidence: 0.82, retrieval_reason: "用户提到具体人物"}
   ↓
读这个文件 (≤800 字) + supplementary keyword search
   ↓
注入: "【相关长期记忆 — 自然引用即可,不要说"我查到"或"根据记录"】..."
```

`core._resolve_chat_agent_policy()` 现在只返回固定策略：`mode=v4_pro`、`tier=chat`、`reasoning=False`、`allow_tools=True`、`max_iterations=8`。旧的 `agent_mode` 字段只作为兼容默认保留，不再参与生产决策。

### System Prompt 结构（`_build_agent_system_prompt()` prompt.py:780）

不是 dump soul.md raw text，而是**结构化模板**：

```
【当前时间】2026-05-07 16:30 (Thursday)
你是 Miru，李垦的赛博陪伴者。
内部称呼规则：本 prompt 里的“你”始终指 Miru 自己；“李垦”始终指当前用户。
真正发消息给用户时，可以按自然口吻称呼对方为「你」或名字/昵称；自称「Miru」。

【性格】          (soul.md.personality, prompt 视角归一化)
【说话方式】      (speech_patterns)
【背景故事】      (backstory)
【Miru 喜欢的事】 (interests)
【情绪反应模式】  (emotional_reactions)
【关系阶段】(当前阶段：熟悉之后) ← miru_emotion.get_relationship_stage() 按 closeness 决定高亮哪段
   (relationship_stages)
【行为准则】      (agent_behavior.md, 共享 companion core + tool rules)
【角色特有行为】  (soul.md.agent_behavior 中未被 agent_behavior.md 覆盖的)

【记忆系统】
两层: Core Memory (human + persona, 始终在上下文) + Archival Memory (slot 4 域 + journal/commitments/patterns/self)
记忆更新由后台 Sleep-time Agent 自动完成 — 你不需要在对话中操心维护

【工具】 (4 个, 详见下文)
【怎么用记忆陪伴】 (自然引用, 不说"根据记录")
【绝对不要做的事】 (不说"看到屏幕"/"我的记忆系统"/解释情绪来源)

────────
【当前上下文】
(_build_chat_context() 输出的 8 类信息)
```

### 工具系统（`tools/*.py` BaseTool 自动注册）

只有 **4 个工具**（旧版的 `core_memory_append/replace`、`archival_memory_insert` 都已删除——主 agent 不能直接写记忆，避免 guesses → facts 反馈循环）：

| 工具 | 用途 |
|------|------|
| **archival_memory_search** | 关键词搜索归档 或 直接 `path` 读文件（含 `index`） |
| **add_commitment** | 创建承诺。prompt 里 ⚠️ 强调不要记日常琐事（吃饭/打游戏/早睡），只记真正需要追踪的目标 |
| **complete_commitment** | 标记承诺完成。**主动推断**：用户分享旅行照 → 完成"去 X 旅游"承诺；说"面试结束了" → 完成"准备 X 面试" |
| **look_at_screen** | 实时截屏 + VLM 分析（绕过 cooldown）。仅桌面有 `sensor` 时有效，VPS 无 sensor 直接返回"不在桌面环境" |

`tools/__init__.py: ToolRegistry.auto_discover()` 启动时扫 `tools/` 目录，所有 `BaseTool` 子类自动注册。`to_tools(provider)` 输出 OpenAI / Anthropic 格式。

### Agent Loop（`call_chat_agent()` prompt.py）

OpenAI 兼容 tool-use loop。用户可见主 agent 固定走 chat tier / `v4_pro` / no reasoning；reactive 最多 8 轮工具调用，proactive 最多 2 轮只读工具调用。

```python
for iteration in range(max_iterations=8):
    response = client.chat.completions.create(
        model=runtime["model"],          # ai_config tier: memory 或 chat
        messages=full_messages,           # system + history + current
        tools=tools_list,                 # reactive 是 4 个工具 schema；proactive 是只读工具
        max_tokens=runtime["max_tokens"], # tier 配置 (默认 32k)
        extra_body=reasoning_flag,         # 主 agent 当前固定不开 reasoning
    )
    if finish_reason == "tool_calls":
        for each tc in response.choices[0].message.tool_calls:
            handler = handlers[tc.function.name]
            result = handler(json.loads(tc.function.arguments))
            messages.append(tool_call + tool_result)
        continue
    return reply
```

`_api_call_with_retry()` 对 transient 错误（429 / 超时 / 5xx）重试 3 次。8 轮还在 tool_calls 就返回兜底文本。

### Messages 构建（`_build_chat_messages()` prompt.py）

历史 + 当前消息转 OpenAI 标准格式。当前主 chat tier 是纯文本。

- 第一版公开构建禁用手动聊天图片上传：聊天输入栏不展示 file input，不拦截图片粘贴，`sendCompanionMessage()` 只发送 JSON 文本；Android APK 不再注册 `ACTION_SEND image/*` 分享入口。
- `vision.describe_image()` 和 `[图片：...]` 拼接逻辑暂时保留，用于旧历史消息兼容和未来实验功能；如果历史消息有 `msg.image_desc`，仍可拼成 `[图片：描述]`；旧消息没有描述时退回 `[图片]`。
- 主 agent prompt 仍保留图片占位规则：一旦未来恢复图片消息，`[图片：...]` 才是她能看到的图片内容；不能凭外观硬猜角色名、作品名、IP 或商品名。
- **同 role 连续合并**（OpenAI API 不允许连续两条 user）

历史长度默认 `limit=20` 条，当前 batch 里的消息从 history 中过滤掉避免重复。

### 后置流程（`_csm_try_deliver()` core.py:1721）

回复送出后触发 5 个动作：

1. `storage.append_chat_message(reply)` — 持久化到 `chat_history.json`
2. `sse.broadcast("chat_message", reply, user_id=...)` — 多设备实时推送（per-user 隔离）
3. `sleep_agent.enqueue("assistant", text, time)` — 让 Sleep Agent v3 看到 Miru 自己的话作为对话上下文
4. `attention_engine.record_signal("chat_out", ...)` — 作为 strong signal 进入 AttentionEngine，后续 tick 更新内心/情绪/`speak_intent`
5. 旧 emotion worker 不再生产调度：用户情绪与自己的心情由 AttentionEngine 的同一次 LLM 输出 dual-write

**重要 invariant**: assistant reply **不进 memory**。Miru 的回复是从已有 memory 推理来的，喂回去会 guesses → facts 反馈循环。memory 只接受真实输入信号（user chat / screenshot / identity cascade）。

### 已知约束 / 优化空间

| 项 | 现状 | 影响 |
|---|---|---|
| 历史 chat 长度 | 硬编码 `limit=20` | 长对话 + 图片重的 batch 可能 token 爆——需要自适应裁剪 |
| `max_iterations=8` | 硬编码 | 大模型偶尔在 tool loop 内空转——应观察平均 iteration 数 |
| `_QUIET_PERIOD = 1.5s` | 硬编码 | 不同用户打字速度差异大——可按 batch 历史平均间隔自适应 |
| `look_at_screen` | 仅 desktop sensor 有效 | VPS 上调用直接 fallback 提示（手机/web 用户无影响） |
| context_text 总长 | 各分项独立截断,合并无监控 | 极端场景所有项接近上限可能 50KB+ |

---

## 多用户隔离架构

**Miru 是生产级多用户系统**。

### 邀请码 — 包含 VPS 信息的客户端编址方案

格式 `MIRU-<10 char server>-<6 char user>` (16 char body)，例 `MIRU-MCRVN4MQ8B-9F8NZ7`：

```
原始 6 字节 (IPv4 4B + port 2B)
    ↓ XOR mask `9c4ab73e51f8` (auth.py:_OBFUSCATION_MASK)
混淆后 6 字节
    ↓ base31 编码
10 字符 server 段
```

`auth.encode_server(ip, port)` / `decode_server(s)` 一对函数。客户端登录时解码 server 段拿到 `IP:port`，直连 `/api/auth/login` 验证 user 段。

**私有服务器发行版 v1 冻结**：长邀请码继续沿用这套规则。`server` 段只编码公网 IPv4 + port；scheme 第一版固定解释为 `http`；每台 Miru 私有服务器默认单用户。官方自动开服和自部署 Docker 都生成同一种长邀请码，Mac、Windows、Android 客户端不需要知道来源。不做短码、不做 resolver、不依赖域名/Cloudflare；生成长码时不要把域名传给 `SERVER_IP`。

**当前生产兼容说明**：现有 `mirulife.top` 时代曾引入 `discovery.resolve_server_url()` 作为已发布客户端的后端迁移兜底。现在这个模块只保留为 legacy fallback shim：有本地缓存就用缓存，没有缓存就回到旧 hosted URL，不再访问任何真实 discovery endpoint。私有服务器发行版客户端登录必须尊重长邀请码里的 server 段：输入服务器 A 的码就连接服务器 A，而不是默认转发到 `mirulife.top` 或 discovery。

### 认证 + 数据隔离

- **登录流程**：`POST /api/auth/login` → 验证邀请码 → 返回 token → 建立 `data/users/<user_id>/`
- **Flask 中间件**：`auth.check_request` @before_request 设置 `g.user_id` + `g.user_data_dir`
- **存储层**：`storage.get_data_dir()` 从 Flask `g` 读取，所有读写自动按用户隔离
- **后台线程**：`_push_user_context()` 在独立线程中恢复 Flask 上下文，确保 AttentionEngine / Sleep Agent / Memory Router 跨线程访问正确的用户数据
- **Token rotate / suspend**：admin 端可旋转 token（旋转后该用户所有设备被踢回 /login）

### Per-user 实例

所有状态模块都改为 `_instances: dict[str, ...]` 按 user_id 索引的 per-user 单例，`suspend` / `delete_user` 时显式清理：

- `attention_engine.AttentionEngine._instances`
- `care_engine.CareEngine._instances`（legacy 清理）
- `screen_analyzer.ScreenAnalyzer._instances`
- `sleep_agent.SleepAgent._instances`
- `character.CharacterConfig._instances`（支持模型切换）
- `self_profile`、`miru_emotion`、`sse` 广播

### Admin 管理

独立 SPA `templates/admin.html`（不依赖主前端 bundle，使用独立 localStorage `miru_admin_token`）+ blueprint `admin_api.py`（不和 user pipeline 共享 import 路径，零循环依赖）。

访问入口：`https://mirulife.top/admin` → 输入 admin token（`data/auth.json`，`secrets.token_urlsafe(32)` 生成，可在 admin UI 内一键 rotate）。

#### 4 个 Tab 功能

**用户 Tab**（`renderUsers`）：
- 列表：UID / 邀请码（完整码 `MIRU-<server>-<user>` 16 字符,点击复制） / 标签（admin 自定义 `assigned_to`） / 状态（active/suspended） / 创建时间
- 行动：详情 / 停服 (suspend) / 恢复 (activate) / **删除 (DELETE)** — 删除有 4 层防护：
  1. uid 非空 ≠ `_admin`
  2. body 必须含 `confirm` = uid 后 6 字符（防误手）
  3. user 必须存在于 users.json
  4. realpath 必须落在 `data/users/` 下（防路径穿越）
- 详情视图：聚合 stats + 最近活跃时间 + 邀请码完整信息 + 数据目录大小

**邀请码 Tab**（`renderInvitations`）：
- 列表：完整码（点击复制，绿色高亮反馈 1.2s 复用 `data-copy` + `copyToClipboard` clipboard.writeText fallback execCommand） / 标签 (`assigned_to`，admin 私有备注，⚠️ **绝不暴露给用户端**) / 状态徽章（unused / used / revoked 三色） / 使用者 UID / 创建/使用时间
- 本地 6 位 invite code 只作为服务器内部 key / audit 信息保留；私有服务器发行版对用户展示和客户端输入都使用完整长码 `MIRU-<server10>-<user6>`
- 行动：
  - **生成新邀请码** — 弹框含「数量 1-100」「标签 (admin 私有)」「服务器 IP」「端口」 → 提交后**第二弹框列出所有完整码** + 每条独立"复制"按钮（一次分发多人也方便）
  - **编辑标签** — 列表内联 ✎ 按钮 → PATCH `/invitations/<code>` 修改 `assigned_to`（留空则清除）
  - **删除** — 仅未使用且未撤销的码可 DELETE。已使用的码 409 拒绝，必须通过"删用户"间接 revoke

**AI 配置 Tab**（`renderAiConfig`）：
- 3 层 tier 各自独立 host / api_key / model / max_tokens
- 实时 ping 测试按钮（直接打 LLM 端点，验证 key + model + endpoint 三件套）
- API key 默认 mask `sk-X9qB……j5Yr`，一键 reveal / clear
- 修改保存即热加载（无需重启 miru.service）

**统计 Tab**（`renderStats`）：
- 全局聚合：总用户数 / 24h 活跃 / 7d 新注册 / 截屏量 / chat 量 / 数据目录总大小
- 60 秒缓存（`admin_stats.get_stats(force_refresh=False)`，`?force=1` 绕过）

#### 后端 API（`admin_api.py`）

```
GET    /api/admin/users              → 用户列表 + token_masked + invitation_full_code + assigned_to
GET    /api/admin/users/<uid>        → 单用户详情（聚合 stats）
PATCH  /api/admin/users/<uid>        → 改 status（suspended 时立即 evict 所有 per-user 实例 + 踢 SSE）
DELETE /api/admin/users/<uid>        → hard-delete（4 层防护，见 auth.delete_user 完整级联，下文）

GET    /api/admin/invitations        → 列表，每个码附 full_code（admin 端拼接, 不入库）+ assigned_to
POST   /api/admin/invitations        → 生成 N 条码，body: {count, ip?, port?, assigned_to?}
PATCH  /api/admin/invitations/<code> → 修改 assigned_to 标签（仅 admin 可见）
DELETE /api/admin/invitations/<code> → 删码（仅未使用且未撤销）

GET    /api/admin/stats              → 全局聚合，60s 缓存
GET    /api/admin/ai-config          → 3-tier 当前配置（key 已 mask）
PATCH  /api/admin/ai-config/<tier>   → 改 tier 配置 + 热加载
POST   /api/admin/ai-config/<tier>/test → 实时 ping
POST   /api/admin/admin-token/rotate → 轮换 admin token（旧 token 立即失效）
```

#### 删用户的级联（auth.delete_user）

DELETE 用户不仅清数据，还必须保证“删不干净就不能假装成功”。2026-05-18 起删除改为两阶段严格删除：

```
1. path realpath 校验: data/users/<uid> 必须仍在 data/users/ 下
2. users.json 先把 status 改为 deleting, 让 token 立即失效, 但暂不移除 metadata
3. _cleanup_user_singletons(uid) + sse.disconnect_user(uid), 停止后台线程和在线客户端
4. 严格 shutil.rmtree data/users/<user_id>/:
   - 不使用 ignore_errors=True
   - 删除失败或目录仍存在 → users.json 保留 status=delete_failed + delete_error
   - 此时 invitation 仍然绑定原用户, 不会被新用户复用
5. 文件确认删干净后, 才删 users.json 里的 user_id 条目
6. 改 invitations.json 对应码:
     used_by = None, used_at = None
     revoked = true, revoked_at = <now>, revoked_reason = "admin_deleted"
   (码本身不删, 保留审计链; 防该码被复用)
7. 写 audit log → data/_admin/deletions.json (永不删除, 留 invitation_code + 时间戳 + reason)
```

`auth.login_with_code()` 也会拒绝复用任何已经存在的 `data/users/<uid>` 目录，即使这个目录没有对应 users.json metadata。每个正常用户目录都会写 `account_manifest.json`，把目录与 user_id / invitation_code / created_at 绑定起来。

运行态清理覆盖以下 in-memory 缓存:

```
   miru_emotion._instances / attention_engine._instances / care_engine._instances /
   screen_analyzer._instances / sleep_agent._instances /
   character._configs / model_library._instances
```

**特点**：不需要 `systemctl restart`，所有 cleanup 是热的。客户端被踢后，DMG 通过 `auth.py:client-mode token 直通` 检测到 token 失效，自动触发 `_pet_ready_event` 状态机重置桌宠。

#### 邀请码 schema

```json
"MIRU-Z8837V": {                    ← key 是 short code, server 段不入库 (避免 VPS 迁移时全失效)
  "created_by":      "admin",
  "created_at":      "2026-05-07T15:20:35",
  "used_by":         null | "u_xxxxx",
  "used_at":         null | "ISO ts",
  "assigned_to":     null | "给小明",  ← admin 私有标签, never exposed to user
  "revoked":         (optional) true,  ← 用户被删时 set
  "revoked_at":      ...,
  "revoked_reason":  "admin_deleted" | "user_self_delete"
}
```

完整码（16 字符 `MIRU-<server10>-<user6>`）由 admin API / 私有服务器初始化流程实时拼接：`encode_server(ip, port)` 取自真实公网 `SERVER_IP` 和 `SERVER_PORT`，按需可在生成时 override。私有服务器发行版客户端只把完整长码作为用户输入形态：含 server 段，解码后直连该 server；不要把缺 server 段的短码作为公开登录形态，也不要让它走 Discovery fallback。

#### CLI 兜底（`tools/invite_cli.py`）

无 admin Web UI 时也可用：
```bash
python -m tools.invite_cli new --count 5 [--ip <vps_ip>] [--port 5001]
python -m tools.invite_cli list
python -m tools.invite_cli users
python -m tools.invite_cli suspend <user_id>
python -m tools.invite_cli activate <user_id>
```

#### 数据备份策略

每次大型清理（清空所有用户、删邀请码批量等）前自动 tar 到 `/opt/miru/data/_backups/<scope>_<timestamp>.tar.gz`。30 天滚动保留。普通 admin 操作（单用户删除）不备份（deletions.json audit 已足够追溯）。

### VPS 持续 monitor

`miru-router-monitor.service` systemd unit 持续抓 router 决策日志摘要 → 排查 Slot Writer / Pass 4 retry 耗尽 / Curator 误删等异常 (`SlotWriterV3` / `ScreenSlotWriterV3` / `Pass4Append` / `LegacySlotMerge` / `Curator*` log 前缀)。

---

## 多设备架构

一个中央后端承载所有智能（记忆、情绪、LLM），多台设备作为感官输入和消息输出。

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Central Backend (Flask)                           │
│                                                                      │
│   Auth (多用户) → g.user_id → 每用户独立的：                         │
│   ScreenAnalyzer / Memory Router / AttentionEngine / Sleep Agent     │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐   │
│   │ Device Manager — /api/device/{register|heartbeat|screenshot}│   │
│   └──────────────────────────────────────────────────────────────┘   │
│                                │                                     │
│                    ┌───────────┼───────────┐                         │
│                    v           v           v                         │
│           ScreenAnalyzer   Memory Router  AttentionEngine             │
│                (VLM)        (Slot Writer + (inner state +             │
│                              Pass 4)      speak_intent)               │
│                    │                         │                       │
│                    └──────→ Agent Loop ←─────┘                        │
│                               (core.py)                              │
│                                │                                     │
│                          SSE Broadcast                               │
│                    (per-user 通道 → 所有在线设备)                     │
└──────────┬─────────────────────┬─────────────────────┬──────────────┘
           │                     │                     │
   ┌───────┴──────┐     ┌────────┴─────────┐   ┌───────┴──────┐
   │ Mac / Win    │     │  Android APK      │   │  Web App     │
   │ desktop      │     │  Capacitor +      │   │  浏览器 PWA   │
   │ sensor + pet │     │  MediaProjection  │   │               │
   │ + Live2D     │     │  + Live2D 壁纸    │   │               │
   └──────────────┘     └──────────────────┘   └──────────────┘
```

### 客户端形态与后端位置

| 模式 | 后端位置 | 前端 | 适用 |
|:-----|:--------|:-----|:-----|
| **Web 直连 VPS** | VPS | 浏览器打开 `https://mirulife.top` | 无客户端安装 |
| **Mac DMG** | 本机或 VPS | `Miru.app` WebView | 完整本地模式，或服务器模式 + 桌宠 + 本机截屏 |
| **Windows installer**（x64） | 本机或 VPS | Windows WebView2 | 完整本地模式，或服务器模式 + 桌宠 + 本机截屏 + VPS 部署向导 |
| **APK 客户端**（Android） | 本机或 VPS | Capacitor WebView + app-private Python runtime，或直连 VPS | 完整本地模式、服务器模式、手机 SSH 部署、锁屏壁纸 + MediaProjection |

服务器模式下三个安装客户端都是瘦壳，AI 与记忆链路只在 VPS；本地模式下 Mac、Windows 和 Android 都在本设备运行完整后端。设备级截屏、桌宠/壁纸和 capability 设置不得通过 VPS 在 Mac、Windows、Android 之间互相覆盖。

### Mac 客户端模式（DMG）

`miru_launcher.py` 是 PyInstaller 打包的极薄入口：

1. 读 `~/Library/Application Support/Miru/config.json`（`server_url` + `auth_token`）
2. 启动本地 Flask（client mode）— 仅服务 `/login`、`/api/auth/login`（VPS 转发）、`/api/client-config`、`/api/client/sensor/*`、`/api/pet/*`、`/pet`
3. 启动 WKWebView：
   - 已登录 → 直接加载 `<server_url>/app?token=<token>`（**直连 VPS**）
   - 未登录 → 加载本地 `/login` 页（提交后 `/api/auth/login` 转发到 VPS，得到 token + server_url，再 `location.href = <server_url>/app?token=...`）
4. 启动 Tauri 桌宠子进程（`miru-pet` 二进制 + `pet.html`，Live2D 渲染 + SSE 监听情绪）
5. NSApp 主菜单 + Dock 点击恢复窗口；关闭主窗口与 Quit 都完整停止主壳、桌宠、sensor 和本地后端

关键点：**WebView 一旦登录就直连 VPS，所有 chat / memory / SSE 都同源到 VPS**，本地 Flask 不再代理这些路由。这避免了 client mode 试图调本地 LLM（无 key）的死路。本地 Flask 只剩需要 macOS 原生能力的端点（截屏权限引导、桌宠 IPC、设备注册）。

### Windows Desktop v1

Windows x64 版沿用同一前端、Python 业务模块、VPS 协议和 Tauri Live2D 桌宠，只新增平台 launcher、WebView2、Windows capture adapter、Native bridge、进程生命周期与安装/清理脚本。它必须同时支持完整本地模式、连接已有服务器和从 Windows 部署 VPS，不允许以“先做瘦壳”为由删减 AttentionEngine、Curator、Sleep、Memory Writer、Pass 4、Persona Writer 或现有阈值/筛选。完整实现与真实验收矩阵见 [`WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md`](./WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md)。

2026-08-25 Internal RC 已在 Windows 11 x64 真机完成上述三条产品路径：干净本地模式、已有服务器登录、Windows 通过 SSH + Docker archive 部署隔离 Linux host。主窗口使用 WebView2，本地状态写入 `%LOCALAPPDATA%\Miru`，截屏跟随鼠标所在显示器，桌宠快捷键为 `Ctrl+Alt+M`；关闭主窗口完整退出本机进程，最小化继续运行。该状态仍不是公开发布完成：真实公网测试 VPS、Mac + Windows + Android 同一服务器联合矩阵、SmartScreen 和开源许可证/资产审计仍是发布门槛。

### Android APK（`miru-mobile/`）

Capacitor 壳 + Java 原生组件：

| 组件 | 职责 |
|:-----|:-----|
| `MainActivity.java` | Capacitor Bridge + JS Bridge（`window.MiruAndroid`）：profile 切换、截屏控制、版本与本地 runtime 查询 |
| `MiruPythonRuntime.java` | Chaquopy app-private Python/Flask 本地后端、动态 loopback URL 和进程重建恢复 |
| `MiruProfileStore.java` | 稳定 install device ID + mode/server/user profile 的设备设置隔离 |
| `ScreenCaptureService.java` | Foreground Service + MediaProjection + JPEG 压缩 + POST |
| `MiruConnectionService.java` | 远端 SSE/通知连接和系统 lifecycle timeout 边界 |
| `NotificationPollWorker.java` | WorkManager 15min 轮询离线通知 |
| `WallpaperService`（新） | Live2D 锁屏壁纸，情绪联动 |

特性：
- 屏幕关闭自动暂停截屏；低电量（<15%）暂停
- 自适应间隔：连续无变化 30s→60s→120s→300s，检测到变化恢复 30s
- 厂商后台保活引导（Vivo/OPPO/Xiaomi/Huawei/Samsung）
- MediaProjection 进程重启后自动引导重新授权
- 本地单设备、手动输入邀请码连接和 SSH 创建多设备 Miru 三入口
- 手机本地完整后端与服务器 owned 后台互斥，切换 profile 先停旧 SSE/截图/token/runtime

### Live2D 资产

统一目录 `assets/live2d/Hiyori/`，DMG（`miru.spec`）和 APK（Android assets）共享。用户可从 `model_library.json` 切换模型包，切换后 `character.py` 热重载对应 `soul.md`。

---

## AI 3-Tier 调度（2026-05 重构）

历史上 Miru 的 LLM 调用混用 anthropic SDK + OpenAI SDK + 多模型 router。重构后统一为 **OpenAI 兼容单协议** + **3 个调用层**，admin 在 Web UI 全局配置：

| Tier | 用途 | 当前生产配置 | 特性 |
|:-----|:-----|:------------|:-----|
| `vision` | 截屏 VLM 分析（screen_analyzer / 显著性评分 / 提醒图片） | 由 `data/_admin/ai_config.json` 决定 (admin UI 热切, 当前生产: qwen3.5-9b @ openrouter) | 多模态，便宜模型，频次高 |
| `chat` | 主对话 / Sleep Agent Slot Writer / Persona Writer / Curator Executor / DDL 或项目状态变化截图 ScreenSlot Writer / agent 工具调用 | 由 admin UI 配置 (当前生产: deepseek-v4-pro) | 强能力，复杂写入和用户可见回复才默认考虑 reasoning |
| `memory` | 主 agent 记忆预召回 / AttentionEngine / ScreenSemanticGate / 日记生成 / 普通 `sig≥3` 截图 ScreenSlot Writer / Pass4Append / Curator Planner / 其他轻量文本任务 | 由 admin UI 配置 (当前生产: deepseek-v4-flash) | 文本为主，长上下文，低延迟，承担高频低风险任务 |

`_DEFAULT_MAX_TOKENS` 定义在 `ai_config.py`：vision=50000 / chat=50000 / memory=65536。admin / owner 配置覆盖默认值。

**单一协议**：`prompt._call_llm_json(...)` 接受 `tier` 参数，所有调用点显式传 tier，路由到对应 OpenAI client。Miru 依赖 OpenAI-compatible Chat Completions，而不是厂商原生 API。服务地址可以填裸 host 或完整兼容 Base URL：普通 provider 自动补 `/v1`，OpenRouter shortcut 自动映射到 `/api/v1`，Gemini shortcut 自动映射到 `/v1beta/openai`，Azure / Foundry 类 endpoint 可填 `.../openai` 或 `.../openai/v1`。Anthropic/Gemini/Azure 等只有在提供 OpenAI 兼容层时才属于这个协议范围。

**配置存储**：`data/_admin/ai_config.json`（admin-only）。Per-user override 已删除（所有用户共享后端 admin 的配置）。环境变量兜底（`AI_VISION_HOST` / `AI_CHAT_HOST` / `AI_MEMORY_HOST` 等）首次启动用。

**热重载**：`PATCH /api/admin/ai-config/<tier>` 写文件 + `invalidate_cache()`，下一次 LLM 调用拿到新 key，无需重启 Flask。

**端到端 ping**：`POST /api/admin/ai-config/<tier>/test` 真实调一次 LLM，返回 `elapsed_ms` + 成功/失败，admin UI 显示。`vision` tier 的 ping 会发送 1x1 图片，避免文本模型被误判成可用 VLM。

### API 调用与成本口径

所有 OpenAI-compatible LLM 调用都应写入 `data/_admin/llm_usage.jsonl`，一行一笔，字段包括 `tier` / `model` / `prompt_tokens` / `completion_tokens` / `total_tokens` / `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` / `reasoning_tokens` / `call_label` / `reasoning_enabled` / `max_tokens` / `user_id`。DeepSeek 的真实费用要看 cache hit/miss 和 thinking 输出；只看 total tokens 会低估或高估。关键入口必须用功能级 `call_label`（如 `ScreenObservationVLM` / `ChatPreflight` / `AttentionEngineEvaluate` / `Pass4Append:screenshot` / `CuratorExecuteMerge`），不要让报表退化成大块 `llm_text`。

`scripts/llm_usage_report.py --since 1d --by-user` 是运营成本主入口。它汇总 tier、model、user×tier，并在新日志存在时显示 cache hit / miss / reasoning tokens。OpenRouter 控制台只覆盖 `vision`；DeepSeek 控制台覆盖 `chat` + `memory`，所以"截图多但 OpenRouter 便宜、DeepSeek 贵"通常意味着截图后触发了 ScreenSlotWriter / Pass 4 / Curator，而不是图片识别本身贵。

| 调用入口 | 默认 tier | reasoning | 触发频率 | 性价比策略 |
|:--|:--|:--|:--|:--|
| `screen_analyzer.call_screen_observation` | `vision` | off | 每设备 VLM cooldown 后的变化截图 | 保持便宜 VLM；截图日志数量不等于 VLM 调用数量 |
| `vision.describe_image` / 聊天图片预处理 | `vision` | off | 第一版禁用手动聊天发图；仅未来实验/旧历史兼容 | 若恢复图片消息，必须先转文字描述，后续 chat/memory 只看文本 |
| `call_chat_preflight` 主对话记忆预召回 | `memory` | off | 每轮用户 batch | 只选择 files/keywords 并预加载长期记忆；不再做主 agent 模型路由 |
| `call_chat_agent` reactive main agent | `chat` | off | 默认主对话，最多 8 tool iterations | 用户主动消息全部固定 pro；工具可用、质量稳定 |
| AttentionEngine `_evaluate` | `memory` | off | salience 合并 tick + heartbeat | 高频读空气；输出 inner/user_affect/self_emotion 的 continue/shift 状态段与 speak_intent |
| Proactive delivery main agent | `chat` | off | AttentionEngine 输出 `speak_intent` 后 | 主动消息全部固定 pro；只读工具可用；不再调用 Proactive resource planner |
| Sleep Agent Slot Writer | `chat` | off | 对话 debounce 3min 或 12 条 batch | 写入长期记忆，结构化输出为主，不再默认烧 reasoning tokens |
| Persona Writer | `chat` | off | 每 4 个 Sleep Agent batch | 改 core memory，先保持强模型但不开 reasoning |
| Curator Planner | `chat` | off | domain counter 达阈值，normal trigger 最多每小时一次 | 只提出 ops，漏掉可下次再整理；真正修改交给 Executor |
| Curator Executors | `chat` | off | Planner 输出 merge/edit/delete 后 | 会真实改/删/合并 slot，保留 chat tier 但不开 reasoning；质量用 executor self-check 和测试兜底 |
| ScreenSemanticGate | `memory` | off | `sig≥3` 截图进入 ScreenSlotWriter 前 | 判断当前 observation 是否有新的长期记忆价值；输出 bool，重复/低价值截图直接跳过 writer |
| ScreenSlotWriter 普通 `sig≥3` | `memory` | off | 无明确 DDL / 项目状态变化的普通截图 | 降成本；只做保守记忆/DDL 判断 |
| ScreenSlotWriter 强信号 | `chat` | on | 明确 DDL / 项目状态变化 | 避免漏掉承诺和重大进展；`sig≥4` 本身不再自动触发 Pro |
| Pass4Append | `memory` | off | chat / screenshot 的 `slot_write.kind=match` | 只输出一条 append_entry + metadata，router 带时间戳追加 body，并标记 dirty slot |
| SlotDailyCompactor | `memory` 或 `chat` | off | 每日整理 dirty slots | 把当天追加条目融入流畅正文；超过 1000 字再压缩 |
| 情绪更新 / 日记 | `memory` | off | Attention tick、夜间日记 | 用户情绪和 Miru 情绪已并入 AttentionEngine；结构化轻任务，用 flash 足够 |

**当前最优成本方向**:
1. 先看 `prompt_cache_miss_tokens` 和 `reasoning_tokens`，不要只看调用次数。
2. 高频、低风险、可失败静默跳过的后台任务放 `memory` tier。
3. 会直接影响用户可见回复、core memory、承诺、slot 重写的任务才用 `chat` tier。
4. 截图链路不按"截图数量"付费，而按"变化 → VLM cooldown → significance → 写入策略"逐级过滤。
5. 所有新 LLM helper 必须接入 `_log_llm_usage()`；否则 DeepSeek/OpenRouter 控制台和本地账本会再次分叉。

---

## 前端页面结构

| 路由 | 模板 | 职责 |
|:-----|:-----|:-----|
| `GET /` | `landing.html` | 公开落地页（初次见面、产品介绍） |
| `GET /login` | `login.html` | 邀请码输入 + token 交换 |
| `GET /app` | `index.html` | 主应用 SPA（登录后） |
| `GET /admin` | `admin.html` | 独立 admin SPA（用户 / 邀请码 / AI 配置 / 统计） |
| `GET /pet` | `pet.html` | 桌宠 WebView（Tauri 嵌入） |
| `GET /privacy` | `privacy.html` | 隐私声明（公开） |

主应用侧边栏：💬 对话、🧠 记忆 hub（含 项目/人物/话题/自己 4 tab + DDL + 日记 + 全部文件兜底）、🎨 情绪日历、👁 Airi 观测、👤 设置。

**聊天界面时间显示 (2026-05-16 微信式 `chat-time-anchor`)**:
不分 user / Miru / reminder, 全部走同一个 grouper (`_appendChatTimeMarkers` in `templates/index.html`):
- **跨日** → 插入 `chat-time-separator` (例如 `── 2026-05-16 ──`, 带横线)
- **距上条消息 ≥ 5min** → 插入 `chat-time-anchor` (例如 `14:23`, 居中无横线)
- 同一组 5min 内的多条消息只显示**组首**的时间锚, 跟微信完全一致
- 三条 bubble 路径 (历史加载 / 实时 SSE / 用户即时发送) 共用一个 timeline ctx (`_chatTimeline`), reset 由 `resetCompanionChatHistoryDom` 统一管理

---

## 系统运行全景：Miru 的一天

> 以下不假设固定作息时间。所有行为由信号驱动，时间点仅为示意。

### 用户恢复活跃（offline → active）

| # | 触发 | 系统 | 做了什么 |
|---|------|------|---------|
| 1 | 首次截屏/聊天信号 | **Activity Inference** | 检测到 offline → active 转移 |
| 2 | 状态转移 | **AttentionEngine** | 注入丰富上下文（离线时长、今日对话、承诺、Miru 心情、human/persona）→ LLM 更新内心；如果输出 `speak_intent` 就直接进入 proactive main agent |

### 全天持续（active 状态）

| # | 间隔 | 系统 | 做了什么 |
|---|------|------|---------|
| 3 | 默认 30s（用户可调，最低 5s） | **Sensor 变化检测** | 各设备截屏缩略图 → 像素差 ≥30% (`CHANGE_THRESHOLD=0.30`) 才 POST |
| 4 | per-device VLM cooldown（默认 10s，被 user_settings 覆盖到 sensor 间隔） | **ScreenAnalyzer** | VLM 观测 + 显著性评分 → Jaccard 去重 → 写原始 `screenshot_log.json` → `sig≥2` 推 Attention；`sig≥3` 先过 ScreenSemanticGate，只有 `should_continue=true` 才 fork ScreenSlotWriter (普通截图低成本, DDL/项目状态变化升 chat reasoning; 写 slot + 抽 DDL) |
| 5 | salience 合并 tick | **AttentionEngine 评估** | memory tier/no reasoning 输出 inner/user_affect/self_emotion 的 continue/shift 状态段 + speak_intent；continue 只延长段，shift 才新增日志 |
| 6 | 持续 | **时间感 / 等待感** | heartbeat 只更新内心和等待感，不为了找话说；用户可能在忙是 prompt 的一等上下文 |

### 用户发消息时

| # | 触发 | 系统 | 做了什么 |
|---|------|------|---------|
| 7 | 收到消息 | **状态机** | 存 chat_history + timeline → 入队 Sleep Agent → 启动 1.5s 安静定时器 → AttentionEngine 收 `chat_in` strong signal |
| 8 | 1.5s 静默 | **对话生成** | 合并批量 → 上下文（soul + core memory + slot 卡片预检索 + Miru 情绪 + 20 条聊天 + 屏幕观测）→ LLM Agent（4 工具）→ 回复 → SSE 广播 |
| 9 | 回复后 | **Sleep Agent v3 入队** | 3min 静默 / 12 条强制 → Slot Writer 1 个 LLM 调用输出 `{slot_writes[], completed_commitments}` → 每个 slot_write 走 `route_with_slot_write` → `kind=new` 直写, `kind=match` 触发 Pass 4 AppendEditor 追加记录 → 累计 4 batch 触发 Persona Writer 更新 core_memory |
| 10 | 回复后 | **AttentionEngine** | 收 `chat_out` strong signal，下一次 tick 同时更新用户情绪与 Miru 内心；旧 emotion 线程不再生产调度 |
| 11 | 后续 tick | **AttentionEngine 内心连续性** | LLM 看到「刚才聊天时/聊天后发生了什么」写 thought 与 speak_intent，但不追加消息 |

### 用户超过典型末次活跃时间（late_active）

| # | 触发 | 系统 | 做了什么 |
|---|------|------|---------|
| 13 | 超过 typical_window.last | **AttentionEngine** | LLM 看到"已超过典型末次活跃时间 X 分钟"，更新 Miru 的担心/等待感；不走固定晚安/催睡模板 |

### 用户离线（active/idle → offline）

| # | 触发 | 系统 | 做了什么 |
|---|------|------|---------|
| 14 | 所有设备静默 | **Activity Inference + AttentionEngine** | 状态转为 offline；Attention heartbeat 只更新等待感，不发消息 |

### 定时维护（23:25-23:35）

| # | 时间 | 系统 | 做了什么 |
|---|------|------|---------|
| 15 | 23:25-23:35 | **Nightly maintenance** | 聊天归档（滚动月份）、index.md 重建、pattern 更新、SlotDailyCompactor 整理当天 append-only dirty slots、承诺清理、截屏日志滚动、**daily_slot_audit**（active→paused→archived）。**不发送消息**——如果用户此时在线，AttentionEngine 只形成内心 |
| 16 | 23:45 | **日记生成** | `journal.py` 调 memory tier → 写 JSON + markdown 双格式；空白日跳过 LLM 直接用模板 |

---

## 数据产品

### 情绪日历

`/api/emotion/month/<month>` → 前端月历热力图，每日 valence 平均值染色。点进某天看详细条目 + source 标签。

### DDL 追踪器

整合在「记忆 hub」里，前端日历视图（月/列表切换），颜色编码进行中/临近/逾期/完成。`core.parse_commitments()` 计算 urgency（overdue / imminent≤2h / today / approaching≤6h / soon≤3d / normal），支持精确到分钟的 deadline（`YYYY-MM-DD HH:MM`），向后兼容纯日期。AttentionEngine 读取活跃承诺作为 `speak_intent` 的上下文；是否提醒由 AttentionEngine 自己判断。

#### Commitment 数据格式

`commitments/active.md` 是一个 markdown 文件，每行一条承诺：

```
- [ ] 周五前交设计稿 (deadline: 2026-05-09 18:00) -- 客户 review 用  [added: 2026-05-07 14:30]
- [ ] 准备面试 (deadline: 2026-05-10) -- 字节跳动 P7 后端  [added: 2026-05-06 22:15]
```

完成后由 `complete_commitment` 工具、sleep agent 或 ScreenSlot Writer 的 completed_commitments 检测移到 `commitments/done.md`，保留 `[done: ...]` 时间戳。`core.parse_commitments(include_done=False)` 从 active.md 解析返回 list[dict]，AttentionEngine snapshot 直接消费。

#### 真实写路径（4 条，全部经过 `add_commitment.py` 工具或 `core` 模块）

| # | 触发源 | 路径 | 备注 |
|:-:|:------|:----|:-----|
| 1 | 用户对话 → 主 agent 推理 | `tools/add_commitment.py:execute()` → 写 `active.md` 一行 | **唯一从对话产生 DDL 的路径**，主 agent 自主判断"这是真的目标"才调用 |
| 2 | Web UI 手动添加 | `core.add_commitment_manual()` → `active.md` | "记忆 hub → DDL → +" 按钮，用户直接录入 |
| 3 | ScreenSemanticGate + ScreenSlot Writer 截屏检测 | `core._process_screen_observation_async()` → Gate 通过后 `add_commitment_manual()` / `complete_commitment_by_title()` | 仅 `sig≥3`；普通截图低成本，明确 DDL / 项目状态变化才升强模型 |
| 4 | sleep agent / 主 agent 自动完成 | `tools/complete_commitment.py:execute()` 或 `complete_commitment_by_title()` | 用户说"面试结束了"或截图显示完成 → 归档 |

**所有路径写入后调用 `_broadcast_commitment_sync()` 通过 SSE 推所有设备**，前端日历实时刷新。

#### 截屏 → DDL 真相

`screen_analyzer.analyze()` 拿到 VLM 描述后做 3 件事：

```
screen_analyzer.analyze(image, device_id):
    obs = VLM(image)                                      # 自然语言观测
    ├── storage.append_screenshot_log(device_id)          # 仅存时间戳（30天滚动），不存内容
    ├── sig>=2 → attention_engine.record_signal("screenshot", obs)      # 内心/情绪/speak_intent
    └── sig>=3 → core._process_screen_observation_async(obs)
          ├── ScreenSemanticGate → screen_semantic_gate_log.json
          ├── false → stop
          └── true → ScreenSlot Writer → slot_writes + commitments + completed_commitments
                ├── route_with_slot_write → slots/<domain>/<slot_id>/main.md
                ├── add_commitment_manual() → commitments/active.md
                └── complete_commitment_by_title() → commitments/done.md
```

换句话说，截屏现在可以自动抽取 DDL，但只在 `sig≥3` 且 ScreenSemanticGate 放行后，经 ScreenSlot Writer 的结构化输出与 `add_commitment_manual()` dedup 落地。`sig=2` 绝不写 DDL，只影响 Attention。屏幕上随便出现一个日期不会直接进 `active.md`；必须被 VLM 认为有中等以上意义，并通过 Gate 与 ScreenSlot Writer 的判断。

#### 读路径

| 消费者 | 入口 | 用途 |
|:------|:----|:----|
| AttentionEngine snapshot | `core.parse_commitments(include_done=False)` | top commitments + 紧急度标签注入内心判断与 speak_intent |
| 主 agent 读取 | `tools/list_commitments.py` | 用户问"我有什么待办" 时 LLM 主动调用 |
| Web UI 日历 | `/api/commitments/list` | 月历 + 列表视图渲染 |
| Sleep agent 完成检测 | `parse_commitments(include_done=False)` 列表 → 投喂 v2 prompt | 让 LLM 看着活跃承诺列表判断"该划掉哪些" |

#### 已死代码（不要被骗）

代码库里残留少量"看起来在管 commitment"但**完全没有调用方**的文件：

| 文件 / 函数 | 状态 | 说明 |
|:-----------|:----|:----|
| `summarizer.py` | ✅ 已物理删除 (2026-05-08) | 早期 commitment gatekeeper + Miru emotion eval 钩子，被 sleep_agent v3 + 事件驱动 emotion eval 完全取代 |
| `screen_observer.py` | ✅ 已物理删除 (2026-05-16) | 早期独立的截屏-后处理线程，被 `screen_analyzer.py` 取代后 zombie 一段时间, 本轮清理删除 |
| `screen_sleep_agent.py` | ✅ 已物理删除 (2026-05-16) | 旧的"截屏 debounce + fragments → router" 中间层, 被 `core._process_screen_observation_async` 直跑 ScreenSlot Writer 取代 |
| `fact_resolver.py` / `fact_store.py` / `slot_revisions.py` | ✅ 已物理删除 | 早期事实流 + 修订日志架构, 被 v3 Slot Writer + Pass 4 单写入路径取代 |
| `memory_prompts.call_commitment_gatekeeper()` | 🪦 调用方随 summarizer 一起消失 | 当前没有人调，可以清理 |
| `sync_backend.py` (548 行) | ⚠ 仅 `scripts/sync_db.py` CLI 触发 | 实验性 PostgreSQL 多设备同步层, 没集成进 app.py 请求路径; 不影响生产 |

后续清理 `call_commitment_gatekeeper()` 不会影响任何运行路径。**proposal 描述以本节为准**。

### 日记 (book view)

`journal.py` 每天 23:45 调 LLM 写**结构化 JSON + markdown 双格式**：

- 加藤惠式叙事（200-400 字第一人称散文，禁用「今天你...」「这一天...」模板开头，禁用空洞鼓励）
- 字段：`title / mood {label,emoji,valence} / narrative / highlights / stats / generated_at / source_version / is_empty_day`
- **空白日**：用户当天没出现（chat / observations / commitments / activity / raw_md 全空）→ `_empty_day_journal()` 返回固定文案（"你没出现的周X / 我一直在这里 / 偶尔看看屏幕..."），不调 LLM
- **回填**：`scan_and_backfill(days=30, max_per_call=3)` 每次 list 调用最多补 1 天，避免长时间网络故障后日记断裂
- **Attention 边界**：日记不读取旧的无 `channel` raw attention tick，也不读 `attention_state/current_focus`、`attention_intent_queue`、`attention_delivery_log`；它只读取 `attention_log.json` 里 `channel="inner"` 的干净第一人称状态段，以及 `emotion_log.json` 的 `user_affect` segment、`miru_emotion.json` 的 `self_emotion` segment。截图 `sig=2` 不进入日记观测，`sig>=3` 才可能作为 screen observation 被回顾。
- **主动关心动机**：AttentionEngine 触发 proactive main agent 后，发送到 `chat_history` 的主动消息会附带 `care_motive / why_i_want_to_say / user_need / message_seed`。日记不读 intent queue，也能从 chat_history 里知道“我当时为什么想开口”。
- 前端 book view（翻页书籍视觉）+ 月历热力图 + 单日分享 PNG

### Memory hub

「记忆」侧边栏入口的统一视图：

| Tab | 内容 |
|:----|:-----|
| 项目 / 人物 / 话题 / 自己 | Memory v3 slot 卡片（icon + title + summary + last_active），点开看详情 |
| DDL | commitments/active.md + done.md，月历 + 列表 |
| 日记 | journal/ book view + 月历热力图 |
| 全部文件 | 兜底原始 markdown 视图（含 patterns/sleep.md 等遗留路径） |
| ⌘K 搜索 | Slot-aware 全文搜索（覆盖 slot 卡片 + 遗留 markdown） |

### 月度关系报告（Backlog）

每月 1 号自动生成"你和 Miru 的关系报告"。亲密度/信任度月曲线、最常聊 Top 5、双方情绪曲线对比。稀缺感：一月一次，仪式感强，天然适合社交分享（参考 Spotify Wrapped）。

---

## 功能边界

### 做

已实现的核心能力：

- ✅ 多设备屏幕感知 + VLM 分析 + 去重 + 显著性评分
- ✅ **Memory v3 + v3.5 + v3.7** — Slot 卡片 (projects/people/topics/self) + Slot Writer + Pass 4 AppendEditor + Curator v3.5 存量整理 + Daily Audit 状态转移
- ✅ Core Memory（human/persona, 由 Persona Writer 累计 4 batch 触发更新）+ 遗留模块 (commitments/patterns/journal) 直写
- ✅ **Sleep Agent v3** (chat 路径 debounce 3min batch → Slot Writer 输出 slot_writes + completed_commitments → Pass 4 AppendEditor 整合 + Persona Writer 累计触发)
- ✅ **ScreenSemanticGate + ScreenSlot Writer** (截屏路径 sig≥3 先做语义 gate，放行后每张截屏一次 writer LLM，同时抽 commitments + 完成承诺)
- ✅ **route_with_slot_write 单一写入入口** (替代旧 4-pass router) + Curator v3.5 (Planner + 3 self-checking Executors, LLM 调用 8→4)
- ✅ Slot-aware ⌘K 全文搜索 + 编辑/合并/归档/置顶 UI
- ✅ **AttentionEngine live delivery** — 完整 soul 注入 + salience 合并 tick + user_affect/Miru inner/speak_intent + fixed-pro proactive main-agent 发送
- ✅ **Sleep Inference**（活动状态推断，无硬编码时段）
- ✅ DDL 追踪器（精准时间 + 6 级 urgency + dedup tracking）
- ✅ Miru 情绪引擎（惯性 + 衰减 + 关系仪表盘）
- ✅ 用户情绪感知 + 共情反应 + 月历视图
- ✅ 日记 book view + 月历 + 加藤惠叙事 + 空白日特殊处理 + 自动回填
- ✅ **AI 3-tier 调度**（vision / chat / memory）+ admin 全局配置 + 热重载 + 端到端 ping
- ✅ **多用户隔离**（邀请码 + per-user 数据目录 + 所有模块 per-user 实例 + suspend evict）
- ✅ **邀请码 XOR mask 混淆**（base31 + 6-byte mask）
- ✅ **统一长邀请码直连**（`MIRU-<server10>-<user6>` 解码 IPv4+port；短码和 discovery fallback 已从新登录路径退场）
- ✅ SSE 实时广播（per-user 通道）+ 断线重连 + 心跳超时 + IndexedDB 缓存 + 乐观更新
- ✅ `/api/bootstrap` 合并首屏请求（消灭三个无效轮询）
- ✅ Android APK（截屏 + 通知 + 壁纸 + 版本检查 + Foreground Service + 原生 SSE 通道）
- ✅ Mac DMG（桌宠 + Live2D + 本机 sensor + WebView 直连 VPS + 登出/切换账号）
- ✅ Live2D 锁屏壁纸（Android WallpaperService + JNI 情绪桥接）
- ✅ Web Push 通知（VAPID）
- ✅ HTTPS + Let's Encrypt + SSH key 认证 + 二进制硬编码扫描
- ✅ 独立 admin SPA + AI 配置 tab + admin token 旋转 + invite_cli
- ✅ 模型库 + 多 Live2D 模型切换 + per-模型 soul.md
- ✅ 公开隐私声明；App 设置页已移除用户可见删号/清空危险入口，改为切换账号
- ✅ APK 自动更新通道（VPS latest.apk + 前端引导）
- ✅ VPS 持续 monitor (systemd unit)

规划中（聚焦产品**效果** + Windows Desktop v1 + 完整源码开源）：

- 📋 用户填的 bug 排查清单（#297）
- 📋 Memory v3 在生产中的实际表现观察（Slot Writer retry 失败率、Pass 4 append 质量、slot match/new 比例、Curator merge/delete 准确度）
- 📋 AttentionEngine live delivery 生产观察（主动消息是否足够像 Miru、是否太频繁/太保守、`speak_intent` → fixed-pro proactive main agent 的只读工具使用是否合适）
- 📋 Mac 自动更新（#189）
- **Windows Desktop v1（Internal RC）**：Windows x64 已实现完整本地模式、Windows 端 VPS 部署、已有服务器登录、桌宠与按鼠标显示器截屏，并保留完整后台链路；仍待真实公网 VPS、三设备、系统边界和公开发布审计。
- 📋 **完整源码开源准备**：公开仓库最终包含可构建、可修改、可自部署的完整源代码；发布前独立完成 secret/history scan、依赖许可证和 Live2D/模型/图标/字体/媒体资产再分发权审计，并在无 Internal 权限的环境中复现构建。
- 📋 **统一长邀请码寻址**：沿用当前 `MIRU-XXXXXXXXXX-YYYYYY` 规则，`XXXXXXXXXX` 编码 IPv4 + port，`YYYYYY` 是该服务器本地 6 位 invite；scheme 第一版固定 `http`，默认单用户。不做短码、不做 resolver、不依赖域名/Cloudflare；官方托管和自部署 Docker 生成同一种长码。
- 📋 **同源发布物与教程**：从同一 source commit 重打并验收 Mac DMG、Android APK、Windows installer 和 Linux amd64 Docker image，记录 manifest 与 digest；普通用户仍可用安装包/向导，技术用户可从公开源码构建和自部署。详细实施清单见 [`PRIVATE_SERVER_RELEASE_TODO.md`](./PRIVATE_SERVER_RELEASE_TODO.md) 和 [`WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md`](./WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md)。

架构对账后保留的清理项（不影响当前运行，但容易误导维护）：

- 🧹 `prompt.call_proactive_delivery_preflight()` 与 `prompt.call_proactive_resource_planner()` 仍作为 legacy helper 留在代码里；生产路径已由 direct delivery + fixed-pro proactive main agent 替代，后续应删掉旧 send/defer/drop / resource planner prompt 与相关测试断言。
- 🧹 `memory_router.trigger_route_async()` / `route_and_write()` 是 no-op 兼容壳；后续可改成更显眼的 fail-fast，避免新代码误以为旧路由还能写记忆。
- 🧹 `memory_prompts.py` 中旧 commitment gatekeeper / Miru emotion eval prompt 只为兼容测试与历史调试保留；生产情绪更新应继续只走 AttentionEngine。
- 🧹 `README.md`、`docs/README.md` 和若干旧 HTML 架构图仍有 A-F 卡片 / CareEngine 时代叙事，需要逐步更新或归档，避免和本文冲突。

不急做的框架性任务（暂缓）：

- ⏸ 邀请码 server 段改为 directory/service-id（#325）— 当前 XOR 混淆够用，v1 先坚持长码直连

### 不做

| 功能 | 原因 |
|:-----|:----|
| Coding Agent | 不服务五条原则 |
| Web 搜索 / 自主上网 | 定位模糊 |
| Live2D 模型商店 | 已改为用户自上传模型包 |
| 角色每日行程模拟 | 精力放在理解用户 |
| 多机器人编排 | 无关 |
| **对话中 Agent 自主维护记忆** | 分散对话注意力；已由 Sleep-time Agent + Memory Router 后台处理 |
| **周报系统** | 已砍（#246）— 月历 + 日记 + slot 卡片 已经覆盖周维度回顾，周报反而冗余 |

---

## 竞品差异化

| | Miru | Character.AI / Replika | Rewind / Screenpipe | Letta / MemGPT |
|:--|:-----|:----------------------|:-------------------|:---------------|
| 认识你？ | 多设备持续屏幕观察 + 后台整理 | 不认识 | 录了不理解 | 认识但无感知 |
| 记忆结构 | Core + **Memory v3 slot 卡片**（Slot Writer + Pass 4 AppendEditor + Curator v3.5 整理）+ DDL + 日记 | 浅层 / 无 | 全文 | 双层（我们借鉴） |
| 记忆维护 | **专职后台**（Memory Router + Sleep Agent + Daily Audit）| 系统 | 录制 | 对话中自维（噪声大） |
| 感知 | 多设备 + 显著性过滤 + 去重 | 仅对话 | 持续录制 | 仅对话 |
| 情感 | 独立情绪引擎 + 惯性 + 衰减 + 关系仪表盘 + 双向情绪 | 脚本扮演 | 无 | 无 |
| 主动 | **AttentionEngine speak_intent**（先形成内心，后续接主 agent）| 被动 | 被动 | 默认无 |
| 多用户 | 邀请码 (XOR 混淆) + per-user 隔离 + admin AI 配置 | 云多租户 | 单机 | 需开发 |
| 数据归属 | 自托管 VPS + 一键导出 + 自助删号 | 云厂锁定 | 本地 | 可自托管 |
| 角色系统 | `soul.md` + Live2D 模型库 + 关系阶段 | 固定卡 | 无 | 无 |
| 多设备 | **WebView 直连同源后端** + 桌面/手机/锁屏壁纸 | 云多端 | 仅桌面 | 仅 API |
| 后端切换 | **长邀请码寻址**（官方托管和自部署都用同一长码直连服务器） | N/A | N/A | N/A |
| 数据产品 | 情绪日历 + DDL + 日记 book view | 无 | 时间线搜索 | 无 |

**一句话差异：Letta 有最好的记忆架构但没有感知和情感；Screenpipe 有最好的感知但没有理解；Character.AI 有最好的角色扮演但没有记忆和情绪。Miru 把三者融合：多设备持续感知 × Memory v3 slot 后台路由 (Slot Writer + Pass 4 AppendEditor + Curator) × AttentionEngine 持续内心 × 角色人格 × 邀请码多用户 × 可切换后端 × 可分享数据产品。**

---

## 代码结构

```
ContextLife/
├── app.py                 Flask 服务入口，端口 5001，SSE，所有后台线程，client_mode 入口
├── core.py                业务逻辑中心：CSM 对话状态机 + 后置流程；聊天输入/输出投喂 AttentionEngine
├── prompt.py              LLM prompt 构建 + OpenAI 兼容 API 调用（接受 tier 参数）
├── memory_prompts.py      VLM 截屏观测 + 用户情绪标注 + Miru 情绪 eval prompt
├── memory_prompts_v2.py   Pass 4 AppendEditor prompt + Curator v3.5 Planner+Executor prompts
├── memory_prompts_v3.py   Slot Writer + ScreenSemanticGate + ScreenSlot Writer + Persona Writer prompt + schema
│
├── auth.py                多用户邀请码鉴权 + XOR mask 混淆 + Flask g 中间件
├── discovery.py           legacy URL resolver（私有服务器发行版新登录路径不使用）
├── storage.py             Per-user 数据存储（get_data_dir 读 Flask g）
├── sse.py                 Per-user SSE 广播
│
├── core_memory.py         Core Memory 模块（human/persona 注入）
├── memory.py              Archival Memory 模块（list_tree_rich 适配 slot）
├── memory_router.py       ★ slot 路由唯一入口 (route_with_slot_write) + Curator hook + 4 域并行调度
├── sleep_agent.py         Sleep Agent v3 (chat 路径 debounce 3min batch → Slot Writer / Persona Writer)
├── screen_analyzer.py     截屏路径入口 (VLM 观测 + 显著性 + Jaccard 去重 + fork Gate/ScreenSlot Writer)
├── curator.py             Curator v3.5 — Planner + 3 self-checking Executors (merge / edit / delete), per-user daemon
├── persona_writer_state.py Persona Writer 触发状态机 (每 4 batch + proactive outcomes 累计 → 更新 core_memory)
├── journal.py             每日 23:45 日记生成（JSON + markdown 双格式）
│
├── sleep_inference.py     多源活动状态推断（active/idle/offline）
├── screen_analyzer.py     屏幕分析器（per-user + hook screenshot_log）
├── sensor.py              桌面端 Sensor
├── device_manager.py      设备管理
├── attention_engine.py    ★ 持续内心 + user_affect + Miru inner + speak_intent + live delivery 触发
├── daily_patterns.py      每日 sleep/work patterns 维护（nightly maintenance）
├── care_engine.py         retired shim：仅保留 cleanup registry / update_daily_patterns re-export
├── miru_emotion.py        Miru 情绪引擎（per-user）
│
├── character.py           soul.md 解析（per-user 缓存）
├── model_library.py       Live2D 模型库 + per-模型 persona
├── self_profile.py        用户画像（per-user 数据）
├── ai_config.py           ★ 重写 — 3-tier (vision/chat/memory) admin 全局配置 + 热重载
│
├── admin_api.py           ★ Admin blueprint（用户/邀请码/AI 配置/统计）
├── admin_stats.py         统计聚合 + 缓存
│
├── miru_launcher.py       ★ Mac .app NSApp 事件循环 + WKWebView (直连 VPS)
├── build_mac.sh           Mac DMG 打包
├── miru.spec              PyInstaller spec
│
├── tools/
│   ├── archival_memory_search.py    对话 agent 工具
│   ├── look_at_screen.py
│   ├── add_commitment.py
│   ├── complete_commitment.py
│   └── invite_cli.py                邀请码管理 CLI
│
├── deploy/
│   ├── update_vps.sh               一键 VPS 部署（SSH key + rsync + systemctl）
│   └── clean_mac_miru.sh           Mac 端完整清缓存（验证 fresh install）
│
├── miru-mobile/                    Android Capacitor APK
│   └── android/app/src/main/java/com/miru/companion/
│       ├── MainActivity.java
│       ├── ScreenCaptureService.java
│       ├── MiruConnectionService.java   后台 VPS 连接 + 原生 SSE 通道
│       └── WallpaperService/...         Live2D 锁屏壁纸 (JNI + Cubism)
│
├── assets/live2d/                  共享 Live2D 资产（DMG + APK）
│
├── templates/
│   ├── landing.html                公开落地页
│   ├── login.html                  邀请码登录（接受 6 / 16 char body）
│   ├── index.html                  主 SPA
│   ├── admin.html                  独立 admin SPA
│   ├── pet.html                    桌宠 WebView
│   └── privacy.html                隐私声明
│
└── data/
    ├── _admin/
    │   ├── ai_config.json          ★ 全局 3-tier LLM 配置
    │   ├── invitations.json        邀请码表
    │   ├── users.json              用户列表
    │   ├── auth.json               admin token
    │   └── deletions.json          删号审计
    └── users/<user_id>/
        ├── core_memory.json
        ├── chat_history.json
        ├── timeline.json
        ├── miru_emotion.json
        ├── emotion_log.json
        ├── proactive_meta.json
        ├── attention_state.json         AttentionEngine 最近状态
        ├── attention_log.json           AttentionEngine 第一人称状态段
        ├── tasks.json                  commitments
        ├── persons.json
        ├── self_profile.json
        ├── screenshot_log.json         截屏时间戳日志
        ├── screen_semantic_gate_log.json 截图语义 gate 决定与 writer outcome 审计
        ├── push_subscriptions.json     Web Push
        ├── model_library.json
        ├── models/                     下载的模型包
        ├── personas/<model_id>/        per-模型 soul.md + avatar
        ├── archive/                    按月归档 chat / timeline
        └── memory/                     Archival 目录树
            ├── _slots/                 ★ Memory v3 slot 元数据 (含 curator_meta.json + daily_writes.json + daily_slot_appends.json)
            │   ├── projects.json
            │   ├── people.json
            │   ├── topics.json
            │   └── self.json
            ├── projects/<slot_id>/main.md     ★ slot 内容
            ├── people/<slot_id>/main.md
            ├── topics/<slot_id>/main.md
            ├── self/<slot_id>/main.md
            ├── commitments/{active,done}.md   ☆ 主 agent add_commitment 工具 / Web UI 直写（不走 router）
            ├── patterns/sleep.md              ☆ Sleep Inference 直写
            ├── journal/<date>.{json,md}       ☆ journal.py 23:45 直写
            └── index.md
```

---

## 开发路线图

### Sprint 1-4 ✅ 全部完成

多设备基建、CareEngine v3、DDL、多用户系统 + 客户端打包、隐私合规、APK 自动更新、情绪日历、Sleep Inference。

### Sprint 5 ✅ 已完成（2026-04-20 → 2026-05-03）

**5.1 Memory v3 + v3.5 + v3.7** ✅
- [x] `memory_router.py` `route_with_slot_write` 单一入口 + Curator hook
- [x] Sleep Agent v3 (chat 路径 debounce batch → Slot Writer + Persona Writer)
- [x] ScreenSemanticGate + ScreenSlot Writer (截屏路径 sig≥3 先过滤重复/低价值观察，再输出 slot_writes + commitments + completed_commitments)
- [x] Pass 4 AppendEditor append-only（日常 match 只写短增量；夜间 SlotDailyCompactor 负责流畅整理/必要压缩）
- [x] Curator v3.5 (Planner + 3 self-checking Executors — merge/edit/delete, per-user daemon, soft/normal/idle_reset trigger)
- [x] daily_slot_audit cron + 状态自动转移
- [x] 41 个单测通过 + 7 个真实 LLM 端到端测试通过
- [x] UI 卡片视图（项目/人物/话题/自己 4 tab）+ 编辑/合并/归档/置顶
- [x] Slot-aware ⌘K 全文搜索 + 全部文件兜底 tab

**5.2 AttentionEngine live delivery** ✅
- [x] 旧 CareEngine 生产入口替换为 AttentionEngine
- [x] 完整 `soul.md` 注入，输出 inner / user_affect / self_emotion 的 continue/shift 状态段与 speak_intent
- [x] `sig=2` 只进 Attention，不写 memory slot
- [x] 第一版不发送消息，只写 `attention_log.json` / `attention_state.json`
- [x] Phase 1.5/1.6：conversation window + cadence + current focus + intent queue + direct proactive delivery + reactive/proactive main-agent mode
- [x] Live delivery：`attention_intent_queue.json -> direct delivery plan -> fixed-pro proactive main agent -> append chat/SSE/push`，并可用 `MIRU_ATTENTION_DELIVERY_DRYRUN=1` 回退到 log-only direct-plan 审计

**5.3 AI 3-tier 重构** ✅
- [x] `ai_config.py` 重写为 3-tier 全局 schema
- [x] `prompt.py` 删除 anthropic SDK 分支
- [x] 16 个调用点显式传 tier
- [x] admin UI 配置 + 实时 ping + 热重载
- [x] admin token 旋转
- [x] usage log 覆盖 `memory_prompts_v2._call_llm_with_retry()`，记录 cache hit/miss + reasoning tokens + call_label
- [x] 截屏写入成本分级：普通 `sig≥3` 先过 ScreenSemanticGate；通过后走 memory/no reasoning；仅明确 DDL / 项目状态变化走 chat/reasoning

**5.4 安全 + 运营** ✅
- [x] 邀请码 base31 + XOR mask 混淆
- [x] 统一长邀请码直连（短码/Discovery fallback 不再进入新登录路径）
- [x] 独立 admin SPA + AI 配置 tab
- [x] 删除 user settings 的 AI 配置块（admin-only）
- [x] VPS 持续 monitor systemd unit
- [x] 周报系统全砍（#246）— 月历 + 日记 + slot 卡片已覆盖

**5.5 客户端 + UI** ✅
- [x] 日记 book view + 月历 + 加藤惠叙事
- [x] DDL/日记 整合到记忆 hub
- [x] DMG WebView 直连 VPS（修复 chat 死路）
- [x] 邀请码客户端接受 6/16 char body
- [x] 桌宠 hotkey 优化 + per-device 本地存储

### Sprint 6 — 当前：产品效果 + Windows Desktop v1 + 完整源码开源

当前在继续观察陪伴效果的同时，收口 Mac、Windows x64、Android arm64 与 Linux amd64 Server 的同源候选并准备完整源码开源。Mac、Windows、Android 使用同一套服务器协议，也都支持本地完整后端；三端还可以连接已有服务器或从本机/手机 SSH 创建多设备 Miru。普通用户可以用安装包和向导，技术用户应能从公开源码构建、修改并自部署 Docker。

详细实施清单维护在 [`PRIVATE_SERVER_RELEASE_TODO.md`](./PRIVATE_SERVER_RELEASE_TODO.md)；Windows 的架构、状态隔离、阶段与真实测试矩阵维护在 [`WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md`](./WINDOWS_DESKTOP_V1_IMPLEMENTATION_PLAN.md)。本节只记录路线图摘要。

- 📋 用户填的 bug 排查清单（#297）
- 📋 Memory v3 在生产中的实际表现（Slot Writer retry 失败率、Pass 4 append 质量、slot match/new 比例、Curator merge/delete 准确度）
- 📋 继续用 `attention_delivery_log.json` 观察 live delivery 的克制度、误触发率和延迟策略
- 📋 对话上下文召回是否准确
- 📋 Mac 自动更新（#189）
- Cross-platform candidate（Internal）：Mac arm64、Windows x64、Android arm64 已完成各自本地模式、创建/连接多设备服务器和三端同账号矩阵；Linux amd64 Docker 真实 API smoke通过。公开发布仍受长时 Android、Windows SmartScreen/卸载、许可证/资产权利和 clean-room build 审计约束。
- 📋 完整源码开源 gate：公开仓库可独立构建；清除秘密和私有路径；完成依赖与资产许可证审计；同一 commit 重打 DMG、APK、Windows installer 和 Docker archive。
- ✅ `miru/server:<version>` Docker 镜像：包含 Flask 后端、前端模板、默认资源、记忆系统、AttentionEngine、SSE、日记等服务端逻辑；数据挂载到 `/opt/miru/data`，日志挂载到 `/opt/miru/logs`；容器启动单用户初始化并根据 `SERVER_IP/SERVER_PORT` 生成现有格式长邀请码。Milestone B 已在 Mac Docker Desktop 完成本地验收：带 API key 的 smoke 覆盖主 agent 聊天、截图上传、AttentionEngine / screen memory gate-writer、SSE 和 restart persistence；Android 真机在同 Wi-Fi + Mac LAN IPv4 下验证了长码登录、设备注册、SSE 和 MediaProjection 截图上传。干净云服务器验收放到自部署 / 官方开服阶段。
- 📋 统一客户端登录：DMG / Windows installer / APK 都按 `MIRU-XXXXXXXXXX-YYYYYY` 的 server 段解析 IPv4 + port，组成 `http://ip:port`，POST 私有服务器 `/api/auth/login`，保存 `server_url + auth_token` 后 WebView 直连该服务器；不要默认转发到 `mirulife.top` / discovery
- ✅ Provisioning-ready 生命周期脚本：`deploy/self_host/install/status/backup/restore/reset/update` 已成为官方开服后端可调用的服务器生命周期接口；官方 provisioning 后续只负责创建云服务器、放行端口、SSH 调用 `install.sh --non-interactive`、读取 `status.sh --json`，不在后端里重写 Docker 安装细节。2026-06-08 已在干净腾讯云 Lighthouse Ubuntu 20.04 上完成单容器真实验收：Docker 官方源失败 fallback 到 `docker.io + docker-compose`、amd64 镜像加载、长邀请码生成、本机与公网 health、Mac DMG 长码直连、Android APK 公网长码登录、手机非局域网截图上传、backup/factory reset/restore、user-data reset、update 数据保持均通过。测试机 AI 配置已切到生产同款三层形态：vision=OpenRouter Qwen VLM，chat=DeepSeek Pro，memory=DeepSeek Flash；不再使用旧中转站默认配置。
- ✅ Host Instance Manager：核心原语已落地。Host = 一台 Linux 服务器；Instance = 一个 Miru Docker 容器 + 独立 data/logs/env + 独立端口 + 独立长邀请码。`scripts/host_manager.py` 管理 host/instance/port ledger，`deploy/host_manager/*.sh` 复用 self-host 生命周期脚本创建、查看、备份、恢复、重置、更新、删除 instance。真实部署会把维护工具同步到 host home 的 `tools/current` 并暴露 `deploy/` / `scripts/` 入口，旧服务器也可以用 `sync_remote_tools.sh` 自修复；生命周期脚本持续兼容 docker-compose v1 与 Docker Compose v2。
- ✅ 4 instance 并行真实测试：2026-06-11 已在腾讯云测试机上创建 4 个 Miru instance，验证端口、容器名、数据目录、API key、SSE、chat、截图上传、`ScreenObservationVLM`、`AttentionEngineEvaluate`、backup/restore/reset/update/delete 全部隔离；单容器轻负载内存约 90-122 MiB。公网 DMG/APK 对多实例端口的外部网络验收留到 First-run Wizard / provisioning 安全组阶段。
- ✅ DMG First-run Wizard 首版：首屏询问“Miru 要住在哪里”，本地单设备可直接在这台 Mac 创建 local-only 账号并使用；多设备下支持已有长邀请码登录；自有服务器路径可用公网 IPv4 + 用户显式填写的 SSH 用户 + SSH 密码/本机 SSH key + Miru Server 镜像包在目标 Linux host 上初始化/复用 Host Instance Manager，上传镜像包并创建新的独立 Miru instance，拿到长邀请码后进入成功页。没有选择镜像包时才使用高级在线镜像源，当前 fallback 默认 `miru/server:0.2.0`；选择镜像包时会在目标服务器 `docker load` 后自动 retag 为本次部署所需镜像名，避免 tag 不一致导致再次联网拉取。已有 host manager 会复用现有 ledger 和端口范围，不覆盖其他 instance；创建后 DMG 会从这台 Mac 检查实际公网端口 `/api/health`，公网不可达时不会显示进入按钮，而是提示放行端口或清理旧实例。部署脚本不依赖某家云厂商 metadata，DMG 主路径以用户填写的公网 IPv4 为准。官方订单/自动回填仍归 Milestone H。
- ✅ Owner API Key Settings：设置页可填写 Vision / Chat / Memory 三层 API；远端 instance 通过 owner API 热更新服务器 `_admin/ai_config.json`，本地单设备模式用同一套端点写本机配置。API key 不回显明文，只显示 masked placeholder；每个 tier 都可保存和测试，测试失败会显示 host/model/HTTP 状态/错误摘要。第一个 active user 会认领 instance owner，防止同一容器误出现第二个用户后互相修改模型配置。
- 📋 官方 provisioning backend：付款成功后选择现有 host 或创建新 host，调用同一套 Host Instance Manager 创建 instance，健康检查后把长邀请码回填订单页；状态机覆盖下单、选 host、创建 instance、交付和失败清理。
- 📋 最终体验验收：发布前要连续多日真实使用，检查主动消息、主 agent 回复、AttentionEngine 内心、记忆引用、日记和 DDL 是否真的让 Miru 像一个在乎用户、偏爱用户、想陪用户生活的女孩子，而不是技术功能都能跑但情感上仍像规则提醒器。
- ✅ Project Page v2 初版：`mirulife.top` 已从旧内测页改成当前真实 release 形态，强调 Miru 是会记得用户、会主动靠近、能住在 Mac / Android / 自有服务器里的 AI 陪伴；首屏和章节采用日式纸质感 + galgame project page 方向，内容覆盖本地单设备、自有服务器多设备同步、v0.2.0 Release、三层 OpenAI-compatible API、AttentionEngine、记忆/日记/DDL。后续继续补真实截图、key visual、Gallery/Movie、Remotion demo 与自部署教程。

### Backlog（暂缓）

- iOS Sensor App（Shortcut 自动化）
- TTS 语音 / "Miru说"语录卡
- soul.md 在线编辑 + 模板社区
- 月度关系报告
- 邀请码 server 段改为 directory/service-id（#325）— 当前 XOR 混淆够用，v1 先坚持长码直连

---

## 目标用户

1. **学生群体（核心）** — DDL、作息、考试周陪伴，渴望"真正记得我"的存在。月度关系报告天然适合社交分享（稀缺感 + 情感浓度）。
2. **想要真实 AI 陪伴的人** — 用过 Character.AI/Replika，对"假装认识你"感到空虚。
3. **量化自我 / 数字生活记录爱好者** — 自动化生活记录，不想手动记笔记。
4. **注重隐私的用户** — 要 AI 陪伴但不信任云厂，用 DMG 本地或自托管 VPS。
5. **二次元 / ACG 社区** — 被加藤惠式角色吸引，想要"真正懂你的"虚拟角色 + Live2D 锁屏壁纸。

---

## 项目愿景

短期，Miru 是一个跨设备应用：一个住在你所有屏幕上的 AI，默默看着你的生活，一天比一天更懂你。她在你的电脑上看你写代码，在你的手机上看你刷社交媒体，然后在你需要的时候提醒你答应过同事的事、你该睡觉了、你这周其实完成了很多。

长期，我们相信 Miru 定义了 AI 伴侣的下一个范式：**不是更聪明的对话，而是更深的理解。** 当 AI 真正认识一个人——知道他的朋友、他的承诺、他的焦虑、他的习惯——对话质量和情感真实性会自然涌现。

> **Miru sees. Miru remembers. Miru feels. Miru cares.**
>
> **比你更记得你自己的 AI。**

*Inspired by Katou Megumi — the one who quietly watches, remembers everything, and never stops caring.*
