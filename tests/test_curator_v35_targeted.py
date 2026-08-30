"""Targeted v3.5 tests covering behaviors specific to the new design.

These supplement test_curator_auto.py with scenarios that exercise the
v3.5 architecture's key contracts:

  T1. Merge Executor: aliases 剔除 — Executor returns new_slot_meta with
      a subset of (source + target) aliases when body shows some are
      mis-tagged.
  T2. Merge Executor: should_skip=true when body shows different objects.
  T3. Edit Executor: should_skip=true when Planner's diagnosis is wrong.
  T4. Edit Executor: meta-only edit (new_body empty) keeps body intact.
  T5. Edit Executor: protects original slot_id even if Executor outputs
      a different id (code-layer guard).
  T6. Delete Executor: should_skip=true on type-A failed (Planner reason
      = "过时", not "错放/无价值").
  T7. Delete Executor: executes when self-check confirms classification
      mis-placement.
  T8. Person domain merge: multi-name same-person (魏佳哲 / 佳哲 /
      jiazhe / void / 小南梁) — Executor consolidates aliases.
"""
import os
import sys
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    return tempfile.mkdtemp(prefix="curator_v35_test_")


def _reset_flask_g():
    try:
        from flask.globals import _cv_app
        while _cv_app.get() is not None:
            _cv_app.get().pop()
    except Exception:
        pass


def _push_g(tmp):
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = "test_user_v35"
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def _seed_slot(domain, slot_id, *, title="x", summary="x summary",
               main_md=None, pinned=False, status="active",
               aliases=None, last_active=None):
    import memory_router
    import memory
    if last_active is None:
        last_active = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    slot = {
        "id": slot_id,
        "title": title,
        "icon": "📄",
        "status": status,
        "pinned": pinned,
        "summary": summary,
        "aliases": aliases or [],
        "people_refs": [],
        "main_file": memory_router._slot_main_file_rel(domain, slot_id),
        "last_active": last_active,
        "created": last_active,
    }
    memory_router.upsert_slot(domain, slot)
    if main_md is None:
        main_md = f"# {title}\n\nseed body for {slot_id}"
    memory.write_file(slot["main_file"], main_md)
    return slot


def _mk_meta(slot_id, title, summary, aliases, icon="📄"):
    # Use Curator's local NewSlotMeta copy (memory_prompts_v2._NewSlotMeta),
    # not v3.NewSlotMeta — see test_curator_auto._mk_meta for rationale.
    from memory_prompts_v2 import _NewSlotMeta
    return _NewSlotMeta(
        id=slot_id, title=title, icon=icon,
        summary=summary, aliases=aliases,
    )


# ============================================================================

def test_t1_merge_aliases_filtered():
    """Executor returns new_slot_meta where some source aliases are dropped
    because the merge body indicated those aliases were mis-attached."""
    print("=" * 60)
    print("T1: Merge aliases pruning")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("person", "weijiazhe", title="魏佳哲",
                       summary="高中同学", aliases=["wjz", "vendor_misc"],
                       main_md="# 魏佳哲\n\nLi Ming's classmate, ML researcher")
            _seed_slot("person", "weijiazhe_2", title="佳哲",
                       summary="高中同学", aliases=["jiazhe", "void"],
                       main_md="# 佳哲\n\nClassmate, recent intern at 字节")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="same person", ops=[
                    mp.CuratorPlannerOp(
                        op="merge", target_slot_id="weijiazhe",
                        source_slot_ids=["weijiazhe_2"],
                        reason="魏佳哲 和 佳哲 是同一人: 高中同学 + ML 方向 + body 提到同一公司")])

            # Executor decides "vendor_misc" was bogus, drops it
            def fake_merge(**kw):
                return mp.MergeExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="weijiazhe", title="魏佳哲",
                        summary="高中同学, ML 方向, 现在字节实习",
                        aliases=["wjz", "jiazhe", "void"]),
                    merged_body="高中同学, ML 方向, 现在字节实习, 多个昵称",
                    self_check="vendor_misc 在 body 里查不到, 剔除",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_merge", side_effect=fake_merge):
                result = curator.run_domain("person")

            assert result.get("executed") == 1, f"merge should execute, got {result}"
            slot = memory_router.get_slot("person", "weijiazhe")
            assert slot is not None
            assert "vendor_misc" not in slot["aliases"], \
                f"vendor_misc should have been dropped, got {slot['aliases']}"
            assert "jiazhe" in slot["aliases"]
            assert "void" in slot["aliases"]
            print(f"  [PASS] aliases pruned: {slot['aliases']}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t2_merge_self_check_skip():
    """Executor reads body and decides slots are different objects → skip."""
    print("\n" + "=" * 60)
    print("T2: Merge self-check rejects same-name different-person")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("person", "wang_lei_a", title="王磊",
                       summary="高中同学", aliases=["wlei", "leixiaowang"],
                       main_md="# 王磊\n\n高中同学, 北京大学, 学物理")
            _seed_slot("person", "wang_lei_b", title="王磊",
                       summary="项目对接人", aliases=["wlei2"],
                       main_md="# 王磊\n\n公司同事, 上海, 做产品")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="same name", ops=[
                    mp.CuratorPlannerOp(
                        op="merge", target_slot_id="wang_lei_a",
                        source_slot_ids=["wang_lei_b"],
                        reason="两个 slot title 都叫王磊, 可能同一人")])

            def fake_merge(**kw):
                return mp.MergeExecutorOutput(
                    should_skip=True,
                    skip_reason="body 显示分别是高中同学(物理)和公司同事(产品), 不同人",
                    self_check="not same object",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_merge", side_effect=fake_merge):
                result = curator.run_domain("person")

            assert result.get("rejected") == 1, f"should reject, got {result}"
            assert memory_router.get_slot("person", "wang_lei_a") is not None
            assert memory_router.get_slot("person", "wang_lei_b") is not None
            print(f"  [PASS] both slots intact after self-check rejection")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t3_edit_self_check_skip():
    """Executor reads body and decides Planner's diagnosis is wrong → skip."""
    print("\n" + "=" * 60)
    print("T3: Edit self-check rejects wrong diagnosis")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "miru", title="Miru AI 陪伴",
                       summary="ContextLife 项目 Miru 主框架",
                       aliases=["miru", "contextlife", "airi"],
                       main_md="# Miru AI 陪伴\n\nContextLife 框架内的 Miru AI 陪伴产品")
            _seed_slot("project", "f1")
            _seed_slot("project", "f2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="title 不准", ops=[
                    mp.CuratorPlannerOp(op="edit", slot_id="miru",
                                        reason="title 偏离 body, 应该改为别的")])

            def fake_edit(**kw):
                return mp.EditExecutorOutput(
                    should_skip=True,
                    skip_reason="读 body 后发现 title 和 body 完全一致, Planner 误判",
                    self_check="diagnosis 不成立",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_edit", side_effect=fake_edit):
                result = curator.run_domain("project")

            assert result.get("rejected") == 1, f"should reject, got {result}"
            slot = memory_router.get_slot("project", "miru")
            assert slot["title"] == "Miru AI 陪伴", f"title should be unchanged, got {slot['title']}"
            print(f"  [PASS] title unchanged after self-check rejection")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t4_edit_meta_only_no_body_change():
    """Edit with empty new_body keeps existing body intact."""
    print("\n" + "=" * 60)
    print("T4: Edit meta-only (new_body='') preserves body")
    print("=" * 60)
    import curator
    import memory_router
    import memory
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            original_body = "项目的具体描述, 多行内容, 应该被保留下来"
            _seed_slot("project", "needs_summary_fix", title="项目1",
                       summary="项目1 stuff", aliases=["proj1", "p1", "p1x"],
                       main_md=f"# 项目1\n\n{original_body}")
            _seed_slot("project", "f1")
            _seed_slot("project", "f2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="summary 太宽", ops=[
                    mp.CuratorPlannerOp(op="edit", slot_id="needs_summary_fix",
                                        reason="summary 太宽不准, 应该具体到 body 主题")])

            def fake_edit(**kw):
                return mp.EditExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="needs_summary_fix", title="项目1",
                        summary="具体到 body 主题的更准 summary",
                        aliases=["proj1", "p1", "p1x"]),
                    new_body="",  # body 不改
                    self_check="只调 summary, body 保持",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_edit", side_effect=fake_edit):
                curator.run_domain("project")

            md = memory.read_file("projects/needs_summary_fix/main.md") or ""
            assert original_body in md, f"original body should be preserved, md={md!r}"
            slot = memory_router.get_slot("project", "needs_summary_fix")
            assert "具体到" in slot["summary"], f"summary should be updated, got {slot['summary']}"
            print(f"  [PASS] meta updated, body preserved")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t5_edit_id_change_guard():
    """If Executor outputs a different id, code-layer guard ignores it."""
    print("\n" + "=" * 60)
    print("T5: Edit id-change guard")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "original_id", title="原 title",
                       summary="x", aliases=["orig", "o1", "o2"])
            _seed_slot("project", "f1")
            _seed_slot("project", "f2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="title 不准", ops=[
                    mp.CuratorPlannerOp(op="edit", slot_id="original_id",
                                        reason="title 应该更具体")])

            def fake_edit(**kw):
                # Executor incorrectly outputs new id — code layer must protect
                return mp.EditExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="rogue_new_id",  # ⚠️ wrong id
                        title="新 title", summary="新 summary",
                        aliases=["a", "b", "c"]),
                    new_body="",
                    self_check="...",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_edit", side_effect=fake_edit):
                curator.run_domain("project")

            # Original id should still exist; rogue id should not
            assert memory_router.get_slot("project", "original_id") is not None
            assert memory_router.get_slot("project", "rogue_new_id") is None
            slot = memory_router.get_slot("project", "original_id")
            assert slot["title"] == "新 title", f"title should be updated, got {slot['title']}"
            print(f"  [PASS] id change ignored, title still updated")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t6_delete_skip_on_stale_diagnosis():
    """Executor sees Planner's reason is '过时' style and skips."""
    print("\n" + "=" * 60)
    print("T6: Delete skip on 'stale' diagnosis")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("topic", "old_topic", summary="一个有价值但旧的话题")
            _seed_slot("topic", "f1")
            _seed_slot("topic", "f2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="清理旧的", ops=[
                    mp.CuratorPlannerOp(op="delete", slot_id="old_topic",
                                        reason="最近没动了, 看起来过时, 删除")])

            def fake_delete(**kw):
                # Executor recognizes "过时" doesn't match delete's purpose
                return mp.DeleteExecutorOutput(
                    should_skip=True,
                    skip_reason="Planner 的理由是'过时', 这是归档的工作不是 delete 的工作",
                    self_check="拒绝执行: 非纠错场景",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_delete", side_effect=fake_delete):
                result = curator.run_domain("topic")

            assert result.get("rejected") == 1
            assert memory_router.get_slot("topic", "old_topic") is not None
            print(f"  [PASS] old_topic preserved despite Planner request")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t7_delete_executes_on_misclassification():
    """Executor confirms domain misclassification → delete proceeds."""
    print("\n" + "=" * 60)
    print("T7: Delete executes on misclassification (type A)")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("topic", "misplaced_person",
                       title="王老师",
                       summary="李垦的高中数学老师",
                       aliases=["wang_laoshi"],
                       main_md="# 王老师\n\n李垦的高中数学老师, 性格严肃, 现在退休")
            _seed_slot("topic", "real_topic1")
            _seed_slot("topic", "real_topic2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="错放", ops=[
                    mp.CuratorPlannerOp(
                        op="delete", slot_id="misplaced_person",
                        reason="title/summary/body 都是描述具体的人, 应该是 person 域 slot, 但在 topic 域")])

            def fake_delete(**kw):
                return mp.DeleteExecutorOutput(
                    should_skip=False,
                    self_check="类型 A 分类错放: body 主体是具体的人(数学老师), 当前 domain 是 topic, 不一致",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_delete", side_effect=fake_delete):
                result = curator.run_domain("topic")

            assert result.get("executed") == 1, f"should execute, got {result}"
            assert memory_router.get_slot("topic", "misplaced_person") is None
            print(f"  [PASS] misclassified slot removed for re-routing")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================================

def test_t8_person_domain_multi_name_merge():
    """Three slots, all describing one person under different names —
    Executor consolidates into one with combined aliases."""
    print("\n" + "=" * 60)
    print("T8: Person domain multi-name same-person merge")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("person", "weijiazhe", title="魏佳哲",
                       summary="ML 同学", aliases=["wjz"],
                       main_md="# 魏佳哲\n\n李垦的高中同学,  现 ML 研究员")
            _seed_slot("person", "void_handle", title="void",
                       summary="GitHub 上常见的 ID", aliases=["void", "void@github"],
                       main_md="# void\n\nGitHub username, 实名 weijiazhe, 同年级 ML 同学")
            _seed_slot("person", "xiao_nan_liang", title="小南梁",
                       summary="朋友圈昵称", aliases=["xiao_nan_liang"],
                       main_md="# 小南梁\n\n朋友圈昵称, 真实姓名 魏佳哲")

            def fake_planner(**kw):
                # Planner sees aliases overlap + same-person hints in summaries
                return mp.CuratorPlannerOutput(
                    reasoning="三个 slot 都指向魏佳哲 (本名/GitHub/朋友昵称)",
                    ops=[mp.CuratorPlannerOp(
                        op="merge", target_slot_id="weijiazhe",
                        source_slot_ids=["void_handle", "xiao_nan_liang"],
                        reason="三个 slot body 都标识同一人魏佳哲 (本名/平台名/朋友昵称)")])

            def fake_merge(**kw):
                # Executor reads all bodies, consolidates aliases
                return mp.MergeExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="weijiazhe", title="魏佳哲",
                        summary="高中同学, ML 研究员, 多名 (void/小南梁)",
                        aliases=["wjz", "void", "void@github", "小南梁", "jiazhe", "weijiazhe"]),
                    merged_body="魏佳哲, 高中同学, ML 研究员. 多个身份: GitHub void, 朋友圈小南梁",
                    self_check="三处 body 互相印证同一人",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_merge", side_effect=fake_merge):
                result = curator.run_domain("person")

            assert result.get("executed") == 1
            slots = memory_router.load_all_slots("person")
            ids = [s["id"] for s in slots]
            assert "weijiazhe" in ids
            assert "void_handle" not in ids
            assert "xiao_nan_liang" not in ids
            slot = memory_router.get_slot("person", "weijiazhe")
            assert "void" in slot["aliases"]
            assert "小南梁" in slot["aliases"]
            print(f"  [PASS] 3 slots → 1, aliases={slot['aliases']}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_t1_merge_aliases_filtered()
    test_t2_merge_self_check_skip()
    test_t3_edit_self_check_skip()
    test_t4_edit_meta_only_no_body_change()
    test_t5_edit_id_change_guard()
    test_t6_delete_skip_on_stale_diagnosis()
    test_t7_delete_executes_on_misclassification()
    test_t8_person_domain_multi_name_merge()
    print("\n" + "=" * 60)
    print("ALL 8 v3.5 TARGETED TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
