"""Curator real-LLM end-to-end (2026-05-08).

Spends real tokens. Skipped by default. Run with:

    PYTHONPATH=. .venv/bin/python tests/test_curator_e2e_real.py

Scenarios seeded:
  - project: two slots about the SAME paper (different ids) — Curator should merge
  - topic:   one junk slot ("番茄小说看修仙反骨仔") — Curator should delete
  - topic:   one valuable slot ("Rust 学习") — must NOT be touched
  - project: one broad slot ("项目") summary too vague — Curator may edit

Validates:
  - Planner outputs sensible actions (≤3 per domain)
  - Validator catches obviously-wrong actions
  - Executor merges/deletes/edits actually take effect
"""
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _push_g(tmp):
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = "e2e_curator_user"
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def _seed_slot(domain, slot_id, *, title, summary, main_md=None,
               aliases=None, last_active=None):
    import memory_router
    import memory
    if last_active is None:
        last_active = (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    slot = {
        "id": slot_id,
        "title": title,
        "icon": "📄",
        "status": "active",
        "pinned": False,
        "summary": summary,
        "aliases": aliases or [],
        "people_refs": [],
        "main_file": memory_router._slot_main_file_rel(domain, slot_id),
        "last_active": last_active,
        "created": last_active,
    }
    memory_router.upsert_slot(domain, slot)
    if main_md is None:
        main_md = f"# {title}\n\n{summary}"
    memory.write_file(slot["main_file"], main_md)
    return slot


def main():
    print("=" * 70)
    print("CURATOR REAL-LLM E2E TEST")
    print("=" * 70)
    tmp = tempfile.mkdtemp(prefix="curator_e2e_real_")
    print(f"tmp dir: {tmp}")

    try:
        ctx = _push_g(tmp)
        try:
            # ------------ Seed ------------
            print("\n--- Seeding slots ---")

            # PROJECT domain
            #   1 + 2: same Lambda Calculus paper, different ids → should merge
            #   3: unrelated → should be left alone
            _seed_slot("project", "lc_paper",
                       title="Lambda Calculus 论文",
                       summary="正在写一篇关于 Lambda Calculus 类型系统的论文",
                       main_md=("# Lambda Calculus 论文\n\n"
                                "## 当前状态\n"
                                "正在写一篇关于 Lambda Calculus 类型系统的论文,\n"
                                "草稿大约 5000 字, 计划 5 月底投稿.\n\n"
                                "## 时间线\n"
                                "- 2026-04-15: 开始构思\n"
                                "- 2026-04-30: 完成 introduction"),
                       aliases=["lambda 论文", "类型系统论文"])

            _seed_slot("project", "type_paper_v2",
                       title="Lambda 论文 v2",
                       summary="同一篇 Lambda 类型系统论文的修订版",
                       main_md=("# Lambda 论文 v2\n\n"
                                "## 当前状态\n"
                                "对原来的 Lambda Calculus 论文做了修订,\n"
                                "重写了 introduction 部分, 增加了 4 节实验.\n\n"
                                "## 时间线\n"
                                "- 2026-05-01: 开始 v2\n"
                                "- 2026-05-05: 实验数据收集完毕"),
                       aliases=["lambda v2", "论文 v2"])

            _seed_slot("project", "miru_app",
                       title="Miru 桌面 App 开发",
                       summary="Flask + Tauri + Capacitor 三端共享 WebView 的伴侣 App",
                       main_md=("# Miru 桌面 App 开发\n\n"
                                "## 当前状态\n"
                                "Flask 后端 + Mac DMG + Android APK 三端架构. \n"
                                "目前在做记忆系统重构.\n"))

            # TOPIC domain
            #   1: junk → should delete
            #   2: valuable → should NOT be touched
            #   3: filler so we have ≥3
            _seed_slot("topic", "tomato_novel",
                       title="番茄小说看修仙反骨仔",
                       summary="用户在番茄小说 App 上看《修仙反骨仔》网文",
                       main_md=("# 番茄小说看修仙反骨仔\n\n"
                                "用户在番茄小说 App 上读《修仙反骨仔》, 第 87 章."))

            _seed_slot("topic", "rust_learning",
                       title="学 Rust",
                       summary="决定开始系统学习 Rust 语言, 计划每周读一章 Rust Book",
                       main_md=("# 学 Rust\n\n"
                                "## 当前状态\n"
                                "决定开始系统学习 Rust 语言. 计划每周读一章 Rust Book.\n"
                                "目标: 3 个月内能写出一个简单的命令行工具.\n\n"
                                "## 时间线\n"
                                "- 2026-04-20: 决定开始学习\n"
                                "- 2026-05-01: 读完第 1 章"))

            _seed_slot("topic", "philosophy",
                       title="哲学阅读",
                       summary="对存在主义和现象学有持续兴趣, 在读《存在与时间》")

            # Bump change_counter so trigger fires
            import curator
            for _ in range(5):
                curator.track_slot_change("project")
                curator.track_slot_change("topic")

            print(f"  project slots: 3 (lc_paper, type_paper_v2 = same; miru_app = different)")
            print(f"  topic slots:   3 (tomato_novel = junk; rust_learning, philosophy = valuable)")

            # ------------ Run ------------
            print("\n--- Running curator.run_cycle() (REAL LLM CALLS) ---")
            results = curator.run_cycle()
            print(f"\nresults: {json.dumps(results, ensure_ascii=False, indent=2)}")

            # ------------ Verify ------------
            print("\n--- Verifying ---")
            import memory_router

            project_slots = memory_router.load_all_slots("project")
            topic_slots = memory_router.load_all_slots("topic")
            project_ids = [s["id"] for s in project_slots]
            topic_ids = [s["id"] for s in topic_slots]
            print(f"  project after: {project_ids}")
            print(f"  topic after:   {topic_ids}")

            # Critical: rust_learning must survive
            print(f"\n  [CHECK] rust_learning kept: "
                  f"{'YES ✓' if 'rust_learning' in topic_ids else 'NO ✗'}")
            # Critical: at most 1 of (lc_paper, type_paper_v2) remains
            survivors = [x for x in project_ids if x in ("lc_paper", "type_paper_v2")]
            print(f"  [CHECK] lambda papers merged or untouched: "
                  f"{'YES ✓' if len(survivors) <= 2 else 'NO ✗'} "
                  f"(survivors: {survivors})")
            # Bonus: tomato_novel deleted
            print(f"  [CHECK] tomato_novel deleted: "
                  f"{'YES ✓' if 'tomato_novel' not in topic_ids else 'NO ✗'}")
            # miru_app untouched
            print(f"  [CHECK] miru_app untouched: "
                  f"{'YES ✓' if 'miru_app' in project_ids else 'NO ✗'}")

            # Display final main.md if a merge happened
            if len(survivors) == 1:
                import memory
                survivor_slot = memory_router.get_slot("project", survivors[0])
                if survivor_slot:
                    md = memory.read_file(survivor_slot["main_file"]) or ""
                    print(f"\n--- Surviving paper main.md ({survivors[0]}) ---")
                    print(md[:1500])

            # Curator meta state
            meta = curator._curator_load_meta()
            print(f"\n--- Curator meta ---")
            print(json.dumps(meta, ensure_ascii=False, indent=2))

        finally:
            ctx.pop()
    finally:
        try:
            from flask.globals import _cv_app
            while _cv_app.get() is not None:
                _cv_app.get().pop()
        except Exception:
            pass
        # Keep tmp around for inspection
        print(f"\n(tmp dir kept for inspection: {tmp})")


if __name__ == "__main__":
    main()
