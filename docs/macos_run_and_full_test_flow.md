# ContextLife macOS 运行与全功能测试流程

## 1. macOS 本地运行（Web 核心）

```bash
cd /Users/chenhz/Documents/work-station/ContextLife
python3 -m pip install -r requirements.txt
python3 app.py
```

浏览器打开：

```text
http://127.0.0.1:5001
```

## 2. macOS 本地运行（桌面 App Shell）

### 2.1 启动本地 Web 后端

```bash
cd /Users/chenhz/Documents/work-station/ContextLife
python3 app.py
```

### 2.2 让 Shell 指向本地地址并启动

```bash
cd /Users/chenhz/Documents/work-station/ContextLife
npm install
npm run shell:url:local
npm run shell:dev
```

### 2.3 产出 macOS 包

```bash
cd /Users/chenhz/Documents/work-station/ContextLife
npm run shell:build
```

产物在 `src-tauri/target/release/bundle/` 下（`.app/.dmg`）。

## 3. BYODB 多端同步配置（可选）

优先方式（无需终端，手机友好）：
- 打开 App 左侧导航 `☁️`（同步设置）
- 在页面中填写数据库连接、用户命名空间、上传策略
- 在同一页面填写 AI API Key（否则分析/分类会提示未配置）
- 点击“保存配置”→“测试连接”→“立即双向同步”

### 3.1 环境变量

```bash
export SYNC_DATABASE_URL='postgresql://user:password@host:5432/dbname'
export SYNC_USER_ID='chenhz'
export SYNC_UPLOADS_ENABLED='1'
export SYNC_UPLOAD_INLINE='0'
export SYNC_UPLOAD_MAX_INLINE_MB='8'
export SYNC_INTERVAL_SECONDS='300'
```

说明：
- `SYNC_UPLOAD_INLINE=0`：仅同步上传文件元数据（推荐，适合大文件场景）。
- `SYNC_UPLOAD_INLINE=1`：小文件二进制直接入库；超阈值文件只同步元数据并在结果中标记 `skipped_large`。

### 3.2 手动同步命令

```bash
python3 scripts/sync_db.py --status
python3 scripts/sync_db.py --mode push
python3 scripts/sync_db.py --mode pull
python3 scripts/sync_db.py --mode bidirectional
```

### 3.3 API 同步命令

```bash
curl -X GET  http://127.0.0.1:5001/api/sync/status
curl -X POST http://127.0.0.1:5001/api/sync/push -H 'Content-Type: application/json' -d '{"include_uploads": true}'
curl -X POST http://127.0.0.1:5001/api/sync/pull -H 'Content-Type: application/json' -d '{"include_uploads": true}'
curl -X POST http://127.0.0.1:5001/api/sync/run  -H 'Content-Type: application/json' -d '{"mode":"bidirectional","include_uploads":true}'
```

## 4. 高强度自动化测试（先跑）

```bash
cd /Users/chenhz/Documents/work-station/ContextLife
pytest
python3 scripts/prompt_stress.py --dry-run
# 真实压测（需要可用模型 API）
python3 scripts/prompt_stress.py --rounds 3 --shuffle --report /tmp/contextlife_prompt_report.json
# 单样例超时保护压测（避免整轮卡死）
python3 scripts/prompt_stress_matrix.py --rounds 2 --shuffle --timeout 90 --report /tmp/contextlife_prompt_matrix.json
```

## 5. 全功能人工回归脚本（你可逐项操作）

### 5.1 基础消息流程

1. 在输入框发一条纯文本消息。
2. 再发一条带图片消息。
3. 点击分析（或调用 `/api/analyze`）。
4. 对卡片分别执行：确认、拒绝、编辑后确认、重试、Refine、回答追问。

验收：
- timeline 中状态从 `pending -> analyzed/confirmed/rejected` 变化正确。
- 确认后 `sub_cards.json` 有新增或更新。

### 5.2 一二级分类回归（A-F 全覆盖）

按顺序输入并确认：

1. `阿强是战神。`（应为 A1，人名卡片标题=阿强，role 含战神）
2. `OpenAI 是一家 AI 公司。`（A2）
3. `周五前把实验报告发给王老师。`（B1）
4. `明天记得买牛奶和鸡蛋。`（B2）
5. `每周日晚上做一次周复盘。`（B4）
6. `今天下午和李明开了项目会。`（C1）
7. `刚刚花了 38 元点外卖。`（D4）
8. `今天发工资到账 12000 元。`（D3）
9. `昨晚睡了 6.2 小时，今天静息心率 58。`（D1）
10. `发布检查清单：先跑 lint，再跑 test。`（E4）
11. `我今年目标是把体脂降到 15%。`（F1）
12. `我偏好上午做深度工作。`（F3）
13. `我的原则是不熬夜。`（F4）

验收：
- 一级类别准确（A/B/C/D/E/F）。
- 二级类别准确（A1/B1/D1/E4/F3 等）。
- `阿强是战神` 不会被拆成非 A1 主卡。

### 5.3 指标、财务、画像页

1. 打开 D 类页面看 D1 指标聚合。
2. 检查财务汇总 `/api/finance-balance`。
3. 打开 Profile 页，看 A+B+F 聚合结果。

### 5.4 日复盘与明日计划

1. 触发 `/api/daily-review`。
2. 触发 `/api/tomorrow-plan`。
3. 检查列表页和详情页历史记录可读。

### 5.5 角色与提醒链路

1. 打开 `/api/character` 确认角色配置。
2. 调用 `/api/character-plan` 生成角色日程。
3. 聊天窗口发起 `/api/chat`。
4. 调用 `/api/reminders/check` 与 `/api/reminders/today` 检查提醒落库与展示。

### 5.6 同步链路（双端模拟）

1. 终端 A：设置 `DATA_DIR=/tmp/contextlife_A`，运行并写入数据后执行 `sync push`。
2. 终端 B：设置 `DATA_DIR=/tmp/contextlife_B`，执行 `sync pull`。
3. 在 B 改动后 `sync push`，再回到 A `sync pull`。
4. 上传一个大文件并执行同步，确认结果里出现 `skipped_large` 或 metadata-only 行为。

验收：
- 双端核心 JSON 一致。
- 大文件行为符合配置，不会导致同步失败。

## 6. 发布前通过标准

1. `pytest` 全绿。
2. prompt stress 至少 3 轮，失败样例已复盘并处理。
3. Web + macOS Shell 都可启动和完成 5.1~5.6 流程。
4. 同步状态接口 `ok=true`，且无持续性失败项。
