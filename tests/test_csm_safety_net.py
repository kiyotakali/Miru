"""Regression tests for the chat state-machine safety net.

Bug being guarded against:
  Before this fix, an uncaught exception inside `_csm_do_full_reply` (e.g.
  the LLM call's exception handler `cfg = get_config()` itself raising) left
  reply_msg undefined → `_csm_try_deliver(reply_msg)` raised NameError →
  the daemon thread died silently → `_csm_state[user_id]` stayed
  "GENERATING" forever → every subsequent user message was just appended to
  `_csm_pending` and never processed. The user appeared "frozen" — no reply,
  no typing indicator, no error message — until the backend was restarted.

Tests below stub out the slow / external pieces (LLM, SSE, storage) and
trigger each crash mode to confirm the state machine recovers.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# Make sure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core


class CSMSafetyNetTests(unittest.TestCase):

    def setUp(self):
        # Use a synthetic user_id with a tmp data dir to avoid auth lookups.
        # The real "_admin" id throws on auth.get_user_data_dir.
        self.user_id = "test_user_csm"
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="csm_test_")
        with core._csm_lock:
            core._csm_state[self.user_id] = "GENERATING"
            core._csm_pending[self.user_id] = []
            core._csm_typing[self.user_id] = False
            core._csm_last_msg_time[self.user_id] = time.time() - 10  # >quiet period
        self.batch = [{
            "id": "test_msg_1",
            "role": "user",
            "text": "hello",
            "_user_id": self.user_id,
            "_user_data_dir": self._tmpdir,
            "_device_id": "test_device",
            "_recv_time": time.time(),
        }]

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        # Clean state machine entries to avoid leaking into other tests
        with core._csm_lock:
            core._csm_state.pop(self.user_id, None)
            core._csm_pending.pop(self.user_id, None)
            core._csm_typing.pop(self.user_id, None)
            core._csm_last_msg_time.pop(self.user_id, None)

    def _broadcast_recorder(self):
        """Returns (mock_module, calls_list) — mock_module.broadcast appends."""
        calls = []
        mock = MagicMock()
        def _bcast(event, payload, **kwargs):
            calls.append((event, payload, kwargs))
        mock.broadcast = _bcast
        return mock, calls

    # -------------------------------------------------------------------
    # Baseline: happy path — preserve existing multi-turn behaviour
    # -------------------------------------------------------------------
    def test_happy_path_delivers_reply_and_resets_state(self):
        """LLM returns successfully → reply persisted, state IDLE, SSE
        chat_message broadcast. This is the primary normal flow that must
        keep working after our hardening."""
        mock_sse, calls = self._broadcast_recorder()
        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", return_value={"reply": "hi back", "tool_calls": []}), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message") as mock_append, \
             patch("core._annotate_emotion_from_chat"):
            core._csm_do_full_reply(self.batch, self.user_id)

        # State recovered to IDLE
        self.assertEqual(core._csm_state[self.user_id], "IDLE")
        # Reply persisted
        self.assertTrue(mock_append.called)
        appended_reply = mock_append.call_args[0][0]
        self.assertEqual(appended_reply["text"], "hi back")
        self.assertEqual(appended_reply["role"], "assistant")
        # SSE events: typing_start, typing_stop, chat_message
        events = [c[0] for c in calls]
        self.assertIn("typing_start", events)
        self.assertIn("typing_stop", events)
        self.assertIn("chat_message", events)
        self.assertFalse(core._csm_typing[self.user_id])

    # -------------------------------------------------------------------
    # LLM raises (normal error path) — must still deliver friendly error
    # -------------------------------------------------------------------
    def test_llm_error_delivers_friendly_error_reply(self):
        """LLM raises → user gets a friendly error message, state IDLE.
        This was already working before — guarding against regression."""
        mock_sse, calls = self._broadcast_recorder()
        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", side_effect=RuntimeError("API timeout")), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message") as mock_append, \
             patch("core._annotate_emotion_from_chat"):
            core._csm_do_full_reply(self.batch, self.user_id)

        self.assertEqual(core._csm_state[self.user_id], "IDLE")
        self.assertTrue(mock_append.called)
        reply_text = mock_append.call_args[0][0]["text"]
        self.assertIn("没连上模型服务", reply_text)
        self.assertIn("等一下再试一次", reply_text)
        self.assertNotIn("API timeout", reply_text)

    # -------------------------------------------------------------------
    # CRITICAL: LLM error AND get_config also raises
    # -------------------------------------------------------------------
    def test_llm_error_AND_get_config_raises_still_recovers(self):
        """The exact crash that used to kill the daemon thread:
          LLM raises → except block runs → get_config() also raises →
          reply_msg never assigned → _csm_try_deliver(reply_msg) NameError
          → daemon thread silently dies → state STUCK at GENERATING.

        After fix: defensive default reply_msg + wrapped get_config means
        reply_msg always exists, _csm_try_deliver always runs, state always
        recovers."""
        mock_sse, calls = self._broadcast_recorder()
        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", side_effect=RuntimeError("API timeout")), \
             patch("core.get_config", side_effect=RuntimeError("config broken")), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message") as mock_append, \
             patch("core._annotate_emotion_from_chat"):
            core._csm_do_full_reply(self.batch, self.user_id)

        # CRITICAL: state must recover to IDLE so user can send again
        self.assertEqual(
            core._csm_state[self.user_id], "IDLE",
            "Regression: state stuck at GENERATING — user permanently silenced",
        )
        # Defensive default reply was delivered
        self.assertTrue(mock_append.called)
        reply_text = mock_append.call_args[0][0]["text"]
        self.assertIn("没连上模型服务", reply_text)
        self.assertNotIn("API timeout", reply_text)
        # typing_stop was broadcast so frontend indicator clears
        self.assertIn("typing_stop", [c[0] for c in calls])

    # -------------------------------------------------------------------
    # Storage failure inside _csm_try_deliver
    # -------------------------------------------------------------------
    def test_storage_append_failure_still_resets_state(self):
        """`storage.append_chat_message` raising used to skip `_csm_state_to_idle`,
        leaving state stuck at GENERATING. After fix: state always resets."""
        mock_sse, calls = self._broadcast_recorder()
        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", return_value={"reply": "hi", "tool_calls": []}), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message", side_effect=OSError("disk full")), \
             patch("core._annotate_emotion_from_chat"):
            core._csm_do_full_reply(self.batch, self.user_id)

        # State must recover even though storage failed
        self.assertEqual(
            core._csm_state[self.user_id], "IDLE",
            "Regression: storage failure left state stuck at GENERATING",
        )
        # SSE chat_message should still go out so the user at least sees
        # the reply in real time even if it wasn't persisted
        self.assertIn("chat_message", [c[0] for c in calls])

    # -------------------------------------------------------------------
    # Catastrophic crash: _csm_try_deliver itself raises after entering lock
    # -------------------------------------------------------------------
    def test_top_level_crash_resets_state(self):
        """Simulate a catastrophic failure inside delivery (e.g. _csm_lock
        contention NameError). The outer safety-net try/except must catch it
        and force state back to IDLE."""
        mock_sse, calls = self._broadcast_recorder()

        def _broken_deliver(*args, **kwargs):
            raise RuntimeError("synthetic crash inside _csm_try_deliver")

        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", return_value={"reply": "hi", "tool_calls": []}), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message"), \
             patch("core._csm_try_deliver", side_effect=_broken_deliver), \
             patch("core._annotate_emotion_from_chat"):
            # Must not raise — outer try/except absorbs the crash
            core._csm_do_full_reply(self.batch, self.user_id)

        # State recovered via the outer except's emergency cleanup
        self.assertEqual(
            core._csm_state[self.user_id], "IDLE",
            "Regression: top-level crash left state stuck — user permanently silenced",
        )
        # Emergency typing_stop broadcast happened
        self.assertIn("typing_stop", [c[0] for c in calls])

    # -------------------------------------------------------------------
    # Pending queue path — preserve existing "merge while generating" UX
    # -------------------------------------------------------------------
    def test_pending_during_generation_starts_next_cycle(self):
        """The continuous-input batching behaviour: if a new user message
        arrives while LLM is processing, the current reply is suppressed
        and a new cycle is started. This must keep working post-hardening.
        """
        mock_sse, calls = self._broadcast_recorder()

        # Inject a pending message during the LLM call so _csm_try_deliver
        # sees pending != []
        def _slow_llm(*a, **kw):
            with core._csm_lock:
                core._csm_pending[self.user_id].append({
                    "id": "msg_2",
                    "role": "user",
                    "text": "msg2 mid-flight",
                    "_user_id": self.user_id,
                    "_user_data_dir": None,
                    "_device_id": "test_device",
                    "_recv_time": time.time(),
                })
                # Push last_msg_time so elapsed < QUIET_PERIOD → COLLECTING
                core._csm_last_msg_time[self.user_id] = time.time()
            return {"reply": "stale reply", "tool_calls": []}

        # Block start_quiet_timer from actually firing (we just inspect state)
        with patch.dict(sys.modules, {"sse": mock_sse}), \
             patch("core.call_chat_agent", side_effect=_slow_llm), \
             patch("core._build_chat_context", return_value=""), \
             patch("core._get_greeting_context", return_value=""), \
             patch("core.storage.get_chat_history", return_value=[]), \
             patch("core.storage._uploads_dir", return_value="/tmp"), \
             patch("core.storage.append_chat_message") as mock_append, \
             patch("core._csm_start_quiet_timer"), \
             patch("core._annotate_emotion_from_chat"):
            core._csm_do_full_reply(self.batch, self.user_id)

        # Stale reply was suppressed — append never called
        self.assertFalse(mock_append.called,
                         "Regression: stale reply was persisted instead of suppressed")
        # State transitioned to COLLECTING (quiet timer would fire next)
        # because elapsed < QUIET_PERIOD
        self.assertEqual(core._csm_state[self.user_id], "COLLECTING")
        # Pending still has msg_2 ready for next cycle
        self.assertEqual(len(core._csm_pending[self.user_id]), 1)


if __name__ == "__main__":
    unittest.main()
