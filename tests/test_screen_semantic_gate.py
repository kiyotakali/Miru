import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, g


_TODAY = datetime.now().strftime("%Y-%m-%d")


def _today_ts(clock: str) -> str:
    return f"{_TODAY}T{clock}"


def _ctx(tmp_path, user_id="u_gate_test"):
    os.makedirs(tmp_path, exist_ok=True)
    app = Flask(__name__)
    ctx = app.app_context()
    ctx.push()
    g.user_id = user_id
    g.user_data_dir = str(tmp_path)
    g.is_admin = False
    return ctx


def test_screen_semantic_gate_prompt_strips_audit_fields():
    import memory_prompts_v3

    messages = memory_prompts_v3.build_screen_semantic_gate_messages(
        observation="他仍停留在同一个 VS Code 文件，只有光标位置变化。",
        significance=3,
        recent_history=[
            {
                "id": "secret_id",
                "t": "2026-05-19T20:00:00",
                "d": "macbook-private",
                "sig": 4,
                "observation": "他在 VS Code 查看 Daily Slot Writes 测试。",
                "passed_gate": True,
                "writer": {"slot_writes": 1, "error": "retry_exhausted"},
            }
        ],
        active_commitments=[{"title": "完成 ScreenSemanticGate", "deadline": "2026-05-19 23:00"}],
    )
    rendered = "\n".join(m["content"] for m in messages)

    assert "secret_id" not in rendered
    assert "macbook-private" not in rendered
    assert "2026-05-19T20:00:00" not in rendered
    assert "passed_gate=true" in rendered
    assert "writer_error='retry_exhausted'" in rendered
    assert '{"should_continue": true}' in rendered
    assert '{"should_continue": false}' in rendered


def test_gate_false_skips_screen_slot_writer_and_logs(tmp_path):
    ctx = _ctx(tmp_path)
    try:
        import core
        import storage

        with patch(
            "memory_prompts_v3.call_screen_semantic_gate",
            return_value=SimpleNamespace(should_continue=False),
        ), patch("memory_prompts_v3.call_screen_slot_writer") as writer:
            core._process_screen_observation_async(
                "他仍停留在同一个 VS Code 文件，只有光标位置变化。",
                3,
                "dev_gate_false",
                "MacBook",
                _today_ts("20:00:00"),
                "u_gate_test",
                str(tmp_path),
            )

        assert not writer.called
        entries = storage.get_recent_screen_semantic_gate_entries()
        assert len(entries) == 1
        assert entries[0]["passed_gate"] is False
        assert entries[0]["status"] == "skipped"
        assert entries[0]["d"] == "dev_gate_false"
    finally:
        ctx.pop()


def test_gate_true_runs_writer_and_updates_gate_log(tmp_path):
    ctx = _ctx(tmp_path)
    try:
        import core
        import storage
        from memory_prompts_v3 import ScreenSlotWriterOutput

        with patch(
            "memory_prompts_v3.call_screen_semantic_gate",
            return_value=SimpleNamespace(should_continue=True),
        ), patch(
            "memory_prompts_v3.call_screen_slot_writer",
            return_value=ScreenSlotWriterOutput(
                slot_writes=[],
                commitments=[],
                completed_commitments=[],
                skipped_reason="test-empty",
            ),
        ) as writer:
            core._process_screen_observation_async(
                "他看到 pytest 通过，并准备继续 ScreenSemanticGate 实现。",
                4,
                "dev_gate_true",
                "MacBook",
                _today_ts("20:05:00"),
                "u_gate_test",
                str(tmp_path),
            )

        assert writer.called
        entries = storage.get_recent_screen_semantic_gate_entries()
        assert len(entries) == 1
        assert entries[0]["passed_gate"] is True
        assert entries[0]["status"] == "writer_done"
        assert entries[0]["writer"]["slot_writes"] == 0
        assert entries[0]["writer"]["skipped_reason"] == "test-empty"
    finally:
        ctx.pop()


def test_screenshot_stats_count_only_passed_gate(tmp_path):
    ctx = _ctx(tmp_path)
    try:
        import storage

        storage.append_screenshot_log("dev_a", captured_at=_today_ts("20:00:00"))
        storage.append_screen_semantic_gate_log(
            observation="低价值 UI 切换",
            significance=3,
            should_continue=False,
            device_id="dev_a",
            captured_at=_today_ts("20:01:00"),
        )
        storage.append_screen_semantic_gate_log(
            observation="出现新的 DDL",
            significance=5,
            should_continue=True,
            device_id="dev_a",
            captured_at=_today_ts("20:02:00"),
        )

        stats = storage.get_screenshot_stats()
        assert stats["dev_a"]["total"] == 1
        assert stats["dev_a"]["last_time"] == _today_ts("20:02:00")
    finally:
        ctx.pop()


def test_screen_semantic_gate_log_is_per_user_isolated(tmp_path):
    ctx1 = _ctx(tmp_path / "u1", user_id="u1")
    try:
        import storage

        storage.append_screenshot_log("dev_shared", captured_at=_today_ts("20:00:00"))
        storage.append_screenshot_log("dev_shared", captured_at=_today_ts("20:00:10"))
        storage.append_screen_semantic_gate_log(
            observation="用户在项目 A 中新增了一个明确 DDL。",
            significance=4,
            should_continue=True,
            device_id="dev_shared",
            captured_at=_today_ts("20:01:00"),
        )
        assert len(storage.get_recent_screen_semantic_gate_entries()) == 1
        assert storage.get_screenshot_stats()["dev_shared"]["total"] == 1
    finally:
        ctx1.pop()

    ctx2 = _ctx(tmp_path / "u2", user_id="u2")
    try:
        import storage

        assert storage.get_recent_screen_semantic_gate_entries() == []
        assert storage.get_screenshot_stats() == {}
        storage.append_screen_semantic_gate_log(
            observation="用户只是切换了一个普通窗口。",
            significance=3,
            should_continue=False,
            device_id="dev_shared",
            captured_at=_today_ts("20:02:00"),
        )
        assert len(storage.get_recent_screen_semantic_gate_entries()) == 1
        assert storage.get_screenshot_stats() == {}
    finally:
        ctx2.pop()

    ctx1_again = _ctx(tmp_path / "u1", user_id="u1")
    try:
        import storage

        entries = storage.get_recent_screen_semantic_gate_entries()
        assert len(entries) == 1
        assert entries[0]["observation"] == "用户在项目 A 中新增了一个明确 DDL。"
        assert storage.get_screenshot_stats()["dev_shared"]["total"] == 1
    finally:
        ctx1_again.pop()
