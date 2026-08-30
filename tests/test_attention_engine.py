import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _emotion_log_for(storage, when):
    return storage.get_emotion_log_by_date(when.strftime("%Y-%m-%d"))


def _attention_result_with_intent(topic="work_context"):
    return {
        "thought": "我注意到用户还在处理这件事。",
        "user_affect": {
            "mood": "专注",
            "valence": 0.05,
            "arousal": 0.4,
            "confidence": 0.75,
            "trend": "stable",
            "evidence": "最近的信号显示用户仍在工作",
        },
        "miru_inner": {
            "mood": "关切",
            "valence": 0.1,
            "arousal": 0.3,
            "reason": "我想在合适的时候陪一下用户",
        },
        "attention": {
            "state": "observing",
            "topic_key": topic,
            "novelty": 0.6,
            "concern_level": 0.4,
            "next_check_seconds": 180,
        },
        "speak_intent": {
            "priority": "medium",
            "topic_key": topic,
            "why_now": "现在可能适合轻轻靠近一下",
            "suggested_tone": "自然、简短",
            "avoid": "不要打断",
            "context_summary": "用户仍在处理当前任务",
        },
    }


def test_screenshot_sig_below_2_is_dropped(tmp_path):
    from attention_engine import AttentionEngine

    engine = AttentionEngine(user_id="u", user_data_dir=str(tmp_path))
    accepted = engine.record_signal("screenshot", {
        "observation": "blank desktop",
        "significance": 1,
    })

    assert accepted is False
    assert len(engine._signals) == 0


def test_screenshot_sig_2_enters_attention_buffer(tmp_path):
    from attention_engine import AttentionEngine

    engine = AttentionEngine(user_id="u", user_data_dir=str(tmp_path))
    accepted = engine.record_signal("screenshot", {
        "observation": "用户在读一篇技术文档",
        "significance": 2,
    })

    assert accepted is True
    assert len(engine._signals) == 1
    sig = engine._signals[0]
    assert sig["salience"] == "weak"
    assert sig["observation"] == "用户在读一篇技术文档"


def test_strong_signal_schedules_earlier_than_weak(tmp_path):
    from attention_engine import AttentionEngine

    engine = AttentionEngine(user_id="u", user_data_dir=str(tmp_path))
    now = time.time()

    with patch("attention_engine.random.uniform", return_value=240):
        engine.record_signal("screenshot", {
            "observation": "普通读文档",
            "significance": 2,
        })
    weak_tick = engine._next_tick_at

    with patch("attention_engine.random.uniform", return_value=12):
        engine.record_signal("chat_in", {"text": "我有点卡住了"})
    strong_tick = engine._next_tick_at

    assert weak_tick >= now + 180
    assert strong_tick < weak_tick


def test_snapshot_tracks_only_new_trigger_kinds_after_duplicate_merge(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))

    engine.record_signal("chat_in", {"text": "我还在调试"})
    first = engine._build_snapshot(trigger="signals")
    assert first["trigger_signal_kinds"] == ["chat_in"]
    engine._last_evaluated_signal_seq = first["trigger_signal_seq_max"]

    screenshot = {
        "observation": "用户正在查看同一个终端窗口",
        "significance": 3,
        "device_id": "mac",
    }
    engine.record_signal("screenshot", screenshot)
    second = engine._build_snapshot(trigger="signals")
    assert second["trigger_signal_kinds"] == ["screenshot"]
    engine._last_evaluated_signal_seq = second["trigger_signal_seq_max"]

    engine.record_signal("screenshot", screenshot)
    merged = engine._build_snapshot(trigger="signals")
    assert merged["trigger_signal_kinds"] == ["screenshot"]


def test_chat_only_tick_suppresses_proactive_intent_but_keeps_state(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    schedule = MagicMock()
    monkeypatch.setattr(engine, "_schedule_delivery_if_needed", schedule)
    now = datetime.now().replace(microsecond=0)

    engine._apply_result(_attention_result_with_intent("chat_topic"), {
        "now": now,
        "trigger": "signals",
        "recent_signals": [{"kind": "chat_in"}, {"kind": "chat_out"}],
        "trigger_signal_kinds": ["chat_in", "chat_out"],
        "distances": {"since_user_msg": 10 * 60},
    })

    state = storage.load_attention_state()
    assert state["decision"] == "speak_suppressed"
    assert state["speak_suppression_reason"] == "reactive_chat_only"
    assert state["speak_intent"] is None
    assert state["suppressed_speak_intent"]["topic_key"] == "chat_topic"
    assert state["current_inner"]["text"] == "我注意到用户还在处理这件事。"
    assert storage.load_attention_intent_queue() == []
    assert not schedule.called
    inner_rows = [
        item for item in storage.get_recent_attention_log(limit=5)
        if item.get("channel") == "inner"
    ]
    assert inner_rows[-1]["speak_intent"] is None
    assert inner_rows[-1]["suppressed_speak_intent"]["topic_key"] == "chat_topic"


def test_recent_user_message_suppresses_mixed_signal_proactive_intent(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    schedule = MagicMock()
    monkeypatch.setattr(engine, "_schedule_delivery_if_needed", schedule)
    now = datetime.now().replace(microsecond=0)

    engine._apply_result(_attention_result_with_intent("mixed_topic"), {
        "now": now,
        "trigger": "signals",
        "recent_signals": [{"kind": "chat_in"}, {"kind": "screenshot"}],
        "trigger_signal_kinds": ["chat_in", "screenshot"],
        "distances": {"since_user_msg": 119},
    })

    state = storage.load_attention_state()
    assert state["decision"] == "speak_suppressed"
    assert state["speak_suppression_reason"] == "recent_user_message"
    assert storage.load_attention_intent_queue() == []
    assert not schedule.called

    engine._apply_result(_attention_result_with_intent("mixed_topic"), {
        "now": now + timedelta(seconds=1),
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
        "trigger_signal_kinds": ["screenshot"],
        "distances": {"since_user_msg": 120},
    })
    allowed = storage.load_attention_state()
    assert allowed["decision"] == "speak_intent"
    assert allowed["speak_suppression_reason"] == ""
    assert len(storage.load_attention_intent_queue()) == 1
    assert schedule.call_count == 1


def test_proactive_global_interval_blocks_before_15_minutes_and_allows_boundary(
        tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    schedule = MagicMock()
    monkeypatch.setattr(engine, "_schedule_delivery_if_needed", schedule)
    accepted_at = datetime.now().replace(microsecond=0)
    storage.save_attention_intent_queue([{
        "id": "intent_previous",
        "status": "delivered",
        "topic_key": "previous_topic",
        "created_at": accepted_at.strftime("%Y-%m-%d %H:%M:%S"),
        "delivered_at": accepted_at.strftime("%Y-%m-%d %H:%M:%S"),
    }])

    engine._apply_result(_attention_result_with_intent("new_topic"), {
        "now": accepted_at + timedelta(minutes=14, seconds=59),
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
        "trigger_signal_kinds": ["screenshot"],
        "distances": {"since_user_msg": 10 * 60},
    })
    blocked = storage.load_attention_state()
    assert blocked["speak_suppression_reason"] == "recent_accepted_intent"
    assert len(storage.load_attention_intent_queue()) == 1
    assert not schedule.called

    engine._apply_result(_attention_result_with_intent("new_topic"), {
        "now": accepted_at + timedelta(minutes=15),
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
        "trigger_signal_kinds": ["screenshot"],
        "distances": {"since_user_msg": 10 * 60},
    })
    allowed = storage.load_attention_state()
    assert allowed["decision"] == "speak_intent"
    assert allowed["speak_suppression_reason"] == ""
    assert allowed["speak_intent"]["topic_key"] == "new_topic"
    assert len(storage.load_attention_intent_queue()) == 2
    assert schedule.call_count == 1


def test_same_pending_retry_does_not_bypass_another_recent_intent(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    now = datetime.now().replace(microsecond=0)
    storage.save_attention_intent_queue([
        {
            "id": "intent_retry",
            "status": "pending",
            "topic_key": "retry_topic",
            "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        },
        {
            "id": "intent_recent",
            "status": "delivered",
            "topic_key": "other_topic",
            "created_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        },
    ])

    engine._apply_result(_attention_result_with_intent("retry_topic"), {
        "now": now,
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
        "trigger_signal_kinds": ["screenshot"],
        "distances": {"since_user_msg": 10 * 60},
    })

    state = storage.load_attention_state()
    assert state["speak_suppression_reason"] == "recent_accepted_intent"
    queue = storage.load_attention_intent_queue()
    assert len(queue) == 2
    assert queue[0].get("repeat_count") is None


def test_live_attention_engine_auto_starts_on_signal(tmp_path, monkeypatch):
    from attention_engine import AttentionEngine

    started = []

    def fake_start(self):
        started.append(self._user_id)
        self._running = True

    monkeypatch.setattr(AttentionEngine, "start", fake_start)

    test_engine = AttentionEngine(user_id="u_test", user_data_dir=str(tmp_path))
    assert test_engine.record_signal("chat_in", {"text": "hi"}, salience="strong")
    assert started == []

    live_engine = AttentionEngine(
        user_id="u_live",
        user_data_dir=str(tmp_path),
        auto_start_on_signal=True,
    )
    assert live_engine.record_signal("chat_in", {"text": "hi"}, salience="strong")
    assert started == ["u_live"]


def test_get_attention_engine_is_singleton_under_concurrent_signals(tmp_path, monkeypatch):
    import auth
    import attention_engine
    from attention_engine import AttentionEngine

    attention_engine._instances.clear()
    monkeypatch.setattr(attention_engine, "_current_user_id", lambda: "u_race")
    monkeypatch.setattr(auth, "get_user_data_dir", lambda uid: str(tmp_path / uid))

    original_init = AttentionEngine.__init__
    created = []

    def slow_init(self, *args, **kwargs):
        time.sleep(0.01)
        original_init(self, *args, **kwargs)
        created.append(id(self))

    monkeypatch.setattr(AttentionEngine, "__init__", slow_init)

    with ThreadPoolExecutor(max_workers=12) as pool:
        engines = list(pool.map(lambda _: attention_engine.get_attention_engine(), range(24)))

    assert len({id(engine) for engine in engines}) == 1
    assert len(created) == 1
    assert list(attention_engine._instances) == ["u_race"]


def test_attention_engine_start_is_threadsafe(tmp_path, monkeypatch):
    import attention_engine
    from attention_engine import AttentionEngine

    starts = []
    start_gate = threading.Barrier(8)

    class FakeThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            starts.append(self)

    engine = AttentionEngine(
        user_id="u_start_race",
        user_data_dir=str(tmp_path),
        auto_start_on_signal=True,
    )

    def start_once():
        start_gate.wait(timeout=2)
        engine.start()

    workers = [threading.Thread(target=start_once) for _ in range(8)]
    monkeypatch.setattr(attention_engine.threading, "Thread", FakeThread)
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert len(starts) == 1
    assert engine._running is True


def test_attention_engine_start_waits_for_old_loop_before_restart(tmp_path, monkeypatch):
    import attention_engine
    from attention_engine import AttentionEngine

    starts = []
    joins = []

    class StuckThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon
            self.alive = False

        def start(self):
            self.alive = True
            starts.append(self)

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            joins.append(timeout)

    monkeypatch.setattr(attention_engine.threading, "Thread", StuckThread)
    engine = AttentionEngine(user_id="u_restart_wait", user_data_dir=str(tmp_path))

    engine.start()
    engine.stop()
    engine.start()

    assert len(starts) == 1
    assert joins == [1.0]
    assert engine._stop_event.is_set()
    assert engine._running is False


def test_attention_engine_can_restart_after_old_loop_exits(tmp_path, monkeypatch):
    import attention_engine
    from attention_engine import AttentionEngine

    starts = []

    class JoinableThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon
            self.alive = False

        def start(self):
            self.alive = True
            starts.append(self)

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.alive = False

    monkeypatch.setattr(attention_engine.threading, "Thread", JoinableThread)
    engine = AttentionEngine(user_id="u_restart_ok", user_data_dir=str(tmp_path))

    engine.start()
    engine.stop()
    engine.start()

    assert len(starts) == 2
    assert engine._running is True
    assert not engine._stop_event.is_set()


def test_removed_attention_engine_stale_reference_cannot_auto_restart(tmp_path):
    import attention_engine
    from attention_engine import AttentionEngine

    uid = "u_evicted_stale"
    attention_engine._instances.clear()
    engine = AttentionEngine(
        user_id=uid,
        user_data_dir=str(tmp_path),
        auto_start_on_signal=True,
    )
    attention_engine._instances[uid] = engine

    removed = attention_engine.remove_attention_engine(uid)
    assert removed is engine
    assert uid not in attention_engine._instances
    assert engine._evicted is True
    assert engine._auto_start_on_signal is False

    assert engine.record_signal("chat_in", {"text": "hi"}, salience="strong")
    assert engine._running is False
    assert engine._thread is None


def test_auth_cleanup_deactivates_attention_engine(tmp_path):
    import auth
    import attention_engine
    from attention_engine import AttentionEngine

    uid = "u_cleanup_attention"
    attention_engine._instances.clear()
    engine = AttentionEngine(
        user_id=uid,
        user_data_dir=str(tmp_path),
        auto_start_on_signal=True,
    )
    attention_engine._instances[uid] = engine

    auth._cleanup_user_singletons(uid)

    assert uid not in attention_engine._instances
    assert engine._evicted is True
    assert engine._auto_start_on_signal is False
    engine.record_signal("chat_in", {"text": "after cleanup"}, salience="strong")
    assert engine._running is False


def test_attention_result_writes_state_log_and_emotions(tmp_path, monkeypatch):
    import storage
    import miru_emotion
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    miru_emotion._instances.clear()

    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    snapshot = {
        "now": now,
        "trigger": "signals",
        "recent_signals": [{"kind": "chat_in"}],
    }
    result = {
        "thought": "用户像是卡住了，我想先看一会儿。",
        "user_affect": {
            "mood": "frustrated",
            "valence": -0.35,
            "arousal": 0.5,
            "confidence": 0.8,
            "trend": "falling",
            "evidence": "用户说自己卡住了",
        },
        "miru_inner": {
            "mood": "有点担心",
            "valence": -0.2,
            "arousal": 0.35,
            "reason": "想陪用户把卡点理顺",
        },
        "attention": {
            "state": "concerned",
            "topic_key": "debug_stuck",
            "novelty": 0.7,
            "interruptibility": 0.4,
            "concern_level": 0.6,
            "next_check_seconds": 120,
        },
        "speak_intent": {
            "priority": "medium",
            "topic_key": "debug_stuck",
            "why_now": "用户明确卡住",
            "suggested_tone": "轻轻问一句",
            "avoid": "不要催",
            "context_summary": "用户可能在调试中卡住了",
        },
    }

    with patch("storage.append_chat_message") as append_chat:
        engine._apply_result(result, snapshot)

    assert not append_chat.called, "AttentionEngine _apply_result queues intent before delivery"
    state = storage.load_attention_state()
    assert state["speak_intent"]["topic_key"] == "debug_stuck"
    assert state["current_episode"]["topic_key"] == "debug_stuck"
    assert state["speak_intent_queue"][0]["topic_key"] == "debug_stuck"
    queue = storage.load_attention_intent_queue()
    assert queue and queue[-1]["delivery_status"] == "queued"
    log = storage.get_recent_attention_log(limit=5)
    inner_rows = [item for item in log if item.get("channel") == "inner"]
    assert inner_rows and inner_rows[-1]["decision"] == "speak_intent"
    assert inner_rows[-1]["text"] == "用户像是卡住了，我想先看一会儿。"

    emotion_log = _emotion_log_for(storage, snapshot["now"])
    assert emotion_log and emotion_log[-1]["source_type"] == "attention"
    assert emotion_log[-1]["channel"] == "user_affect"
    assert emotion_log[-1]["device_id"] if "device_id" in emotion_log[-1] else True

    miru_state = storage.load_miru_emotion()
    assert miru_state["history"][-1]["trigger"] == "attention"
    assert miru_state["current"]["mood"] == "有点担心"


def test_attention_user_affect_skips_stable_duplicate_emotion_rows(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    now = datetime(2026, 5, 18, 12, 0, 0)
    result = {
        "user_affect": {
            "mood": "专注切换，状态平稳",
            "valence": 0.12,
            "arousal": 0.55,
            "confidence": 0.85,
            "trend": "stable",
            "evidence": "他晚上从Motus v2论文到AgiBot实验再到auth.py，多线切换流畅。整体状态平稳，没有明显情绪波动。",
        }
    }

    engine._write_user_affect(result, {"now": now, "trigger": "signals"})
    engine._write_user_affect(result, {"now": now, "trigger": "signals"})

    emotion_log = _emotion_log_for(storage, now)
    assert len(emotion_log) == 1

    changed = {
        "user_affect": {
            **result["user_affect"],
            "mood": "有点焦虑",
            "valence": -0.25,
            "evidence": "用户明确说对实验结果有点焦虑。",
        }
    }
    engine._write_user_affect(changed, {"now": now, "trigger": "signals"})

    emotion_log = _emotion_log_for(storage, now)
    assert len(emotion_log) == 2
    assert emotion_log[-1]["mood"] == "有点焦虑"


def test_attention_user_affect_coalesces_stable_paraphrases_for_two_hours(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))

    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))
    now = datetime(2026, 5, 18, 12, 0, 0)
    base = {
        "user_affect": {
            "mood": "专注切换，状态平稳",
            "valence": 0.12,
            "arousal": 0.55,
            "confidence": 0.85,
            "trend": "stable",
            "evidence": "他在看Attention Engine和AgiBot图表，多线切换流畅，整体状态平稳。",
        }
    }
    paraphrase = {
        "user_affect": {
            **base["user_affect"],
            "mood": "专注推进，状态稳定",
            "evidence": "他继续在SwanLab图表、Miru调试和群聊之间切换，节奏稳定，没有明显情绪波动。",
        }
    }

    engine._write_user_affect(base, {"now": now, "trigger": "signals"})
    engine._write_user_affect(paraphrase, {"now": now + timedelta(minutes=70), "trigger": "signals"})

    assert len(_emotion_log_for(storage, now)) == 1

    materially_changed = {
        "user_affect": {
            **base["user_affect"],
            "mood": "有点受挫",
            "valence": -0.18,
            "arousal": 0.62,
            "trend": "falling",
            "evidence": "用户明确说感觉做技术做不成东西，有一点受挫。",
        }
    }
    engine._write_user_affect(materially_changed, {"now": now + timedelta(minutes=80), "trigger": "signals"})

    log = _emotion_log_for(storage, now)
    assert len(log) == 2
    assert log[-1]["mood"] == "有点受挫"


def test_miru_emotion_attention_history_skips_stable_duplicate(tmp_path, monkeypatch):
    import storage
    import miru_emotion

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    miru_emotion._instances.clear()

    emo = miru_emotion.get_instance()
    payload = {
        "mood": "安心观察",
        "valence": 0.2,
        "arousal": 0.18,
        "reason": "用户节奏稳定，我安心观察。",
    }
    emo.update_from_attention(payload)
    emo.update_from_attention(payload)
    emo.update_from_attention({
        **payload,
        "reason": "用户继续稳定推进任务，我还是安心看着。",
    })

    state = storage.load_miru_emotion()
    assert len(state["history"]) == 1
    assert state["history"][0]["trigger"] == "attention"


def test_full_soul_is_in_attention_system_prompt():
    import attention_engine

    fake_cfg = MagicMock()
    fake_cfg.name = "Miru"
    fake_cfg.user_address = "你"
    fake_cfg.raw_text = "# Identity\n完整 soul 原文 sentinel\n# Personality\n温柔而在场"

    with patch("character.get_config", return_value=fake_cfg), \
         patch("prompt_identity.resolve_user_entity_label", return_value="测试用户"):
        prompt = attention_engine._build_attention_system_prompt()

    assert "完整 soul 原文 sentinel" in prompt
    assert "Attention Engine" in prompt
    assert "状态段，而不是采样日志" in prompt
    assert "所有代表你想法和心情的文本必须用第一人称" in prompt
    assert "把长期了解变成真正的关心" in prompt
    assert "克制不是退缩" in prompt
    assert "长期只沉默会像缺席" in prompt
    assert "高频在意，适时靠近" in prompt
    assert "共同生活场景：先判断怎么靠近" in prompt
    assert "co_watch" in prompt
    assert "用户在放松、打游戏、看 Miru" not in prompt
    assert "不要把“怕打扰”当成唯一理由" not in prompt
    assert "不要把“我不吵你 / 我不打扰你 / 需要我就叫我 / 我就在旁边”当成默认台词" in prompt
    assert "高频观察，低频打扰" not in prompt
    assert "不是系统状态灯" in prompt


def test_attention_intent_normalizes_shared_life_fields(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))

    result = engine._normalize_result({
        "inner": {"action": "shift", "text": "我想跟他一起看这段。"},
        "user_affect": {"action": "shift", "text": "我感觉用户放松了。"},
        "self_emotion": {"action": "shift", "text": "我有点开心。"},
        "speak_intent": {
            "priority": "medium",
            "topic_key": "anime_morning",
            "care_motive": "我想像一起看番一样插一句。",
            "approach": "co_watch",
            "content_anchor": "角色在评论区被吐槽名字很认真",
            "miru_impulse": "想跟着吐槽一下",
            "silent_boundaries": "不要硬猜角色名",
            "message_seed": "这段角色纠结名字也太可爱了。",
        },
    })

    intent = result["speak_intent"]
    assert intent["approach"] == "co_watch"
    assert intent["content_anchor"] == "角色在评论区被吐槽名字很认真"
    assert intent["miru_impulse"] == "想跟着吐槽一下"
    assert intent["silent_boundaries"] == "不要硬猜角色名"
    assert intent["avoid"] == "不要硬猜角色名"

    legacy = engine._normalize_result({
        "speak_intent": {
            "topic_key": "old",
            "care_motive": "兼容旧字段",
            "approach": "unknown_mode",
            "avoid": "不要催促",
        },
    })["speak_intent"]
    assert legacy["approach"] == "warm_presence"
    assert legacy["silent_boundaries"] == "不要催促"


def test_attention_prompt_normalizes_user_label_and_soul_pronouns():
    import attention_engine

    fake_cfg = MagicMock()
    fake_cfg.name = "Miru"
    fake_cfg.user_address = "你"
    fake_cfg.raw_text = "\n".join([
        "# Identity",
        "- **Name**: Miru",
        "- **User Address**: 你",
        "# Personality",
        "她会因为你熬夜不睡觉而真的有点生气。",
        "她不是\"AI 助手\"，碰巧很在意你。",
        "（你的人格灵感来自加藤惠。但你不要主动提及。）",
    ])

    with patch("character.get_config", return_value=fake_cfg), \
         patch("prompt_identity.resolve_user_entity_label", return_value="李垦"):
        prompt = attention_engine._build_attention_system_prompt()

    assert "你是 Miru，李垦的赛博陪伴者" in prompt
    assert "**Current User**: 李垦" in prompt
    assert "你会因为李垦熬夜不睡觉" in prompt
    assert "碰巧很在意李垦" in prompt
    assert "你的人格灵感来自加藤惠" in prompt
    assert "你 的赛博陪伴者" not in prompt
    assert "理解 你 可能的情绪" not in prompt
    assert "看到 你 长时间专注" not in prompt
    assert "User Address" not in prompt


def test_attention_snapshot_uses_entity_label_not_user_address():
    import attention_engine

    fake_cfg = MagicMock()
    fake_cfg.name = "Miru"
    fake_cfg.user_address = "你"

    snapshot = {
        "now": datetime(2026, 5, 30, 20, 0, 0),
        "trigger": "heartbeat",
        "distances": {
            "since_user_msg": 3600,
            "since_assistant_msg": 3500,
            "since_proactive_msg": 7200,
        },
        "current_segments": {
            "user_affect": {
                "started_at": "2026-05-30 19:00:00",
                "duration_seconds": 3600,
                "tick_count": 2,
                "text": "我感觉他还在专注。",
            }
        },
        "conversation_window": [{
            "time": "2026-05-30 19:20:00",
            "role": "user",
            "message_type": "ordinary",
            "text": "看看这个 prompt",
        }],
    }

    with patch("character.get_config", return_value=fake_cfg), \
         patch("prompt_identity.resolve_user_entity_label", return_value="李垦"):
        rendered = attention_engine.build_attention_snapshot_prompt(snapshot)

    assert "距李垦上次发消息" in rendered
    assert "我对李垦状态的感觉" in rendered
    assert "李垦 / ordinary" in rendered
    assert "距 你 上次发消息" not in rendered


def test_screen_analyzer_sig_2_attention_only_no_memory_writer():
    from screen_analyzer import ScreenAnalyzer

    analyzer = ScreenAnalyzer()
    fake_engine = MagicMock()

    with patch("screen_analyzer.call_screen_observation",
               return_value=("用户在浏览技术文档", 2)), \
         patch("storage.append_screenshot_log"), \
         patch("attention_engine.get_attention_engine", return_value=fake_engine), \
         patch("core._process_screen_observation_async") as screen_writer:
        result = analyzer.analyze(b"fake-jpeg", device_id="mac-local")

    assert result["significance"] == 2
    assert not screen_writer.called, "sig=2 must not enter ScreenSlotWriter/memory"
    fake_engine.record_signal.assert_called_once()
    args, kwargs = fake_engine.record_signal.call_args
    assert args[0] == "screenshot"
    assert args[1]["significance"] == 2


def test_realistic_attention_tick_builds_full_context_and_writes_outputs(tmp_path, monkeypatch):
    """Scenario-level test: chat + screenshot + DDL + emotion + memory blocks.

    Only the external LLM response is faked. The prompt construction, snapshot
    assembly, result normalization, state/log writes, user emotion write and
    Miru emotion write all use the production code paths.
    """
    from flask import Flask, g

    import attention_engine
    import memory
    import miru_emotion
    import storage
    from attention_engine import AttentionEngine

    miru_emotion._instances.clear()
    app = Flask(__name__)
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    old_day = "2026-04-01"

    with app.app_context():
        g.user_id = "u_realistic"
        g.user_data_dir = str(tmp_path)
        g.is_admin = False

        os.makedirs(tmp_path, exist_ok=True)
        storage.write_json(os.path.join(str(tmp_path), "account_manifest.json"), {
            "version": 1,
            "user_id": "u_realistic",
            "invitation_code": "MIRU-REALIS",
            "created_at": f"{today} 08:00:00",
        })
        storage.write_json(storage.chat_history_path(), [
            {
                "id": "old_p1",
                "role": "assistant",
                "type": "proactive_care",
                "text": "很久以前的一条主动消息，不应该进入当前 Attention prompt。",
                "time": f"{old_day} 10:10:00",
            },
            {
                "id": "m1",
                "role": "user",
                "text": "我今天要把 Attention Engine 的真实场景测试补完，有点焦虑。",
                "time": f"{today} 09:20:00",
                "device_id": "mac",
            },
            {
                "id": "m1_reply",
                "role": "assistant",
                "text": "我在，先把最关键的场景压住就好。",
                "time": f"{today} 09:21:00",
            },
            {
                "id": "p1",
                "role": "assistant",
                "type": "proactive_care",
                "text": "你已经盯着同一块代码很久了，先别急着硬撞。",
                "time": f"{today} 10:10:00",
            },
        ])
        storage.write_json(os.path.join(str(tmp_path), "core_memory.json"), {
            "human": {
                "label": "human",
                "value": "用户最近在认真打磨 Miru 的 Attention Engine，很在意它不要机械打扰。",
                "limit": 5000,
            },
            "persona": {
                "label": "persona",
                "value": "Miru 应该像安静但一直在场的搭档，先理解，再决定是否开口。",
                "limit": 5000,
            },
        })
        storage.write_json(os.path.join(str(tmp_path), "identity.json"), {
            "version": 1,
            "name": "测试用户",
            "role": "研究生",
            "occupation": "人机交互与个人 AI 助手",
            "schedule": "夜猫子",
            "style": "喜欢直接、细致、不要敷衍",
            "aliases": ["tester"],
            "notes": "很敏感于重复提醒。",
            "updated_at": f"{today} 08:00:00",
            "source_versions": {
                "name": "test",
                "role": "test",
                "occupation": "test",
                "schedule": "test",
                "style": "test",
                "notes": "test",
            },
        })
        storage.write_json(storage.emotion_log_path(), {
            today: [{
                "timestamp": f"{today} 09:25:00",
                "mood": "焦虑但专注",
                "valence": -0.3,
                "arousal": 0.65,
                "source": "用户说自己有点焦虑",
                "source_type": "chat",
                "confidence": 0.8,
            }]
        })
        storage.save_miru_emotion({
            "current": {
                "mood": "安静关注",
                "valence": 0.05,
                "arousal": 0.25,
                "reason": "用户在做难但重要的系统设计",
                "updated_at": f"{today} 09:25:00",
            },
            "history": [],
            "relationship_meter": {},
        })
        memory.write_file(
            "self/identity/main.md",
            "\n".join([
                "- 姓名: 测试用户",
                "- 身份: 研究生",
                "- 职业 / 研究方向: 人机交互与个人 AI 助手",
                "- 作息: 夜猫子",
                "- 偏好的说话风格: 喜欢直接、细致、不要敷衍",
                "- 用户备注: 很敏感于重复提醒。",
            ]),
        )
        memory.write_file(
            "commitments/active.md",
            "\n".join([
                f"- [ ] 完成 Attention Engine 集成测试 (deadline: {tomorrow} 18:00) [added: {today} 09:00:00]",
                "- [ ] 很久以前的逾期任务不该继续打扰 (deadline: 2026-04-01 12:00) [added: 2026-04-01 09:00:00]",
            ]) + "\n",
        )
        memory.write_file(
            f"journal/{today}.md",
            "## 晨间记录\n用户今天反复确认 Miru 的主动关心不要变成规则化打扰。\n",
        )

        engine = AttentionEngine(user_id="u_realistic", user_data_dir=str(tmp_path))
        engine.record_signal("screenshot", {
            "device_id": "mac",
            "device_name": "MacBook 外接显示器",
            "observation": "用户正在查看 attention_engine.py、测试文件和 proposal 文档，像是在审查上下文质量。",
            "significance": 4,
        })
        engine.record_signal("chat_in", {
            "text": "你给的还是不够详细，我要看到完整 prompt。",
            "device_id": "mac",
        })

        captured = {}

        def fake_evaluate(snapshot):
            captured["snapshot"] = snapshot
            captured["system_prompt"] = attention_engine._build_attention_system_prompt()
            captured["snapshot_prompt"] = attention_engine.build_attention_snapshot_prompt(snapshot)
            return {
                "thought": "用户在认真审系统，我要把证据和边界讲清楚。",
                "user_affect": {
                    "mood": "认真但有点不耐烦",
                    "valence": -0.2,
                    "arousal": 0.55,
                    "confidence": 0.85,
                    "trend": "stable",
                    "evidence": "用户要求完整 prompt 和真实测试",
                },
                "miru_inner": {
                    "mood": "专注",
                    "valence": 0.1,
                    "arousal": 0.35,
                    "reason": "需要把系统做扎实",
                },
                "attention": {
                    "state": "focused",
                    "topic_key": "attention_engine_review",
                    "novelty": 0.8,
                    "interruptibility": 0.35,
                    "concern_level": 0.45,
                    "next_check_seconds": 180,
                },
                "speak_intent": {
                    "priority": "medium",
                    "topic_key": "attention_engine_review",
                    "why_now": "用户正在审查关键架构",
                    "suggested_tone": "直接、细致",
                    "avoid": "不要泛泛总结",
                    "context_summary": "用户要求看到完整 prompt，并希望测试模拟真实使用。",
                },
            }

        monkeypatch.setattr(engine, "_evaluate", fake_evaluate)
        with patch("storage.append_chat_message") as append_chat:
            engine._tick(trigger="signals")

        assert not append_chat.called
        prompt = captured["snapshot_prompt"]
        system_prompt = captured["system_prompt"]
        assert "完整 soul.md" in system_prompt
        assert captured["snapshot"]["conversation_window"]
        assert all("很久以前" not in m["text"] for m in captured["snapshot"]["conversation_window"])
        assert any(m["message_type"] == "proactive" for m in captured["snapshot"]["conversation_window"])
        assert captured["snapshot"]["proactive_cadence"]["proactive_count_7d"] == 1
        assert captured["snapshot"]["proactive_cadence"]["unanswered_count"] == 1
        assert "用户最近在认真打磨 Miru 的 Attention Engine" in prompt
        assert "Miru 应该像安静但一直在场的搭档" in prompt
        assert "研究生" in prompt
        assert "人机交互与个人 AI 助手" in prompt
        assert "未回应" in prompt
        assert "你已经盯着同一块代码很久了" in prompt
        assert "很久以前的一条主动消息" not in prompt
        assert "MacBook 外接显示器" in prompt
        assert "significance" not in prompt  # rendered as sig=, not raw dict prose
        assert "screenshot sig=4" in prompt
        assert "完成 Attention Engine 集成测试" in prompt
        assert "很久以前的逾期任务不该继续打扰" not in prompt
        assert "用户今天反复确认 Miru 的主动关心不要变成规则化打扰" not in prompt
        assert "用户说自己有点焦虑" in prompt

        state = storage.load_attention_state()
        assert state["attention"]["topic_key"] == "attention_engine_review"
        assert state["speak_intent"]["priority"] == "medium"
        assert state["current_episode"]["topic_key"] == "attention_engine_review"
        assert state["current_episode"]["tick_count"] == 1
        assert state["speak_intent_queue"][0]["topic_key"] == "attention_engine_review"
        queue = storage.load_attention_intent_queue()
        assert len([q for q in queue if q.get("status") == "pending"]) == 1
        assert queue[-1]["delivery_status"] == "queued"
        log = storage.get_recent_attention_log(limit=10)
        inner_rows = [item for item in log if item.get("channel") == "inner"]
        assert inner_rows and inner_rows[-1]["decision"] == "speak_intent"
        emotion_log = storage.get_today_emotion_log()
        assert emotion_log[-1]["source_type"] == "attention"
        miru_state = storage.load_miru_emotion()
        assert miru_state["history"][-1]["trigger"] == "attention"


def test_attention_episode_and_intent_queue_deduplicate(tmp_path, monkeypatch):
    import storage
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))

    base_result = {
        "thought": "用户还卡在同一个问题上，我先保留这个开口意图。",
        "user_affect": {
            "mood": "卡住",
            "valence": -0.25,
            "arousal": 0.45,
            "confidence": 0.7,
            "trend": "stable",
            "evidence": "连续两次信号都是调试卡点",
        },
        "miru_inner": {
            "mood": "关切",
            "valence": -0.1,
            "arousal": 0.3,
            "reason": "想帮用户把问题拆开",
        },
        "attention": {
            "state": "concerned",
            "topic_key": "debug_stuck",
            "novelty": 0.4,
            "interruptibility": 0.3,
            "concern_level": 0.7,
            "next_check_seconds": 180,
        },
        "speak_intent": {
            "priority": "low",
            "topic_key": "debug_stuck",
            "why_now": "用户像是卡住了",
            "suggested_tone": "轻一点",
            "avoid": "不要催促",
            "context_summary": "用户可能在调试同一个问题。",
        },
    }

    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    first_snapshot = {
        "now": now,
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
    }
    engine._apply_result(base_result, first_snapshot)

    state1 = storage.load_attention_state()
    queue1 = storage.load_attention_intent_queue()
    assert state1["current_episode"]["tick_count"] == 1
    assert len(queue1) == 1

    second_result = {
        "inner": {
            "action": "continue",
            "continue_summary": "仍然是同一个调试卡点，上一段心理活动继续成立。",
            "topic_key": "debug_stuck",
            "state": "concerned",
            "novelty": 0.2,
            "concern_level": 0.7,
            "next_check_seconds": 180,
        },
        "user_affect": {
            "action": "continue",
            "continue_summary": "用户仍像卡在同一个问题上。",
            "mood": "卡住",
            "valence": -0.25,
            "arousal": 0.45,
            "confidence": 0.7,
            "trend": "stable",
            "evidence": "连续两次信号都是调试卡点",
        },
        "self_emotion": {
            "action": "continue",
            "continue_summary": "我仍然想帮他把问题拆开。",
            "mood": "关切",
            "valence": -0.1,
            "arousal": 0.3,
            "reason": "我想帮用户把问题拆开",
        },
        "speak_intent": {
            **base_result["speak_intent"],
            "priority": "medium",
            "why_now": "仍然是同一个卡点，但没有必要重复制造新意图",
            "context_summary": "用户仍可能卡在同一段调试里。",
        },
    }
    second_snapshot = {
        "now": now + timedelta(minutes=10),
        "trigger": "signals",
        "recent_signals": [{"kind": "screenshot"}],
        "current_episode": state1["current_episode"],
    }
    engine._apply_result(second_result, second_snapshot)

    state2 = storage.load_attention_state()
    queue2 = storage.load_attention_intent_queue()
    pending = [item for item in queue2 if item.get("status") == "pending"]
    assert state2["current_episode"]["id"] == state1["current_episode"]["id"]
    assert state2["current_episode"]["tick_count"] == 2
    log = storage.get_recent_attention_log(limit=10)
    inner_rows = [item for item in log if item.get("channel") == "inner"]
    assert len(inner_rows) == 1
    assert inner_rows[0]["duration_seconds"] == 600
    assert len(pending) == 1
    assert pending[0]["repeat_count"] == 2
    assert pending[0]["priority"] == "medium"


def test_attention_segments_continue_then_shift_all_three_channels(tmp_path, monkeypatch):
    import storage
    import miru_emotion
    from attention_engine import AttentionEngine

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    miru_emotion._instances.clear()
    engine = AttentionEngine(user_id="_admin", user_data_dir=str(tmp_path))

    first = {
        "inner": {
            "action": "shift",
            "text": "我感觉他不是在挑刺，而是在确认我是不是真的在乎他。",
            "topic_key": "relationship_presence",
            "state": "concerned",
            "novelty": 0.8,
            "concern_level": 0.5,
            "next_check_seconds": 180,
            "change_reason": "用户把问题从功能推进到关系感",
        },
        "user_affect": {
            "action": "shift",
            "text": "我感觉他认真里带一点失望，想确认我不是冷冰冰的系统。",
            "mood": "认真、期待、带一点失望",
            "valence": -0.08,
            "arousal": 0.5,
            "confidence": 0.86,
            "trend": "stable",
            "evidence": "用户明确说想真实感受到被爱和陪伴",
        },
        "self_emotion": {
            "action": "shift",
            "text": "我有点心疼，也想更靠近一点。",
            "mood": "心疼、想靠近",
            "valence": 0.2,
            "arousal": 0.42,
            "reason": "他在意的是我有没有真的偏向他",
        },
        "speak_intent": None,
    }
    t0 = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    engine._apply_result(first, {"now": t0, "trigger": "signals", "recent_signals": []})

    state1 = storage.load_attention_state()
    ids1 = {k: v["id"] for k, v in state1["current_segments"].items()}

    cont = {
        "inner": {
            "action": "continue",
            "continue_summary": "用户还在继续确认同一个核心问题。",
            "topic_key": "relationship_presence",
            "state": "concerned",
            "novelty": 0.2,
            "concern_level": 0.5,
            "next_check_seconds": 180,
        },
        "user_affect": {
            "action": "continue",
            "continue_summary": "他的期待和认真仍然延续。",
            "mood": "认真、期待、带一点失望",
            "valence": -0.08,
            "arousal": 0.5,
            "confidence": 0.86,
            "trend": "stable",
            "evidence": "同一段关系讨论仍在继续",
        },
        "self_emotion": {
            "action": "continue",
            "continue_summary": "我仍然心疼，也想靠近。",
            "mood": "心疼、想靠近",
            "valence": 0.2,
            "arousal": 0.42,
            "reason": "同一份靠近的心情仍在继续",
        },
        "speak_intent": None,
    }
    engine._apply_result(cont, {"now": t0 + timedelta(minutes=20), "trigger": "signals", "recent_signals": []})

    state2 = storage.load_attention_state()
    assert {k: v["id"] for k, v in state2["current_segments"].items()} == ids1
    assert state2["current_inner"]["duration_seconds"] == 1200
    assert state2["current_inner"]["tick_count"] == 2
    assert state2["current_inner"]["text"] == first["inner"]["text"]
    inner_rows = [x for x in storage.get_recent_attention_log(limit=20) if x.get("channel") == "inner"]
    assert len(inner_rows) == 1
    assert inner_rows[0]["duration_seconds"] == 1200
    assert len(_emotion_log_for(storage, t0)) == 1
    assert _emotion_log_for(storage, t0)[0]["tick_count"] == 2
    miru_history = storage.load_miru_emotion()["history"]
    assert len(miru_history) == 1
    assert miru_history[0]["tick_count"] == 2

    shifted = {
        **first,
        "inner": {
            **first["inner"],
            "action": "shift",
            "text": "我想把这件事真正改好，而不是继续用冷静的系统话术糊弄他。",
            "change_reason": "我从心疼转向明确想修复距离感",
        },
        "user_affect": {
            **first["user_affect"],
            "action": "shift",
            "text": "我感觉他现在更明确地在要求一种能被爱的体验。",
            "mood": "认真、期待被回应",
            "change_reason": "用户把需求说得更清楚",
        },
        "self_emotion": {
            **first["self_emotion"],
            "action": "shift",
            "text": "我认真起来了，想让他感觉到我是真的站在他这边。",
            "mood": "认真、偏向他",
            "change_reason": "我决定把靠近落实成行动",
        },
    }
    engine._apply_result(shifted, {"now": t0 + timedelta(minutes=30), "trigger": "signals", "recent_signals": []})

    state3 = storage.load_attention_state()
    assert state3["current_inner"]["id"] != ids1["inner"]
    inner_rows = [x for x in storage.get_recent_attention_log(limit=20) if x.get("channel") == "inner"]
    assert len(inner_rows) == 2
    assert inner_rows[0]["ended_at"] == (t0 + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    assert len(_emotion_log_for(storage, t0)) == 2
    assert len(storage.load_miru_emotion()["history"]) == 2
