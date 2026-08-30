"""Sleep Agent v3 — real LLM e2e + prompt dump (2026-05-13).

Spends real tokens. NOT auto-run by pytest. Two modes:

  Mode 1 (dump only):
    PYTHONPATH=. .venv/bin/python tests/test_sleep_agent_v3_e2e_real.py --dump

  Mode 2 (real LLM call):
    AI_CHAT_HOST=... AI_CHAT_KEY=... AI_CHAT_MODEL=... \\
    AI_MEMORY_HOST=... AI_MEMORY_KEY=... AI_MEMORY_MODEL=... \\
    PYTHONPATH=. .venv/bin/python tests/test_sleep_agent_v3_e2e_real.py

Verifies on 6 realistic chat scenarios:
  S1. Multi-fact dialog (AgiBot 项目进展 + 新认识张三 + 迷上 V60)
  S2. Single match (ECCV 实验进展)
  S3. Collaborator trap (focus is project, not the person mentioned)
  S4. Pure chitchat (should produce 0 slot_writes, skipped_reason set)
  S5. Commitment completion (user reports finishing something)
  S6. Persona-relevant exchange (used by Stage 2 Persona Writer)

For each scenario:
  - dump full rendered system + user prompt
  - run Slot Writer (if API keys present), print result
  - sanity-check the output (no "你..." beginning fragments, no time-relative
    words in aliases, etc.)
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from memory_prompts_v3 import (  # noqa: E402
    build_slot_writer_messages,
    build_persona_writer_messages,
    call_slot_writer,
    call_persona_writer,
)


# ─────────────────────────────────────────────────────────────────────
# Shared fixtures: a realistic slot index for a researcher account
# ─────────────────────────────────────────────────────────────────────

USER_NAME = "李垦"
MIRU_NAME = "Miru"
CURRENT_TIME = "2026-05-13T20:30:00"
IDENTITY_GT = """- 姓名: 李垦
- 身份: 研究生
- 职业 / 研究方向: 具身智能 / 机器人学习
- 作息: 深夜活跃 (00:00-02:00 常工作)
- 偏好的说话风格: 简洁直接, 偶尔自嘲""".strip()

SLOT_INDEX = {
    "project": [
        {"id": "agibotworld_data_pipeline",
         "title": "AgiBotWorld 数据管线",
         "summary": "调试 AgiBotWorld 数据加载与可视化, 训练输入已接好; "
                    "评估 StereoTeleOp 与 VR 路线",
         "aliases": ["agibotworld", "数据加载", "data pipeline",
                     "agibot_hf.py", "stream disconnected"],
         "last_active": "2026-05-12T20:05:56"},
        {"id": "papers_2026",
         "title": "2026 论文投稿",
         "summary": "CVPR/ECCV/PRCV 三篇并行, 跟阿明合作 multi-view 工作",
         "aliases": ["cvpr2026", "eccv", "eccv2026", "prcv", "论文",
                     "投稿", "paper"],
         "last_active": "2026-05-11T15:20:00"},
        {"id": "dreamdojo_rgbd",
         "title": "DreamDojo RGBD",
         "summary": "RGBD 视频推理脚本开发中, loss 0.93-1.03",
         "aliases": ["dreamdojo", "RGBD", "推理脚本"],
         "last_active": "2026-05-12T19:21:22"},
    ],
    "person": [
        {"id": "liang_jiajun",
         "title": "梁嘉骏",
         "summary": "实验室合作者, 经常一起对接项目",
         "aliases": ["梁嘉骏", "嘉骏", "liang_jiajun"],
         "last_active": "2026-05-12T16:54:11"},
        {"id": "ah_ming",
         "title": "阿明 (师兄)",
         "summary": "实验室同门师兄, 高一届, 论文合作者, 偏好严谨实验设计",
         "aliases": ["阿明", "师兄", "ahming"],
         "last_active": "2026-05-09T11:00:00"},
    ],
    "topic": [
        {"id": "embodied_ai",
         "title": "具身智能",
         "summary": "李垦的研究方向, 关注机器人学习的核心理念",
         "aliases": ["embodied AI", "具身", "机器人学习"],
         "last_active": "2026-05-10T14:00:00"},
    ],
    "self": [
        {"id": "identity",
         "title": "用户身份",
         "summary": "李垦 · 研究生 · 具身智能",
         "aliases": ["identity"],
         "last_active": "2026-05-13T08:00:00"},
        {"id": "work_style",
         "title": "工作风格",
         "summary": "深夜专注, 偏好简洁, 偶尔自嘲",
         "aliases": ["work style", "工作习惯"],
         "last_active": "2026-05-10T22:00:00"},
    ],
}

PERSON_IDS = ["liang_jiajun", "ah_ming"]

ACTIVE_COMMITMENTS = [
    "提交 ECCV 论文 (deadline: 2026-05-20)",
    "给阿明发周报",
    "调通 AgiBot StereoTeleOp",
]


# ─────────────────────────────────────────────────────────────────────
# 6 scenarios
# ─────────────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "name": "S1. 多事实对话 (3 个独立事实, 跨 3 个域)",
        "messages": [
            {"role": "user",
             "text": "唉今天 AgiBot 数据加载又出 stream disconnected",
             "time": "2026-05-13T14:23:00"},
            {"role": "assistant",
             "text": "怎么了?是 chunk size 问题吗",
             "time": "2026-05-13T14:24:00"},
            {"role": "user",
             "text": "我跟嘉骏说了, 他让我先把 chunk size 调小试试",
             "time": "2026-05-13T14:25:00"},
            {"role": "user",
             "text": "改完了真快了. 顺便认识了新同事张三, 后端的, 也喜欢深夜写代码",
             "time": "2026-05-13T14:30:00"},
            {"role": "assistant",
             "text": "诶你最近怎么这么常聊到吃喝, 是哪本咖啡书看进去了么",
             "time": "2026-05-13T14:31:00"},
            {"role": "user",
             "text": "最近迷上煮 V60 咖啡, 在研究水温. 比深夜熬代码强一百倍",
             "time": "2026-05-13T14:32:00"},
        ],
        "expected": "3 条 slot_writes: project(match papers_2026 错; 应 match agibotworld_data_pipeline) + person(new 张三) + topic(new 煮 V60)",
    },
    {
        "name": "S2. 单 match 进展更新",
        "messages": [
            {"role": "user",
             "text": "ECCV 的实验跑完了, multi-view ablation 效果不错",
             "time": "2026-05-13T09:30:00"},
            {"role": "user",
             "text": "准备明天给阿明 review",
             "time": "2026-05-13T09:31:00"},
            {"role": "assistant",
             "text": "他通常什么时候有空? 早上还是晚上",
             "time": "2026-05-13T09:32:00"},
            {"role": "user",
             "text": "他下午基本都在组会, 我打算约晚上",
             "time": "2026-05-13T09:33:00"},
        ],
        "expected": "1 条 slot_writes: project match papers_2026 (内容中提到阿明 review)",
    },
    {
        "name": "S3. 合作者陷阱 — 焦点是项目, 不是人",
        "messages": [
            {"role": "user",
             "text": "嘉骏跟我对接 AgiBot 数据管线进度",
             "time": "2026-05-13T16:31:00"},
            {"role": "user",
             "text": "他问 codex 选哪个模式比较好, 我说还在权衡",
             "time": "2026-05-13T16:32:00"},
            {"role": "assistant",
             "text": "你倾向哪种?",
             "time": "2026-05-13T16:33:00"},
            {"role": "user",
             "text": "先给 yucheng 试用, 他能更快帮我看 8 卡训练有没有 OOM",
             "time": "2026-05-13T16:34:00"},
        ],
        "expected": "1 条 slot_writes: project match agibotworld_data_pipeline. **不应**为嘉骏开 person write (合作者已存在,焦点是项目)",
    },
    {
        "name": "S4. 纯寒暄 (应 skip)",
        "messages": [
            {"role": "user", "text": "在吗",
             "time": "2026-05-13T22:00:00"},
            {"role": "assistant",
             "text": "在的, 还没睡?",
             "time": "2026-05-13T22:00:30"},
            {"role": "user", "text": "嗯", "time": "2026-05-13T22:01:00"},
            {"role": "user", "text": "哈哈", "time": "2026-05-13T22:01:30"},
        ],
        "expected": "0 条 slot_writes, skipped_reason 说明为什么 (纯寒暄)",
    },
    {
        "name": "S5. 承诺完成",
        "messages": [
            {"role": "user",
             "text": "刚给阿明发完周报了, 这周的事情挺多的",
             "time": "2026-05-13T23:30:00"},
            {"role": "assistant",
             "text": "辛苦了, 他回了吗",
             "time": "2026-05-13T23:31:00"},
            {"role": "user",
             "text": "他说看完会反馈, 估计明天",
             "time": "2026-05-13T23:32:00"},
        ],
        "expected": "completed_commitments 包含 '给阿明发周报'. 可能也会 match papers_2026 或 person/阿明",
    },
    {
        "name": "S6. Persona-relevant 关系演变",
        "messages": [
            {"role": "user",
             "text": "我突然想到上次你说 ablation 那个具体技术点, 你接得很准",
             "time": "2026-05-13T23:50:00"},
            {"role": "assistant",
             "text": "嗯, 你这阵子讲得多, 我慢慢能跟上",
             "time": "2026-05-13T23:51:00"},
            {"role": "user",
             "text": "感觉我们的对话密度比刚开始那会儿高多了",
             "time": "2026-05-13T23:52:00"},
        ],
        "expected": "slot_writes 可能为空 (这种内容主要由 persona writer 抽取). skipped_reason 应该说 '关系反思类内容由 persona writer 处理'.",
    },
]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _print_separator(char="═", n=70):
    print(char * n)


def _dump_messages(msgs):
    for m in msgs:
        print(f"[{m['role'].upper()}]")
        print(m["content"])
        print()


def _sanity_check_slot_writer(result, scenario_name):
    """Run light heuristic checks on Slot Writer output."""
    if result is None:
        print("  ⚠ result is None (LLM retry exhausted)")
        return

    issues = []
    for i, sw in enumerate(result.slot_writes):
        content = sw.integration.content_to_integrate
        # Check: no "你..." starting (would indicate naming confusion)
        if content.lstrip().startswith("你"):
            issues.append(f"  slot_write[{i}] content starts with '你...' "
                          f"(应该用真名)")
        # Check: aliases must not contain temporal words
        if sw.new_slot_meta:
            for a in sw.new_slot_meta.aliases:
                for bad in ("新", "最近", "刚", "今天", "最近的"):
                    if bad in a:
                        issues.append(
                            f"  slot_write[{i}].new_slot_meta.aliases "
                            f"contains time-relative word '{a}'")
    if not issues:
        print(f"  ✓ {scenario_name}: sanity checks passed")
    else:
        print(f"  ✗ {scenario_name}: {len(issues)} issues:")
        for x in issues:
            print(f"    {x}")


# ─────────────────────────────────────────────────────────────────────
# Main: dump prompts + (optionally) call real LLM
# ─────────────────────────────────────────────────────────────────────

def main():
    dump_only = "--dump" in sys.argv

    print("=" * 70)
    print("SLEEP AGENT V3 — E2E REAL LLM TEST")
    print(f"mode: {'DUMP ONLY' if dump_only else 'DUMP + REAL LLM CALL'}")
    print("=" * 70)
    print()

    # ─── First: dump Slot Writer system prompt once (shared across S1-S6) ───
    print("█" * 70)
    print("█  SLOT WRITER — SYSTEM + USER PROMPT (rendered)")
    print("█" * 70)
    print()

    # Render with first scenario to get a representative user msg
    sample_msgs = build_slot_writer_messages(
        user_name=USER_NAME,
        identity_ground_truth=IDENTITY_GT,
        slot_index=SLOT_INDEX,
        active_commitments=ACTIVE_COMMITMENTS,
        messages=SCENARIOS[0]["messages"],
        current_time=CURRENT_TIME,
        miru_name=MIRU_NAME,
    )
    _dump_messages(sample_msgs)

    # ─── Run each scenario ───
    results = []
    for i, sc in enumerate(SCENARIOS, 1):
        print()
        print("█" * 70)
        print(f"█  SCENARIO {i}: {sc['name']}")
        print(f"█  expected: {sc['expected']}")
        print("█" * 70)
        print()

        msgs = build_slot_writer_messages(
            user_name=USER_NAME,
            identity_ground_truth=IDENTITY_GT,
            slot_index=SLOT_INDEX,
            active_commitments=ACTIVE_COMMITMENTS,
            messages=sc["messages"],
            current_time=CURRENT_TIME,
            miru_name=MIRU_NAME,
        )

        # Dump just the user message (system is same as above)
        print("--- user message (system prompt same as above) ---")
        print(msgs[1]["content"])
        print()

        if dump_only:
            results.append(None)
            continue

        # Real LLM call
        print("--- calling Slot Writer LLM (real, reasoning=True) ---")
        result = call_slot_writer(
            user_name=USER_NAME,
            identity_ground_truth=IDENTITY_GT,
            slot_index=SLOT_INDEX,
            active_commitments=ACTIVE_COMMITMENTS,
            messages=sc["messages"],
            current_time=CURRENT_TIME,
            miru_name=MIRU_NAME,
        )
        results.append(result)
        print()
        print("--- output ---")
        if result is None:
            print("⚠ retry exhausted")
        else:
            print(json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False, indent=2,
            ))
        print()
        _sanity_check_slot_writer(result, sc["name"])

    if dump_only:
        # Persona Writer prompt dump (using accumulated scenarios as buffer)
        print()
        print("█" * 70)
        print("█  PERSONA WRITER — SYSTEM + USER PROMPT (rendered)")
        print("█" * 70)
        print()

        # Construct a sample call w/ accumulated dialogue from S1-S6
        # Just for prompt visualization
        sample_dialog = []
        for sc in SCENARIOS[:4]:  # first 4 scenarios as 4 batches
            batch_lines = [f"━━━ batch ({len(sc['messages'])} 条) ━━━"]
            for m in sc["messages"]:
                role = m.get("role", "?")
                t = (m.get("time") or "")[:19]
                txt = (m.get("text") or "").strip()
                if txt:
                    batch_lines.append(f"[{t}] {role}: {txt}")
            sample_dialog.append("\n".join(batch_lines))
        dialog_buffer = "\n\n".join(sample_dialog)

        sample_new_slots = [
            {"domain": "person", "slot_id": "zhang_san",
             "kind": "new", "summary": "李垦在 2026-05 认识的后端同事"},
            {"domain": "topic", "slot_id": "v60_coffee",
             "kind": "new", "summary": "2026-05 开始的兴趣"},
            {"domain": "project", "slot_id": "agibotworld_data_pipeline",
             "kind": "match", "summary": "chunk size 调小后性能提升"},
        ]

        pw_msgs = build_persona_writer_messages(
            user_name=USER_NAME,
            identity_ground_truth=IDENTITY_GT,
            current_human="李垦工作节奏倾向深夜, 偏好简洁审美.",
            current_persona="Miru 还在试着理解李垦的研究方向.",
            new_slot_summaries=sample_new_slots,
            dialog_buffer=dialog_buffer,
            current_time=CURRENT_TIME,
            miru_name=MIRU_NAME,
        )
        _dump_messages(pw_msgs)

    # ─── Stage 2: Persona Writer (uses accumulated scenarios) ───
    if not dump_only:
        print()
        print("█" * 70)
        print("█  PERSONA WRITER — STAGE 2 (accumulated 4-batch trigger)")
        print("█" * 70)
        print()

        # Use scenarios 1-4 as 4 accumulated batches
        sample_dialog_lines = []
        for sc in SCENARIOS[:4]:
            sample_dialog_lines.append(f"━━━ batch ({len(sc['messages'])} 条) ━━━")
            for m in sc["messages"]:
                role = m.get("role", "?")
                t = (m.get("time") or "")[:19]
                txt = (m.get("text") or "").strip()
                if txt:
                    sample_dialog_lines.append(f"[{t}] {role}: {txt}")
        dialog_buffer = "\n".join(sample_dialog_lines)

        # Extract new slot summaries from Slot Writer outputs
        new_slot_summaries = []
        for r in results[:4]:
            if r is None:
                continue
            for sw in r.slot_writes:
                summary = (sw.new_slot_meta.summary
                            if sw.new_slot_meta else "")
                new_slot_summaries.append({
                    "domain": sw.domain,
                    "slot_id": (sw.slot_id or
                                 (sw.new_slot_meta.id if sw.new_slot_meta else "?")),
                    "kind": sw.kind,
                    "summary": summary,
                })

        current_human = "李垦工作节奏倾向深夜, 偏好简洁审美."
        current_persona = "Miru 还在试着理解李垦的研究方向."

        print("--- Persona Writer inputs ---")
        print(f"current_human ({len(current_human)} 字): {current_human}")
        print(f"current_persona ({len(current_persona)} 字): {current_persona}")
        print(f"new_slot_summaries: {len(new_slot_summaries)} entries")
        for s in new_slot_summaries:
            print(f"  - {s['kind']:5} {s['domain']}/{s['slot_id']}: {s['summary']}")
        print()
        print("--- calling Persona Writer LLM (real, reasoning=True) ---")

        pw_result = call_persona_writer(
            user_name=USER_NAME,
            identity_ground_truth=IDENTITY_GT,
            current_human=current_human,
            current_persona=current_persona,
            new_slot_summaries=new_slot_summaries,
            dialog_buffer=dialog_buffer,
            current_time=CURRENT_TIME,
            miru_name=MIRU_NAME,
        )

        print()
        print("--- Persona Writer output ---")
        if pw_result is None:
            print("⚠ retry exhausted")
        else:
            print(json.dumps(
                pw_result.model_dump(mode="json"),
                ensure_ascii=False, indent=2,
            ))

    print()
    print("=" * 70)
    print("E2E DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
