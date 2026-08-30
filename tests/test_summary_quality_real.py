"""真实 LLM 验证: summary 写法规范是否落地 (v3.2, 2026-05-14).

Spends real tokens. NOT auto-run by pytest.

跑法:
  AI_CHAT_HOST=... AI_CHAT_KEY=... AI_CHAT_MODEL=... \
  AI_MEMORY_HOST=... AI_MEMORY_KEY=... AI_MEMORY_MODEL=... \
  PYTHONPATH=. .venv/bin/python tests/test_summary_quality_real.py

针对性场景 (针对 Section X 的规范设计):
  1. NEW project — 验证 Slot Writer 出的 summary 不是流水账, 不是字段拼接
  2. NEW person — 验证 summary 带关系/特征而非单次事件
  3. NEW topic  — 验证 summary 包含起始时间 + 关注方向
  4. MATCH 反复进展 — 验证 Pass 4 不会被单次事件污染 summary
  5. MATCH body 主线质变 — 验证 Pass 4 会重写 summary

每个场景跑完后做自动 lint:
  - 建议长度 ≤80 字；代码容错上限 200 字
  - 不以日期开头 ("2026-05-13 ...")
  - 不含"完成了"等单次事件动词
  - 不全是分号分隔字段 (≥2 分号 + 平均字段长度 ≤8 → 疑似字段拼接)
  - 包含至少 1 个语义连接词 ("跟"/"主线"/"目前"/"在做"/"在"/"觉得"/"偏"...)

并在结尾人工 review 角度打印, 让用户看实际写出的 summary 长什么样.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch  # noqa: E402


USER_NAME = "李垦"
MIRU_NAME = "Miru"
CURRENT_TIME = "2026-05-14T16:00:00"
IDENTITY_GT = """- 姓名: 李垦
- 身份: 研究生
- 职业 / 研究方向: 具身智能 / 机器人学习
- 作息: 深夜活跃 (00:00-02:00 常工作)
- 偏好的说话风格: 简洁直接, 偶尔自嘲""".strip()

# ─────────────────────────────────────────────────────────────────────
# Lint rules — automatically catch "流水账 / 字段拼接 / 太抽象"
# ─────────────────────────────────────────────────────────────────────

_DATE_PREFIX_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}")
_EVENT_VERBS = ["完成了", "做完了", "刚做", "今天", "昨天", "提交了", "投出了"]
_NATURAL_CONNECTORS = [
    "跟", "主线", "目前", "在做", "在", "觉得", "偏", "对", "起的",
    "起", "上", "之间", "之前", "现在", "中", "和", "是",
]


def lint_summary(summary: str) -> list[str]:
    """Return list of lint issues. Empty = good."""
    issues = []
    if not summary:
        issues.append("空 summary")
        return issues

    if len(summary) > 200:
        issues.append(f"长度 {len(summary)} > 200 字 (代码容错上限)")

    if _DATE_PREFIX_RE.match(summary):
        issues.append("以日期开头 — 像流水账")

    for v in _EVENT_VERBS:
        if v in summary:
            issues.append(f"含单次事件动词 '{v}' — 像流水账")
            break

    # 字段拼接判定: ≥2 个分号 + 各段都很短
    parts = re.split(r"[;；]", summary)
    if len(parts) >= 3:
        avg_len = sum(len(p.strip()) for p in parts) / len(parts)
        if avg_len <= 8:
            issues.append(
                f"疑似字段拼接 ({len(parts)} 段, 平均 {avg_len:.1f} 字)"
            )

    # 至少含 1 个自然连接词 → 像叙事
    if not any(c in summary for c in _NATURAL_CONNECTORS):
        issues.append("缺少自然连接词 — 可能不是叙事腔")

    return issues


# ─────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────

SLOT_INDEX_EMPTY = {"project": [], "person": [], "topic": [], "self": []}

SLOT_INDEX_WITH_PROJECT = {
    "project": [
        {
            "id": "papers_2026",
            "title": "2026 论文投稿",
            "summary": "李垦在并行准备 CVPR/ECCV/PRCV 三篇论文投稿, 主线是跟阿明合作的 multi-view 工作.",
            "aliases": ["eccv", "cvpr2026", "论文"],
            "last_active": "2026-05-12T20:05:56",
        },
    ],
    "person": [
        {
            "id": "ah_ming",
            "title": "阿明 (师兄)",
            "summary": "李垦实验室同门师兄, 高一届, 论文合作者, 偏好严谨实验设计.",
            "aliases": ["阿明", "师兄"],
            "last_active": "2026-05-09T11:00:00",
        }
    ],
    "topic": [],
    "self": [],
}


# ─────────────────────────────────────────────────────────────────────
# Scenarios
# ─────────────────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "name": "S1. NEW project — 验证 summary 是叙事不是流水账",
        "kind_filter": "new",
        "domain_filter": "project",
        "slot_index": SLOT_INDEX_EMPTY,
        "messages": [
            {"role": "user", "text": "我准备开一个新项目, 把家里旧的录像带数字化",
             "time": "2026-05-14T15:30:00"},
            {"role": "user", "text": "买了个 VHS 采集卡, 在测各种参数",
             "time": "2026-05-14T15:32:00"},
            {"role": "user", "text": "目标是把这批 90 年代的家庭录像变成可分享的 mp4",
             "time": "2026-05-14T15:35:00"},
            {"role": "user", "text": "目前在权衡 720p vs 480p 哪个更省空间又保留质感",
             "time": "2026-05-14T15:36:00"},
        ],
    },
    {
        "name": "S2. NEW person — 验证 summary 带关系而非单次事件",
        "kind_filter": "new",
        "domain_filter": "person",
        "slot_index": SLOT_INDEX_EMPTY,
        "messages": [
            {"role": "user", "text": "今天部门来了个新同事, 叫陈雨, 之前在字节做推荐系统",
             "time": "2026-05-14T15:00:00"},
            {"role": "user", "text": "她说话很直, 跟我说我那个 PR 写得有点啰嗦, 我笑死",
             "time": "2026-05-14T15:02:00"},
            {"role": "user", "text": "感觉聊得来, 工作风格也对得上",
             "time": "2026-05-14T15:04:00"},
        ],
    },
    {
        "name": "S3. NEW topic — 验证 summary 含起始 + 当前方向",
        "kind_filter": "new",
        "domain_filter": "topic",
        "slot_index": SLOT_INDEX_EMPTY,
        "messages": [
            {"role": "user", "text": "最近开始练自由泳, 一周三次",
             "time": "2026-05-14T14:00:00"},
            {"role": "user", "text": "在改划水的高肘姿势, 教练说我手腕老外翻",
             "time": "2026-05-14T14:01:00"},
            {"role": "user", "text": "目前主要在练这个细节, 顺便提一下耐力",
             "time": "2026-05-14T14:02:00"},
        ],
    },
    {
        "name": "S4. MATCH 单次进展 — Pass 4 不应被单次事件污染 summary",
        "kind_filter": "match",
        "domain_filter": "project",
        "slot_index": SLOT_INDEX_WITH_PROJECT,
        "messages": [
            {"role": "user", "text": "ECCV 的 ablation 跑完了, 效果不错",
             "time": "2026-05-14T13:00:00"},
            {"role": "user", "text": "明天给阿明 review 一下", "time": "2026-05-14T13:01:00"},
        ],
        "expected_summary_keeps": ["跟阿明", "multi-view"],
    },
    {
        "name": "S5. MATCH 主线质变 — Pass 4 应重写 summary",
        "kind_filter": "match",
        "domain_filter": "project",
        "slot_index": SLOT_INDEX_WITH_PROJECT,
        "messages": [
            {"role": "user", "text": "ECCV 中了! 阿明那条线打通, 接下来重心转向 CVPR 2027 的 single-view 路线",
             "time": "2026-05-14T13:00:00"},
            {"role": "user", "text": "PRCV 这条线决定停掉, 优先级降到最低",
             "time": "2026-05-14T13:01:00"},
        ],
        "expected_summary_evolves": True,  # 应该明显不同于原 summary
    },
]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _push_g(tmp):
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = "test_user"
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def _seed_slots_from_index(slot_index):
    """Write the fixture slot_index to memory so route_with_slot_write can match."""
    import memory
    import memory_router as mr
    for domain, slots in slot_index.items():
        for s in slots:
            slot = {
                "id": s["id"], "title": s["title"], "icon": "📄",
                "status": "active", "pinned": False,
                "summary": s["summary"],
                "aliases": s.get("aliases", []),
                "main_file": mr._slot_main_file_rel(domain, s["id"]),
                "last_active": s["last_active"],
                "created": s["last_active"],
            }
            mr.upsert_slot(domain, slot)
            memory.write_file(slot["main_file"],
                              f"# {s['title']}\n\n{s['summary']}\n初始 body 内容.\n")


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────

def run_scenario(sc):
    """Returns: dict with action / slot_id / summary / lint_issues / body_excerpt"""
    import memory_router as mr
    from memory_prompts_v3 import call_slot_writer

    tmp = tempfile.mkdtemp(prefix=f"sumq_{sc['name'].split('.')[0]}_")
    ctx = _push_g(tmp)
    try:
        _seed_slots_from_index(sc["slot_index"])

        result = call_slot_writer(
            user_name=USER_NAME,
            identity_ground_truth=IDENTITY_GT,
            slot_index=sc["slot_index"],
            active_commitments=[],
            messages=sc["messages"],
            current_time=CURRENT_TIME,
            miru_name=MIRU_NAME,
        )

        if result is None:
            return {"ok": False, "reason": "Slot Writer retry exhausted"}

        if not result.slot_writes:
            return {
                "ok": False,
                "reason": "Slot Writer skipped batch",
                "skipped_reason": result.skipped_reason,
            }

        # 取第一条符合 kind/domain 过滤的 slot_write
        sw = next(
            (
                w for w in result.slot_writes
                if (sc.get("kind_filter") is None or w.kind == sc["kind_filter"])
                and (sc.get("domain_filter") is None or w.domain == sc["domain_filter"])
            ),
            None,
        )
        if sw is None:
            return {
                "ok": False,
                "reason": (
                    f"no slot_write matching kind={sc.get('kind_filter')} "
                    f"domain={sc.get('domain_filter')}; got: "
                    + ", ".join(f"{w.kind}/{w.domain}" for w in result.slot_writes)
                ),
            }

        # 跑 route_with_slot_write
        route_res = mr.route_with_slot_write(sw, source_context="")

        if not route_res.get("ok"):
            return {"ok": False, "reason": "router failed: " + str(route_res)}

        slot = mr.get_slot(sw.domain, route_res["slot_id"])
        if slot is None:
            return {"ok": False, "reason": "slot disappeared after write"}

        import memory
        body = memory.read_file(slot["main_file"]) or ""

        return {
            "ok": True,
            "action": route_res["action"],
            "domain": sw.domain,
            "slot_id": route_res["slot_id"],
            "summary": slot.get("summary", ""),
            "title": slot.get("title", ""),
            "body_excerpt": body[:300],
            "body_len": len(body),
            "lint_issues": lint_summary(slot.get("summary", "")),
        }
    finally:
        ctx.pop()


def main():
    print("=" * 70)
    print("SUMMARY QUALITY E2E (v3.2 — Section X 落地验证)")
    print("=" * 70)
    print()

    results = []
    for i, sc in enumerate(SCENARIOS, 1):
        print("█" * 70)
        print(f"█  SCENARIO {i}: {sc['name']}")
        print("█" * 70)
        for m in sc["messages"]:
            print(f"  [{m['time'][-8:-3]}] {m['role']}: {m['text']}")
        print()

        r = run_scenario(sc)
        results.append((sc, r))

        if not r.get("ok"):
            print(f"  ✗ FAILED: {r.get('reason')}")
            if "skipped_reason" in r:
                print(f"    Slot Writer skip 原因: {r['skipped_reason']}")
            print()
            continue

        print(f"  action: {r['action']}")
        print(f"  domain/slot_id: {r['domain']}/{r['slot_id']}")
        print(f"  title: {r['title']}")
        print(f"  summary ({len(r['summary'])} 字):")
        print(f"    \"{r['summary']}\"")
        print(f"  body ({r['body_len']} 字, 前 300 字):")
        for line in r["body_excerpt"].split("\n"):
            print(f"    {line}")

        if r["lint_issues"]:
            print(f"  ✗ LINT FAIL ({len(r['lint_issues'])} 个):")
            for issue in r["lint_issues"]:
                print(f"    · {issue}")
        else:
            print(f"  ✓ lint pass")

        # 场景特定的额外校验
        if sc.get("expected_summary_keeps"):
            missing = [
                kw for kw in sc["expected_summary_keeps"] if kw not in r["summary"]
            ]
            if missing:
                print(f"  ✗ summary 丢失了应保留的关键词: {missing}")
            else:
                print(f"  ✓ 应保留的关键词都在")

        if sc.get("expected_summary_evolves"):
            orig = sc["slot_index"][r["domain"]][0]["summary"]
            if r["summary"] == orig:
                print(f"  ✗ summary 没演进 — 主线质变后应重写")
            else:
                print(f"  ✓ summary 有演进")
                print(f"    原: \"{orig}\"")
                print(f"    新: \"{r['summary']}\"")
        print()

    # ─── 总结 ───
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n_ok = sum(1 for _, r in results if r.get("ok"))
    n_lint_clean = sum(
        1 for _, r in results
        if r.get("ok") and not r.get("lint_issues")
    )
    print(f"  跑通: {n_ok}/{len(results)}")
    print(f"  lint 干净: {n_lint_clean}/{n_ok}")
    print()
    print("  详细 (每个 slot 的 summary):")
    for sc, r in results:
        if not r.get("ok"):
            print(f"    {sc['name'][:30]}: FAILED ({r.get('reason', '')[:50]})")
            continue
        flag = "✓" if not r["lint_issues"] else "✗"
        print(f"    {flag} {sc['name'][:35]}: \"{r['summary']}\"")


if __name__ == "__main__":
    main()
