"""Regression test for proactive-message hallucination caused by stale
completed commitments leaking into the agent context.

Bug story (the "Zone2 有氧" 11pm reminder):
  User adds commitment "- [ ] Zone2 有氧 (deadline: 2026-04-29 23:00)" at
  16:58. Completes it at 20:49 — `complete_commitment` rewrites the line
  to "- [x] Zone2 有氧 ..." in active.md AND appends to done.md, but does
  NOT physically remove the [x] line from active.md. That removal happens
  in `_nightly_commitment_cleanup` once a day after midnight.

  Around 22:00, CareEngine evaluates. `parse_commitments(include_done=False)`
  used to return EVERY line in active.md regardless of [x] status — only
  done.md was skipped. The completed Zone2 有氧 entry was returned with
  `status="completed"` AND `urgency="imminent"` (deadline within 2h).

  CareEngine's filter only checked `urgency` (not `status`), so the
  completed item was passed to the LLM as a "🔴🔴 2小时内" active DDL.
  The LLM then generated a proactive reminder telling the user to "记得 11
  点前去把有氧做了" — for a task they had already done 2 hours earlier.

  Same bug also affected `_build_chat_context` (main agent), so the user
  could see stale "completed" commitments suggested as still-pending in
  ordinary chat too.

Fix: `parse_commitments(include_done=False)` now filters out items where
`status == "completed"`, regardless of which file the line came from.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from flask import Flask


class ParseCommitmentsFilterTests(unittest.TestCase):

    def setUp(self):
        # Need a Flask app context so storage.get_data_dir() in
        # _user_now()/etc. resolves; we don't actually touch storage but
        # parse_commitments imports memory which routes through it.
        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()
        from flask import g
        g.user_data_dir = "/tmp/fake_user_dir_for_test"
        g.user_id = "test_user"

    def tearDown(self):
        self.ctx.pop()

    @staticmethod
    def _active_md(*lines):
        """Build an active.md fixture with given commitment lines."""
        return "# Active Commitments\n\n" + "\n".join(lines) + "\n"

    def _stub_memory(self, active_content, done_content=""):
        """Patch memory.read_file to return our fixtures."""
        def _read(path):
            if path == "commitments/active.md":
                return active_content
            if path == "commitments/done.md":
                return done_content
            return None
        return patch("memory.read_file", side_effect=_read)

    # -----------------------------------------------------------------
    # The exact bug scenario: completed [x] item with imminent deadline
    # -----------------------------------------------------------------
    def test_completed_imminent_item_excluded_when_include_done_false(self):
        """The 'Zone2 有氧 11pm' regression test.

        An [x] item with a near-future deadline used to leak into the
        proactive agent context as 'imminent'. After fix, it must NOT
        appear when include_done=False.
        """
        # Today's date, deadline 1h in future, [x] completed
        from datetime import datetime, timedelta
        now = datetime.now()
        future_dl = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        active = self._active_md(
            f"- [x] Zone2 有氧 (deadline: {future_dl})  [added: 2026-01-01 16:58]  [completed: 2026-01-01 20:49]",
            "- [ ] 真正待办的事 (deadline: 2099-12-31)  [added: 2026-01-01 10:00]",
        )
        with self._stub_memory(active):
            from core import parse_commitments
            items = parse_commitments(include_done=False)

        titles = [it["title"] for it in items]
        self.assertNotIn(
            "Zone2 有氧", titles,
            "Regression: completed [x] item leaked into include_done=False output. "
            "This is the exact bug that caused the hallucinated 11pm '去做有氧' "
            "proactive reminder for an already-done task."
        )
        # Sanity: the genuinely-pending item still shows up
        self.assertIn("真正待办的事", titles)

    # -----------------------------------------------------------------
    # include_done=True must still return both — preserve existing behavior
    # for journal / commitment-list UI use cases that need completed items.
    # -----------------------------------------------------------------
    def test_completed_item_still_returned_when_include_done_true(self):
        active = self._active_md(
            "- [x] 已完成项 (deadline: 2026-01-01)  [added: 2026-01-01 10:00]  [completed: 2026-01-01 11:00]",
            "- [ ] 进行中项 (deadline: 2099-12-31)  [added: 2026-01-01 10:00]",
        )
        with self._stub_memory(active):
            from core import parse_commitments
            items = parse_commitments(include_done=True)

        titles = [it["title"] for it in items]
        self.assertIn("已完成项", titles, "include_done=True must return [x] items from active.md")
        self.assertIn("进行中项", titles)
        # Completed item is correctly tagged
        completed = next(it for it in items if it["title"] == "已完成项")
        self.assertEqual(completed["status"], "completed")

    # -----------------------------------------------------------------
    # done.md items are still included via the include_done=True path
    # -----------------------------------------------------------------
    def test_done_md_items_still_loaded_when_include_done_true(self):
        active = self._active_md(
            "- [ ] 进行中项 (deadline: 2099-12-31)  [added: 2026-01-01 10:00]",
        )
        done = "# Completed\n\n- [x] 早就完成的项 (deadline: 2025-12-01)  [added: 2025-12-01 10:00]  [completed: 2025-12-01 11:00]\n"
        with self._stub_memory(active, done):
            from core import parse_commitments
            items_with = parse_commitments(include_done=True)
            items_without = parse_commitments(include_done=False)

        titles_with = [it["title"] for it in items_with]
        titles_without = [it["title"] for it in items_without]
        self.assertIn("早就完成的项", titles_with,
                      "include_done=True must merge done.md")
        self.assertNotIn("早就完成的项", titles_without,
                         "include_done=False must skip done.md")
        # In-progress item appears in both
        self.assertIn("进行中项", titles_with)
        self.assertIn("进行中项", titles_without)

    # -----------------------------------------------------------------
    # Mixed scenario from the actual VPS data — many completed [x] lines,
    # a couple of genuine open ones. The proactive agent should only see
    # the open ones.
    # -----------------------------------------------------------------
    def test_realistic_mix_only_open_items_returned(self):
        active = self._active_md(
            "- [x] 已完成A (deadline: 2026-04-29)  [completed: 2026-04-29 12:00]",
            "- [x] 已完成B (deadline: 2026-04-29 23:00)  [completed: 2026-04-29 20:49]",
            "- [ ] 还没做的事 (deadline: 2026-05-01)",
            "- [x] 已完成C (deadline: 2026-04-28)  [completed: 2026-04-28 19:00]",
            "- [ ] 另一件待办 (deadline: 2026-05-02)",
        )
        with self._stub_memory(active):
            from core import parse_commitments
            items = parse_commitments(include_done=False)

        titles = sorted(it["title"] for it in items)
        self.assertEqual(
            titles, ["另一件待办", "还没做的事"],
            f"include_done=False should return ONLY the [ ] items, "
            f"got {titles!r}"
        )

    # -----------------------------------------------------------------
    # Empty active.md must not raise
    # -----------------------------------------------------------------
    def test_empty_active_md_returns_empty(self):
        with self._stub_memory("", ""):
            from core import parse_commitments
            items = parse_commitments(include_done=False)
            self.assertEqual(items, [])
            items_with = parse_commitments(include_done=True)
            self.assertEqual(items_with, [])


if __name__ == "__main__":
    unittest.main()
