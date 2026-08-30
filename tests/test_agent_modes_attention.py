import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_reactive_chat_context_includes_attention_compact_block(tmp_path, monkeypatch):
    import core
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({
        "updated_at": "2026-05-18 16:00:00",
        "current_inner": {
            "id": "inner_test",
            "channel": "inner",
            "topic_key": "attention_design",
            "state": "focused",
            "duration_seconds": 900,
            "tick_count": 3,
            "text": "我还在认真看他怎么审 Attention 架构。",
        },
        "current_user_affect": {
            "id": "affect_test",
            "channel": "user_affect",
            "mood": "认真",
            "valence": -0.1,
            "arousal": 0.45,
            "confidence": 0.8,
            "text": "我感觉他在要求第一性原理。",
        },
        "current_self_emotion": {
            "id": "self_test",
            "channel": "self_emotion",
            "mood": "专注",
            "valence": 0.1,
            "arousal": 0.35,
            "text": "我想把设计讲清楚。",
        },
        "attention": {"state": "focused", "topic_key": "attention_design"},
    })
    storage.append_attention_log({
        "id": "inner_test",
        "channel": "inner",
        "started_at": "2026-05-18 15:45:00",
        "updated_at": "2026-05-18 16:00:00",
        "duration_seconds": 900,
        "ts": "2026-05-18 16:00:00",
        "decision": "observe",
        "text": "我先不要急着发主动消息。",
    })
    storage.save_attention_intent_queue([{
        "id": "intent_test",
        "status": "pending",
        "priority": "medium",
        "topic_key": "attention_design",
        "context_summary": "用户正在评审 Attention 主动链路。",
    }])

    ctx = core._build_chat_context()

    assert "我最近没有说出口的想法" in ctx
    assert "我对用户状态的感觉" in ctx
    assert "attention_design" in ctx
    assert "我已经想说但还没说出口的意图" in ctx
    assert "不要提系统名、日志、检测或 AttentionEngine" in ctx


def test_attention_delivery_dryrun_uses_direct_plan_without_fast_llm_gate(tmp_path, monkeypatch):
    import core
    import storage
    import prompt

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({
        "attention": {"interruptibility": 0.0},
    })
    intent = {
        "id": "intent_low",
        "status": "pending",
        "priority": "low",
        "topic_key": "debug_stuck",
        "context_summary": "用户像是在 debug 卡住。",
        "expires_at": "2099-01-01 00:00:00",
    }
    storage.save_attention_intent_queue([intent])

    with patch.object(prompt, "call_proactive_delivery_preflight", side_effect=AssertionError("preflight should not run")), \
         patch.object(prompt, "call_proactive_resource_planner", side_effect=AssertionError("resource planner should not run")):
        entry = core.evaluate_attention_delivery_preflight_once()

    assert entry["decision"] == "send_now"
    assert entry["delivery_status"] == "dryrun_direct"
    assert entry["delivery_plan"]["direct_from_attention"] is True
    assert entry["hard_rule_reasons"] == []
    assert entry["model_mode"] == "v4_pro"
    assert entry["tool_policy"] == "allow_readonly"
    assert entry["resource_plan"] is None
    assert entry["policy"]["policy_reason"] == "main_agent_fixed_pro"
    log = storage.get_recent_attention_delivery_log(limit=1)
    assert log and log[0]["intent_id"] == "intent_low"


def test_attention_delivery_send_now_appends_chat_push_and_marks_intent(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({
        "attention": {"interruptibility": 0.9},
        "current_focus": {"topic_key": "debug_stuck", "state": "concerned"},
    })
    storage.save_attention_intent_queue([{
        "id": "intent_send",
        "status": "pending",
        "priority": "high",
        "topic_key": "debug_stuck",
        "context_summary": "用户明确卡在调试里。",
        "expires_at": "2099-01-01 00:00:00",
    }])
    def fake_agent(*args, **kwargs):
        assert kwargs["tier"] == "chat"
        assert kwargs["reasoning"] is False
        assert kwargs["max_iterations"] == 2
        tool_names = {
            t.get("function", {}).get("name")
            for t in kwargs.get("tools_list", [])
        }
        assert "archival_memory_search" in tool_names
        return {"reply": "要不要我陪你把这个卡点拆一下？", "tool_calls": []}

    with patch.object(prompt, "call_proactive_delivery_preflight", side_effect=AssertionError("preflight should not run")), \
         patch.object(prompt, "call_proactive_resource_planner", side_effect=AssertionError("resource planner should not run")), \
         patch.object(prompt, "call_proactive_agent", side_effect=fake_agent):
        entry = core.deliver_attention_intent_once("intent_send")

    assert entry["decision"] == "send_now"
    assert entry["delivery_status"] == "delivered"
    assert entry["delivery_plan"]["direct_from_attention"] is True
    history = storage.get_chat_history(limit=10)
    assert len(history) == 1
    msg = history[0]
    assert msg["role"] == "assistant"
    assert msg["type"] == "proactive"
    assert msg["source"] == "attention_engine"
    assert msg["intent_id"] == "intent_send"
    assert "卡点" in msg["text"]

    queue = storage.load_attention_intent_queue()
    assert queue[0]["status"] == "delivered"
    assert queue[0]["message_id"] == msg["id"]
    notifs = storage.read_json(storage.pending_notifications_path())
    assert notifs and notifs[0]["id"] == msg["id"]
    log = storage.get_recent_attention_delivery_log(limit=1)
    assert log and log[0]["delivery_status"] == "delivered"


def test_attention_delivery_same_intent_is_single_flight(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({})
    storage.save_attention_intent_queue([{
        "id": "intent_race",
        "status": "pending",
        "priority": "medium",
        "topic_key": "academic_review",
        "context_summary": "用户正在审阅论文。",
        "expires_at": "2099-01-01 00:00:00",
    }])

    agent_entered = threading.Event()
    release_agent = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()
    results = {}

    def fake_agent(*args, **kwargs):
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        agent_entered.set()
        assert release_agent.wait(timeout=5)
        return {"reply": "这次只应该发送一条。", "tool_calls": []}

    def run_delivery(result_key):
        try:
            results[result_key] = core.deliver_attention_intent_once("intent_race")
        except Exception as exc:  # surfaced by assertions below
            results[result_key] = exc

    with patch.object(prompt, "call_proactive_agent", side_effect=fake_agent):
        first = threading.Thread(target=run_delivery, args=("first",))
        second = threading.Thread(target=run_delivery, args=("second",))
        first.start()
        try:
            assert agent_entered.wait(timeout=3)
            second.start()
            second.join(timeout=3)
            assert not second.is_alive()
            assert results.get("second") is None
            assert call_count == 1
        finally:
            release_agent.set()
            first.join(timeout=5)
            if second.ident is not None:
                second.join(timeout=5)

    assert not first.is_alive()
    assert not isinstance(results.get("first"), Exception)
    assert results["first"]["delivery_status"] == "delivered"
    assert call_count == 1

    history = storage.get_chat_history(limit=10)
    assert len(history) == 1
    assert history[0]["intent_id"] == "intent_race"
    notifications = storage.read_json(storage.pending_notifications_path())
    assert len(notifications) == 1
    delivery_log = storage.get_recent_attention_delivery_log(limit=10)
    assert len(delivery_log) == 1
    assert delivery_log[0]["intent_id"] == "intent_race"
    queue = storage.load_attention_intent_queue()
    assert queue[0]["status"] == "delivered"
    assert queue[0]["message_id"] == history[0]["id"]


def test_attention_delivery_claim_releases_after_failure(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({})
    storage.save_attention_intent_queue([{
        "id": "intent_retry",
        "status": "pending",
        "priority": "medium",
        "topic_key": "retry_topic",
        "context_summary": "第一次生成失败后应该允许重试。",
        "expires_at": "2099-01-01 00:00:00",
    }])
    responses = iter([
        {"reply": "", "tool_calls": []},
        {"reply": "第二次生成成功。", "tool_calls": []},
    ])

    with patch.object(prompt, "call_proactive_agent", side_effect=lambda *a, **k: next(responses)):
        failed = core.deliver_attention_intent_once("intent_retry")
        delivered = core.deliver_attention_intent_once("intent_retry")

    assert failed["delivery_status"] == "failed"
    assert delivered["delivery_status"] == "delivered"
    assert len(storage.get_chat_history(limit=10)) == 1


def test_attention_delivery_inflight_claim_is_scoped_by_user():
    import core

    try:
        assert core._claim_attention_delivery("u_one", "intent_same") is True
        assert core._claim_attention_delivery("u_one", "intent_same") is False
        assert core._claim_attention_delivery("u_two", "intent_same") is True
    finally:
        core._release_attention_delivery("u_one", "intent_same")
        core._release_attention_delivery("u_two", "intent_same")


def test_attention_delivery_does_not_veto_on_interruptibility_score(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({
        "attention": {"interruptibility": 0.0},
        "current_focus": {"topic_key": "miru_review", "state": "focused"},
    })
    storage.save_attention_intent_queue([{
        "id": "intent_warm",
        "status": "pending",
        "priority": "low",
        "topic_key": "miru_review",
        "context_summary": "用户一直在审 Miru 的主动关心链路，适合一句低负担的在场感。",
        "why_now": "AttentionEngine 判断现在适合轻轻说一句。",
        "suggested_tone": "短、轻、不索取回复",
        "avoid": "不要像系统通知",
        "expires_at": "2099-01-01 00:00:00",
    }])
    def fake_agent(*args, **kwargs):
        assert kwargs["tier"] == "chat"
        assert kwargs["tools_list"]
        return {"reply": "我在这儿，你慢慢审就好。刚才这轮改动我会陪你盯稳一点。", "tool_calls": []}

    with patch.object(prompt, "call_proactive_delivery_preflight", side_effect=AssertionError("preflight should not run")), \
         patch.object(prompt, "call_proactive_resource_planner", side_effect=AssertionError("resource planner should not run")), \
         patch.object(prompt, "call_proactive_agent", side_effect=fake_agent):
        entry = core.deliver_attention_intent_once("intent_warm")

    assert entry["decision"] == "send_now"
    assert entry["delivery_status"] == "delivered"
    assert entry["hard_rule_reasons"] == []
    history = storage.get_chat_history(limit=10)
    assert len(history) == 1
    assert history[0]["type"] == "proactive"
    assert "慢慢审" in history[0]["text"]


def test_attention_delivery_ignores_expired_intent_without_chat(tmp_path, monkeypatch):
    import core
    import prompt
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_intent_queue([{
        "id": "intent_expired",
        "status": "pending",
        "priority": "medium",
        "topic_key": "old_topic",
        "context_summary": "过期的主动意图。",
        "expires_at": "2000-01-01 00:00:00",
    }])

    with patch.object(prompt, "call_proactive_agent", side_effect=AssertionError("expired intent should not deliver")):
        entry = core.deliver_attention_intent_once("intent_expired")

    assert entry is None
    assert storage.get_chat_history(limit=10) == []


def test_proactive_policy_ignores_planner_and_uses_fixed_pro_readonly():
    import core

    policy = core._direct_attention_delivery_policy(
        {"id": "intent_low"},
        {
            "model_mode": "fast",
            "confidence": 0.2,
            "tool_policy": "allow_all",
            "complexity": "simple",
            "reasoning_need": "none",
        },
    )

    assert policy["model_mode"] == "v4_pro"
    assert policy["tool_policy"] == "allow_readonly"
    assert policy["reasoning"] is False
    assert policy["tier"] == "chat"
    assert policy["max_iterations"] == 2
    assert policy["policy_reason"] == "main_agent_fixed_pro"


def test_proactive_policy_never_auto_enables_reasoning():
    import core

    normal = core._direct_attention_delivery_policy(
        {"id": "intent_normal"},
        {
            "model_mode": "v4_pro_reasoning",
            "confidence": 0.9,
            "tool_policy": "allow_readonly",
            "complexity": "normal",
            "reasoning_need": "low",
        },
    )
    hard = core._direct_attention_delivery_policy(
        {"id": "intent_hard"},
        {
            "model_mode": "v4_pro_reasoning",
            "confidence": 0.9,
            "tool_policy": "allow_readonly",
            "complexity": "hard",
            "reasoning_need": "high",
        },
    )

    assert normal["model_mode"] == "v4_pro"
    assert normal["reasoning"] is False
    assert hard["model_mode"] == "v4_pro"
    assert hard["reasoning"] is False
    assert hard["tier"] == "chat"


def test_direct_attention_delivery_seed_is_inspiration_not_must_include():
    import core

    plan = core._build_direct_attention_delivery_plan({
        "id": "intent_warm",
        "topic_key": "warm_presence",
        "care_motive": "我想像一起看番一样自然插一句。",
        "user_need": "低负担的陪伴",
        "context_summary": "用户长时间工作后切到放松。",
        "approach": "co_watch",
        "content_anchor": "他切到番剧放松，剧情节奏很慢",
        "miru_impulse": "想跟着评论一下",
        "silent_boundaries": "不要硬猜角色名",
        "avoid": "不要把内部边界说出口",
        "message_seed": "天亮了。我在。",
    })

    brief = plan["delivery_brief"]
    assert plan["decision"] == "send_now"
    assert brief["must_include"] == []
    assert brief["message_seed"] == "天亮了。我在。"
    assert brief["approach"] == "co_watch"
    assert brief["content_anchor"] == "他切到番剧放松，剧情节奏很慢"
    assert brief["miru_impulse"] == "想跟着评论一下"
    assert brief["silent_boundaries"] == "不要硬猜角色名"
    assert "不要把内部边界说出口" not in brief["must_avoid"]
    assert any("不要把“我不吵你" in item for item in brief["must_avoid"])
    assert "不要把这些限制原样说给用户" in brief["silent_boundaries_usage"]
    assert "不是必须逐字包含" in brief["message_seed_usage"]
    assert "自然改写" in brief["message_seed_usage"]
    assert "状态灯" in brief["tone"]


def test_legacy_proactive_resource_planner_filters_to_index_paths(monkeypatch):
    import prompt

    def fake_retrieval(
        system_prompt, user_text, temperature=0.1, max_tokens=None,
        tier="memory", call_label="retrieval_llm",
    ):
        assert "send/defer/drop" in system_prompt
        assert "Attention Delivery Context" in user_text
        return {
            "model_mode": "v4_pro",
            "confidence": 0.9,
            "complexity": "normal",
            "reasoning_need": "low",
            "tool_policy": "allow_readonly",
            "memory_files": ["projects/ok/main.md", "projects/not_in_index/main.md"],
            "keywords": ["Miru"],
            "context_brief": "需要读一个真实 slot。",
            "routing_reason": "测试路径过滤",
        }

    monkeypatch.setattr(prompt, "_call_retrieval_llm", fake_retrieval)
    plan = prompt.call_proactive_resource_planner(
        "用户正在 debug 主动消息。",
        index_content="# Index\n- `projects/ok/main.md`\n",
    )

    assert plan["memory_files"] == ["projects/ok/main.md"]
    assert plan["tool_policy"] == "allow_readonly"


def test_proactive_agent_preview_includes_recent_psychological_activity(tmp_path, monkeypatch):
    import core
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.save_attention_state({
        "current_inner": {
            "id": "inner_preview",
            "channel": "inner",
            "topic_key": "proactive_design",
            "state": "focused",
            "tick_count": 2,
            "duration_seconds": 600,
            "text": "我在确认主动开口链路。",
        },
        "current_self_emotion": {"id": "self_preview", "channel": "self_emotion", "mood": "认真", "text": "我想把话说得不打扰"},
    })

    preview = core.build_proactive_agent_preview(
        {"topic_key": "proactive_design", "context_summary": "用户在评审主动链路。"},
        {"delivery_brief": {"goal": "轻轻跟进", "tone": "短、轻", "context_summary": "继续保持理解。"}},
    )

    assert "Proactive Mode" in preview["system_prompt"]
    assert "【我最近没有说出口的想法】" in preview["context_text"]
    assert "我最近没有说出口的想法" in preview["context_text"]
    assert "proactive_design" in preview["context_text"]


def test_proactive_agent_system_prompt_marks_mode():
    import prompt

    system = prompt._build_agent_system_prompt(agent_mode="proactive")
    user_msg = prompt.build_proactive_agent_user_message(
        {"topic_key": "debug_stuck", "context_summary": "用户像是在调试。"},
        {
            "decision": "send_now",
            "confidence": 0.9,
            "reason": "明确卡住",
            "delivery_brief": {
                "goal": "轻轻问一句",
                "tone": "短、轻",
                "context_summary": "用户卡在调试。",
                "care_motive": "我想陪他把问题拆开。",
                "user_need": "被稳稳陪着",
                "approach": "deep_work",
                "content_anchor": "用户卡在调试。",
                "miru_impulse": "想稳一下他",
                "silent_boundaries": "不要像系统通知",
                "silent_boundaries_usage": "silent_boundaries 是内部边界，不要原样说给用户。",
                "message_seed": "我在这儿。",
                "message_seed_usage": "message_seed 只是情绪方向和开口灵感，不是必须逐字包含的句子。",
                "must_avoid": ["不要说检测到"],
            },
        },
    )

    assert "Proactive Mode" in system
    assert "用户没有刚刚问你问题" in system
    assert "我最近没有说出口的想法" in system
    assert "Reactive Mode 还是 Proactive Mode" in system
    assert "不是判断要不要说" in system
    assert "短而有体温" in system
    assert "不要像状态提示灯亮一下" in system
    assert "一起生活的 Miru" in system
    assert "想吐槽、想一起看、想夸他、想心疼他" in system
    assert "默认要短、轻、克制" not in system
    assert "只输出最终要发给用户的一条消息" in user_msg
    assert "不要提系统" in user_msg
    assert "message_seed: 我在这儿。" in user_msg
    assert "不是要照抄的原文" in user_msg
    assert "短但不能冷" in user_msg
    assert "approach: deep_work" in user_msg
    assert "content_anchor: 用户卡在调试。" in user_msg
    assert "silent_boundaries: 不要像系统通知" in user_msg
    assert "不要把它原样说给用户" in user_msg
    assert "co_watch" in user_msg
    assert "我不吵你/不打扰你/需要我就叫我" in user_msg


def test_attention_engine_schedules_direct_delivery(tmp_path, monkeypatch):
    import attention_engine
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MIRU_ENABLE_ATTENTION_DELIVERY_DRYRUN_IN_TESTS", "1")
    engine = attention_engine.AttentionEngine(user_id="u_test", user_data_dir=str(tmp_path))
    called = []

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(attention_engine.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(engine, "_run_delivery", lambda intent_id: called.append(intent_id))

    now = attention_engine.datetime.now()
    intent = {"topic_key": "debug_stuck"}
    queue = [{
        "id": "intent_debug",
        "status": "pending",
        "topic_key": "debug_stuck",
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }]

    engine._schedule_delivery_if_needed(intent, queue, {}, now)
    assert called == ["intent_debug"]

    called.clear()
    storage.append_attention_delivery_log({
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "intent_id": "intent_debug",
    })
    engine._schedule_delivery_if_needed(intent, queue, {}, now)
    assert called == []


def test_attention_engine_delivery_runner_calls_live_path_by_default(tmp_path, monkeypatch):
    import attention_engine
    import core

    engine = attention_engine.AttentionEngine(user_id="u_test", user_data_dir="")
    called = []
    monkeypatch.delenv("MIRU_ATTENTION_DELIVERY_DRYRUN", raising=False)
    monkeypatch.setattr(core, "deliver_attention_intent_once", lambda intent_id: called.append(intent_id))

    engine._run_delivery("intent_live")

    assert called == ["intent_live"]


def test_attention_engine_delivery_runner_can_be_forced_to_dryrun(tmp_path, monkeypatch):
    import attention_engine
    import core

    engine = attention_engine.AttentionEngine(user_id="u_test", user_data_dir="")
    called = []
    monkeypatch.setenv("MIRU_ATTENTION_DELIVERY_DRYRUN", "1")
    monkeypatch.setattr(core, "evaluate_attention_delivery_preflight_once", lambda intent_id: called.append(intent_id))

    engine._run_delivery("intent_dry")

    assert called == ["intent_dry"]
