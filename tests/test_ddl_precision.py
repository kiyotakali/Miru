"""Tests for DDL precision timing, dedup tracking, and proactive message context."""

import json
import os
import time
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. DDL regex + urgency calculation (core.py)
# ---------------------------------------------------------------------------

class TestDDLParsing:
    """_parse_commitment_line must handle YYYY-MM-DD and YYYY-MM-DD HH:MM."""

    def _parse(self, line):
        from core import _parse_commitment_line
        return _parse_commitment_line(line)

    def test_date_only_deadline(self):
        r = self._parse("- [ ] 交报告 (deadline: 2026-04-20)  [added: 2026-04-16 10:00]")
        assert r is not None
        assert r["deadline"] == "2026-04-20"
        assert r["title"] == "交报告"

    def test_datetime_deadline(self):
        r = self._parse("- [ ] 开会 (deadline: 2026-04-16 15:00)  [added: 2026-04-16 08:00]")
        assert r is not None
        assert r["deadline"] == "2026-04-16 15:00"

    def test_no_deadline(self):
        r = self._parse("- [ ] 看书  [added: 2026-04-16 10:00]")
        assert r is not None
        assert r["deadline"] is None
        assert r["urgency"] == "no_deadline"

    def test_completed_ignored_urgency(self):
        r = self._parse("- [x] 交报告 (deadline: 2026-01-01)  [added: 2025-12-01]  [completed: 2025-12-31 23:00]")
        assert r is not None
        assert r["urgency"] == "overdue"  # still computed, but status is completed

    def test_deadline_with_detail(self):
        r = self._parse("- [ ] 交报告 (deadline: 2026-04-20 14:00) -- 发给老师  [added: 2026-04-16 10:00]")
        assert r is not None
        assert r["deadline"] == "2026-04-20 14:00"
        assert r["detail"] == "发给老师"


class TestUrgencyLevels:
    """Urgency should differentiate imminent/approaching/today/soon/normal."""

    def _parse_with_now(self, line, fake_now):
        with patch("core._user_now", return_value=fake_now):
            from core import _parse_commitment_line
            return _parse_commitment_line(line)

    def test_overdue_past_date(self):
        now = datetime(2026, 4, 16, 10, 0)
        r = self._parse_with_now(
            "- [ ] 交报告 (deadline: 2026-04-15)  [added: 2026-04-10]", now)
        assert r["urgency"] == "overdue"

    def test_overdue_today_past_time(self):
        now = datetime(2026, 4, 16, 16, 0)
        r = self._parse_with_now(
            "- [ ] 开会 (deadline: 2026-04-16 14:00)  [added: 2026-04-10]", now)
        assert r["urgency"] == "overdue"

    def test_imminent_within_2h(self):
        now = datetime(2026, 4, 16, 13, 30)
        r = self._parse_with_now(
            "- [ ] 开会 (deadline: 2026-04-16 15:00)  [added: 2026-04-10]", now)
        assert r["urgency"] == "imminent"

    def test_today_with_time_more_than_2h(self):
        now = datetime(2026, 4, 16, 10, 0)
        r = self._parse_with_now(
            "- [ ] 开会 (deadline: 2026-04-16 15:00)  [added: 2026-04-10]", now)
        assert r["urgency"] == "today"

    def test_today_date_only(self):
        now = datetime(2026, 4, 16, 10, 0)
        r = self._parse_with_now(
            "- [ ] 交报告 (deadline: 2026-04-16)  [added: 2026-04-10]", now)
        assert r["urgency"] == "today"

    def test_approaching_tomorrow_within_6h(self):
        now = datetime(2026, 4, 16, 22, 0)
        r = self._parse_with_now(
            "- [ ] 交报告 (deadline: 2026-04-17 02:00)  [added: 2026-04-10]", now)
        assert r["urgency"] == "approaching"

    def test_soon_within_3days(self):
        now = datetime(2026, 4, 16, 10, 0)
        r = self._parse_with_now(
            "- [ ] 交报告 (deadline: 2026-04-18)  [added: 2026-04-10]", now)
        assert r["urgency"] == "soon"

    def test_normal_far_away(self):
        now = datetime(2026, 4, 16, 10, 0)
        r = self._parse_with_now(
            "- [ ] 交报告 (deadline: 2026-05-01)  [added: 2026-04-10]", now)
        assert r["urgency"] == "normal"


# ---------------------------------------------------------------------------
# 2. DDL dedup persistence — REMOVED 2026-05-07
# ---------------------------------------------------------------------------
# TestDDLDedupPersistence + TestReportCareSentTextMatch removed because the
# whole _reminded_ddls / ddl_remind_tracking.json / substring-match-on-message
# mechanism was replaced. The LLM now sees today_chat + today_care_log in the
# snapshot and judges duplicate-mention from full context, which avoids the
# Chinese substring false-positives this dedup tracker was prone to (e.g.
# title="周五交报告" wrongly matching message "今天周五啦").
# ---------------------------------------------------------------------------

# All TestDDLDedupPersistence + TestReportCareSentTextMatch tests removed
# 2026-05-07: see comment block above.

# ---------------------------------------------------------------------------
# 4. Dedup markers in _gather_memory_context
# ---------------------------------------------------------------------------

# NOTE: TestDedupMarkers and TestProactiveMessageContext (v2) used to test
# `_gather_memory_context()` — that internal API is gone in CareEngine v3.
# Equivalent coverage now lives in tests/test_care_engine_v3.py against the
# new `_build_snapshot()` and the prompt builder.


# ---------------------------------------------------------------------------
# 6. add_commitment tool schema accepts HH:MM
# ---------------------------------------------------------------------------

class TestAddCommitmentSchema:
    """add_commitment tool should document YYYY-MM-DD HH:MM format."""

    def test_due_description_mentions_time(self):
        from tools.add_commitment import AddCommitmentTool
        schema = AddCommitmentTool.input_schema
        due_desc = schema["properties"]["due"]["description"]
        assert "HH:MM" in due_desc
        assert "YYYY-MM-DD" in due_desc
