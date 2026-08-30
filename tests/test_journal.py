"""Tests for the new structured journal module.

Focus on non-LLM logic: context gathering, empty-day fallback, file I/O,
list/backfill flow. LLM calls are mocked.
"""
import importlib
import os
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import g

# Anchor for the time-windowed tests (scan_and_backfill / regenerate_all
# walk back N days from "today"). Tests below pin "today" to 2026-04-19
# so the hard-coded sample dates 2026-04-15..18 always fall inside the
# default windows. Without pinning, the tests rot when wall-clock today
# drifts past the window — classic time-bomb test bug.
_FROZEN_NOW = datetime(2026, 4, 19, 12, 0, 0)


def _fake_check_request(req=None):
    import auth
    g.user_id = "_admin"
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
    g.is_admin = True
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated test env with fresh data dir + app context.

    Context is popped automatically on test exit so subsequent tests don't
    inherit our `g` bindings.
    """
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)

    import auth
    importlib.reload(auth)  # pick up fresh DATA_DIR env
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import journal
    importlib.reload(journal)
    monkeypatch.setattr(auth, "check_request", _fake_check_request)

    import app as _app
    importlib.reload(_app)
    ctx = _app.app.app_context()
    ctx.push()
    g.user_id = "_admin"
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
    g.is_admin = True

    yield {"app": _app, "journal": journal, "storage": storage}

    try:
        ctx.pop()
    except Exception:
        pass


def test_empty_day_generates_short_placeholder(env):
    journal = env["journal"]
    result = journal.generate_daily_journal("2026-04-18", force=True)
    assert result is not None
    assert result["is_empty_day"] is True
    assert "没怎么出现" in result["narrative"]
    assert result["source_version"] == journal.JOURNAL_SCHEMA_VERSION
    assert os.path.exists(journal._json_path("2026-04-18"))
    assert os.path.exists(journal._md_path("2026-04-18"))


def test_get_journal_returns_saved(env):
    journal = env["journal"]
    journal.generate_daily_journal("2026-04-18", force=True)
    got = journal.get_journal("2026-04-18")
    assert got is not None
    assert got["date"] == "2026-04-18"


def test_invalid_json_returns_none(env):
    journal = env["journal"]
    p = journal._json_path("2026-04-17")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("{ malformed")
    assert journal.get_journal("2026-04-17") is None


def test_list_journals_only_returns_json(env):
    """v2: list_journals only returns JSON-format journals.

    Legacy .md files are ignored — the raw_md fallback was removed
    after task #418 (no consumer was reading it for new accounts).
    """
    journal = env["journal"]
    journal.generate_daily_journal("2026-04-18", force=True)
    import memory
    memory.write_file("journal/2026-04-17.md", "# 手写旧日记\n- [12:00] 上班")

    import user_settings
    with patch.object(user_settings, "user_now", return_value=_FROZEN_NOW):
        listed = journal.list_journals(days_back=60)
    dates = [e["date"] for e in listed]
    assert "2026-04-18" in dates
    # md-only days no longer appear in the list
    assert "2026-04-17" not in dates
    j18 = next(e for e in listed if e["date"] == "2026-04-18")
    assert j18["has_json"] is True
    assert "has_md" not in j18  # field removed


def test_llm_called_when_day_has_content(env):
    journal = env["journal"]
    storage = env["storage"]
    storage.write_json(storage.chat_history_path(), [
        {"role": "user", "text": "今天好累", "time": "2026-04-17 23:30:00"},
    ])

    fake = {
        "title": "疲惫的周五",
        "mood": {"label": "疲惫", "emoji": "😪"},
        "narrative": "你晚上发了一句'今天好累'。我没多问，只是记下了。",
        "highlights": [{"time": "23:30", "text": "你发了'今天好累'"}],
    }
    with patch.object(journal, "_call_journal_llm", return_value=fake):
        result = journal.generate_daily_journal("2026-04-17", force=True)
    assert result is not None
    assert result.get("is_empty_day") is not True
    assert result["title"] == "疲惫的周五"
    assert result["mood"]["emoji"] == "😪"
    assert len(result["highlights"]) == 1
    md = env["journal"]._md_path("2026-04-17")
    with open(md, encoding="utf-8") as f:
        rendered = f.read()
    assert "# 疲惫的周五" in rendered
    assert "你晚上发了一句" in rendered
    import memory
    assert "疲惫的周五" in (memory.read_file("journal/2026-04-17.md") or "")
    assert "journal/2026-04-17.md" in memory.read_index()


def test_scan_and_backfill_skips_empty_days(env):
    journal = env["journal"]
    info = journal.scan_and_backfill(days=7, max_per_call=5)
    assert info["regenerated"] == []


def test_scan_and_backfill_regenerates_missing(env):
    journal = env["journal"]
    storage = env["storage"]
    storage.write_json(storage.chat_history_path(), [
        {"role": "user", "text": "test", "time": "2026-04-16 10:00:00"},
    ])
    fake = {"title": "t", "mood": {"label": "平静", "emoji": "🌙"},
             "narrative": "短短一句。", "highlights": []}
    import user_settings
    with patch.object(journal, "_call_journal_llm", return_value=fake), \
         patch.object(user_settings, "user_now", return_value=_FROZEN_NOW):
        info = journal.scan_and_backfill(days=7, max_per_call=5)
    assert "2026-04-16" in info["regenerated"]
    assert journal.get_journal("2026-04-16") is not None


def test_regenerate_all_forces_overwrite(env):
    journal = env["journal"]
    storage = env["storage"]
    storage.write_json(storage.chat_history_path(), [
        {"role": "user", "text": "hi", "time": "2026-04-15 10:00:00"},
    ])
    journal._save_journal("2026-04-15", {
        "date": "2026-04-15", "title": "old", "mood": {}, "narrative": "old",
        "highlights": [], "stats": {}, "generated_at": "2026-04-15T12:00:00",
        "source_version": 2,
    })
    fake = {"title": "new", "mood": {"label": "", "emoji": ""},
             "narrative": "new content", "highlights": []}
    import user_settings
    with patch.object(journal, "_call_journal_llm", return_value=fake), \
         patch.object(user_settings, "user_now", return_value=_FROZEN_NOW):
        info = journal.regenerate_all(days=7)
    assert "2026-04-15" in info["regenerated"]
    j = journal.get_journal("2026-04-15")
    assert j["title"] == "new"
