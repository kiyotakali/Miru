"""Unit tests for memory_router.route_with_slot_write (Sleep Agent v3 B2).

Verifies that a Slot Writer v3 output flows through to Pass 4 + slot
storage correctly. We mock Pass 4 helpers to avoid real LLM calls.
"""

import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import memory  # noqa: E402
import memory_router  # noqa: E402
from memory_prompts_v3 import (  # noqa: E402
    NewSlotMeta,
    IntegrationSpec,
    SlotWrite,
)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _push_g(tmp):
    """Set up Flask context bound to tmp dir."""
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = "test_user"
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def _seed_slot(domain, slot_id, *, title="测试", summary="测试 slot",
                aliases=None, main_md=None):
    slot = {
        "id": slot_id, "title": title, "icon": "📄",
        "status": "active", "pinned": False,
        "summary": summary, "aliases": aliases or [slot_id, title],
        "main_file": memory_router._slot_main_file_rel(domain, slot_id),
        "last_active": _now_iso(),
        "created": _now_iso(),
    }
    memory_router.upsert_slot(domain, slot)
    if main_md is None:
        main_md = f"# {title}\n\n{summary}\n"
    memory.write_file(slot["main_file"], main_md)
    return slot


def _append_result(entry="记录了一条新的记忆。", *, title_update=None,
                   summary_update=None, entry_kind="progress"):
    return {
        "should_append": True,
        "title_update": title_update,
        "summary_update": summary_update,
        "append_entry": entry,
        "entry_kind": entry_kind,
        "skip_reason": "",
        "alias_additions": [],
    }


# ─────────────────────────────────────────────────────────────────────
# match path
# ─────────────────────────────────────────────────────────────────────

def test_route_with_slot_write_match_calls_pass4_append_for_chat():
    tmp = tempfile.mkdtemp(prefix="route_slot_write_match_")
    ctx = _push_g(tmp)
    try:
        _seed_slot("project", "papers_2026",
                    title="2026 论文投稿",
                    summary="CVPR/ECCV/PRCV 三篇并行",
                    aliases=["论文", "papers"])

        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦完成 ECCV multi-view ablation 实验, 效果不错.",
            ),
        )

        with patch.object(memory_router, "call_legacy_slot_merge_rewrite") as edit_mock, \
                patch.object(memory_router, "call_pass4_append_slot",
                             return_value=_append_result(
                                 "完成 ECCV multi-view ablation 实验, 效果不错.",
                                 summary_update="ECCV multi-view 已 ablation 完成",
                             )) as mocked:
            result = memory_router.route_with_slot_write(
                sw, source_context="[14:23] 李垦: 实验跑完了")
        assert result["ok"], result
        assert result["action"] == "matched"
        assert result["slot_id"] == "papers_2026"
        assert mocked.called
        assert not edit_mock.called

        # Pass 4 received correct inputs
        call_kwargs = mocked.call_args.kwargs
        assert call_kwargs["domain"] == "project"
        assert call_kwargs["current_title"] == "2026 论文投稿"
        assert "multi-view ablation" in call_kwargs["new_content"]
        assert call_kwargs["source_type"] == "chat"
        # 2026-05-16: is_subtopic 参数已完全移除. Slot Writer 只做 new/match/skip,
        # Pass 4 自决章节 — 这个 assert 保留作为反退化标记 (kwargs 里不应有它)
        assert "is_subtopic" not in call_kwargs

        # Slot meta updated
        updated = memory_router.get_slot("project", "papers_2026")
        assert updated["summary"] == "ECCV multi-view 已 ablation 完成"
        assert updated["append_count_since_compact"] == 1
        assert updated["append_dirty_since"]
        body = memory.read_file(updated["main_file"])
        assert "## 近期记录" in body
        assert "完成 ECCV multi-view ablation" in body
    finally:
        ctx.pop()


def test_route_with_slot_write_match_missing_slot_fails():
    """If slot_id doesn't exist, return failed without calling Pass 4."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_missing_")
    ctx = _push_g(tmp)
    try:
        sw = SlotWrite(
            kind="match", domain="project", slot_id="nonexistent",
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦完成 ECCV multi-view ablation 实验, 效果不错.",
            ),
        )
        with patch.object(memory_router, "call_pass4_append_slot") as mocked:
            result = memory_router.route_with_slot_write(sw)
            assert result["ok"] is False
            assert "slot_id_not_exist" in result["reason"]
            assert not mocked.called
    finally:
        ctx.pop()


# ─────────────────────────────────────────────────────────────────────
# new path
# ─────────────────────────────────────────────────────────────────────

def test_route_with_slot_write_new_creates_slot_skips_pass4():
    """v3.2 (2026-05-14): new path skips Pass 4. summary + body come directly
    from Slot Writer's new_slot_meta + content_to_integrate."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_new_")
    ctx = _push_g(tmp)
    try:
        sw = SlotWrite(
            kind="new", domain="person",
            new_slot_meta=NewSlotMeta(
                id="zhang_san", title="张三", icon="👨‍💻",
                summary="李垦的后端同事张三, 偏深夜写代码, 在工作风格上有共鸣.",
                aliases=["张三", "zhangsan", "san"],
            ),
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦认识同事张三, 后端开发, 也偏好深夜.",
            ),
        )

        with patch.object(memory_router, "call_pass4_append_slot") as mocked:
            result = memory_router.route_with_slot_write(sw)
        assert result["ok"], result
        assert result["action"] == "created"
        assert result["slot_id"] == "zhang_san"
        # CRITICAL: Pass 4 NOT called on new path (v3.2)
        assert not mocked.called, "new path should NOT call Pass 4"

        created = memory_router.get_slot("person", "zhang_san")
        assert created is not None
        assert created["title"] == "张三"
        assert created["aliases"] == ["张三", "zhangsan", "san"]
        # summary comes straight from Slot Writer (new path skips Pass 4)
        assert created["summary"] == (
            "李垦的后端同事张三, 偏深夜写代码, 在工作风格上有共鸣."
        )
        # body = content_to_integrate (Slot Writer 出的片段)
        body = memory.read_file(created["main_file"])
        assert "李垦认识同事张三" in body
        assert "# 张三" in body, "body 应以 title 为 H1"

        writes = memory_router.list_daily_writes(_today())
        assert len(writes) == 1
        event = writes[0]
        assert event["action"] == "created"
        assert event["slot_title"] == "张三"
        assert event["slot_summary"] == (
            "李垦的后端同事张三, 偏深夜写代码, 在工作风格上有共鸣."
        )
        assert "李垦认识同事张三" in event["initial_body"]
        assert event["raw_content_to_integrate"] == event["initial_body"]
        assert event["content"] == event["initial_body"]
        assert event["main_file"] == created["main_file"]
        assert memory_router.list_daily_slot_appends(_today()) == {}
    finally:
        ctx.pop()


def test_route_with_slot_write_new_id_collision_resolved():
    """If proposed id collides, _resolve_id_collision adds a suffix.
    Also verifies new path still skips Pass 4."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_collision_")
    ctx = _push_g(tmp)
    try:
        # Pre-seed a slot with the same id
        _seed_slot("person", "zhang_san", title="张三 (旧)",
                    aliases=["张三", "zhangsan"])

        sw = SlotWrite(
            kind="new", domain="person",
            new_slot_meta=NewSlotMeta(
                id="zhang_san", title="张三 (新)", icon="👨",
                summary="李垦在另一场合认识的另一个张三, 不同的人.",
                aliases=["zhang", "san", "三"],
            ),
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦在另一场合认识同名张三, 不同的人.",
            ),
        )

        with patch.object(memory_router, "call_pass4_append_slot") as mocked:
            result = memory_router.route_with_slot_write(sw)
        assert result["ok"], result
        # New slot got a different id (suffix)
        assert result["slot_id"] != "zhang_san"
        assert result["slot_id"].startswith("zhang_san")
        # Pass 4 still not called
        assert not mocked.called, "new path should NOT call Pass 4"
        # Both slots exist
        assert memory_router.get_slot("person", "zhang_san") is not None
        assert memory_router.get_slot("person", result["slot_id"]) is not None
    finally:
        ctx.pop()


def test_route_with_slot_write_match_appends_without_rewriting_body():
    """match path uses append-only Pass 4 and preserves existing body."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_match_pass4_")
    ctx = _push_g(tmp)
    try:
        _seed_slot("project", "papers_2026",
                    title="2026 论文投稿",
                    summary="LLM 应该重写这个 summary",
                    main_md="# 2026 论文投稿\n\n旧 body 内容: CVPR 投稿准备中.\n")

        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate=(
                    "2026-05-13 李垦完成 ECCV multi-view ablation 实验, 效果不错."
                ),
            ),
        )

        with patch.object(memory_router, "call_legacy_slot_merge_rewrite") as edit_mock, \
                patch.object(memory_router, "call_pass4_append_slot",
                             return_value=_append_result(
                                 "完成 ECCV multi-view ablation 实验, 效果不错.",
                                 summary_update="李垦在并行准备论文投稿, multi-view ablation 已推进.",
                             )) as mocked:
            result = memory_router.route_with_slot_write(sw)
        assert result["ok"], result
        assert result["action"] == "matched"
        assert mocked.called, "match path must call append-only Pass 4"
        assert not edit_mock.called

        updated = memory_router.get_slot("project", "papers_2026")
        assert "multi-view ablation" in updated["summary"]
        body = memory.read_file(updated["main_file"])
        assert "旧 body 内容: CVPR 投稿准备中." in body
        assert "## 近期记录" in body
        assert "完成 ECCV multi-view ablation" in body
    finally:
        ctx.pop()


def test_route_with_slot_write_match_records_append_entry_and_compactor_ledger():
    """matched writes record the actual append_entry for journal + compactor."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_match_ledger_")
    ctx = _push_g(tmp)
    try:
        seeded = _seed_slot(
            "project", "papers_2026",
            title="2026 论文投稿",
            summary="旧 summary",
            main_md="# 2026 论文投稿\n\n旧 body 内容: CVPR 投稿准备中.\n",
        )
        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate=(
                    "截图显示用户已经把 ECCV multi-view ablation 的结果整理完，"
                    "并准备检查论文段落。"
                ),
            ),
        )
        append_entry = "整理完 ECCV multi-view ablation，准备检查论文段落。"
        with patch.object(memory_router, "call_pass4_append_slot",
                          return_value=_append_result(
                              append_entry,
                              summary_update="ECCV ablation 已整理，论文段落待检查",
                              entry_kind="progress",
                          )):
            result = memory_router.route_with_slot_write(
                sw, source_context="(screenshot device=Mac time=2026-05-19 sig=3)"
            )

        assert result["ok"], result
        writes = memory_router.list_daily_writes(_today())
        assert len(writes) == 1
        event = writes[0]
        assert event["action"] == "matched"
        assert event["source_type"] == "screenshot"
        assert event["slot_title"] == "2026 论文投稿"
        assert event["slot_summary"] == "ECCV ablation 已整理，论文段落待检查"
        assert event["append_entry"] == append_entry
        assert event["entry_kind"] == "progress"
        assert event["content"] == append_entry
        assert "截图显示用户" in event["raw_content_to_integrate"]
        assert event["main_file"] == seeded["main_file"]

        ledger = memory_router.list_daily_slot_appends(_today())
        bucket = ledger["project/papers_2026"]
        assert bucket["domain"] == "project"
        assert bucket["slot_id"] == "papers_2026"
        assert bucket["main_file"] == seeded["main_file"]
        assert bucket["slot_title_at_append"] == "2026 论文投稿"
        assert bucket["slot_summary_at_append"] == "ECCV ablation 已整理，论文段落待检查"
        assert len(bucket["entries"]) == 1
        ledger_entry = bucket["entries"][0]
        assert ledger_entry["entry"] == append_entry
        assert ledger_entry["entry_kind"] == "progress"
        assert ledger_entry["compacted"] is False
        assert ledger_entry["id"].startswith(datetime.now().strftime("%Y%m%d"))
    finally:
        ctx.pop()


def test_route_with_slot_write_passes_low_cost_pass4_policy():
    """Routine screenshot matches use append-only Pass 4 on memory/no reasoning."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_pass4_policy_")
    ctx = _push_g(tmp)
    try:
        seeded = _seed_slot(
            "project", "papers_2026",
            title="2026 论文投稿",
            main_md="# 2026 论文投稿\n\n旧 body 内容: CVPR 投稿准备中.\n",
        )
        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate=(
                    "2026-05-13 李垦整理了论文实验记录, 把主要 ablation 结果"
                    "和待检查项放进同一个文档里."
                ),
            ),
        )

        with patch.object(memory_router, "call_legacy_slot_merge_rewrite") as edit_mock, \
                patch.object(memory_router, "call_pass4_append_slot",
                             return_value={
                                 "should_append": True,
                                 "title_update": None,
                                 "summary_update": "论文实验记录已更新",
                                 "append_entry": "整理实验记录，汇总 ablation 和待检查项。",
                                 "entry_kind": "progress",
                                 "skip_reason": "",
                                 "alias_additions": [],
                             }) as append_mock:
            result = memory_router.route_with_slot_write(
                sw,
                source_context="(screenshot device=Mac time=2026-05-17 sig=3)",
                pass4_tier="memory",
                pass4_reasoning=False,
                pass4_max_tokens=12000,
                pass4_reasoning_budget=0,
            )
        assert result["ok"], result
        assert result["reason"] == "slot_writer_v3_match_append"
        assert not edit_mock.called
        assert append_mock.called
        kwargs = append_mock.call_args.kwargs
        assert kwargs["source_type"] == "screenshot"
        assert kwargs["tier"] == "memory"
        assert kwargs["reasoning"] is False
        assert kwargs["max_tokens"] == 1000

        body = memory.read_file(seeded["main_file"])
        assert "旧 body 内容: CVPR 投稿准备中." in body
        assert "## 近期记录" in body
        assert "- [" in body
        assert "整理实验记录，汇总 ablation 和待检查项。" in body
    finally:
        ctx.pop()


def test_route_with_slot_write_strong_screenshot_still_appends():
    """Strong screenshot writes still use unified append-only Pass 4."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_pass4_strong_")
    ctx = _push_g(tmp)
    try:
        _seed_slot("project", "papers_2026", title="2026 论文投稿")
        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate="DDL 从 5 月 20 日变成 5 月 19 日, 今天必须处理.",
            ),
        )

        with patch.object(memory_router, "call_legacy_slot_merge_rewrite") as edit_mock, \
                patch.object(memory_router, "call_pass4_append_slot",
                             return_value={
                                 "should_append": True,
                                 "title_update": None,
                                 "summary_update": "DDL 提前到 5 月 19 日",
                                 "append_entry": "DDL 提前到 5 月 19 日，今天必须处理。",
                                 "entry_kind": "ddl",
                                 "skip_reason": "",
                                 "alias_additions": [],
                             }) as append_mock:
            result = memory_router.route_with_slot_write(
                sw,
                source_context="(screenshot device=Mac time=2026-05-17 sig=4)",
                pass4_tier="chat",
                pass4_reasoning=True,
                pass4_max_tokens=50000,
                pass4_reasoning_budget=16000,
            )
        assert result["ok"], result
        assert result["reason"] == "slot_writer_v3_match_append"
        assert append_mock.called
        assert not edit_mock.called
        assert append_mock.call_args.kwargs["source_type"] == "screenshot"
        assert append_mock.call_args.kwargs["reasoning"] is False
        assert append_mock.call_args.kwargs["max_tokens"] == 1000
    finally:
        ctx.pop()


def test_route_with_slot_write_append_skip_does_not_dirty_slot():
    """If Pass 4 says the fact is duplicate, router leaves slot unchanged."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_append_skip_")
    ctx = _push_g(tmp)
    try:
        seeded = _seed_slot(
            "project", "papers_2026",
            title="2026 论文投稿",
            summary="旧 summary",
            main_md="# 2026 论文投稿\n\n旧 body 内容: CVPR 投稿准备中.\n",
        )
        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate=(
                    "旧 body 内容: CVPR 投稿准备中。用户仍在围绕同一篇论文准备投稿材料, "
                    "没有出现新的实验结果或状态变化。"
                ),
            ),
        )
        with patch.object(memory_router, "call_pass4_append_slot",
                          return_value={
                              "should_append": False,
                              "title_update": None,
                              "summary_update": None,
                              "append_entry": "",
                              "entry_kind": "other",
                              "skip_reason": "重复现有内容",
                              "alias_additions": [],
                          }):
            result = memory_router.route_with_slot_write(sw)
        assert result["ok"], result
        assert result["action"] == "skipped"
        updated = memory_router.get_slot("project", "papers_2026")
        assert "append_dirty_since" not in updated
        assert memory.read_file(seeded["main_file"]) == (
            "# 2026 论文投稿\n\n旧 body 内容: CVPR 投稿准备中.\n"
        )
        assert memory_router.list_daily_writes(_today()) == []
        assert memory_router.list_daily_slot_appends(_today()) == {}
    finally:
        ctx.pop()


def test_route_with_slot_write_duplicate_append_entry_does_not_log_ledger():
    """If append_entry is already in body, no daily ledger entry is written."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_duplicate_append_")
    ctx = _push_g(tmp)
    try:
        existing_entry = "已经记录过的论文状态。"
        seeded = _seed_slot(
            "project", "papers_2026",
            title="2026 论文投稿",
            summary="旧 summary",
            main_md=(
                "# 2026 论文投稿\n\n"
                "旧 body 内容: CVPR 投稿准备中.\n\n"
                "## 近期记录\n\n"
                f"- [2026-05-19 10:00] {existing_entry}\n"
            ),
        )
        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate=(
                    "用户再次提到了已经记录过的论文状态，没有实际新增事实，"
                    "这里用来验证重复 append_entry 不会污染 ledger。"
                ),
            ),
        )
        with patch.object(memory_router, "call_pass4_append_slot",
                          return_value=_append_result(existing_entry)):
            result = memory_router.route_with_slot_write(sw)

        assert result["ok"], result
        assert result["action"] == "skipped"
        updated = memory_router.get_slot("project", "papers_2026")
        assert "append_dirty_since" not in updated
        assert memory.read_file(seeded["main_file"]) == (
            "# 2026 论文投稿\n\n"
            "旧 body 内容: CVPR 投稿准备中.\n\n"
            "## 近期记录\n\n"
            f"- [2026-05-19 10:00] {existing_entry}\n"
        )
        assert memory_router.list_daily_writes(_today()) == []
        assert memory_router.list_daily_slot_appends(_today()) == {}
    finally:
        ctx.pop()


def test_route_with_slot_write_self_identity_blocked():
    """new + domain=self + id=identity must fail (onboarding territory)."""
    tmp = tempfile.mkdtemp(prefix="route_slot_write_identity_")
    ctx = _push_g(tmp)
    try:
        # Pydantic schema-level check should catch first
        with pytest.raises(Exception):
            SlotWrite(
                kind="new", domain="self",
                new_slot_meta=NewSlotMeta(
                    id="identity", title="用户身份", icon="🪪",
                    summary="身份事实",
                    aliases=["self", "identity", "me"],
                ),
                integration=IntegrationSpec(
                    content_to_integrate="2026-05-13 这条事实试图覆盖 identity slot.",
                ),
            )
    finally:
        ctx.pop()


# ─────────────────────────────────────────────────────────────────────
# Pass 4 failure handling
# ─────────────────────────────────────────────────────────────────────

def test_route_with_slot_write_pass4_failure_returns_failed():
    tmp = tempfile.mkdtemp(prefix="route_slot_write_pass4_fail_")
    ctx = _push_g(tmp)
    try:
        _seed_slot("project", "papers_2026", title="2026 论文投稿")

        sw = SlotWrite(
            kind="match", domain="project", slot_id="papers_2026",
            integration=IntegrationSpec(
                content_to_integrate="2026-05-13 李垦完成 ECCV multi-view ablation 实验.",
            ),
        )

        with patch.object(memory_router, "call_pass4_append_slot",
                           return_value=None):  # retry exhausted
            result = memory_router.route_with_slot_write(sw)
        assert result["ok"] is False
        assert "pass4 append retry exhausted" in result["reason"]
    finally:
        ctx.pop()


# ─────────────────────────────────────────────────────────────────────
# Dict input form (accept dict directly, not only SlotWrite Pydantic)
# ─────────────────────────────────────────────────────────────────────

def test_route_with_slot_write_accepts_dict():
    tmp = tempfile.mkdtemp(prefix="route_slot_write_dict_")
    ctx = _push_g(tmp)
    try:
        _seed_slot("project", "papers_2026", title="2026 论文投稿")

        sw_dict = {
            "kind": "match",
            "domain": "project",
            "slot_id": "papers_2026",
            "integration": {
                "content_to_integrate":
                    "2026-05-13 李垦完成 ECCV multi-view ablation 实验, 效果不错.",
            },
        }
        with patch.object(memory_router, "call_pass4_append_slot",
                           return_value={
                               "title_update": None,
                               "summary_update": "更新后",
                               "should_append": True,
                               "append_entry": "完成 multi-view 实验.",
                               "entry_kind": "progress",
                               "skip_reason": "",
                               "alias_additions": [],
                           }):
            result = memory_router.route_with_slot_write(sw_dict)
        assert result["ok"], result
        assert result["action"] == "matched"
    finally:
        ctx.pop()


def test_route_with_slot_write_daily_logs_are_user_scoped():
    """daily_writes and daily_slot_appends live under the current user data dir."""
    tmp_a = tempfile.mkdtemp(prefix="route_slot_write_user_a_")
    tmp_b = tempfile.mkdtemp(prefix="route_slot_write_user_b_")

    def write_one(tmp, entry):
        ctx = _push_g(tmp)
        try:
            _seed_slot("project", "papers_2026", title="2026 论文投稿")
            sw = SlotWrite(
                kind="match", domain="project", slot_id="papers_2026",
                integration=IntegrationSpec(
                    content_to_integrate=(
                        f"原始输入显示用户今天确实产生了新的记忆增量: {entry} "
                        "这条内容用于验证不同账号的数据不会互相串写。"
                    )
                ),
            )
            with patch.object(memory_router, "call_pass4_append_slot",
                              return_value=_append_result(entry)):
                result = memory_router.route_with_slot_write(sw)
            assert result["ok"], result
            writes = memory_router.list_daily_writes(_today())
            ledger = memory_router.list_daily_slot_appends(_today())
            return writes, ledger
        finally:
            ctx.pop()

    writes_a, ledger_a = write_one(tmp_a, "A 用户的记忆增量。")
    writes_b, ledger_b = write_one(tmp_b, "B 用户的记忆增量。")

    assert "A 用户的记忆增量。" in writes_a[0]["append_entry"]
    assert "B 用户的记忆增量。" not in str(writes_a)
    assert "A 用户的记忆增量。" in str(ledger_a)
    assert "B 用户的记忆增量。" not in str(ledger_a)

    assert "B 用户的记忆增量。" in writes_b[0]["append_entry"]
    assert "A 用户的记忆增量。" not in str(writes_b)
    assert "B 用户的记忆增量。" in str(ledger_b)
    assert "A 用户的记忆增量。" not in str(ledger_b)


if __name__ == "__main__":
    import unittest
    unittest.main()
