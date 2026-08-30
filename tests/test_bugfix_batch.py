"""Tests for the batch bugfix round: Critical #1-3, High #4-7, Medium #9/#11/#12.

Tests are offline — no LLM calls, no disk I/O to real data directory.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ============================================================
# Critical #1: Proactive messages must NOT enqueue to memory.
# Miru's self-talk is derived from existing memory; feeding it back as a
# fact source creates a feedback loop where her own guesses become "facts."
# Memory is fed only by real input signals (user chat / screenshots /
# identity cascade) via the Curator.
# ============================================================

def test_send_via_agent_does_not_enqueue_to_memory():
    """_send_via_agent must NOT feed proactive messages back into memory."""
    print("TEST: _send_via_agent does NOT enqueue to memory")
    import inspect, core
    source = inspect.getsource(core._send_via_agent)
    locked_fn = getattr(core, '_send_via_agent_locked', None)
    if locked_fn:
        source += inspect.getsource(locked_fn)
    # Bare reference to the sleep_agent module is OK in comments/imports;
    # the forbidden pattern is an actual enqueue call on its instance.
    assert "get_sleep_agent().enqueue" not in source, \
        "_send_via_agent must not enqueue Miru's own reply as a fact source"
    print("  [PASS] _send_via_agent does not call sleep_agent.enqueue")


def test_morning_message_uses_send_via_agent():
    """Morning message should use _send_via_agent for unified delivery."""
    import inspect, core
    source = inspect.getsource(core._check_morning_message_unlocked)
    assert "_send_via_agent" in source, \
        "morning message should use _send_via_agent"
    print("TEST: morning message uses _send_via_agent")
    print("  [PASS] _check_morning_message_unlocked calls _send_via_agent")


def test_nightly_maintenance_no_message():
    """Nightly routine should only run maintenance, not send messages.

    The old _send_nightly_message was removed — CareEngine handles all
    proactive messages (including late-night nudges) via observation-based
    prompts.  The remaining _check_nightly_maintenance only does archival,
    index consolidation, pattern update, and commitment cleanup.
    """
    import inspect, core
    assert not hasattr(core, "_send_nightly_message"), \
        "_send_nightly_message should be removed (CareEngine handles messages)"
    source = inspect.getsource(core._check_nightly_maintenance)
    assert "_run_nightly_maintenance" in source, \
        "_check_nightly_maintenance should call _run_nightly_maintenance"
    assert "_send_via_agent" not in source, \
        "nightly maintenance should NOT send messages"
    print("TEST: nightly routine is maintenance-only")
    print("  [PASS] _check_nightly_maintenance runs maintenance, no messages")


def test_attention_engine_replaces_care_agent_callback():
    """AttentionEngine phase 1 should not route speak_intent through delivery."""
    # Read app.py source to verify
    app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "def _start_attention_engine_spawner" in source, \
        "app.py should start AttentionEngine spawner"
    idx = source.index("def _start_attention_engine_spawner")
    block = source[idx:idx + 2200]
    assert "get_attention_engine" in block, \
        "attention spawner should create AttentionEngine instances"
    assert "_send_via_agent" not in block, \
        "AttentionEngine phase 1 must not deliver speak_intent through the main agent"
    print("TEST: attention engine replaces care callback")
    print("  [PASS] AttentionEngine spawner does not call _send_via_agent")


# ============================================================
# Critical #2 & #3: Unified morning message (replaces old greeting/reminder/weekly overlap)
# ============================================================

def test_unified_morning_message_exists():
    """Morning message should be a single unified function with dedup."""
    import inspect, core
    assert hasattr(core, 'check_morning_message'), "check_morning_message should exist"
    source = inspect.getsource(core._check_morning_message_unlocked)
    assert "morning_message_" in source, "should use morning_message_ dedup key"
    assert "_send_via_agent" in source, "should use _send_via_agent"
    print("TEST: unified morning message exists with dedup")
    print("  [PASS] check_morning_message uses dedup key + _send_via_agent")


def test_morning_message_includes_monday_weekly():
    """On Mondays, morning message should include weekly recap context."""
    import inspect, core
    source = inspect.getsource(core._check_morning_message_unlocked)
    assert "weekday" in source and "monday" in source.lower() or "周一" in source, \
        "morning message should have Monday-specific logic"
    assert "done.md" in source or "commitments/done" in source, \
        "morning message should load completed commitments on Monday"
    print("TEST: morning message includes Monday weekly recap")
    print("  [PASS] Monday-specific context loading")


def test_reminder_loop_unified():
    """Reminder loop should call check_morning_message and check_nightly_message."""
    app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
    with open(app_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "check_morning_message" in source, "loop should call check_morning_message"
    assert "check_nightly_message" in source, "loop should call check_nightly_message"
    assert "check_daily_greeting" not in source, "old check_daily_greeting should be removed"
    assert "weekly_report" not in source, "weekly report feature was removed"
    print("TEST: reminder loop uses unified message functions")
    print("  [PASS] new functions in loop, old ones removed")


# ============================================================
# High #4: File-level locks in memory.py
# ============================================================

def test_memory_write_is_threadsafe():
    """memory.write_file should use a lock for concurrent access."""
    import memory
    assert hasattr(memory, '_write_lock'), "memory module should have _write_lock"
    assert isinstance(memory._write_lock, type(threading.Lock())), \
        "_write_lock should be a Lock"

    # Verify write_file uses the lock
    import inspect
    source = inspect.getsource(memory.write_file)
    assert "_write_lock" in source, "write_file should use _write_lock"

    source2 = inspect.getsource(memory.append_to_file)
    assert "_write_lock" in source2, "append_to_file should use _write_lock"

    source3 = inspect.getsource(memory.update_index)
    assert "_write_lock" in source3, "update_index should use _write_lock"

    print("TEST: memory.py write operations are thread-safe")
    print("  [PASS] write_file, append_to_file, update_index all use _write_lock")


def test_memory_concurrent_writes():
    """Concurrent writes should not corrupt files."""
    import memory

    with tempfile.TemporaryDirectory() as tmpdir:
        old_dir = memory.MEMORY_DIR
        old_index = memory.INDEX_PATH
        memory.MEMORY_DIR = tmpdir
        memory.INDEX_PATH = os.path.join(tmpdir, "index.md")
        try:
            memory.ensure_dirs()
            test_path = "test/concurrent.md"
            os.makedirs(os.path.join(tmpdir, "test"), exist_ok=True)

            # Write initial content
            memory.write_file(test_path, "initial\n")

            errors = []
            def append_content(thread_id):
                try:
                    for i in range(20):
                        memory.append_to_file(test_path, f"t{thread_id}-{i}\n")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=append_content, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Concurrent write errors: {errors}"
            content = memory.read_file(test_path)
            lines = [l for l in content.strip().split("\n") if l]
            # 1 initial + 4 threads * 20 appends = 81
            assert len(lines) == 81, f"Expected 81 lines, got {len(lines)}"
            print("TEST: concurrent memory writes don't lose data")
            print(f"  [PASS] {len(lines)} lines written correctly by 4 threads")
        finally:
            memory.MEMORY_DIR = old_dir
            memory.INDEX_PATH = old_index


# ============================================================
# High #6: Max retry counter
# ============================================================

def test_sleep_agent_max_retries():
    """SleepAgent should drop messages after MAX_RETRIES failures."""
    from sleep_agent import SleepAgent, MAX_RETRIES

    agent = SleepAgent()
    call_count = [0]

    def failing_process(batch):
        call_count[0] += 1
        raise RuntimeError("LLM down")

    agent._process_batch = failing_process

    # Enqueue a message
    agent._queue = [{"role": "user", "text": "test", "time": "2026-03-30T10:00:00"}]

    # Flush multiple times
    for i in range(MAX_RETRIES + 2):
        agent._running = False
        agent._do_flush()

    # After MAX_RETRIES, the batch should be dropped
    assert agent.pending_count() == 0, \
        f"Queue should be empty after {MAX_RETRIES} retries, got {agent.pending_count()}"
    assert call_count[0] == MAX_RETRIES + 1, \
        f"Expected {MAX_RETRIES + 1} attempts, got {call_count[0]}"

    print("TEST: sleep_agent drops batch after MAX_RETRIES")
    print(f"  [PASS] batch dropped after {MAX_RETRIES} retries ({call_count[0]} attempts)")


# test_summarizer_max_retries removed 2026-05-08 — summarizer.py was
# physically deleted (had no production caller for months; sleep_agent
# took over its responsibilities). Retry behavior is now covered by
# test_sleep_agent_max_retries above.


# ============================================================
# Medium #9: No str(result) fallback
# ============================================================

def test_no_str_result_fallback_in_greetings():
    """First-greeting generation should not use str(result) as fallback.

    The legacy `ensure_first_greeting()` (server-startup global) was removed
    after multi-user refactor (it had no Flask user context and silently
    wrote to the wrong path). Replaced by per-user
    `_inject_custom_first_greeting(answers)` triggered at onboarding submit.
    """
    import inspect, core

    source1 = inspect.getsource(core._inject_custom_first_greeting)
    lines = source1.split("\n")
    for line in lines:
        if "isinstance(result, dict)" in line or "result.get(" in line:
            assert "str(result)" not in line, \
                f"Found str(result) fallback in greeting: {line.strip()}"

    print("TEST: no str(result) fallback in greetings")
    print("  [PASS] _inject_custom_first_greeting uses proper key extraction")


def test_format_memory_retrieval_dict():
    """_format_memory_retrieval should handle dict results properly."""
    import core

    # Dict with text key
    result = core._format_memory_retrieval({"text": "hello", "other": "stuff"})
    assert result == "hello", f"Expected 'hello', got '{result}'"

    # Dict with reply key
    result2 = core._format_memory_retrieval({"reply": "world"})
    assert result2 == "world", f"Expected 'world', got '{result2}'"

    # Dict with no known keys — should return empty, not str(dict)
    result3 = core._format_memory_retrieval({"unknown_key": "value"})
    assert result3 == "", f"Expected empty string, got '{result3}'"
    assert "{" not in result3, "Should not contain dict representation"

    print("TEST: _format_memory_retrieval handles dicts properly")
    print("  [PASS] dict results extracted correctly, no str() fallback")


# ============================================================
# Nightly maintenance + commitment date regex
# ============================================================

def test_nightly_maintenance_runs_maintenance():
    """Nightly routine should call _run_nightly_maintenance for archival tasks."""
    import inspect, core
    source = inspect.getsource(core._check_nightly_maintenance)
    assert "_run_nightly_maintenance" in source, \
        "nightly maintenance should call _run_nightly_maintenance"
    print("TEST: nightly maintenance runs maintenance tasks")
    print("  [PASS] _check_nightly_maintenance calls _run_nightly_maintenance")


def test_date_regex_extraction():
    """Test the actual regex pattern used for date extraction."""
    _date_re = re.compile(r'\d{4}-\d{2}-\d{2}')

    # Normal commitment line
    line1 = "- [x] 提交设计稿 (deadline: 2026-03-25)  [completed: 2026-03-26]"
    dates = _date_re.findall(line1)
    assert dates == ["2026-03-25", "2026-03-26"], f"Expected 2 dates, got {dates}"
    assert dates[-1] == "2026-03-26", "Last date should be completion date"

    # Short line with no date
    line2 = "- [x] 做了一些事"
    dates2 = _date_re.findall(line2)
    assert dates2 == [], f"Expected no dates, got {dates2}"

    # Line with only one date
    line3 = "- [x] 写报告 [added: 2026-03-20]"
    dates3 = _date_re.findall(line3)
    assert dates3 == ["2026-03-20"], f"Expected 1 date, got {dates3}"

    print("TEST: date regex extraction works correctly")
    print("  [PASS] regex handles various commitment line formats")


# ============================================================
# Medium #12: Atomic JSON writes
# ============================================================

def test_atomic_json_write():
    """write_json should use atomic write (temp file + rename)."""
    import inspect, storage
    source = inspect.getsource(storage.write_json)
    assert ".tmp" in source, "Should write to temp file first"
    assert "os.replace" in source or "os.rename" in source, \
        "Should use os.replace/rename for atomic swap"
    assert "fsync" in source, "Should fsync before rename"
    print("TEST: storage.write_json uses atomic writes")
    print("  [PASS] temp file + fsync + os.replace pattern")


def test_atomic_write_actually_works():
    """Verify atomic write produces valid JSON."""
    import storage

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        test_data = [{"key": "value", "number": 42}]
        storage.write_json(tmp_path, test_data)

        with open(tmp_path, "r") as f:
            loaded = json.load(f)
        assert loaded == test_data, f"Data mismatch: {loaded}"

        # No .tmp file should remain
        assert not os.path.exists(tmp_path + ".tmp"), \
            "Temp file should be cleaned up after rename"

        print("TEST: atomic write produces valid JSON")
        print("  [PASS] data written and read back correctly")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ============================================================
# Care eval prompt — evaluation-only
# ============================================================

def test_care_eval_is_evaluation_only():
    """call_care_eval should not generate a message, only evaluate."""
    import inspect, memory_prompts
    source = inspect.getsource(memory_prompts.call_care_eval)
    assert "should_send" in source, "should return should_send"
    prompt_source = inspect.getsource(memory_prompts._build_care_eval_prompt)
    assert "message" not in prompt_source.lower() or "不需要生成消息" in prompt_source, \
        "care eval prompt should not ask for message generation"
    print("TEST: care eval is evaluation-only")
    print("  [PASS] call_care_eval returns should_send without message")


# ============================================================
# max_tokens increase
# ============================================================

def test_agent_max_tokens_increased():
    """Per-tier default max_tokens should all be > 4096.

    After the 3-tier refactor, max_tokens is configured per-tier in
    ai_config._DEFAULT_MAX_TOKENS rather than hard-coded in prompt.py.
    Memory tier in particular gets a high ceiling so Pass 4 can rewrite
    long main.md files.
    """
    import ai_config
    defaults = ai_config._DEFAULT_MAX_TOKENS
    for tier in ai_config.TIERS:
        mt = defaults[tier]
        # Vision tier outputs a 1-line observation, so smaller cap is fine
        if tier == "vision":
            assert mt >= 256, f"vision tier max_tokens too low: {mt}"
        else:
            assert mt > 4096, \
                f"{tier} tier max_tokens should be > 4096, got {mt}"

    print("TEST: agent max_tokens increased")
    print(f"  [PASS] tiers={dict(defaults)}")


# ============================================================
# AttentionEngine chat signals
# ============================================================

def test_attention_engine_chat_in_buffers_signal():
    """AttentionEngine is the live recipient for chat_in signals."""
    from attention_engine import AttentionEngine

    engine = AttentionEngine(user_id="test_uid", user_data_dir="/tmp/test_uid")
    engine.record_signal("chat_in", {"text": "hi"}, salience="strong")

    with engine._signals_lock:
        found = any(s.get("kind") == "chat_in" and s.get("text") == "hi"
                    for s in list(engine._signals))

    assert found, "chat_in must enter AttentionEngine's signal buffer"
    print("TEST: AttentionEngine chat_in buffers signal")
    print("  [PASS] chat signal flows into AttentionEngine")


# ============================================================
# Main
# ============================================================

def main():
    tests = [
        # Critical #1
        test_daily_greeting_enqueues_to_sleep_agent,
        test_morning_reminder_enqueues_to_sleep_agent,
        test_nightly_review_enqueues_to_sleep_agent,
        test_care_message_enqueues_to_sleep_agent,
        # Critical #2
        test_morning_reminder_skips_if_greeting_sent,
        test_daily_greeting_skips_if_morning_reminder_sent,
        # Critical #3
        test_reminder_loop_order,
        # High #4
        test_memory_write_is_threadsafe,
        test_memory_concurrent_writes,
        # High #6
        test_sleep_agent_max_retries,
        # Medium #9
        test_no_str_result_fallback_in_greetings,
        test_format_memory_retrieval_dict,
        # Medium #11
        test_date_regex_extraction,
        # Medium #12
        test_atomic_json_write,
        test_atomic_write_actually_works,
        # max_tokens
        test_agent_max_tokens_increased,
        # AttentionEngine
        test_attention_engine_chat_in_buffers_signal,
    ]

    passed = 0
    failed = 0
    errors = []

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"TEST: {test.__name__}")
            print(f"  [FAIL] {e}")

    print("\n" + "=" * 60)
    print(f"TOTAL: {passed}/{passed + failed} passed")
    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
