# 截屏路径重构 TODO

**状态**: 暂缓
**记录时间**: 2026-05-13
**优先级**: 在对话路径 v3 稳定 1-2 周后启动

## 背景

聊天对话路径 (chat → sleep_agent) 已经在 2026-05-13 完成了 v3 重构:
1. **Slot Writer**: 每批对话 → JSON (slot_writes + completed_commitments)
2. **Pass 4 AppendEditor**: 每个 match slot_write → 追加一条带时间的记录
3. **Persona Writer**: 累计 4 批后 → human / persona block 更新

Pass 1 (classify) / Pass 2 (route) / Pass 3 (metadata) 这条 LLM 链已被
**完全删除** —— 由 Slot Writer 一次出齐 (kind=match|new + 显式 slot_id /
new_slot_meta + integration content).

但**截屏路径还停留在旧架构**:
- `screen_observer.py` (VLM 观察) → `screen_sleep_agent.py` (聚合) →
  fragments → ~~Pass 1+2+3+4~~ ← **断了!** Pass 1+2+3 已经被删

为了保持代码可以 import (避免破坏 `app.py` 启动), 这次重构在 `memory_router`
里留下了 **no-op stubs**:
- `trigger_route_async(...)` → 直接打印 warning 然后 return
- `route_and_write(...)` → return failed
- `route_fragments(...)` → return failed

## 当前的截屏链路 (2026-05-13 现状)

```
sensor.py (cron 截屏)
    ↓ 上传 PNG/JPEG
app.py /api/device/screenshot
    ↓
screen_analyzer.analyze(image_b64, device_id)
    ↓ 调用 VLM 写 JSON 观察
    └→ screenshot_log.json (storage)
    └→ miru_emotion.evaluate (情绪打分)
    └→ screen_observer.process(observation)
            ↓ 聚合相邻观察
        screen_sleep_agent.enqueue(fragment)
            ↓ 60s debounce / 20 fragment 上限
        screen_sleep_agent._do_flush()
            ↓ call_screen_sleep_agent → fragments
        memory_router.trigger_route_async(fragment)  ← **NO-OP 了**
```

所以现在: **截屏可以正常分析 + 写 screenshot_log + 触发情绪打分,
但不会再写 slot main.md** (走到 trigger_route_async 就 silent drop).

## 重构方案 (Phase Future)

### 选项 A: 截屏走 Slot Writer (推荐)

把 `screen_sleep_agent` 改为输出 SlotWriterOutput 而不是 fragments:
- input: 一批 VLM 观察文本
- output: SlotWriterOutput (slot_writes[])
- 后续走 `memory_router.route_with_slot_write`

需要做的事:
1. 改 `memory_prompts.py` 的 `_build_screen_sleep_agent_prompt`:
   - 加入完整 slot_index (类比 chat Slot Writer)
   - 输出 schema 改成 SlotWriterOutput 兼容形式
   - 注意: 没有"完成承诺"逻辑 (截屏不应推断承诺完成)
2. 改 `screen_sleep_agent.py` 的 flush:
   - 调用 `call_slot_writer` (复用 chat 的) 而不是 call_screen_sleep_agent?
     - **决策点**: chat 的 Slot Writer 期望 `messages` 是对话格式
       (role/text/time), 截屏是单条观察 + context.
       两种选择:
       (a) 复用 call_slot_writer, 把观察包装成 messages 格式
       (b) 写一个新的 call_slot_writer_screen, prompt 改成截屏视角

### 选项 B: 截屏直写 (跳过 Slot Writer)

截屏 VLM 输出已经是结构化的 (有 project/topic 关联). 让 VLM 直接
输出 SlotWrite list:
- 优点: 少一次 LLM 调用
- 缺点: VLM 要同时做"观察 + 写记忆决策"两个任务,精度可能下降

### 选项 C: 关掉截屏写记忆 (最保守)

只保留截屏 → screenshot_log + 情绪打分,完全不写 slot.
用户记忆只来自对话.

**这是当前默认状态** (因为 stubs 让所有写操作 silent drop).
可以观察一段时间, 如果用户觉得"截屏不参与记忆"也 OK, 就不需要重构.

## 重构上下文 (启动时需要的文件)

- `screen_observer.py` — VLM 观察聚合
- `screen_sleep_agent.py` — 后台 batcher
- `memory_prompts.py` 的 `call_screen_sleep_agent` 和
  `_build_screen_sleep_agent_prompt`
- `memory_router.py` 的 no-op stubs (要替换或删除)

## 决策建议

**先观察 1-2 周用户实际体验**:
- 如果对话写得很全 → 截屏写记忆是冗余的 → 选 C
- 如果用户经常在屏幕上做的事没被记下 → 需要补 → 选 A

不要在不知道收益的情况下提前重构 (避免 over-engineering).
