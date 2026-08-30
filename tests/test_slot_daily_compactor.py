import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import memory  # noqa: E402
import memory_prompts_v2  # noqa: E402
import memory_router  # noqa: E402


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _iso_today(hhmmss: str) -> str:
    return f"{_today()}T{hhmmss}"


def _date_offset(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _push_g(tmp, uid="test_user"):
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = uid
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def _seed_dirty_slot(domain="project", slot_id="papers_2026", *,
                     title="2026 论文投稿", summary="论文推进状态",
                     last_appended_at=None):
    last_appended_at = last_appended_at or _iso_today("20:15:00")
    day = last_appended_at[:10]
    slot = {
        "id": slot_id,
        "title": title,
        "icon": "📄",
        "status": "active",
        "pinned": False,
        "summary": summary,
        "aliases": [slot_id, title],
        "main_file": memory_router._slot_main_file_rel(domain, slot_id),
        "last_active": last_appended_at,
        "created": f"{day}T10:00:00",
        "append_dirty_since": f"{day}T10:12:00",
        "append_count_since_compact": 2,
        "last_appended_at": last_appended_at,
    }
    memory_router.upsert_slot(domain, slot)
    body = (
        f"# {title}\n\n"
        "用户正在准备 2026 年论文投稿。\n\n"
        "## 近期记录\n\n"
        f"- [{day} 10:12] 整理了 multi-view ablation 结果。\n"
        f"- [{day} 20:15] 开始检查论文段落。\n"
    )
    memory.write_file(slot["main_file"], body)
    return slot


def _seed_append_ledger(slot, *,
                        entry="整理了 multi-view ablation，开始检查论文段落。",
                        entry_kind="progress",
                        ts=None):
    ts = ts or _iso_today("20:15:00")
    memory_router._record_daily_slot_append(
        domain="project",
        slot_id=slot["id"],
        main_file=slot["main_file"],
        slot_title=slot["title"],
        slot_summary=slot["summary"],
        append_entry=entry,
        entry_kind=entry_kind,
        current_time=ts,
    )


def test_slot_daily_compactor_rewrites_body_and_marks_ledger():
    tmp = tempfile.mkdtemp(prefix="slot_daily_compactor_")
    ctx = _push_g(tmp)
    try:
        slot = _seed_dirty_slot()
        _seed_append_ledger(slot)

        compacted = SimpleNamespace(
            title="2026 论文投稿",
            summary="用户在整理 multi-view ablation，并检查论文段落。",
            body="用户正在准备 2026 年论文投稿，已经整理完 multi-view ablation，并开始检查论文段落。",
        )
        with patch.object(memory_router, "call_slot_daily_compactor",
                          return_value=compacted) as mocked:
            result = memory_router.run_slot_daily_compactor(_today())

        assert result["compacted"] == 1
        assert result["entries_compacted"] == 1
        kwargs = mocked.call_args.kwargs
        assert kwargs["domain"] == "project"
        assert kwargs["slot_id"] == "papers_2026"
        assert kwargs["target_mode"] == "preserve"
        assert kwargs["reasoning"] is False
        assert kwargs["tier"] == "memory"
        assert kwargs["append_entries"][0]["entry_kind"] == "progress"

        updated = memory_router.get_slot("project", "papers_2026")
        assert updated["summary"] == "用户在整理 multi-view ablation，并检查论文段落。"
        assert updated["append_count_since_compact"] == 0
        assert "append_dirty_since" not in updated
        assert updated["last_compacted_at"]

        body = memory.read_file(slot["main_file"])
        assert body.startswith("# 2026 论文投稿")
        assert "整理完 multi-view ablation" in body
        assert "## 近期记录" not in body

        ledger = memory_router.list_daily_slot_appends(_today())
        entry = ledger["project/papers_2026"]["entries"][0]
        assert entry["compacted"] is True
        assert entry["compacted_at"]
    finally:
        ctx.pop()


def test_slot_daily_compactor_skips_if_slot_changed_after_ledger():
    tmp = tempfile.mkdtemp(prefix="slot_daily_compactor_conflict_")
    ctx = _push_g(tmp)
    try:
        slot = _seed_dirty_slot(last_appended_at=_iso_today("21:00:00"))
        _seed_append_ledger(slot, ts=_iso_today("20:15:00"))

        with patch.object(memory_router, "call_slot_daily_compactor") as mocked:
            result = memory_router.run_slot_daily_compactor(_today())

        assert result["compacted"] == 0
        assert result["skipped"][0]["reason"] == "last_appended_at_changed"
        assert not mocked.called
        ledger = memory_router.list_daily_slot_appends(_today())
        assert ledger["project/papers_2026"]["entries"][0]["compacted"] is False
    finally:
        ctx.pop()


def test_slot_daily_compactor_merges_cross_day_pending_entries_for_same_slot():
    tmp = tempfile.mkdtemp(prefix="slot_daily_compactor_cross_day_")
    ctx = _push_g(tmp)
    try:
        yesterday = _date_offset(-1)
        today = _today()
        slot = _seed_dirty_slot(last_appended_at=f"{today}T11:20:00")
        body = (
            f"# {slot['title']}\n\n"
            "用户正在准备 2026 年论文投稿。\n\n"
            "## 近期记录\n\n"
            f"- [{yesterday} 21:10] 昨天补了一条人物相关观察。\n"
            f"- [{today} 11:20] 今天又补了一条同一 slot 的观察。\n"
        )
        memory.write_file(slot["main_file"], body)
        _seed_append_ledger(
            slot,
            entry="昨天补了一条人物相关观察。",
            ts=f"{yesterday}T21:10:00",
        )
        _seed_append_ledger(
            slot,
            entry="今天又补了一条同一 slot 的观察。",
            ts=f"{today}T11:20:00",
        )

        compacted = SimpleNamespace(
            title="2026 论文投稿",
            summary="用户连续两天补充了同一记忆卡的关键观察。",
            body="用户正在准备 2026 年论文投稿，昨天和今天连续补充了同一记忆卡的关键观察。",
        )
        with patch.object(memory_router, "call_slot_daily_compactor",
                          return_value=compacted) as mocked:
            result = memory_router.run_slot_daily_compactor(today)

        assert result["compacted"] == 1
        assert result["entries_compacted"] == 2
        entries = mocked.call_args.kwargs["append_entries"]
        assert [e["entry"] for e in entries] == [
            "昨天补了一条人物相关观察。",
            "今天又补了一条同一 slot 的观察。",
        ]

        yesterday_ledger = memory_router.list_daily_slot_appends(yesterday)
        today_ledger = memory_router.list_daily_slot_appends(today)
        assert yesterday_ledger["project/papers_2026"]["entries"][0]["compacted"] is True
        assert today_ledger["project/papers_2026"]["entries"][0]["compacted"] is True
        updated = memory_router.get_slot("project", "papers_2026")
        assert "append_dirty_since" not in updated
        assert updated["append_count_since_compact"] == 0
    finally:
        ctx.pop()


def test_slot_daily_compactor_catches_prior_day_pending_without_today_update():
    """A missed prior-day dirty slot is compacted even if it had no new writes today."""
    tmp = tempfile.mkdtemp(prefix="slot_daily_compactor_prior_only_")
    ctx = _push_g(tmp)
    try:
        yesterday = _date_offset(-1)
        today = _today()
        slot = _seed_dirty_slot(last_appended_at=f"{yesterday}T21:10:00")
        body = (
            f"# {slot['title']}\n\n"
            "用户正在准备 2026 年论文投稿。\n\n"
            "## 近期记录\n\n"
            f"- [{yesterday} 21:10] 昨天有一条没有来得及整理的记录。\n"
        )
        memory.write_file(slot["main_file"], body)
        _seed_append_ledger(
            slot,
            entry="昨天有一条没有来得及整理的记录。",
            ts=f"{yesterday}T21:10:00",
        )

        compacted = SimpleNamespace(
            title="2026 论文投稿",
            summary="用户昨天补充了一条尚未整理的论文推进记录。",
            body="用户正在准备 2026 年论文投稿，昨天补充了一条尚未整理的论文推进记录。",
        )
        with patch.object(memory_router, "call_slot_daily_compactor",
                          return_value=compacted) as mocked:
            assert memory_router.has_uncompacted_slot_appends(yesterday) is True
            result = memory_router.run_slot_daily_compactor(today)

        assert result["compacted"] == 1
        assert result["entries_compacted"] == 1
        assert mocked.call_args.kwargs["append_entries"][0]["entry"] == "昨天有一条没有来得及整理的记录。"

        yesterday_ledger = memory_router.list_daily_slot_appends(yesterday)
        assert yesterday_ledger["project/papers_2026"]["entries"][0]["compacted"] is True
        assert memory_router.has_uncompacted_slot_appends(yesterday) is False
        updated = memory_router.get_slot("project", "papers_2026")
        assert "append_dirty_since" not in updated
        assert updated["append_count_since_compact"] == 0
    finally:
        ctx.pop()


def test_slot_daily_compactor_is_user_scoped():
    tmp_a = tempfile.mkdtemp(prefix="slot_daily_user_a_")
    tmp_b = tempfile.mkdtemp(prefix="slot_daily_user_b_")

    def run_for(tmp, uid, body_text):
        ctx = _push_g(tmp, uid=uid)
        try:
            slot = _seed_dirty_slot()
            _seed_append_ledger(slot, entry=f"{body_text} append entry")
            compacted = SimpleNamespace(
                title="2026 论文投稿",
                summary=f"{body_text} summary",
                body=f"{body_text} compacted body",
            )
            with patch.object(memory_router, "call_slot_daily_compactor",
                              return_value=compacted):
                memory_router.run_slot_daily_compactor(_today())
            return memory.read_file(slot["main_file"]), memory_router.list_daily_slot_appends(_today())
        finally:
            ctx.pop()

    body_a, ledger_a = run_for(tmp_a, "user_a", "USER_A_ONLY")
    body_b, ledger_b = run_for(tmp_b, "user_b", "USER_B_ONLY")

    assert "USER_A_ONLY" in body_a
    assert "USER_B_ONLY" not in body_a
    assert "USER_A_ONLY" in str(ledger_a)
    assert "USER_B_ONLY" not in str(ledger_a)

    assert "USER_B_ONLY" in body_b
    assert "USER_A_ONLY" not in body_b
    assert "USER_B_ONLY" in str(ledger_b)
    assert "USER_A_ONLY" not in str(ledger_b)


def test_slot_daily_compactor_output_allows_long_summary_for_tolerance():
    long_summary = "这是一段偏长但仍然可接受的总结" * 8
    out = memory_prompts_v2.SlotDailyCompactorOutput.model_validate({
        "title": "论文投稿",
        "summary": long_summary,
        "body": "用户正在整理论文投稿相关信息。",
    })

    assert out.summary == long_summary

    with pytest.raises(ValueError, match="summary 长度 1-500"):
        memory_prompts_v2.SlotDailyCompactorOutput.model_validate({
            "title": "论文投稿",
            "summary": "过长" * 251,
            "body": "用户正在整理论文投稿相关信息。",
        })


def test_slot_daily_compactor_terminalizes_missing_slot_ledger():
    tmp = tempfile.mkdtemp(prefix="slot_daily_missing_slot_")
    ctx = _push_g(tmp)
    try:
        now = _iso_today("20:15:00")
        memory_router._record_daily_slot_append(
            domain="project",
            slot_id="ghost_slot",
            main_file=memory_router._slot_main_file_rel("project", "ghost_slot"),
            slot_title="Ghost Slot",
            slot_summary="已不存在的 slot",
            append_entry="这条记录指向一个已经不存在的 slot。",
            entry_kind="other",
            current_time=now,
        )

        with patch.object(memory_router, "call_slot_daily_compactor") as mocked:
            result = memory_router.run_slot_daily_compactor(_today())

        assert result["checked"] == 1
        assert result["compacted"] == 0
        assert result["skipped"][0]["reason"] == "missing_slot"
        assert result["skipped"][0]["terminalized"] == 1
        assert not mocked.called

        ledger = memory_router.list_daily_slot_appends(_today())
        entry = ledger["project/ghost_slot"]["entries"][0]
        assert entry["compacted"] is False
        assert entry["terminal"] is True
        assert entry["terminal_reason"] == "missing_slot"
        assert entry["terminal_at"]
        assert memory_router.has_uncompacted_slot_appends(_today()) is False
    finally:
        ctx.pop()


def test_delete_slot_terminalizes_pending_append_entries():
    tmp = tempfile.mkdtemp(prefix="slot_daily_delete_terminal_")
    ctx = _push_g(tmp)
    try:
        slot = _seed_dirty_slot()
        _seed_append_ledger(slot)

        assert memory_router.has_uncompacted_slot_appends(_today()) is True
        assert memory_router.delete_slot("project", slot["id"], delete_file=True) is True

        ledger = memory_router.list_daily_slot_appends(_today())
        entry = ledger["project/papers_2026"]["entries"][0]
        assert entry["compacted"] is False
        assert entry["terminal"] is True
        assert entry["terminal_reason"] == "slot_deleted"
        assert entry["terminal_at"]
        assert "terminal_target_slot_id" not in entry
        assert memory_router.has_uncompacted_slot_appends(_today()) is False
    finally:
        ctx.pop()


def test_merge_slots_terminalizes_source_pending_append_entries():
    tmp = tempfile.mkdtemp(prefix="slot_daily_merge_terminal_")
    ctx = _push_g(tmp)
    try:
        target = _seed_dirty_slot(
            slot_id="paper",
            title="Lambda 论文",
            summary="论文主卡片",
            last_appended_at=_iso_today("10:00:00"),
        )
        source = _seed_dirty_slot(
            slot_id="paper_v2",
            title="Lambda v2",
            summary="论文修订卡片",
            last_appended_at=_iso_today("20:15:00"),
        )
        _seed_append_ledger(
            source,
            entry="Lambda v2 卡片里有一条尚未整理的追加记录。",
            ts=_iso_today("20:15:00"),
        )

        with patch.object(memory_router, "call_legacy_slot_merge_rewrite",
                          return_value={
                              "title": "Lambda 论文",
                              "summary": "用户在推进 Lambda 论文和 v2 修订。",
                              "body": "用户在推进 Lambda 论文，并整合了 v2 修订内容。",
                              "alias_additions": ["Lambda v2"],
                          }):
            merged = memory_router.merge_slots("project", "paper_v2", "paper")

        assert merged is not None
        assert memory_router.get_slot("project", "paper") is not None
        assert memory_router.get_slot("project", "paper_v2") is None

        ledger = memory_router.list_daily_slot_appends(_today())
        entry = ledger["project/paper_v2"]["entries"][0]
        assert entry["compacted"] is False
        assert entry["terminal"] is True
        assert entry["terminal_reason"] == "merged_into"
        assert entry["terminal_target_slot_id"] == target["id"]
        assert memory_router.has_uncompacted_slot_appends(_today()) is False
    finally:
        ctx.pop()


def test_call_slot_daily_compactor_builds_expected_prompt(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return kwargs["output_schema"].model_validate({
            "title": "AgiBot / Motus v2",
            "summary": "用户在推进 Motus v2 实验与论文段落整理。",
            "body": "用户正在推进 Motus v2，auth.py 阻塞解除后开始对齐论文段落。",
        })

    monkeypatch.setattr(memory_prompts_v2, "_call_llm_with_retry", fake_call)

    out = memory_prompts_v2.call_slot_daily_compactor(
        domain="project",
        slot_id="agibot_motus_v2",
        current_title="AgiBot / Motus v2",
        current_summary="用户在推进具身智能 world model 与 Motus v2 相关实验。",
        current_body="用户正在推进 Motus v2。\n\n## 近期记录\n\n- [2026-05-19 20:15] auth.py 阻塞解除。",
        current_aliases=["AgiBot", "Motus v2"],
        append_dirty_since="2026-05-19T10:12:00",
        append_count_since_compact=1,
        last_appended_at="2026-05-19T20:15:00",
        append_entries=[{
            "id": "20260519201500_ab12cd",
            "ts": "2026-05-19T20:15:00",
            "entry_kind": "progress",
            "entry": "auth.py 阻塞解除，准备对齐论文段落。",
        }],
        markdown_char_count=88,
        rendered_char_count=74,
        target_mode="preserve",
        current_time="2026-05-19T23:55:00",
    )

    assert out.title == "AgiBot / Motus v2"
    assert captured["pass_label"] == "SlotDailyCompactor:project"
    assert captured["tier"] == "memory"
    assert captured["reasoning"] is False
    assert captured["output_schema"] is memory_prompts_v2.SlotDailyCompactorOutput
    messages = captured["messages"]
    assert "你是 Miru 的 SlotDailyCompactor" in messages[0]["content"]
    user_msg = messages[1]["content"]
    assert "current_body_rendered_char_count: 74" in user_msg
    assert "target_mode: preserve" in user_msg
    assert "auth.py 阻塞解除，准备对齐论文段落。" in user_msg
    assert "输出严格 JSON" not in user_msg


def test_nightly_maintenance_invokes_slot_daily_compactor(monkeypatch):
    import core
    import daily_patterns

    calls = []
    monkeypatch.setattr(core.storage, "archive_old_chat_messages", lambda: {"archived": 0})
    monkeypatch.setattr(memory, "consolidate_index", lambda: {"deduped": 0, "journal_pruned": 0})
    monkeypatch.setattr(daily_patterns, "update_daily_patterns", lambda: None)
    monkeypatch.setattr(core, "_nightly_commitment_cleanup", lambda: None)
    monkeypatch.setattr(core, "_user_now", lambda: datetime(2026, 5, 19, 23, 30, 0))
    monkeypatch.setattr(memory_router, "run_slot_daily_compactor",
                        lambda date_str=None: calls.append(date_str) or {
                            "compacted": 0, "failed": [], "skipped": []
                        })

    core._run_nightly_maintenance()

    assert calls == ["2026-05-19"]


def test_journal_auto_runs_slot_compactor_catchup_for_missed_prior_day(monkeypatch):
    import app as app_mod
    import journal
    import user_settings

    tmp = tempfile.mkdtemp(prefix="slot_daily_auto_catchup_")
    ctx = _push_g(tmp, uid="catchup_user")
    try:
        app_mod._slot_compactor_backfill_attempts.clear()
        fake_now = datetime(2026, 6, 21, 11, 30, 0)
        calls = []
        monkeypatch.setattr(user_settings, "user_now", lambda: fake_now)
        monkeypatch.setattr(memory_router, "has_uncompacted_slot_appends",
                            lambda date_str=None: date_str == "2026-06-20")
        monkeypatch.setattr(memory_router, "run_slot_daily_compactor",
                            lambda date_str=None, max_slots=20: calls.append((date_str, max_slots)) or {
                                "ok": True,
                                "date": date_str,
                                "checked": 1,
                                "compacted": 1,
                                "entries_compacted": 2,
                                "skipped": [],
                                "failed": [],
                            })
        monkeypatch.setattr(journal, "scan_and_backfill",
                            lambda days=30, max_per_call=2: {"scanned": 0, "regenerated": []})

        app_mod._check_journal_auto()

        assert calls == [("2026-06-21", 8)]
    finally:
        ctx.pop()
