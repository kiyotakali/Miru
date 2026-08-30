"""Tests for core_memory size-control logic.

Verifies the soft/hard threshold behavior — blocks must NOT grow
unbounded, and the chat-agent prompt budget must stay bounded.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("MIRU_DATA_DIR", tmp)
    monkeypatch.setenv("DATA_DIR", tmp)
    # core_memory's CORE_MEMORY_PATH is captured at module load (uses
    # the env at import time, which is the project's real data/ dir).
    # Force it to point at our tmp for isolation per-test.
    import core_memory
    monkeypatch.setattr(
        core_memory, "_core_memory_path",
        lambda: os.path.join(tmp, "core_memory.json"),
    )
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def test_default_limit_is_5000(env):
    import core_memory
    assert core_memory.DEFAULT_BLOCK_LIMIT == 5000
    assert core_memory.SOFT_RATIO == 0.80
    assert core_memory.TARGET_RATIO == 0.60


def test_append_below_soft_threshold_no_consolidate(env):
    """Append that doesn't cross 80% should NOT trigger consolidate."""
    import core_memory
    consolidate_calls = []
    with patch.object(core_memory, "consolidate",
                      side_effect=lambda label, **kw:
                          (consolidate_calls.append(label),
                           {"ok": True, "old_len": 0, "new_len": 0})[-1]):
        # Soft threshold = 5000 × 0.8 = 4000. Add 3000 chars (60%).
        r = core_memory.append("human", "x" * 3000)
        assert r["ok"]
        assert r["new_len"] == 3000
    assert consolidate_calls == [], "should not consolidate below soft threshold"


def test_append_crossing_soft_triggers_consolidate(env):
    """Append that crosses 80% should fire one consolidate (proactive)."""
    import core_memory
    consolidate_calls = []
    with patch.object(core_memory, "consolidate",
                      side_effect=lambda label, **kw:
                          (consolidate_calls.append(label),
                           {"ok": True, "old_len": 4500, "new_len": 3000})[-1]):
        # First append: 3000 chars (below soft).
        r1 = core_memory.append("human", "x" * 3000)
        assert r1["ok"]
        # Second append: 1500 chars → total 4500 (90%). Crosses 4000 (80%).
        r2 = core_memory.append("human", "y" * 1500)
        assert r2["ok"]
    assert consolidate_calls == ["human"], \
        "should fire exactly one proactive consolidate when crossing soft threshold"


def test_append_already_above_soft_does_not_re_fire(env):
    """If we're already above soft threshold and append more, don't
    re-fire (prev append already triggered)."""
    import core_memory
    consolidate_calls = []
    with patch.object(core_memory, "consolidate",
                      side_effect=lambda label, **kw:
                          (consolidate_calls.append(label),
                           {"ok": True, "old_len": 0, "new_len": 0})[-1]):
        # Cross threshold once
        core_memory.append("human", "x" * 4500)  # 0 → 4500: crosses
        # Already above; next small append should NOT re-fire
        core_memory.append("human", "y" * 100)   # 4500 → 4600: still above
    # Only the first crossing fires
    assert consolidate_calls == ["human"]


def test_append_overflow_calls_hard_consolidate_then_retries(env):
    """If new content would overflow the hard limit, run consolidate then retry."""
    import core_memory
    consolidate_calls = []
    def fake_consolidate(label, **kw):
        consolidate_calls.append(label)
        # Simulate consolidate compressing back to 3000 chars
        with core_memory._lock:
            data = core_memory._load()
            data[label]["value"] = "z" * 3000
            core_memory._save(data)
        return {"ok": True, "old_len": 5000, "new_len": 3000}
    with patch.object(core_memory, "consolidate", side_effect=fake_consolidate):
        # Pre-fill block to near the hard limit
        core_memory.append("human", "a" * 4900, auto_consolidate=False)
        # This overflow should trigger consolidate + retry
        r = core_memory.append("human", "b" * 500)
        assert r["ok"], f"overflow path should succeed after consolidate: {r}"
    assert "human" in consolidate_calls


def test_replace_respects_limit(env):
    """replace() rejects edits that would exceed the hard limit."""
    import core_memory
    # Pre-fill
    core_memory.append("human", "a" * 4500, auto_consolidate=False)
    # Replace "aa" (2 chars) with 3000-char text → would push past 5000
    r = core_memory.replace("human", "aa", "x" * 3000)
    assert not r["ok"]
    assert "limit" in r.get("error", "").lower()


def test_block_size_bounded_under_repeated_appends(env):
    """End-to-end: many small appends with proactive consolidate keep size bounded.

    Without auto-consolidate, repeated appends would grow the file linearly.
    With it (and a working consolidate), size stays in [target_chars, soft_chars]
    plus one append.
    """
    import core_memory
    # Stub consolidate to actually do compression (down to 60%)
    def fake_consolidate(label, **kw):
        with core_memory._lock:
            data = core_memory._load()
            current = data[label]["value"]
            limit = data[label]["limit"]
            target = int(limit * 0.6)
            data[label]["value"] = current[:target]  # naive truncate
            core_memory._save(data)
        return {"ok": True, "old_len": 0, "new_len": target}

    with patch.object(core_memory, "consolidate", side_effect=fake_consolidate):
        # Append 100-char chunks 100 times → 10,000 raw chars
        for i in range(100):
            core_memory.append("human", "x" * 100)
        final_len = len(core_memory.get_block("human") or "")
    # Without limit: would be 10,000. With limit: must stay near 60-100% of 5000.
    assert final_len <= 5000, f"block exceeded hard limit: {final_len}"
    assert final_len >= 1000, f"block consolidated too aggressively: {final_len}"
