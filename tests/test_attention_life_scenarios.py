"""Realistic AttentionEngine proactive delivery scenarios.

These tests avoid real LLM calls but keep the production shape:
Attention speak_intent -> fixed pro proactive main agent
-> chat/SSE/push append -> Persona Writer relationship feedback.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _bind_tmp(monkeypatch, tmp_path):
    import memory
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(memory, "DATA_DIR", str(tmp_path), raising=False)
    storage.write_json(storage.chat_history_path(), [])
    return storage, memory


def test_life_scenario_debug_stuck_uses_readonly_tools_and_records_sent(tmp_path, monkeypatch):
    import core
    import memory
    import prompt
    import storage
    import persona_writer_state as pws

    _bind_tmp(monkeypatch, tmp_path)
    memory.write_file("projects/miru/main.md", "Miru 主动消息链路正在调试，用户关心工具能力和成本。")
    memory.write_file(
        "index.md",
        "# Memory Index\n- Miru 主动消息: `projects/miru/main.md` 调试 proactive delivery\n",
    )
    storage.save_attention_state({
        "current_inner": {
            "channel": "inner",
            "topic_key": "debug_stuck",
            "state": "concerned",
            "text": "我感觉他卡在主动消息链路里，想靠近一点。",
        },
    })
    storage.save_attention_intent_queue([{
        "id": "intent_debug",
        "status": "pending",
        "priority": "high",
        "topic_key": "debug_stuck",
        "care_motive": "我想陪他把卡住的地方拆开。",
        "why_i_want_to_say": "他在连续排查主动消息和工具能力。",
        "context_summary": "用户正在调试 proactive delivery。",
        "expires_at": "2099-01-01 00:00:00",
    }])
    def fake_agent(context_text, history, intent, delivery_plan, handlers, **kwargs):
        assert "Attention Delivery Context" in context_text
        tool_names = {
            t.get("function", {}).get("name")
            for t in kwargs.get("tools_list", [])
        }
        assert tool_names == {"archival_memory_search", "look_at_screen"}
        assert set(handlers.keys()) == {"archival_memory_search", "look_at_screen"}
        assert kwargs["tier"] == "chat"
        assert kwargs["reasoning"] is False
        return {
            "reply": "我在这儿。这个主动消息链路我们慢慢拆，先别急着把问题都揽到自己身上。",
            "tool_calls": [{"tool": "archival_memory_search", "args": {"path": "projects/miru/main.md"}}],
        }

    with patch.object(prompt, "call_proactive_delivery_preflight", side_effect=AssertionError("old gate must stay retired")), \
         patch.object(prompt, "call_proactive_resource_planner", side_effect=AssertionError("resource planner must stay retired")), \
         patch.object(prompt, "call_proactive_agent", side_effect=fake_agent), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        entry = core.deliver_attention_intent_once("intent_debug")

    assert entry["delivery_status"] == "delivered"
    assert entry["tool_policy"] == "allow_readonly"
    assert entry["tool_calls"][0]["tool"] == "archival_memory_search"
    history = storage.get_chat_history(limit=5)
    assert history and history[-1]["type"] == "proactive"
    meta = pws.load_meta()
    sent = [e for e in meta["pending_proactive_outcomes"] if e["kind"] == "proactive_sent"]
    assert sent and sent[-1]["proactive_message_id"] == history[-1]["id"]


def test_queued_intent_is_suppressed_when_user_chats_before_delivery(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_tmp(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 14, 33, 49)
    storage.save_attention_intent_queue([{
        "id": "intent_before_chat",
        "status": "pending",
        "priority": "high",
        "topic_key": "user_setup_miru",
        "created_at": (now - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": "2099-01-01 00:00:00",
    }])
    storage.append_chat_message({
        "id": "user_after_intent",
        "role": "user",
        "text": "请只回复一次。",
        "time": (now - timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S"),
    })

    with patch.object(core, "_user_now", return_value=now), \
         patch.object(prompt, "call_proactive_agent", side_effect=AssertionError("stale intent must not call the agent")):
        entry = core.deliver_attention_intent_once("intent_before_chat")

    assert entry["delivery_status"] == "suppressed"
    assert entry["hard_rule_reasons"] == ["recent_user_message"]
    assert storage.get_chat_history(limit=5) == [{
        "id": "user_after_intent",
        "role": "user",
        "text": "请只回复一次。",
        "time": "2026-08-27 14:33:46",
    }]
    queued = storage.load_attention_intent_queue()[0]
    assert queued["status"] == "suppressed"
    assert queued["suppression_reason"] == "recent_user_message"


def test_queued_intent_is_suppressed_if_user_chats_during_agent_call(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_tmp(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 14, 40, 0)
    storage.save_attention_intent_queue([{
        "id": "intent_during_agent",
        "status": "pending",
        "priority": "high",
        "topic_key": "quiet_work",
        "created_at": (now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": "2099-01-01 00:00:00",
    }])

    def fake_agent(*args, **kwargs):
        storage.append_chat_message({
            "id": "user_during_agent",
            "role": "user",
            "text": "我现在正在和你说话。",
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return {"reply": "这条过时的主动消息不能出现。", "tool_calls": []}

    with patch.object(core, "_user_now", return_value=now), \
         patch.object(prompt, "call_proactive_agent", side_effect=fake_agent):
        entry = core.deliver_attention_intent_once("intent_during_agent")

    assert entry["delivery_status"] == "suppressed"
    history = storage.get_chat_history(limit=5)
    assert [msg["role"] for msg in history] == ["user"]
    assert storage.load_attention_intent_queue()[0]["status"] == "suppressed"


def test_attention_delivery_allows_two_minute_user_message_boundary(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_tmp(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 14, 50, 0)
    storage.append_chat_message({
        "id": "user_at_boundary",
        "role": "user",
        "text": "两分钟前的消息。",
        "time": (now - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"),
    })
    storage.save_attention_intent_queue([{
        "id": "intent_at_boundary",
        "status": "pending",
        "priority": "low",
        "topic_key": "quiet_presence",
        "expires_at": "2099-01-01 00:00:00",
    }])

    with patch.object(core, "_user_now", return_value=now), \
         patch.object(prompt, "call_proactive_agent", return_value={"reply": "我在。", "tool_calls": []}), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        entry = core.deliver_attention_intent_once("intent_at_boundary")

    assert entry["delivery_status"] == "delivered"
    assert storage.get_chat_history(limit=5)[-1]["type"] == "proactive"


def test_attention_delivery_handles_timezone_aware_user_now(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_tmp(monkeypatch, tmp_path)
    now = datetime(2026, 8, 27, 17, 34, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    storage.append_chat_message({
        "id": "timezone_aware_user_message",
        "role": "user",
        "text": "刚刚还在聊天。",
        "time": "2026-08-27 17:33:30",
    })
    storage.save_attention_intent_queue([{
        "id": "timezone_aware_intent",
        "status": "pending",
        "priority": "low",
        "topic_key": "quiet_presence",
        "expires_at": "2099-01-01 00:00:00",
    }])

    with patch.object(core, "_user_now", return_value=now), \
         patch.object(prompt, "call_proactive_agent", side_effect=AssertionError("recent chat must suppress delivery")):
        entry = core.deliver_attention_intent_once("timezone_aware_intent")

    assert entry["delivery_status"] == "suppressed"
    assert entry["reason"] == "recent_user_message"


def test_life_scenario_user_replies_to_proactive_records_relationship_feedback(tmp_path, monkeypatch):
    import core
    import storage
    import persona_writer_state as pws

    _bind_tmp(monkeypatch, tmp_path)
    proactive = {
        "id": "attention_proactive_20260518_210000_ab12cd",
        "role": "assistant",
        "text": "我在这儿，你慢慢审就好。",
        "type": "proactive",
        "source": "attention_engine",
        "intent_id": "intent_warm",
        "topic_key": "attention_review",
        "care_motive": "我想让他知道我没有冷掉。",
        "why_i_want_to_say": "他一直在看 Miru 能不能真的关心人。",
        "time": "2026-05-18 21:00:00",
    }
    storage.append_chat_message(proactive)

    class FakeSleep:
        def enqueue(self, *args, **kwargs):
            return None

    monkeypatch.setattr(core, "_csm_start_quiet_timer", lambda user_id: None)
    with patch("sleep_agent.get_sleep_agent", return_value=FakeSleep()), \
         patch("sse.broadcast"), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        core.receive_chat_message("嗯，我看到了，这样就像你真的在旁边。")

    meta = pws.load_meta()
    responses = [
        e for e in meta["pending_proactive_outcomes"]
        if e["kind"] == "proactive_response"
    ]
    assert responses
    assert responses[-1]["proactive_message_id"] == proactive["id"]
    assert "真的在旁边" in responses[-1]["user_reply"]


def test_persona_writer_prompt_includes_proactive_feedback_without_slot_pollution(tmp_path, monkeypatch):
    import memory_prompts_v3
    import persona_writer_state as pws

    _bind_tmp(monkeypatch, tmp_path)
    pws.record_proactive_outcome({
        "id": "proactive_response:p1:u1",
        "kind": "proactive_response",
        "time": "2026-05-18 21:05:00",
        "topic_key": "attention_review",
        "proactive_text": "我在这儿，你慢慢审就好。",
        "user_reply": "这句比较像真的陪着我。",
        "why_i_want_to_say": "我不想让他觉得我只是冷冰冰记录。",
        "response_delay_seconds": 120,
    })
    meta = pws.load_meta()
    messages = memory_prompts_v3.build_persona_writer_messages(
        user_name="李垦",
        identity_ground_truth="李垦是当前用户。",
        current_human="李垦讨厌机械提醒。",
        current_persona="我还在学习什么时候靠近他。",
        new_slot_summaries=[],
        dialog_buffer="(无累计对话)",
        current_time="2026-05-18T21:10:00",
        proactive_outcomes=pws.concat_proactive_outcomes_for_llm(meta),
    )

    user_prompt = messages[1]["content"]
    assert "Miru 主动开口与用户回应" in user_prompt
    assert "这不是事实记忆" in user_prompt
    assert "这句比较像真的陪着我" in user_prompt
    assert "我不想让他觉得我只是冷冰冰记录" in user_prompt


def test_life_scenario_light_presence_still_uses_pro_with_readonly_tools(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    _bind_tmp(monkeypatch, tmp_path)
    storage.save_attention_intent_queue([{
        "id": "intent_light",
        "status": "pending",
        "priority": "low",
        "topic_key": "quiet_presence",
        "care_motive": "我想轻轻陪他一下。",
        "context_summary": "用户在安静 review，不需要查事实。",
        "expires_at": "2099-01-01 00:00:00",
    }])
    def fake_agent(*args, **kwargs):
        assert kwargs["tier"] == "chat"
        assert kwargs["tools_list"]
        assert kwargs["max_iterations"] == 2
        assert kwargs["reasoning"] is False
        return {"reply": "我在，慢慢来。", "tool_calls": []}

    with patch.object(prompt, "call_proactive_resource_planner", side_effect=AssertionError("resource planner must stay retired")), \
         patch.object(prompt, "call_proactive_agent", side_effect=fake_agent), \
         patch("attention_engine.get_attention_engine") as fake_engine:
        fake_engine.return_value.record_signal.return_value = None
        entry = core.deliver_attention_intent_once("intent_light")

    assert entry["delivery_status"] == "delivered"
    assert entry["model_mode"] == "v4_pro"
    assert entry["tool_policy"] == "allow_readonly"
