# ContextLife 更新日志

> 按日期记录每次 commit 的变更内容，方便 Windows 版本同步更新。
> 标注 `[backend]` `[frontend]` `[pet]` `[infra]` `[docs]` 便于快速定位影响范围。

---

## 2026-03-20

### 每日主动问候 + 共同天数 `[backend]` `[frontend]`
- **core.py**: 新增 `check_daily_greeting()` — 基于角色行程（character_plan）中第一个活动的时间主动发送问候
  - 结合 Airi 当天行程生成个性化问候内容
  - 按 Airi 作息时间发送，用户打开时看到"已经发过的消息"
  - 每天最多一条，持久化记录在 `data/.daily_greeting_date`
  - 特殊日期（第7天倍数/30/100/365天）附加纪念提示
- **core.py**: 新增 `check_weekly_report()` — 每周一早上自动生成周报
  - 统计本周完成任务、事件，用角色语气回顾
  - 使用主 LLM 生成，注入 soul.md 语气要求
- **prompt.py**: 新增 `call_weekly_report()` — 角色风格周报生成
- **storage.py**: 新增 `relationship_meta.json` 管理（first_meet_date）+ 每日问候追踪
- **app.py**: reminder loop 新增 `check_daily_greeting()` 和 `check_weekly_report()` 调用
- **app.py**: 新增 `/api/relationship` 端点（返回共同天数、角色名）

### UI 默认视图切换 + 信息投递隐藏 `[frontend]`
- **templates/index.html**: 默认打开 Airi 聊天而非信息投递
- **templates/index.html**: 📥 信息投递按钮隐藏（`display:none`），功能代码保留
- **templates/index.html**: 所有 `showInfoDelivery()` 回退导航改为 `showMikuChat()`
- **templates/index.html**: Airi 聊天顶部新增"共同天数"banner（`{name} ♡ 第 N 天`）
  - 从 `/api/relationship` 获取数据，柔和小字设计

### 异步聊天系统重构 `[backend]` `[frontend]`
- **core.py**: 全新异步消息处理状态机 `receive_chat_message()`:
  - 消息进入缓冲区 → 1.5s 静默期 → 便宜 LLM 分类（FULL_REPLY / FOLLOWUP）
  - FULL_REPLY: 贵 LLM 生成完整回复（含记忆预检索），期间显示"输入中"
  - FOLLOWUP: 首条消息后 5s，若用户无新消息则发送追问（便宜 LLM 生成，语气灵活）
  - 用户连续发消息时自动累积，新消息取消未发送的追问
  - `get_typing_status()` 跟踪贵 LLM 生成状态
  - 保留 `chat_with_airi()` 作为同步兼容接口（companion API 使用）
- **prompt.py**: 新增 `call_classify_reply()` — 便宜 LLM 判断是否需要完整回复
- **prompt.py**: 新增 `call_generate_followup()` — 便宜 LLM 生成角色风格追问
- **app.py**: `/api/chat` 默认异步模式（立即返回），`sync=true` 参数保留同步模式
- **app.py**: 新增 `/api/chat/typing` 端点（轮询 Airi 是否正在输入）
- **templates/index.html**: `sendMikuMessage()` 改为异步发送，不显示"思考中"
- **templates/index.html**: 新增 typing indicator 动画（三点跳动 + "{name} 输入中"）
- **templates/index.html**: 发送消息后自动启动 1.5s 间隔轮询拉取回复

---

## 2026-03-19

### 智能记忆预检索 + 统一检索工具 `[backend]`
- **ai_config.py**: 新增 `retrieval_model` 配置（默认 `claude-haiku-4-5-20251001`），用于记忆检索的便宜模型
- **prompt.py**: 新增两步记忆检索流程：
  - Step 1: 便宜 LLM 分类相关卡片类别(A-F) + 提取关键词
  - Step 2: 从选定类别加载全部卡片 → 便宜 LLM 选择最相关的卡片(最多15张)
  - 分别从用户记忆库（高置信度）和 Airi 观测库（低置信度）检索，结果按来源分组
  - 新增 `call_memory_retrieval()`、`_call_retrieval_llm()`、`_build_card_summary()`、`_memory_keyword_fallback()`
- **core.py**: `chat_with_airi()` 聊天前自动执行预检索，将相关记忆卡片注入 agent 初始 context
- **tools/query_memory.py**: 新增统一记忆检索工具 `QueryMemoryTool`，Agent 可按需调用进行补充检索
- **删除**: `tools/query_sub_cards.py`、`tools/search_timeline.py`（合并为 `query_memory`）
- **prompt.py**: Agent system prompt 更新，说明预检索机制和 query_memory 工具用法
- **templates/index.html**: 前端工具标签新增 `query_memory: '记忆检索'`
- **templates/index.html**: AI 设置面板拆分为"主模型"和"检索模型"两个独立配置项，保存时分别写入 `claude_model` 和 `retrieval_model`

### 首次启动问候 `[backend]`
- **core.py**: 新增 `ensure_first_greeting()` — 应用启动时检查 `chat_history.json` 是否为空，若为空则自动注入角色问候消息
  - 已设置用户名时：个性化问候（"你好呀{name}！我是{character}～"）
  - 未设置用户名时：引导式问候（"先告诉我你的名字吧？"）
- **app.py**: 启动流程中调用 `ensure_first_greeting()`，确保新用户打开 Airi 聊天不会看到空白界面

### 自动截图间隔修复 + 关闭 Debug 模式 `[backend]`
- **tools/auto_screenshot.py**: 新增持久化冷却机制——每次截图后写入时间戳文件 (`data/.auto_screenshot_last_tick`)，进程重启时检查上次截图时间，未到间隔则跳过，完全根据用户设置的 `auto_screenshot_interval` 动态控制
- **app.py**: `_start_auto_screenshot_loop()` 移除30秒硬编码初始延迟，循环直接使用用户设置的间隔
- **app.py**: 循环改用 `threading.Event.wait()` 替代 `time.sleep()`，保存设置时调用 `wake_screenshot_loop()` 立即唤醒循环，修改截图间隔后即时生效
- **app_settings.py**: `flask_debug` 默认值从 `True` 改为 `False`，避免 Werkzeug 热重载器频繁重启子进程导致截图间隔失效

### Step 2 子卡片派生管线重写 `[backend]`
- **prompt.py**: 删除旧的 `PLAN_SUB_CARDS_SYSTEM_PROMPT`、`GENERATE_SUB_CARD_SYSTEM_PROMPT`、`call_plan_sub_cards()`、`call_generate_sub_card()`
- **prompt.py**: 新增 3 阶段管线：
  - `CLASSIFY_METAS_SYSTEM_PROMPT` + `call_classify_metas()` — Step 2a 分类
  - `MERGE_METAS_SYSTEM_PROMPT` + `call_merge_metas_for_category()` — Step 2b 合并
  - `CARD_DECISION_SYSTEM_PROMPT` + `call_card_decision()` — Step 2c 决策（skip/update/create）
- **core.py**: 重写 `_plan_and_generate_sub_cards()` 使用新 3 阶段管线
- **core.py**: 删除 `_get_existing_sub_cards_with_tags()`（不再需要加载全部卡片）
- **core.py**: `confirm_card()` 更新合并逻辑——信任 Step 2c 输出，非列表字段直接覆盖

### Step 1 证据卡片清理 `[backend]`
- **prompt.py**: `call_evidence_card_screenshot()` 移除 `existing_persons`/`existing_commitments` 参数及注入逻辑
- **core.py**: 所有 6 个调用点移除 `existing_persons=`/`existing_commitments=` 参数
  - `rebuild_cards()`、`analyze_pending()`、`process_message()`、`retry_card()`、`answer_card_questions()`、`_do_chat_summarization()`
- **tools/auto_screenshot.py**: `_path_b_memory()` 移除 persons/commitments 注入

### API 调用 Retry 机制 `[backend]`
- **prompt.py**: 新增通用重试基础设施：
  - `_unwrap_cause()` — 沿 `__cause__` 链查找原始异常
  - `_is_retryable_error()` — 判断是否可重试（429/5xx/连接/超时→重试；401/403/400/404→不重试）
  - `_get_retry_after()` — 提取 Retry-After header
  - `_api_call_with_retry(fn, max_retries=3, base_delay=2.0)` — 指数退避 + 随机抖动 + 60s 上限
- 覆盖所有 6 个 API 调用点：
  - `_call_api_claude()` — API 错误 + JSON 解析失败均重试
  - `_call_api_claude_multimodal()` — 同上
  - `call_screenshot_chat()` — API 错误重试
  - `call_reminder_text()` — API 错误重试（耗尽后走 fallback）
  - `call_chat()` — API 错误 + JSON 解析失败重试
  - `call_chat_agent()` — 每次迭代的 API 调用重试

### 子卡片 → 证据卡片导航 `[frontend]`
- **templates/index.html**: 子卡片详情中"来源卡片"从纯文本改为可点击链接
- **templates/index.html**: 新增 `navigateToEvidenceCard(cardId)` 函数，实现双向跳转

### NanoClaw API Key 同步 + 自动重启 `[backend]` `[infra]`
- **app.py**: 新增 `_sync_nanoclaw_env()` — 从 `ai_config.json` 同步 API key + host 到 `claw/nanoclaw/.env`
- **app.py**: 新增 `_restart_nanoclaw()` — 用户保存 AI 设置后自动重启 NanoClaw（后台线程，不阻塞）
- 启动时自动同步 `.env`，保存设置时同步 + 重启

### 桌宠可见性修复 `[pet]` `[frontend]`
- **scripts/airi_pet.swift**: `windowWillClose` 关闭原生窗口时主动 POST reset 心跳 + 立即 `orderFrontRegardless()` 恢复桌宠
- **contextlife.vue**: `pollUiActive` 的 `!res.ok` 分支从静默跳过改为恢复桌宠（与 catch 分支一致）

### 原生窗口加载提速 `[pet]`
- **scripts/airi_pet.swift**: 去掉 `.nonPersistent()` 数据存储，改用默认持久化存储（允许缓存复用）
- **scripts/airi_pet.swift**: `reloadIgnoringLocalCacheData` → `reloadRevalidatingCacheData`（用缓存但验证新鲜度）

### NanoClaw 子模块内联化 `[infra]`
- 移除指向 `Chenhzjs/nanoclaw.git` 的 git submodule，改为直接跟踪文件
- 包含之前未提交的本地改动：
  - **credential-proxy.ts**: HTTPS 代理支持（`HTTPS_PROXY`/`HTTP_PROXY` 环境变量）
  - **container-runner.ts**: 支持通过 `.env` 的 `CLAUDE_MODEL` 覆盖容器内模型
  - **package.json**: 新增 `global-agent`、`https-proxy-agent` 依赖

**涉及文件**: `prompt.py`, `core.py`, `tools/auto_screenshot.py`, `templates/index.html`, `scripts/airi_pet.swift`, `contextlife.vue`, `app.py`, `.gitignore`, `claw/nanoclaw/`, `docs/CHANGELOG.md`

---

## 2026-03-18

### `f1cfe0a` ui: redesign identity settings with inline underline inputs and fixed modal height `[frontend]`
- 身份设置面板重新设计：内联下划线输入框、固定模态框高度

**涉及文件**: `templates/index.html`

---

## 2026-03-17

### `77c3e88` fix: hide scrollbars on all modes `[frontend]` `[pet]`
- Pet/panel 模式隐藏滚动条

### `57e6689` fix: native drag, model-bounds UI tracking, and companion panel loading in WKWebView `[pet]`
- macOS WKWebView 原生拖拽、模型边界 UI 追踪、伴随面板加载修复

### `cdd773d` fix: sync pet UI overlays with model position and scale `[pet]`
- 桌宠 UI 覆盖层与模型位置/缩放同步

### `77f771e` fix: add httpx[socks] for SOCKS proxy support `[infra]`
- 新增 SOCKS 代理支持依赖

### `43c62be` chore: update README, add pynput dep, remove dead TTS button code `[docs]` `[frontend]`
- README 更新 NanoClaw/Electron/port 3001 信息
- 新增 pynput 依赖
- 移除废弃的 TTS 按钮代码

### `f411a10` ui: change stage-shell background from dark to sakura gradient `[pet]`
- 桌宠 stage-shell 背景改为樱花渐变色

### `651bf81` ui: rename AIRI面板 to 伴随面板 `[pet]`
- "AIRI面板" 重命名为 "伴随面板"，强制樱花背景

### `bfefc08` ui: hide voice input buttons `[frontend]`
- 暂时隐藏语音输入按钮

### `896356e` `2f08542` docs: streamline README `[docs]`
- README 聚焦价值，移除 Fly.io 相关内容，改为 uv 快速启动

### `47e5007` fix: track MediaPipe model and WASM assets `[infra]`
- 跟踪 MediaPipe 模型和 WASM 资源文件，避免 postinstall 下载

### `946c739` feat: update nanoclaw with host file access `[infra]`
- NanoClaw 容器代理可访问宿主文件

### `21fd5da` fix: track VRM model files `[infra]`
- 跟踪 VRM 模型文件，防止 Vite 下载失败

### `7beb32d` chore: remove Fly.io deployment config and GitHub Action `[infra]`
- 移除 Fly.io 部署配置和 GitHub Action

### `0861de3` fix: preflight auto-install uses uv pip `[infra]`
- 预检自动安装优先使用 uv pip

### `d92b116` fix: track Live2D model files `[infra]`
- 跟踪 Live2D 模型文件

### `4b64d72` Remove Show Runtime panel `[pet]`
- 移除 AIRI 桌宠的 Show Runtime 面板及所有相关代码

### `b976679` Fix shutdown killing all children `[infra]`
- 改进关闭流程：正确结束所有子进程（Vite, pet, NanoClaw）
- 改进 claw stop/start

### `158387b` fix: move Python dependency auto-install before third-party imports `[infra]`
- 依赖自动安装移到第三方 import 之前

### `bde0e66` fix: track Cubism SDK cache `[infra]`
- 跟踪 Cubism SDK 缓存，防止 Vite 启动时下载

### `0463677` Merge PR #4: windows-adapt-new-features `[infra]`

### `795cca0` Reduce Electron pet console noise `[pet]`
- 只转发真实错误，过滤无害警告

### `2c34740` Fix shutdown: kill all child processes `[infra]`

### `808cf08` Fix Claw Hub start: detect port conflict `[infra]`
- 检测端口冲突，避免重复启动进程

### `ed3ac53` feat: add startup preflight check `[infra]`
- 启动时检查缺失依赖

### `1767f7f` Fix pet not rendering: add Cubism 2 runtime `[pet]`
- 新增 Cubism 2 运行时，清理 Electron 旧锁

### `258cde6` fix: track Live2D SDK files `[infra]`
- 跟踪 Airi 前端所需的 Live2D SDK 文件

### `9b990f6` Skip auto-screenshot silently when API key is not configured `[backend]`
- 无 API key 时静默跳过自动截屏

### `fe84d18` Fix UTF-8 read crash `[backend]`
- 读取文件时 UTF-8 失败回退到系统编码（解决 GBK 文件问题）

### `a04a905` Harden cross-platform compatibility `[backend]` `[infra]`
- 进程管理和文件编码的跨平台兼容性加固

**涉及文件**: 大量文件，涵盖 `app.py`, `core.py`, `prompt.py`, `templates/index.html`, `scripts/`, `integrations/airi/`, `windows/`, `README.md` 等

---

## 2026-03-16

### `c928162` ui: refactor identity settings panel `[frontend]`
- 身份设置面板改为卡片式网格布局

### `7ecc3c0` docs: update README for new collaborator onboarding `[docs]`

### `481df76` layout: renew identity setting `[frontend]`
- 身份设置布局更新，调整间距和行列

### `49e0fab` docs: new color for serenbanka and layout like mihoyo `[docs]`

### `02f1a81` Adapt new main features for Windows `[pet]` `[backend]`
- Windows 适配：桌宠重启、截屏、自动截屏功能

### `3e198a9` Harden Windows pet restart handling `[pet]`
- Windows 桌宠重启处理加固

### `ee5a873` Merge PR #3: windows-platform-isolation `[infra]`

### `a06b0aa` Track model assets in git `[infra]`
- 跟踪 models、personas、model_library 资源文件

### `0056c6b` Live2D model library, per-model personas, sidebar groups `[frontend]` `[backend]`
- Live2D 模型库、每模型 persona、侧边栏分组、身份简化

**涉及文件**: `templates/index.html`, `app.py`, `core.py`, `data/model_library.json`, `data/personas/`, `windows/`

---

## 2026-03-15

### `56b78b5` Add Windows Electron pet, NanoClaw auto-launch, Claw Hub skills `[pet]` `[infra]`
- Windows Electron 桌宠
- NanoClaw 自动启动
- Claw Hub 技能
- 跨平台修复

### `fb9e594` Isolate Windows code into windows/ folder `[infra]`
- Windows 特定代码隔离到 `windows/` 目录
- 清理 `webview.py`

### `14a6034` Fix post-merge issues `[backend]`
- 修复合并后的死代码、缺失 shell 标志、断裂的 fallback

**涉及文件**: `windows/`, `webview.py`, `app.py`, `claw/`

---

## 2026-03-14

### `ec1c94f` Add transparency diagnostics `[pet]`
- 添加透明度诊断：在每个状态变化时 dump ExStyle/BackColor/DefaultBg

---

## 2026-03-13

### `6a1771c` feat: add Claw Hub — multi-bot delegation system `[backend]` `[infra]`
- Claw Hub 多机器人委托系统

### `5b1f322` Clean up dead code and dependencies `[backend]`

### `ec696ec` Add Windows compatibility for AIRI Vite dev server launch `[infra]`

### `ba71d73` ~ `c8ece7e` Windows pet 系列修复 `[pet]`
- 透明背景、热键支持、进程生命周期
- hide/show 逻辑、首次加载透明度
- pywebview shim 时序竞争
- WinForms Form.BackColor 透明
- exit 按钮修复（os._exit 替代 sys.exit）

### `dbd2619` Auto-launch NanoClaw from app.py `[infra]`

### `c1957b5` Simplify settings UI and add pet auto-restart `[frontend]` `[pet]`

**涉及文件**: `claw/`, `windows/`, `app.py`, `templates/index.html`

---

## 2026-03-12

### `2fe0883` Fix Vite dev server crash `[infra]`
- 添加缺失的 rolldown native binding (macOS ARM64)

### `4843611` Fix input box paste/newline and add voice-to-text `[frontend]`
- 修复输入框粘贴/换行
- 所有 UI 添加语音转文字功能

**涉及文件**: `templates/index.html`, `integrations/airi/`

---

## 2026-03-11

### `989546b` Fix desktop pet message sync `[pet]`
- 禁用 App Nap 定时器节流，修复消息同步

### `b36c742` Move chat summarization cards to user memory bank `[backend]`
- 聊天总结卡片移至用户记忆库
- 提高总结质量门槛

### `e034fee` Unify card ID to msg_id, validate memory_scope input `[backend]`
- 统一卡片 ID 为 msg_id
- 验证 memory_scope 输入

### `92aeec5` Update defaults and docs `[backend]` `[docs]`
- Claude 模型默认值更新
- 自动截屏间隔改为 1 小时
- 修复过时引用

**涉及文件**: `core.py`, `prompt.py`, `storage.py`, `app.py`

---

## 2026-03-10

### `373151c` Airi auto-summarize chat, auto-screenshot observation, and Airi card UI `[backend]` `[frontend]`
- **聊天自动总结**（每 10 条用户消息）：`_maybe_summarize_chat()` → `_do_chat_summarization()`
- **自动截屏**（桌面端每 10 分钟）：双并行路径 Path A（聊天回复）+ Path B（证据卡片）
- **Airi 卡片 UI**：侧边栏 AE/AA 按钮，证据卡片和子卡片浏览

### `194bc69` Extract auto-screenshot into standalone skill `[backend]`
- 自动截屏提取为独立技能模块 `tools/auto_screenshot.py`
- 改进观测 prompt

### `cb2456f` Native Swift pet window `[pet]`
- macOS 原生 Swift 桌宠窗口，可浮于全屏应用之上

### `6e18a03` Bundle AIRI stage-web into Flask deploy `[infra]`
- AIRI stage-web 打包进 Flask 部署

### `4d123aa` Cross-platform desktop pet: pywebview fallback `[pet]`
- Windows/Linux pywebview 回退方案

### `1d9dba3` ~ `5c2c201` Docker/CI 构建修复 `[infra]`
- 跳过 electron postinstall
- 安装 git
- 增加 Node.js 堆内存
- 改为 GitHub Actions 构建 Airi
- 创建 static/ 目录
- Vite 回退到静态资源

**涉及文件**: `core.py`, `prompt.py`, `storage.py`, `app.py`, `templates/index.html`, `tools/auto_screenshot.py`, `scripts/airi_pet.swift`

---

## 2026-03-09

### `67ad205` Integrate AIRI host and pet experiences `[backend]` `[pet]` `[frontend]`
- Miku → Airi 重命名
- CORS 支持桌宠
- Airi 观测记忆库（双记忆系统）
- Airi 桌宠集成

### `324617a` Airi pet UX `[pet]`
- 聊天覆盖层、WebKit 修复、Master 地址、Tauri open_url

**涉及文件**: `soul.md`, `character.py`, `core.py`, `prompt.py`, `storage.py`, `app.py`, `templates/index.html`
