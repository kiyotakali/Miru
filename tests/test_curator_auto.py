"""Test the auto-cleanup Curator (v3.5 2026-05-14).

Architecture under test (Planner + 3 self-checking Executors):
  Layer 1 Planner   → ≤3 op/domain (merge / delete / edit) + reason
                      (no new_slot_meta — Planner only sees summaries)
  Layer 2 Executors → each op = 1 LLM that both re-checks Planner's
                      diagnosis against full main.md AND produces the
                      result. should_skip=true means复核 failed, no change.
                        merge  → new_slot_meta + merged_body
                        delete → decision only; caller archives
                        edit   → new_slot_meta (+ optional new_body)

This file mocks all 4 LLM call points so we can verify orchestration
without spending tokens. End-to-end with real LLM is in a manual test.

Coverage:
   1. Schema sanity (CuratorPlannerOp / 3 Executor outputs)
   2. Trigger conditions: cooldown + change threshold + per-domain isolation
   3. memory_router writes (upsert/delete/update_metadata) fire track_slot_change
   4. run_domain MERGE happy: planner → executor passes self-check → slot rewritten
   5. run_domain MERGE skipped: executor should_skip=true → slots intact
   6. run_domain DELETE: executor skips one, executes another
   7. run_domain EDIT happy: title/summary updated, aliases overwritten
   8. Pinned slot invisible to planner AND immune to executor
   9. self/identity invisible to planner AND immune to executor
  10. run_cycle resets counter for triggered domains, sets last_run_ts
  11. last_active < 10min slot excluded from planner input
"""
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    return tempfile.mkdtemp(prefix="curator_auto_test_")


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
    g.user_id = "test_user"
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


def _mk_meta(slot_id="paper", title="Lambda 论文", summary="LC 论文索引",
             icon="📄", aliases=None):
    """Build Curator's NewSlotMeta with reasonable defaults.
    aliases must be 1-8 items per NewSlotMeta.aliases_count validator.

    Use v2._NewSlotMeta (Curator's local copy) — the v3.NewSlotMeta in
    memory_prompts_v3 is for chat Slot Writer; Curator has its own copy
    to break the circular import between v2 ↔ v3."""
    from memory_prompts_v2 import _NewSlotMeta
    if aliases is None:
        aliases = ["a1", "a2", "a3"]
    return _NewSlotMeta(
        id=slot_id, title=title, icon=icon,
        summary=summary, aliases=aliases,
    )


# ============================================================================

def test_schemas():
    print("=" * 60)
    print("TEST: Pydantic schemas (v3.5)")
    print("=" * 60)
    from memory_prompts_v2 import (
        CuratorPlannerOp, CuratorPlannerOutput,
        MergeExecutorOutput, EditExecutorOutput, DeleteExecutorOutput,
    )
    passed, total = 0, 0

    cases = [
        ("valid merge", lambda: CuratorPlannerOp(
            op="merge", target_slot_id="b", source_slot_ids=["a"],
            reason="same paper across versions"), True),
        ("valid delete", lambda: CuratorPlannerOp(
            op="delete", slot_id="x", reason="empty noise content"), True),
        ("valid edit", lambda: CuratorPlannerOp(
            op="edit", slot_id="x", reason="title偏离 body 主题"), True),
        ("merge w/o target", lambda: CuratorPlannerOp(
            op="merge", source_slot_ids=["a"], reason="ok ok ok ok"), False),
        ("merge w/o sources", lambda: CuratorPlannerOp(
            op="merge", target_slot_id="b", reason="ok ok ok ok"), False),
        ("delete w/o slot_id", lambda: CuratorPlannerOp(
            op="delete", reason="ok ok ok ok"), False),
        ("edit w/o slot_id", lambda: CuratorPlannerOp(
            op="edit", reason="ok ok ok ok"), False),
        (">3 ops", lambda: CuratorPlannerOutput(reasoning="x", ops=[
            CuratorPlannerOp(op="delete", slot_id="a", reason="r1 ok"),
            CuratorPlannerOp(op="delete", slot_id="b", reason="r2 ok"),
            CuratorPlannerOp(op="delete", slot_id="c", reason="r3 ok"),
            CuratorPlannerOp(op="delete", slot_id="d", reason="r4 ok"),
        ]), False),
        ("short reason", lambda: CuratorPlannerOp(
            op="delete", slot_id="x", reason="no"), False),
        ("self-merge", lambda: CuratorPlannerOp(
            op="merge", target_slot_id="a", source_slot_ids=["a"],
            reason="same id is invalid"), False),
        ("merge executor should_skip ok", lambda: MergeExecutorOutput(
            should_skip=True, skip_reason="not same object"), True),
        ("merge executor missing meta", lambda: MergeExecutorOutput(
            should_skip=False, merged_body="x"), False),
        ("merge executor full ok", lambda: MergeExecutorOutput(
            should_skip=False, new_slot_meta=_mk_meta(),
            merged_body="a long enough body"), True),
        ("edit executor should_skip needs reason", lambda: EditExecutorOutput(
            should_skip=True), False),
        ("edit executor full ok", lambda: EditExecutorOutput(
            should_skip=False, new_slot_meta=_mk_meta()), True),
        ("delete executor should_skip needs reason", lambda: DeleteExecutorOutput(
            should_skip=True), False),
        ("delete executor proceed ok", lambda: DeleteExecutorOutput(
            should_skip=False, self_check="type B noise"), True),
    ]
    for label, builder, should_pass in cases:
        total += 1
        try:
            builder()
            ok = should_pass
        except Exception:
            ok = not should_pass
        if ok:
            print(f"  [PASS] {label}")
            passed += 1
        else:
            print(f"  [FAIL] {label}")

    print(f"\n  Schemas: {passed}/{total} passed")
    assert passed == total, f"schemas {passed}/{total}"
    return passed, total


def test_curator_meta_normalizes_recoverable_llm_shape():
    """Recover usable Curator metadata instead of spending retries.

    Production merge executor outputs can be semantically fine but slightly too
    long in summary or too generous in aliases. Those should be normalized
    deterministically before schema validation.
    """
    from memory_prompts_v2 import _NewSlotMeta

    meta = _NewSlotMeta(
        id="agent_project",
        title="Agent 项目",
        icon="🧠",
        summary="x" * 201,
        aliases=[
            "agent 项目", "AI agent", "Miru", "ContextLife",
            "AttentionEngine", "多端同步", "Docker", "成本优化",
            "多设备支持", "agent 项目", "",
        ],
    )

    assert len(meta.summary) == 200
    assert meta.summary.endswith("…")
    assert meta.aliases == [
        "agent 项目", "AI agent", "Miru", "ContextLife",
        "AttentionEngine", "多端同步", "Docker", "成本优化",
    ]


# ============================================================================

def test_trigger_conditions():
    print("\n" + "=" * 60)
    print("TEST: Trigger — cooldown + change threshold + isolation")
    print("=" * 60)
    import curator
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            # Mark a fresh last_run_ts so the soft trigger (counter≥1 + age≥60min)
            # doesn't fire on "never run" state. Isolates the normal-trigger
            # logic (counter ≥ threshold AND age ≥ cooldown).
            curator._mark_run("project")
            curator.track_slot_change("project")
            curator.track_slot_change("project")
            meta = curator._curator_load_meta()
            total += 1
            if not curator._should_run("project", meta):
                print("  [PASS] counter=2 blocks run (under threshold)")
                passed += 1
            else:
                print(f"  [FAIL] counter=2 should block, meta={meta}")

            curator.track_slot_change("project")
            # Backdate last_run_ts to clear the 1h cooldown so normal trigger fires.
            with curator._curator_meta_lock:
                m = curator._curator_load_meta()
                m["last_run_ts"]["project"] = (
                    datetime.now() - timedelta(minutes=65)
                ).isoformat(timespec="seconds")
                curator._curator_save_meta(m)
            meta = curator._curator_load_meta()
            total += 1
            if curator._should_run("project", meta):
                print("  [PASS] counter=3 + age>1h → run")
                passed += 1
            else:
                print(f"  [FAIL] meta={meta}")

            curator._mark_run("project")
            curator.track_slot_change("project")
            curator.track_slot_change("project")
            curator.track_slot_change("project")
            meta = curator._curator_load_meta()
            total += 1
            if not curator._should_run("project", meta):
                print("  [PASS] just-ran, cooldown blocks")
                passed += 1
            else:
                print(f"  [FAIL] cooldown failed, meta={meta}")

            # Cross-domain isolation
            with open(curator._curator_meta_path()) as f:
                m = json.load(f)
            m["change_counter"] = {"project": 5, "person": 0, "topic": 0, "self": 0}
            m["last_run_ts"] = {"project": "", "person": "", "topic": "", "self": ""}
            with open(curator._curator_meta_path(), "w") as f:
                json.dump(m, f)
            meta = curator._curator_load_meta()
            total += 1
            if curator._should_run("project", meta) and not curator._should_run("person", meta):
                print("  [PASS] domain isolation")
                passed += 1
            else:
                print(f"  [FAIL] meta={meta}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Trigger: {passed}/{total} passed")
    assert passed == total, f"trigger {passed}/{total}"
    return passed, total


# ============================================================================

def test_router_writes_fire_hook():
    print("\n" + "=" * 60)
    print("TEST: memory_router writes fire track_slot_change")
    print("=" * 60)
    import curator
    import memory_router
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "p1")
            meta = curator._curator_load_meta()
            total += 1
            if meta["change_counter"]["project"] >= 1:
                print(f"  [PASS] upsert_slot bumped counter (={meta['change_counter']['project']})")
                passed += 1
            else:
                print(f"  [FAIL] counter still 0")

            before = curator._curator_load_meta()["change_counter"]["project"]
            memory_router.update_slot_metadata("project", "p1", {"summary": "new"})
            after = curator._curator_load_meta()["change_counter"]["project"]
            total += 1
            if after > before:
                print(f"  [PASS] update_slot_metadata bumped ({before} → {after})")
                passed += 1
            else:
                print(f"  [FAIL] no bump ({before} → {after})")

            before = curator._curator_load_meta()["change_counter"]["project"]
            memory_router.delete_slot("project", "p1")
            after = curator._curator_load_meta()["change_counter"]["project"]
            total += 1
            if after > before:
                print(f"  [PASS] delete_slot bumped ({before} → {after})")
                passed += 1
            else:
                print(f"  [FAIL] no bump")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Hooks: {passed}/{total} passed")
    assert passed == total, f"hooks {passed}/{total}"
    return passed, total


# ============================================================================

def test_run_domain_merge_happy():
    print("\n" + "=" * 60)
    print("TEST: run_domain merge happy path (v3.5)")
    print("=" * 60)
    import curator
    import memory_router
    import memory
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "paper", title="Lambda 论文",
                       summary="LC 论文",
                       main_md="# Lambda 论文\n\n研究 LC", aliases=["lambda paper"])
            _seed_slot("project", "paper_v2", title="Lambda v2",
                       summary="同一篇 v2",
                       main_md="# Lambda v2\n\nv2 草稿", aliases=["lambda v2"])
            _seed_slot("project", "different_thing", title="另一项目",
                       summary="不相关")
            append_ts = datetime.now().isoformat(timespec="seconds")
            memory_router._record_daily_slot_append(
                domain="project",
                slot_id="paper_v2",
                main_file=memory_router._slot_main_file_rel("project", "paper_v2"),
                slot_title="Lambda v2",
                slot_summary="同一篇 v2",
                append_entry="paper_v2 里有一条尚未整理的追加记录。",
                entry_kind="progress",
                current_time=append_ts,
            )

            def fake_planner(*, domain, slots, current_time="", **_):
                return mp.CuratorPlannerOutput(
                    reasoning="paper 和 paper_v2 是同一篇论文",
                    ops=[mp.CuratorPlannerOp(
                        op="merge",
                        target_slot_id="paper",
                        source_slot_ids=["paper_v2"],
                        reason="paper_v2 是 paper 的修订版同篇论文 (body 主题一致)")])

            def fake_execute_merge(**kw):
                return mp.MergeExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="paper", title="Lambda 论文 (合并)",
                        summary="LC 论文与 v2 修订版的合并",
                        aliases=["lambda", "lambda paper", "lambda v2"]),
                    merged_body="merged paper + paper_v2 -- 研究 LC 含 v2 修订",
                    self_check="body 一致 ✓",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_merge", side_effect=fake_execute_merge):
                result = curator.run_domain("project")

            total += 1
            if result.get("executed") == 1:
                print(f"  [PASS] executed=1")
                passed += 1
            else:
                print(f"  [FAIL] {result}")

            ids = [s["id"] for s in memory_router.load_all_slots("project")]
            total += 1
            if "paper" in ids and "paper_v2" not in ids and "different_thing" in ids:
                print(f"  [PASS] paper kept, paper_v2 deleted, different_thing untouched")
                passed += 1
            else:
                print(f"  [FAIL] slots={ids}")

            md = memory.read_file("projects/paper/main.md") or ""
            total += 1
            if "merged" in md and "v2" in md:
                print(f"  [PASS] merged body has both sources")
                passed += 1
            else:
                print(f"  [FAIL] md={md!r}")

            slot = memory_router.get_slot("project", "paper")
            total += 1
            if slot and "合并" in slot.get("title", ""):
                print(f"  [PASS] slot title updated to merged title")
                passed += 1
            else:
                print(f"  [FAIL] {slot}")

            ledger = memory_router.list_daily_slot_appends(append_ts[:10])
            terminal_entry = ledger["project/paper_v2"]["entries"][0]
            total += 1
            if (
                terminal_entry.get("terminal") is True
                and terminal_entry.get("terminal_reason") == "merged_into"
                and terminal_entry.get("terminal_target_slot_id") == "paper"
            ):
                print(f"  [PASS] source pending append terminalized as merged_into")
                passed += 1
            else:
                print(f"  [FAIL] ledger={terminal_entry}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  MergeHappy: {passed}/{total} passed")
    assert passed == total, f"merge happy {passed}/{total}"
    return passed, total


# ============================================================================

def test_run_domain_merge_skipped():
    """Executor self-check returns should_skip=true → no slot changes."""
    print("\n" + "=" * 60)
    print("TEST: run_domain merge SKIPPED by executor self-check")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "p1")
            _seed_slot("project", "p2")
            _seed_slot("project", "p3")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="x", ops=[
                    mp.CuratorPlannerOp(
                        op="merge", target_slot_id="p2", source_slot_ids=["p1"],
                        reason="planner 觉得 p1 p2 相同 maybe")])

            def fake_execute_merge(**kw):
                return mp.MergeExecutorOutput(
                    should_skip=True,
                    skip_reason="读完 body 后发现是两个不同对象, 不该合并",
                    self_check="not same object",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_merge", side_effect=fake_execute_merge):
                result = curator.run_domain("project")

            total += 1
            if result.get("rejected") == 1 and result.get("executed") == 0:
                print(f"  [PASS] result rejected=1, executed=0")
                passed += 1
            else:
                print(f"  [FAIL] {result}")

            ids = [s["id"] for s in memory_router.load_all_slots("project")]
            total += 1
            if all(x in ids for x in ("p1", "p2", "p3")):
                print(f"  [PASS] all 3 slots intact")
                passed += 1
            else:
                print(f"  [FAIL] slots={ids}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  MergeSkipped: {passed}/{total} passed")
    assert passed == total, f"merge skipped {passed}/{total}"
    return passed, total


# ============================================================================

def test_run_domain_delete_mixed():
    """Executor approves one delete (junk), skips another (valuable)."""
    print("\n" + "=" * 60)
    print("TEST: run_domain delete (one approved, one self-check skipped)")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("topic", "junk", summary="一次性垃圾")
            _seed_slot("topic", "valuable", summary="重要长期目标")
            _seed_slot("topic", "filler", summary="filler")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="清理 + 误识", ops=[
                    mp.CuratorPlannerOp(op="delete", slot_id="junk",
                                        reason="一次性娱乐内容无价值"),
                    mp.CuratorPlannerOp(op="delete", slot_id="valuable",
                                        reason="planner 误判要删有价值的"),
                ])

            def fake_execute_delete(**kw):
                if kw["slot"]["id"] == "junk":
                    return mp.DeleteExecutorOutput(
                        should_skip=False, self_check="类型 B 真无价值")
                return mp.DeleteExecutorOutput(
                    should_skip=True,
                    skip_reason="body 含承诺, 不能删", self_check="拒绝执行")

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_delete",
                              side_effect=fake_execute_delete):
                result = curator.run_domain("topic")

            ids = [s["id"] for s in memory_router.load_all_slots("topic")]
            total += 1
            if "junk" not in ids and "valuable" in ids and "filler" in ids:
                print(f"  [PASS] junk gone, valuable kept by self-check")
                passed += 1
            else:
                print(f"  [FAIL] slots={ids}")

            total += 1
            if result.get("executed") == 1 and result.get("rejected") == 1:
                print(f"  [PASS] executed=1, rejected=1")
                passed += 1
            else:
                print(f"  [FAIL] {result}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Delete: {passed}/{total} passed")
    assert passed == total, f"delete {passed}/{total}"
    return passed, total


# ============================================================================

def test_run_domain_edit():
    print("\n" + "=" * 60)
    print("TEST: run_domain edit happy path (v3.5)")
    print("=" * 60)
    import curator
    import memory_router
    import memory
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "broad", title="项目", summary="过宽",
                       main_md="# 项目\n\n实际是 ContextLife")
            _seed_slot("project", "f1")
            _seed_slot("project", "f2")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="broad 太泛", ops=[
                    mp.CuratorPlannerOp(op="edit", slot_id="broad",
                                        reason="title 应改为 ContextLife 项目, 更具体")])

            def fake_execute_edit(**kw):
                # body retained (new_body empty)
                return mp.EditExecutorOutput(
                    should_skip=False,
                    new_slot_meta=_mk_meta(
                        slot_id="broad", title="ContextLife 项目",
                        summary="Miru 后端框架 ContextLife",
                        aliases=["broad", "miru-backend", "contextlife"]),
                    new_body="",
                    self_check="title 偏离 body 主题, 修正",
                )

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner), \
                 patch.object(curator, "call_curator_execute_edit",
                              side_effect=fake_execute_edit):
                result = curator.run_domain("project")

            total += 1
            if result.get("executed") == 1:
                print(f"  [PASS] executed=1")
                passed += 1
            else:
                print(f"  [FAIL] {result}")

            slot = memory_router.get_slot("project", "broad")
            total += 1
            if slot and slot["title"] == "ContextLife 项目":
                print(f"  [PASS] title updated")
                passed += 1
            else:
                print(f"  [FAIL] {slot}")

            total += 1
            if slot and "miru-backend" in (slot.get("aliases") or []):
                print(f"  [PASS] aliases overwritten with executor values")
                passed += 1
            else:
                print(f"  [FAIL] aliases={slot.get('aliases') if slot else None}")

            md = memory.read_file("projects/broad/main.md") or ""
            total += 1
            if "# ContextLife 项目" in md and "ContextLife" in md:
                print(f"  [PASS] main.md heading rewritten, body preserved")
                passed += 1
            else:
                print(f"  [FAIL] md={md!r}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Edit: {passed}/{total} passed")
    assert passed == total, f"edit {passed}/{total}"
    return passed, total


# ============================================================================

def test_pinned_slot_invisible_and_immune():
    print("\n" + "=" * 60)
    print("TEST: pinned slot — invisible to planner + immune to executor")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "pinned_paper", pinned=True,
                       title="重要项目", summary="pinned by user")
            _seed_slot("project", "f1")
            _seed_slot("project", "f2")

            seen = []

            def planner_seer(*, domain, slots, current_time="", **_):
                seen.append([s["id"] for s in slots])
                return mp.CuratorPlannerOutput(reasoning="x", ops=[])

            with patch.object(curator, "call_curator_planner", side_effect=planner_seer):
                curator.run_domain("project")

            total += 1
            if seen and "pinned_paper" not in seen[0]:
                print(f"  [PASS] planner did NOT see pinned ({seen[0]})")
                passed += 1
            else:
                print(f"  [FAIL] planner saw {seen}")

            # Even if planner returns delete on pinned, executor must skip
            def evil_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="x", ops=[
                    mp.CuratorPlannerOp(op="delete", slot_id="pinned_paper",
                                        reason="planner ignores pinned somehow")])

            # Executor approves (would-be malicious) — code-layer guard must
            # block before LLM even runs.
            def fake_execute_delete(**kw):
                return mp.DeleteExecutorOutput(
                    should_skip=False, self_check="should not reach")

            with patch.object(curator, "call_curator_planner", side_effect=evil_planner), \
                 patch.object(curator, "call_curator_execute_delete",
                              side_effect=fake_execute_delete):
                curator.run_domain("project")

            total += 1
            if memory_router.get_slot("project", "pinned_paper") is not None:
                print(f"  [PASS] pinned survives malicious delete")
                passed += 1
            else:
                print(f"  [FAIL] pinned was deleted")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  PinnedProtect: {passed}/{total} passed")
    assert passed == total, f"pinned protect {passed}/{total}"
    return passed, total


# ============================================================================

def test_identity_invisible_and_immune():
    print("\n" + "=" * 60)
    print("TEST: self/identity — invisible to planner + immune to executor")
    print("=" * 60)
    import curator
    import memory_router
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("self", "identity", title="用户身份")
            _seed_slot("self", "preferences")
            _seed_slot("self", "habit")

            seen = []

            def planner_seer(*, domain, slots, current_time="", **_):
                seen.append([s["id"] for s in slots])
                return mp.CuratorPlannerOutput(reasoning="x", ops=[])

            with patch.object(curator, "call_curator_planner", side_effect=planner_seer):
                curator.run_domain("self")

            total += 1
            if seen and "identity" not in seen[0]:
                print(f"  [PASS] planner did NOT see identity ({seen[0]})")
                passed += 1
            else:
                print(f"  [FAIL] planner saw {seen}")

            def evil_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="x", ops=[
                    mp.CuratorPlannerOp(op="delete", slot_id="identity",
                                        reason="planner gone wild somehow")])

            def fake_execute_delete(**kw):
                return mp.DeleteExecutorOutput(
                    should_skip=False, self_check="should not reach")

            with patch.object(curator, "call_curator_planner", side_effect=evil_planner), \
                 patch.object(curator, "call_curator_execute_delete",
                              side_effect=fake_execute_delete):
                curator.run_domain("self")

            total += 1
            if memory_router.get_slot("self", "identity") is not None:
                print(f"  [PASS] identity survives malicious action")
                passed += 1
            else:
                print(f"  [FAIL] identity was deleted!")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  IdentityProtect: {passed}/{total} passed")
    assert passed == total, f"identity protect {passed}/{total}"
    return passed, total


# ============================================================================

def test_run_cycle_resets_counter():
    print("\n" + "=" * 60)
    print("TEST: run_cycle resets counter for triggered domains")
    print("=" * 60)
    import curator
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "p1")
            _seed_slot("project", "p2")
            _seed_slot("project", "p3")

            for _ in range(5):
                curator.track_slot_change("project")

            def fake_planner(**kw):
                return mp.CuratorPlannerOutput(reasoning="no op", ops=[])

            with patch.object(curator, "call_curator_planner", side_effect=fake_planner):
                results = curator.run_cycle()

            total += 1
            if "project" in results:
                print(f"  [PASS] project ran")
                passed += 1
            else:
                print(f"  [FAIL] {results}")

            meta = curator._curator_load_meta()
            total += 1
            if meta["change_counter"]["project"] == 0:
                print(f"  [PASS] counter reset")
                passed += 1
            else:
                print(f"  [FAIL] counter still {meta['change_counter']['project']}")

            total += 1
            if meta["last_run_ts"]["project"]:
                print(f"  [PASS] last_run_ts set")
                passed += 1
            else:
                print(f"  [FAIL] empty")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  RunCycleReset: {passed}/{total} passed")
    assert passed == total, f"run cycle reset {passed}/{total}"
    return passed, total


# ============================================================================

def test_recent_slot_excluded():
    print("\n" + "=" * 60)
    print("TEST: last_active < 10min slot excluded from planner")
    print("=" * 60)
    import curator
    import memory_prompts_v2 as mp
    tmp = _setup_user_dir()
    passed, total = 0, 0

    try:
        ctx = _push_g(tmp)
        try:
            old = (datetime.now() - timedelta(hours=5)).isoformat(timespec="seconds")
            recent = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
            # Distinct titles to avoid the collision-override path
            # (same-normalized-title bypasses the cooldown filter).
            _seed_slot("project", "old1", title="alpha project",
                       last_active=old, aliases=["alpha"])
            _seed_slot("project", "old2", title="beta project",
                       last_active=old, aliases=["beta"])
            _seed_slot("project", "fresh", title="gamma project",
                       last_active=recent, aliases=["gamma"])

            seen = []

            def planner_seer(*, domain, slots, current_time="", **_):
                seen.append([s["id"] for s in slots])
                return mp.CuratorPlannerOutput(reasoning="x", ops=[])

            with patch.object(curator, "call_curator_planner", side_effect=planner_seer):
                curator.run_domain("project")

            total += 1
            if seen and "fresh" not in seen[0] and "old1" in seen[0]:
                print(f"  [PASS] only old slots seen ({seen[0]})")
                passed += 1
            else:
                print(f"  [FAIL] {seen}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  RecentExclude: {passed}/{total} passed")
    assert passed == total, f"recent exclude {passed}/{total}"
    return passed, total


def main():
    results = []
    results.append(test_schemas())
    results.append(test_trigger_conditions())
    results.append(test_router_writes_fire_hook())
    results.append(test_run_domain_merge_happy())
    results.append(test_run_domain_merge_skipped())
    results.append(test_run_domain_delete_mixed())
    results.append(test_run_domain_edit())
    results.append(test_pinned_slot_invisible_and_immune())
    results.append(test_identity_invisible_and_immune())
    results.append(test_run_cycle_resets_counter())
    results.append(test_recent_slot_excluded())

    total_passed = sum(r[0] for r in results)
    total_tests = sum(r[1] for r in results)
    print("\n" + "=" * 60)
    print(f"TOTAL: {total_passed}/{total_tests}")
    print("=" * 60)
    return 0 if total_passed == total_tests else 1


if __name__ == "__main__":
    sys.exit(main())
