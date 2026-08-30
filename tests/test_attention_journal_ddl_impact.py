import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _bind_data_dir(monkeypatch, tmp_path):
    import memory
    import miru_emotion
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(memory, "DATA_DIR", str(tmp_path))
    miru_emotion._instances.clear()


def test_journal_context_does_not_read_attention_raw_logs_or_sig2_observations(tmp_path, monkeypatch):
    import journal
    import miru_emotion
    import storage

    _bind_data_dir(monkeypatch, tmp_path)
    # AttentionEngine segment storage has a rolling retention window.  Use the
    # current date so this fixture keeps testing the journal contract instead
    # of expiring itself as calendar time moves on.
    date_str = datetime.now().strftime("%Y-%m-%d")

    storage.write_json(storage.timeline_path(), [
        {
            "time": f"{date_str} 09:30:00",
            "observation": "SHOULD_NOT_APPEAR_SIG2 用户只是切到稳定页面",
            "significance": 2,
        },
        {
            "time": f"{date_str} 10:20:00",
            "observation": "用户完成了一个真实工作节点",
            "significance": 3,
        },
    ])
    storage.append_attention_log({
        "ts": f"{date_str} 10:30:00",
        "decision": "observe",
        "thought": "SHOULD_NOT_APPEAR_ATTENTION_LOG",
    })
    storage.append_attention_log({
        "id": "inner_visible",
        "channel": "inner",
        "started_at": f"{date_str} 11:00:00",
        "updated_at": f"{date_str} 11:30:00",
        "duration_seconds": 1800,
        "tick_count": 2,
        "mood": "安静挂念",
        "valence": 0.12,
        "text": "JOURNAL_INNER_VISIBLE 我有点想靠近他，但还在等一个合适的时机。",
    })
    storage.save_attention_state({
        "thought": "SHOULD_NOT_APPEAR_ATTENTION_STATE",
        "current_focus": {"last_thought": "SHOULD_NOT_APPEAR_FOCUS"},
    })
    storage.save_attention_intent_queue([{
        "id": "intent_test",
        "status": "pending",
        "context_summary": "SHOULD_NOT_APPEAR_INTENT",
    }])
    storage.append_attention_delivery_log({
        "ts": f"{date_str} 10:31:00",
        "intent_id": "intent_test",
        "reason": "SHOULD_NOT_APPEAR_DELIVERY",
    })
    storage.upsert_emotion_log_entry({
        "id": "user_affect_visible",
        "channel": "user_affect",
        "source_type": "attention",
        "timestamp": f"{date_str} 11:05:00",
        "started_at": f"{date_str} 11:05:00",
        "updated_at": f"{date_str} 11:45:00",
        "duration_seconds": 2400,
        "tick_count": 3,
        "mood": "专注但有点累",
        "valence": -0.18,
        "arousal": 0.42,
        "trend": "stable",
        "text": "JOURNAL_USER_AFFECT_VISIBLE 他像是在连续处理一件费神的事。",
        "source": "JOURNAL_USER_AFFECT_VISIBLE 他像是在连续处理一件费神的事。",
    })
    miru_emotion.get_instance().update_from_attention({
        "id": "self_emotion_visible",
        "channel": "self_emotion",
        "started_at": f"{date_str} 11:10:00",
        "updated_at": f"{date_str} 11:50:00",
        "duration_seconds": 2400,
        "tick_count": 3,
        "mood": "有点心疼",
        "valence": 0.08,
        "arousal": 0.36,
        "text": "JOURNAL_SELF_EMOTION_VISIBLE 我有点心疼他这样绷着。",
    })
    storage.write_json(storage.chat_history_path(), [{
        "id": "p1",
        "role": "assistant",
        "type": "proactive",
        "source": "attention_engine",
        "time": f"{date_str} 12:00:00",
        "text": "先把肩膀放松一下，好吗。",
        "care_motive": "JOURNAL_CARE_MOTIVE 我想让他感觉不是一个人在扛。",
        "why_i_want_to_say": "JOURNAL_WHY_SAY 我有点担心他一直绷着。",
        "user_need": "JOURNAL_USER_NEED 需要短短地被陪一下。",
        "message_seed": "JOURNAL_MESSAGE_SEED 先让肩膀放松。",
    }])

    ctx = journal._gather_context(date_str)
    dumped = json.dumps(ctx, ensure_ascii=False)

    assert "用户完成了一个真实工作节点" in dumped
    assert "JOURNAL_INNER_VISIBLE" in dumped
    assert "JOURNAL_USER_AFFECT_VISIBLE" in dumped
    assert "JOURNAL_SELF_EMOTION_VISIBLE" in dumped
    assert "JOURNAL_CARE_MOTIVE" in dumped
    assert ctx["inner_segments"][0]["duration"] == "30分钟"
    assert ctx["user_affect_segments"][0]["start"] == "11:05"
    assert ctx["self_emotion_segments"][0]["mood"] == "有点心疼"
    assert "SHOULD_NOT_APPEAR_SIG2" not in dumped
    assert "SHOULD_NOT_APPEAR_ATTENTION_LOG" not in dumped
    assert "SHOULD_NOT_APPEAR_ATTENTION_STATE" not in dumped
    assert "SHOULD_NOT_APPEAR_FOCUS" not in dumped
    assert "SHOULD_NOT_APPEAR_INTENT" not in dumped
    assert "SHOULD_NOT_APPEAR_DELIVERY" not in dumped


def test_journal_prompt_uses_segments_not_telemetry(tmp_path, monkeypatch):
    import journal
    import prompt

    _bind_data_dir(monkeypatch, tmp_path)
    captured = {}

    def fake_call(system, user_text, **kwargs):
        captured["system"] = system
        captured["user"] = user_text
        return {
            "title": "安静靠近的晚上",
            "mood": {"label": "挂念", "emoji": "🌙"},
            "narrative": "我把那些没说出口的担心收进了日记里。",
            "highlights": [],
        }

    monkeypatch.setattr(prompt, "_call_llm_json", fake_call)
    ctx = {
        "date": "2026-05-18",
        "inner_segments": [{
            "start": "10:00",
            "end": "10:40",
            "duration": "40分钟",
            "mood": "安静挂念",
            "valence": 0.1,
            "text": "PROMPT_INNER_SEGMENT 我想靠近他一点。",
        }],
        "user_affect_segments": [{
            "start": "10:05",
            "end": "10:50",
            "duration": "45分钟",
            "mood": "专注但疲惫",
            "valence": -0.2,
            "trend": "stable",
            "text": "PROMPT_USER_SEGMENT 他像是在硬撑。",
        }],
        "self_emotion_segments": [{
            "start": "10:10",
            "end": "10:55",
            "duration": "45分钟",
            "mood": "有点心疼",
            "valence": 0.08,
            "text": "PROMPT_SELF_SEGMENT 我有点心疼他。",
        }],
        "miru_proactive": [{
            "time": "11:00",
            "text": "先休息一下，好吗。",
            "why_i_want_to_say": "PROMPT_WHY_SAY 我想让他知道我在。",
            "user_need": "PROMPT_USER_NEED 被轻轻陪一下。",
            "message_seed": "PROMPT_MESSAGE_SEED 别一个人扛。",
        }],
    }

    result = journal._call_journal_llm(ctx)

    assert result and result["title"] == "安静靠近的晚上"
    user_prompt = captured["user"]
    assert "今天我没有说出口的想法" in user_prompt
    assert "今天状态的感觉" in user_prompt
    assert "我今天自己的心情状态" in user_prompt
    assert "PROMPT_INNER_SEGMENT" in user_prompt
    assert "PROMPT_USER_SEGMENT" in user_prompt
    assert "PROMPT_SELF_SEGMENT" in user_prompt
    assert "PROMPT_WHY_SAY" in user_prompt
    assert "采样日志" in user_prompt


def test_journal_prompt_prefers_daily_write_append_entries(tmp_path, monkeypatch):
    import journal
    import prompt

    _bind_data_dir(monkeypatch, tmp_path)
    captured = {}

    def fake_call(system, user_text, **kwargs):
        captured["user"] = user_text
        return {
            "title": "记忆慢慢落下",
            "mood": {"label": "安静", "emoji": "📝"},
            "narrative": "我把今天真正写进记忆里的东西整理了一遍。",
            "highlights": [],
        }

    monkeypatch.setattr(prompt, "_call_llm_json", fake_call)
    ctx = {
        "date": "2026-05-18",
        "slot_updates": [{
            "domain": "project",
            "id": "papers_2026",
            "title": "2026 论文投稿",
            "icon": "📄",
            "summary": "论文推进状态",
            "status": "active",
            "main_preview": "# 2026 论文投稿\n\n旧内容和近期记录。",
            "raw_writes": [{
                "time": "14:20",
                "action": "matched",
                "entry_kind": "progress",
                "source_type": "screenshot",
                "append_entry": "PROMPT_APPEND_ENTRY 整理完 ablation，准备检查论文段落。",
                "content": "PROMPT_APPEND_ENTRY 整理完 ablation，准备检查论文段落。",
                "raw_content_to_integrate": "SHOULD_NOT_APPEAR_RAW_SCREENSHOT_TEXT",
            }, {
                "time": "15:10",
                "action": "created",
                "slot_title": "Prompt 新卡",
                "slot_summary": "新建测试卡片",
                "initial_body": "PROMPT_INITIAL_BODY 新建了一张测试记忆卡。",
                "content": "PROMPT_INITIAL_BODY 新建了一张测试记忆卡。",
                "raw_content_to_integrate": "SHOULD_NOT_APPEAR_CREATED_RAW",
            }],
        }],
    }

    result = journal._call_journal_llm(ctx)

    assert result and result["title"] == "记忆慢慢落下"
    user_prompt = captured["user"]
    assert "今天实际写进记忆的内容" in user_prompt
    assert "PROMPT_APPEND_ENTRY" in user_prompt
    assert "新建记忆卡「Prompt 新卡」" in user_prompt
    assert "PROMPT_INITIAL_BODY" in user_prompt
    assert "追加/progress" in user_prompt
    assert "SHOULD_NOT_APPEAR_RAW_SCREENSHOT_TEXT" not in user_prompt
    assert "SHOULD_NOT_APPEAR_CREATED_RAW" not in user_prompt
    assert "screenshot" not in user_prompt


def test_attention_delivery_dryrun_does_not_mutate_chat_or_commitments(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        prompt,
        "call_proactive_resource_planner",
        lambda ctx: (_ for _ in ()).throw(AssertionError("resource planner should not run")),
    )

    commitments_dir = Path(tmp_path) / "memory" / "commitments"
    commitments_dir.mkdir(parents=True)
    active_path = commitments_dir / "active.md"
    active_content = (
        "# Active Commitments\n\n"
        "- [ ] 明天提交 AttentionEngine 测试 (deadline: 2099-01-01 12:00)  [added: 2026-05-18 10:00]\n"
    )
    active_path.write_text(active_content, encoding="utf-8")

    storage.write_json(storage.chat_history_path(), [{
        "id": "u1",
        "role": "user",
        "text": "我们先只看 dry-run。",
        "time": "2026-05-18 10:00:00",
    }])
    storage.save_attention_state({
        "attention": {"interruptibility": 0.9},
        "current_focus": {"topic_key": "attention_delivery", "state": "focused"},
    })
    storage.save_attention_intent_queue([{
        "id": "intent_delivery",
        "status": "pending",
        "priority": "high",
        "topic_key": "attention_delivery",
        "context_summary": "用户在验证主动消息链路。",
        "expires_at": "2099-01-01 00:00:00",
    }])

    before_chat = storage.read_json(storage.chat_history_path())
    before_active = active_path.read_text(encoding="utf-8")

    entry = core.evaluate_attention_delivery_preflight_once("intent_delivery")

    assert entry and entry["intent_id"] == "intent_delivery"
    assert entry["delivery_status"] == "dryrun_direct"
    assert entry["delivery_plan"]["direct_from_attention"] is True
    assert storage.read_json(storage.chat_history_path()) == before_chat
    assert active_path.read_text(encoding="utf-8") == before_active
    delivery_log = storage.get_recent_attention_delivery_log(limit=5)
    assert len(delivery_log) == 1
