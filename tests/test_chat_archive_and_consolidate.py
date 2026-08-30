"""Tests for #10 (chat_history rolling archive) and #15 (core_memory auto-consolidation).

All tests are offline — mock LLM calls, use temp directories.
"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ============================================================
# #10: Chat History Rolling Archive
# ============================================================

class ChatArchiveTests:
    """Test storage.archive_old_chat_messages and related functions."""

    def __init__(self):
        self.passed = 0
        self.total = 0
        self.tmpdir = None

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        import storage
        self._old_data_dir = storage.DATA_DIR
        self._old_archive_dir = storage.ARCHIVE_DIR
        storage.DATA_DIR = self.tmpdir
        storage.ARCHIVE_DIR = os.path.join(self.tmpdir, "archive")
        os.makedirs(storage.ARCHIVE_DIR, exist_ok=True)

    def teardown(self):
        import storage
        storage.DATA_DIR = self._old_data_dir
        storage.ARCHIVE_DIR = self._old_archive_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def check(self, condition, pass_msg, fail_msg):
        self.total += 1
        if condition:
            print(f"  [PASS] {pass_msg}")
            self.passed += 1
        else:
            print(f"  [FAIL] {fail_msg}")

    def test_no_archive_when_small(self):
        """Should not archive when fewer than keep_recent messages."""
        print("TEST: no archive when history is small")
        import storage
        self.setup()
        try:
            # Write 100 messages
            msgs = [{"id": f"msg_{i}", "role": "user", "text": f"msg {i}",
                     "time": f"2026-03-{10 + i % 20:02d} 10:00:00"} for i in range(100)]
            storage.write_json(storage.chat_history_path(), msgs)

            result = storage.archive_old_chat_messages(keep_recent=500)
            self.check(result["archived"] == 0, "0 messages archived", f"archived={result['archived']}")
            self.check(result["remaining"] == 100, "100 messages remain", f"remaining={result['remaining']}")

            # Verify chat_history unchanged
            history = storage.read_json(storage.chat_history_path())
            self.check(len(history) == 100, "history unchanged", f"history len={len(history)}")
        finally:
            self.teardown()

    def test_archive_splits_by_month(self):
        """Should split archived messages by month."""
        print("\nTEST: archive splits messages by month")
        import storage
        self.setup()
        try:
            msgs = []
            # 200 messages in January
            for i in range(200):
                msgs.append({"id": f"jan_{i}", "role": "user", "text": f"jan msg {i}",
                             "time": f"2026-01-{(i % 28) + 1:02d} 10:00:00"})
            # 200 messages in February
            for i in range(200):
                msgs.append({"id": f"feb_{i}", "role": "user", "text": f"feb msg {i}",
                             "time": f"2026-02-{(i % 28) + 1:02d} 10:00:00"})
            # 200 messages in March (recent)
            for i in range(200):
                msgs.append({"id": f"mar_{i}", "role": "user", "text": f"mar msg {i}",
                             "time": f"2026-03-{(i % 28) + 1:02d} 10:00:00"})

            storage.write_json(storage.chat_history_path(), msgs)

            result = storage.archive_old_chat_messages(keep_recent=200)

            self.check(result["archived"] == 400, "400 messages archived",
                      f"archived={result['archived']}")
            self.check(result["remaining"] == 200, "200 messages remain",
                      f"remaining={result['remaining']}")

            # Check archive files exist
            self.check("chat_history_2026-01.json" in result["files"],
                      "January archive created", f"files={result['files']}")
            self.check("chat_history_2026-02.json" in result["files"],
                      "February archive created", f"files={result['files']}")

            # Verify January archive content
            jan_archive = storage.get_archived_chat_history("2026-01")
            self.check(len(jan_archive) == 200, "January has 200 messages",
                      f"January has {len(jan_archive)}")
            self.check(jan_archive[0]["id"] == "jan_0", "First Jan msg preserved",
                      f"First id={jan_archive[0].get('id')}")

            # Verify active file only has recent 200
            active = storage.read_json(storage.chat_history_path())
            self.check(len(active) == 200, "Active file has 200",
                      f"Active has {len(active)}")
            self.check(active[0]["id"] == "mar_0", "Active starts with March",
                      f"First active id={active[0].get('id')}")
        finally:
            self.teardown()

    def test_archive_appends_to_existing(self):
        """Running archive twice should append, not overwrite existing archives."""
        print("\nTEST: archive appends to existing archive files")
        import storage
        self.setup()
        try:
            # First batch: 300 Jan messages + 200 March messages
            msgs1 = []
            for i in range(300):
                msgs1.append({"id": f"jan_batch1_{i}", "role": "user", "text": f"b1 {i}",
                              "time": f"2026-01-{(i % 28) + 1:02d} 10:00:00"})
            for i in range(200):
                msgs1.append({"id": f"mar_batch1_{i}", "role": "user", "text": f"b1 mar {i}",
                              "time": f"2026-03-{(i % 28) + 1:02d} 10:00:00"})
            storage.write_json(storage.chat_history_path(), msgs1)
            storage.archive_old_chat_messages(keep_recent=200)

            jan_after_first = storage.get_archived_chat_history("2026-01")
            first_archive_count = len(jan_after_first)
            self.check(first_archive_count == 300, "First archive: 300 Jan messages",
                      f"First archive: {first_archive_count}")

            # Second batch: add 300 more Jan messages + 200 March
            msgs2 = []
            for i in range(300):
                msgs2.append({"id": f"jan_batch2_{i}", "role": "user", "text": f"b2 {i}",
                              "time": f"2026-01-15 12:00:00"})
            for i in range(200):
                msgs2.append({"id": f"mar_batch2_{i}", "role": "user", "text": f"b2 mar {i}",
                              "time": f"2026-03-20 12:00:00"})

            # Load current active, prepend old, write back
            active = storage.read_json(storage.chat_history_path())
            storage.write_json(storage.chat_history_path(), msgs2 + active)

            storage.archive_old_chat_messages(keep_recent=200)

            jan_after_second = storage.get_archived_chat_history("2026-01")
            self.check(len(jan_after_second) == 600, "Second archive: 600 Jan messages (appended)",
                      f"Second archive: {len(jan_after_second)}")
        finally:
            self.teardown()

    def test_get_archived_months(self):
        """get_archived_chat_months should list available archives."""
        print("\nTEST: get_archived_chat_months")
        import storage
        self.setup()
        try:
            # Create some archive files
            storage.write_json(os.path.join(storage.ARCHIVE_DIR, "chat_history_2026-01.json"), [])
            storage.write_json(os.path.join(storage.ARCHIVE_DIR, "chat_history_2026-03.json"), [])
            storage.write_json(os.path.join(storage.ARCHIVE_DIR, "chat_history_2025-12.json"), [])

            months = storage.get_archived_chat_months()
            self.check(months == ["2025-12", "2026-01", "2026-03"],
                      "Months sorted correctly",
                      f"Got {months}")
        finally:
            self.teardown()

    def test_missing_time_field(self):
        """Messages without time field should be archived under 'unknown' month."""
        print("\nTEST: messages without time field")
        import storage
        self.setup()
        try:
            msgs = [{"id": f"no_time_{i}", "role": "user", "text": "no time"} for i in range(300)]
            # Add 200 recent with time
            for i in range(200):
                msgs.append({"id": f"recent_{i}", "role": "user", "text": "recent",
                             "time": "2026-03-30 10:00:00"})
            storage.write_json(storage.chat_history_path(), msgs)

            result = storage.archive_old_chat_messages(keep_recent=200)
            self.check(result["archived"] == 300, "300 archived",
                      f"archived={result['archived']}")
            self.check("chat_history_unknown.json" in result["files"],
                      "'unknown' month file created",
                      f"files={result['files']}")

            unknown = storage.get_archived_chat_history("unknown")
            self.check(len(unknown) == 300, "300 in unknown archive",
                      f"unknown has {len(unknown)}")
        finally:
            self.teardown()

    def test_get_chat_history_unaffected(self):
        """get_chat_history(limit=N) should work the same before and after archive."""
        print("\nTEST: get_chat_history unaffected by archive")
        import storage
        self.setup()
        try:
            msgs = [{"id": f"msg_{i}", "role": "user", "text": f"msg {i}",
                     "time": f"2026-03-{(i % 28) + 1:02d} 10:00:00"} for i in range(600)]
            storage.write_json(storage.chat_history_path(), msgs)

            # Before archive
            before = storage.get_chat_history(limit=20)
            self.check(len(before) == 20, "Before: 20 messages", f"Before: {len(before)}")
            self.check(before[-1]["id"] == "msg_599", "Before: last is msg_599",
                      f"Before last: {before[-1].get('id')}")

            # Archive
            storage.archive_old_chat_messages(keep_recent=500)

            # After archive
            after = storage.get_chat_history(limit=20)
            self.check(len(after) == 20, "After: 20 messages", f"After: {len(after)}")
            self.check(after[-1]["id"] == "msg_599", "After: last is msg_599",
                      f"After last: {after[-1].get('id')}")
        finally:
            self.teardown()

    def test_idempotent(self):
        """Running archive twice should not change anything on second run."""
        print("\nTEST: archive is idempotent")
        import storage
        self.setup()
        try:
            msgs = [{"id": f"msg_{i}", "role": "user", "text": f"msg {i}",
                     "time": "2026-03-15 10:00:00"} for i in range(600)]
            storage.write_json(storage.chat_history_path(), msgs)

            r1 = storage.archive_old_chat_messages(keep_recent=500)
            self.check(r1["archived"] == 100, "First run: 100 archived",
                      f"First: {r1['archived']}")

            r2 = storage.archive_old_chat_messages(keep_recent=500)
            self.check(r2["archived"] == 0, "Second run: 0 archived",
                      f"Second: {r2['archived']}")
        finally:
            self.teardown()

    def run_all(self):
        print("=" * 60)
        print("SUITE: Chat History Rolling Archive (#10)")
        print("=" * 60)
        self.test_no_archive_when_small()
        self.test_archive_splits_by_month()
        self.test_archive_appends_to_existing()
        self.test_get_archived_months()
        self.test_missing_time_field()
        self.test_get_chat_history_unaffected()
        self.test_idempotent()
        print(f"\n  Archive: {self.passed}/{self.total} passed")
        return self.passed, self.total


# ============================================================
# #15: Core Memory Auto-Consolidation
# ============================================================

class CoreMemoryConsolidateTests:
    """Test core_memory.consolidate() and auto-consolidation in append()."""

    def __init__(self):
        self.passed = 0
        self.total = 0
        self.tmpdir = None

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        import core_memory
        self._old_path = core_memory.CORE_MEMORY_PATH
        core_memory.CORE_MEMORY_PATH = os.path.join(self.tmpdir, "core_memory.json")

    def teardown(self):
        import core_memory
        core_memory.CORE_MEMORY_PATH = self._old_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_block(self, label, value, limit=200):
        """Helper to write a test block directly."""
        import core_memory
        data = {
            label: {"label": label, "value": value, "limit": limit}
        }
        # Ensure both blocks exist
        for l in ["human", "persona"]:
            if l not in data:
                data[l] = {"label": l, "value": "", "limit": 100000}
        with open(core_memory.CORE_MEMORY_PATH, "w") as f:
            json.dump(data, f)

    def check(self, condition, pass_msg, fail_msg):
        self.total += 1
        if condition:
            print(f"  [PASS] {pass_msg}")
            self.passed += 1
        else:
            print(f"  [FAIL] {fail_msg}")

    def test_consolidate_calls_llm(self):
        """consolidate() should call LLM and compress the block."""
        print("TEST: consolidate() calls LLM and compresses")
        import core_memory
        self.setup()
        try:
            # Write a block that's 180/200 chars (90% full)
            content = "A" * 180
            self._write_block("human", content, limit=200)

            mock_compressed = "compressed content here"
            with patch("prompt._call_llm_text", return_value=mock_compressed) as mock_llm:
                result = core_memory.consolidate("human", target_ratio=0.6)

            self.check(result["ok"], "consolidation succeeded", f"result={result}")
            self.check(result["old_len"] == 180, f"old_len=180", f"old_len={result.get('old_len')}")
            self.check(result["new_len"] == len(mock_compressed),
                      f"new_len={len(mock_compressed)}", f"new_len={result.get('new_len')}")

            # Verify block was updated
            val = core_memory.get_block("human")
            self.check(val == mock_compressed, "block value updated",
                      f"block value={val[:50]}")
        finally:
            self.teardown()

    def test_consolidate_skips_when_small(self):
        """consolidate() should skip when block is already small enough."""
        print("\nTEST: consolidate() skips when already small")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "small", limit=200)

            result = core_memory.consolidate("human", target_ratio=0.6)
            self.check(result["ok"], "returned ok", f"result={result}")
            self.check("already within" in result.get("message", ""),
                      "message says already within target",
                      f"message={result.get('message')}")

            # Block should be unchanged
            val = core_memory.get_block("human")
            self.check(val == "small", "block unchanged", f"block={val}")
        finally:
            self.teardown()

    def test_consolidate_handles_empty(self):
        """consolidate() should fail gracefully on empty block."""
        print("\nTEST: consolidate() handles empty block")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "", limit=200)

            result = core_memory.consolidate("human")
            self.check(not result["ok"], "returned not ok", f"result={result}")
            self.check("empty" in result.get("error", "").lower(),
                      "error mentions empty", f"error={result.get('error')}")
        finally:
            self.teardown()

    def test_consolidate_unknown_block(self):
        """consolidate() should fail for unknown block label."""
        print("\nTEST: consolidate() handles unknown block")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "data", limit=200)

            result = core_memory.consolidate("nonexistent")
            self.check(not result["ok"], "returned not ok", f"result={result}")
        finally:
            self.teardown()

    def test_append_auto_consolidates(self):
        """append() should auto-consolidate when block is full, then retry."""
        print("\nTEST: append() auto-consolidates on full block")
        import core_memory
        self.setup()
        try:
            # Block at 180/200 chars, trying to append 30 chars (would exceed limit)
            self._write_block("human", "X" * 180, limit=200)

            # Mock LLM to return compressed version (100 chars)
            mock_compressed = "Y" * 100
            with patch("prompt._call_llm_text", return_value=mock_compressed):
                result = core_memory.append("human", "Z" * 30)

            self.check(result["ok"], "append succeeded after consolidation",
                      f"result={result}")

            # Block should now have compressed + appended content
            val = core_memory.get_block("human")
            self.check(val == "Y" * 100 + "Z" * 30, "value = compressed + appended",
                      f"val len={len(val)}, expected {130}")
            self.check(len(val) == 130, "total length = 130", f"length={len(val)}")
        finally:
            self.teardown()

    def test_append_fails_when_consolidation_fails(self):
        """append() should return error if consolidation also fails."""
        print("\nTEST: append() fails gracefully when consolidation fails")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "X" * 180, limit=200)

            # Mock LLM to raise
            with patch("prompt._call_llm_text", side_effect=RuntimeError("LLM down")):
                result = core_memory.append("human", "Z" * 30)

            self.check(not result["ok"], "append failed", f"result={result}")
            self.check("chars_remaining" in result, "has chars_remaining",
                      f"keys={result.keys()}")
            self.check(result["chars_remaining"] == 20, "chars_remaining=20",
                      f"chars_remaining={result.get('chars_remaining')}")

            # Block should be unchanged
            val = core_memory.get_block("human")
            self.check(val == "X" * 180, "block unchanged after failed consolidation",
                      f"val={val[:20]}...")
        finally:
            self.teardown()

    def test_append_no_consolidation_when_disabled(self):
        """append(auto_consolidate=False) should not attempt consolidation."""
        print("\nTEST: append with auto_consolidate=False")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "X" * 180, limit=200)

            result = core_memory.append("human", "Z" * 30, auto_consolidate=False)
            self.check(not result["ok"], "append failed (no auto-consolidate)",
                      f"result={result}")
            self.check("chars_remaining" in result, "has chars_remaining",
                      f"keys={result.keys()}")
        finally:
            self.teardown()

    def test_append_normal_still_works(self):
        """Normal append (within limit) should work without consolidation."""
        print("\nTEST: normal append still works")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "hello", limit=200)

            result = core_memory.append("human", " world")
            self.check(result["ok"], "append succeeded", f"result={result}")

            val = core_memory.get_block("human")
            self.check(val == "hello world", "value correct", f"val={val}")
        finally:
            self.teardown()

    def test_consolidate_llm_failure(self):
        """consolidate() should handle LLM failure gracefully."""
        print("\nTEST: consolidate() handles LLM failure")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "A" * 180, limit=200)

            with patch("prompt._call_llm_text", side_effect=Exception("timeout")):
                result = core_memory.consolidate("human")

            self.check(not result["ok"], "returned not ok", f"result={result}")
            self.check("LLM" in result.get("error", ""), "error mentions LLM",
                      f"error={result.get('error')}")

            # Block should be unchanged
            val = core_memory.get_block("human")
            self.check(val == "A" * 180, "block unchanged after LLM failure",
                      f"val len={len(val)}")
        finally:
            self.teardown()

    def test_consolidate_empty_llm_response(self):
        """consolidate() should reject empty LLM response."""
        print("\nTEST: consolidate() rejects empty LLM response")
        import core_memory
        self.setup()
        try:
            self._write_block("human", "A" * 180, limit=200)

            with patch("prompt._call_llm_text", return_value=""):
                result = core_memory.consolidate("human")

            self.check(not result["ok"], "returned not ok", f"result={result}")

            # Block should be unchanged
            val = core_memory.get_block("human")
            self.check(val == "A" * 180, "block unchanged", f"val len={len(val)}")
        finally:
            self.teardown()

    def test_no_infinite_recursion(self):
        """append with auto_consolidate should not recurse infinitely."""
        print("\nTEST: no infinite recursion on consolidation")
        import core_memory
        self.setup()
        try:
            # Even after consolidation, the block is still too small for the append
            self._write_block("human", "X" * 180, limit=200)

            # LLM returns something that's still 190 chars (not enough room for 30 more)
            mock_compressed = "Y" * 190
            with patch("prompt._call_llm_text", return_value=mock_compressed):
                result = core_memory.append("human", "Z" * 30)

            # Should fail (no second consolidation attempt)
            self.check(not result["ok"], "append failed (even after consolidation)",
                      f"result={result}")
        finally:
            self.teardown()

    def run_all(self):
        print("\n" + "=" * 60)
        print("SUITE: Core Memory Auto-Consolidation (#15)")
        print("=" * 60)
        self.test_consolidate_calls_llm()
        self.test_consolidate_skips_when_small()
        self.test_consolidate_handles_empty()
        self.test_consolidate_unknown_block()
        self.test_append_auto_consolidates()
        self.test_append_fails_when_consolidation_fails()
        self.test_append_no_consolidation_when_disabled()
        self.test_append_normal_still_works()
        self.test_consolidate_llm_failure()
        self.test_consolidate_empty_llm_response()
        self.test_no_infinite_recursion()
        print(f"\n  Consolidate: {self.passed}/{self.total} passed")
        return self.passed, self.total


# ============================================================
# #14: Index.md Smart Consolidation
# ============================================================

class IndexConsolidationTests:
    """Test memory.consolidate_index() — dedup, journal pruning, formatting."""

    def __init__(self):
        self.passed = 0
        self.total = 0
        self.tmpdir = None

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        import memory
        self._old_memory_dir = memory.MEMORY_DIR
        self._old_index_path = memory.INDEX_PATH
        memory.MEMORY_DIR = os.path.join(self.tmpdir, "memory")
        memory.INDEX_PATH = os.path.join(memory.MEMORY_DIR, "index.md")
        os.makedirs(memory.MEMORY_DIR, exist_ok=True)
        for d in memory.DEFAULT_DIRS:
            os.makedirs(os.path.join(memory.MEMORY_DIR, d), exist_ok=True)

    def teardown(self):
        import memory
        memory.MEMORY_DIR = self._old_memory_dir
        memory.INDEX_PATH = self._old_index_path
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_index(self, content):
        import memory
        with open(memory.INDEX_PATH, "w", encoding="utf-8") as f:
            f.write(content)

    def _read_index(self):
        import memory
        with open(memory.INDEX_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def check(self, condition, pass_msg, fail_msg):
        self.total += 1
        if condition:
            print(f"  [PASS] {pass_msg}")
            self.passed += 1
        else:
            print(f"  [FAIL] {fail_msg}")

    def test_no_changes_when_clean(self):
        """Should return zero counts when index has no duplicates or old journals."""
        print("\nTEST: no changes when index is clean")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "_Auto-maintained._\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — Alice info\n\n"
                "## Journal\n\n"
                "- [2026-03-25.md](journal/2026-03-25.md) — Recent entry\n"
            )
            result = memory.consolidate_index()
            self.check(result["deduped"] == 0, "deduped=0", f"deduped={result['deduped']}")
            self.check(result["journal_pruned"] == 0, "journal_pruned=0",
                       f"journal_pruned={result['journal_pruned']}")
        finally:
            self.teardown()

    def test_dedup_same_section(self):
        """Should deduplicate entries with same file path in same section."""
        print("\nTEST: dedup entries in same section")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — OLD description\n"
                "- [bob.md](people/bob.md) — Bob info\n"
                "- [alice.md](people/alice.md) — NEW description\n"
            )
            result = memory.consolidate_index()
            self.check(result["deduped"] == 1, "deduped=1", f"deduped={result['deduped']}")

            content = self._read_index()
            self.check("NEW description" in content, "kept latest entry",
                       f"content={content}")
            self.check("OLD description" not in content, "removed old entry",
                       f"content={content}")
            self.check(content.count("alice.md") == 2, "alice appears exactly once (link+text)",
                       f"count={content.count('alice.md')}")
        finally:
            self.teardown()

    def test_dedup_multiple_duplicates(self):
        """Should handle file appearing 3+ times (keep only last)."""
        print("\nTEST: dedup triple duplicate")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — v1\n"
                "- [alice.md](people/alice.md) — v2\n"
                "- [alice.md](people/alice.md) — v3\n"
                "- [bob.md](people/bob.md) — Bob\n"
            )
            result = memory.consolidate_index()
            self.check(result["deduped"] == 2, "deduped=2", f"deduped={result['deduped']}")

            content = self._read_index()
            self.check("v3" in content, "kept last (v3)", f"content={content}")
            self.check("v1" not in content, "removed v1", f"content={content}")
            self.check("v2" not in content, "removed v2", f"content={content}")
        finally:
            self.teardown()

    def test_dedup_across_different_sections_no_cross(self):
        """Same file path in different sections should NOT be deduped."""
        print("\nTEST: no cross-section dedup")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [note.md](people/note.md) — People note\n\n"
                "## Topics\n\n"
                "- [note.md](topics/note.md) — Topics note\n"
            )
            result = memory.consolidate_index()
            self.check(result["deduped"] == 0, "deduped=0 (different sections)",
                       f"deduped={result['deduped']}")

            content = self._read_index()
            self.check("People note" in content, "people entry kept", f"content={content}")
            self.check("Topics note" in content, "topics entry kept", f"content={content}")
        finally:
            self.teardown()

    def test_journal_pruning_old_entries(self):
        """Should prune journal entries older than keep_journal_days."""
        print("\nTEST: prune old journal entries")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## Journal\n\n"
                "- [2020-01-01.md](journal/2020-01-01.md) — Very old\n"
                "- [2020-06-15.md](journal/2020-06-15.md) — Also old\n"
                "- [2026-03-29.md](journal/2026-03-29.md) — Yesterday\n"
                "- [2026-03-30.md](journal/2026-03-30.md) — Today\n"
            )
            result = memory.consolidate_index(keep_journal_days=30)
            self.check(result["journal_pruned"] == 2, "pruned 2 old entries",
                       f"journal_pruned={result['journal_pruned']}")

            content = self._read_index()
            self.check("2020-01-01" not in content, "2020-01-01 removed", f"content={content}")
            self.check("2020-06-15" not in content, "2020-06-15 removed", f"content={content}")
            self.check("2026-03-29" in content, "yesterday kept", f"content={content}")
            self.check("2026-03-30" in content, "today kept", f"content={content}")
        finally:
            self.teardown()

    def test_journal_pruning_only_affects_journal(self):
        """Date-based pruning should NOT affect non-journal sections."""
        print("\nTEST: pruning only affects Journal section")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## Projects\n\n"
                "- [2020-01-01.md](projects/2020-01-01.md) — Old project\n\n"
                "## Journal\n\n"
                "- [2020-01-01.md](journal/2020-01-01.md) — Old journal\n"
            )
            result = memory.consolidate_index(keep_journal_days=30)
            self.check(result["journal_pruned"] == 1, "pruned 1 journal entry",
                       f"journal_pruned={result['journal_pruned']}")

            content = self._read_index()
            # Project entry with old date in path should be untouched
            self.check("Old project" in content, "project entry preserved",
                       f"content={content}")
            self.check("Old journal" not in content, "journal entry pruned",
                       f"content={content}")
        finally:
            self.teardown()

    def test_journal_non_date_entries_kept(self):
        """Journal entries without date in path should not be pruned."""
        print("\nTEST: journal non-date entries preserved")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## Journal\n\n"
                "- [reflections.md](journal/reflections.md) — General reflections\n"
                "- [2020-01-01.md](journal/2020-01-01.md) — Old dated entry\n"
            )
            result = memory.consolidate_index(keep_journal_days=30)
            self.check(result["journal_pruned"] == 1, "pruned only dated entry",
                       f"journal_pruned={result['journal_pruned']}")

            content = self._read_index()
            self.check("reflections" in content, "non-dated entry kept",
                       f"content={content}")
        finally:
            self.teardown()

    def test_combined_dedup_and_prune(self):
        """Should handle dedup + journal pruning together."""
        print("\nTEST: combined dedup and journal prune")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — old\n"
                "- [alice.md](people/alice.md) — new\n\n"
                "## Journal\n\n"
                "- [2020-01-01.md](journal/2020-01-01.md) — Ancient\n"
                "- [2026-03-30.md](journal/2026-03-30.md) — Today\n"
            )
            result = memory.consolidate_index(keep_journal_days=30)
            self.check(result["deduped"] == 1, "deduped=1", f"deduped={result['deduped']}")
            self.check(result["journal_pruned"] == 1, "journal_pruned=1",
                       f"journal_pruned={result['journal_pruned']}")
        finally:
            self.teardown()

    def test_empty_index(self):
        """Should handle empty/default index gracefully."""
        print("\nTEST: empty index")
        import memory
        self.setup()
        try:
            memory._write_default_index()
            result = memory.consolidate_index()
            self.check(result["deduped"] == 0, "deduped=0", f"deduped={result['deduped']}")
            self.check(result["journal_pruned"] == 0, "journal_pruned=0",
                       f"journal_pruned={result['journal_pruned']}")
        finally:
            self.teardown()

    def test_idempotent(self):
        """Running consolidation twice should produce same result."""
        print("\nTEST: idempotent")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — old\n"
                "- [alice.md](people/alice.md) — new\n\n"
                "## Journal\n\n"
                "- [2020-01-01.md](journal/2020-01-01.md) — Old\n"
            )
            r1 = memory.consolidate_index()
            content_after_first = self._read_index()

            r2 = memory.consolidate_index()
            content_after_second = self._read_index()

            self.check(r1["deduped"] == 1, "first run deduped=1", f"deduped={r1['deduped']}")
            self.check(r2["deduped"] == 0, "second run deduped=0", f"deduped={r2['deduped']}")
            self.check(r2["journal_pruned"] == 0, "second run journal_pruned=0",
                       f"journal_pruned={r2['journal_pruned']}")
            self.check(content_after_first == content_after_second,
                       "content unchanged after second run",
                       "content differs between runs")
        finally:
            self.teardown()

    def test_formatting_cleanup(self):
        """Should normalize blank lines (no excessive gaps)."""
        print("\nTEST: formatting cleanup")
        import memory
        self.setup()
        try:
            # Messy formatting with extra blank lines
            self._write_index(
                "# Memory Index\n\n\n\n"
                "_Auto-maintained._\n\n\n"
                "## People\n\n\n\n"
                "- [alice.md](people/alice.md) — Alice\n\n\n\n"
                "- [alice.md](people/alice.md) — Alice updated\n\n\n\n"
                "## Topics\n"
            )
            result = memory.consolidate_index()
            content = self._read_index()
            # Should not have triple blank lines
            self.check("\n\n\n\n" not in content, "no excessive blank lines",
                       f"content has 4+ consecutive newlines")
            self.check(result["before"] > result["after"], "line count reduced",
                       f"before={result['before']} after={result['after']}")
        finally:
            self.teardown()

    def test_preamble_preserved(self):
        """Should preserve preamble (title, description) before sections."""
        print("\nTEST: preamble preserved")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "_Auto-maintained by Miru._\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — old\n"
                "- [alice.md](people/alice.md) — new\n"
            )
            memory.consolidate_index()
            content = self._read_index()
            self.check("# Memory Index" in content, "title preserved", f"content={content}")
            self.check("_Auto-maintained by Miru._" in content, "description preserved",
                       f"content={content}")
        finally:
            self.teardown()

    def test_keep_journal_days_custom(self):
        """Custom keep_journal_days should work."""
        print("\nTEST: custom keep_journal_days=7")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## Journal\n\n"
                "- [2026-03-15.md](journal/2026-03-15.md) — 15 days ago\n"
                "- [2026-03-28.md](journal/2026-03-28.md) — 2 days ago\n"
            )
            # With 7 days, March 15 should be pruned but not March 28
            result = memory.consolidate_index(keep_journal_days=7)
            self.check(result["journal_pruned"] == 1, "pruned 1 entry",
                       f"journal_pruned={result['journal_pruned']}")

            content = self._read_index()
            self.check("2026-03-15" not in content, "old entry removed", f"content={content}")
            self.check("2026-03-28" in content, "recent entry kept", f"content={content}")
        finally:
            self.teardown()

    def test_no_write_when_nothing_to_do(self):
        """Should not write to disk when there are no changes."""
        print("\nTEST: no write when no changes needed")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "## People\n\n"
                "- [alice.md](people/alice.md) — Alice\n"
            )
            mtime_before = os.path.getmtime(memory.INDEX_PATH)
            # Small sleep to ensure mtime would change if written
            time.sleep(0.05)

            result = memory.consolidate_index()
            mtime_after = os.path.getmtime(memory.INDEX_PATH)

            self.check(result["deduped"] == 0, "no dedup needed", f"deduped={result['deduped']}")
            self.check(mtime_before == mtime_after, "file not rewritten",
                       f"mtime changed: {mtime_before} → {mtime_after}")
        finally:
            self.teardown()

    def test_regex_patterns(self):
        """Test the regex patterns used for extraction."""
        print("\nTEST: regex patterns")
        import memory

        # Index entry regex
        m = memory._INDEX_ENTRY_RE.search("- [alice.md](people/alice.md) — description")
        self.check(m and m.group(1) == "people/alice.md", "extracts file path",
                   f"match={m}")

        m2 = memory._INDEX_ENTRY_RE.search("- [日记](journal/2026-03-30.md) — 今日日记")
        self.check(m2 and m2.group(1) == "journal/2026-03-30.md", "extracts CJK entry path",
                   f"match={m2}")

        # Journal date regex
        m3 = memory._JOURNAL_DATE_RE.search("journal/2026-03-30.md")
        self.check(m3 and m3.group(1) == "2026-03-30", "extracts journal date",
                   f"match={m3}")

        m4 = memory._JOURNAL_DATE_RE.search("people/alice.md")
        self.check(m4 is None, "no date in non-journal path", f"match={m4}")

    def test_real_world_format(self):
        """Test with realistic index content mimicking production format."""
        print("\nTEST: real-world format")
        import memory
        self.setup()
        try:
            self._write_index(
                "# Memory Index\n\n"
                "_Auto-maintained by Miru. Maps all memory files. Keep under 200 lines._\n\n"
                "## People\n\n"
                "- [chenyang_si.md](people/chenyang_si.md) — 关于陈阳丝\n"
                "- [li_ken.md](people/li_ken.md) — 李恳的信息\n"
                "- [chenyang_si.md](people/chenyang_si.md) — 陈阳丝 (更新版)\n"
                "- [li_ken.md](people/li_ken.md) — 李恳 (更新版)\n\n"
                "## Commitments\n\n"
                "## Journal\n\n"
                "- [2025-12-01.md](journal/2025-12-01.md) — 12月初的日记\n"
                "- [2026-01-15.md](journal/2026-01-15.md) — 1月中日记\n"
                "- [2026-03-28.md](journal/2026-03-28.md) — 前天\n"
                "- [2026-03-29.md](journal/2026-03-29.md) — 昨天\n"
                "- [2026-03-30.md](journal/2026-03-30.md) — 今天\n\n"
                "## Patterns\n\n"
                "## Self\n\n"
                "- [profile.md](self/profile.md) — 用户信息\n\n"
                "## Projects\n\n"
                "## Topics\n\n"
                "- [coding.md](topics/coding.md) — 编程话题\n"
            )
            result = memory.consolidate_index(keep_journal_days=30)
            self.check(result["deduped"] == 2, "deduped 2 (chenyang_si + li_ken)",
                       f"deduped={result['deduped']}")
            self.check(result["journal_pruned"] == 2, "pruned 2 old journals (2025-12, 2026-01)",
                       f"journal_pruned={result['journal_pruned']}")

            content = self._read_index()
            self.check("更新版" in content, "kept updated entries", f"content={content}")
            self.check("关于陈阳丝" not in content, "removed old chenyang_si entry",
                       f"content={content}")
            self.check("2025-12-01" not in content, "removed Dec 2025 journal",
                       f"content={content}")
            self.check("2026-03-30" in content, "kept today's journal",
                       f"content={content}")
            self.check("profile.md" in content, "self entries preserved", f"content={content}")
            self.check("coding.md" in content, "topic entries preserved", f"content={content}")
        finally:
            self.teardown()

    def run_all(self):
        print("\n" + "=" * 60)
        print("SUITE: Index.md Smart Consolidation (#14)")
        print("=" * 60)
        self.test_no_changes_when_clean()
        self.test_dedup_same_section()
        self.test_dedup_multiple_duplicates()
        self.test_dedup_across_different_sections_no_cross()
        self.test_journal_pruning_old_entries()
        self.test_journal_pruning_only_affects_journal()
        self.test_journal_non_date_entries_kept()
        self.test_combined_dedup_and_prune()
        self.test_empty_index()
        self.test_idempotent()
        self.test_formatting_cleanup()
        self.test_preamble_preserved()
        self.test_keep_journal_days_custom()
        self.test_no_write_when_nothing_to_do()
        self.test_regex_patterns()
        self.test_real_world_format()
        print(f"\n  Index Consolidation: {self.passed}/{self.total} passed")
        return self.passed, self.total


# ============================================================
# Integration: nightly maintenance triggers
# ============================================================

def test_nightly_review_triggers_archive():
    """_run_nightly_maintenance should call archive_old_chat_messages."""
    import inspect, core
    source = inspect.getsource(core._run_nightly_maintenance)
    assert "archive_old_chat_messages" in source, \
        "_run_nightly_maintenance should call archive_old_chat_messages"
    print("\nTEST: nightly maintenance triggers archive")
    print("  [PASS] _run_nightly_maintenance contains archive_old_chat_messages")
    return 1, 1


def test_nightly_review_triggers_index_consolidation():
    """_run_nightly_maintenance should call memory.consolidate_index."""
    import inspect, core
    source = inspect.getsource(core._run_nightly_maintenance)
    assert "consolidate_index" in source, \
        "_run_nightly_maintenance should call consolidate_index"
    print("\nTEST: nightly maintenance triggers index consolidation")
    print("  [PASS] _run_nightly_maintenance contains consolidate_index")
    return 1, 1


# ============================================================
# Main
# ============================================================

def main():
    archive_tests = ChatArchiveTests()
    p1, t1 = archive_tests.run_all()

    consolidate_tests = CoreMemoryConsolidateTests()
    p2, t2 = consolidate_tests.run_all()

    index_tests = IndexConsolidationTests()
    p3, t3 = index_tests.run_all()

    p4, t4 = test_nightly_review_triggers_archive()
    p5, t5 = test_nightly_review_triggers_index_consolidation()

    total_p = p1 + p2 + p3 + p4 + p5
    total_t = t1 + t2 + t3 + t4 + t5

    print("\n" + "=" * 60)
    print(f"TOTAL: {total_p}/{total_t} passed")
    print("=" * 60)

    if total_p < total_t:
        sys.exit(1)


if __name__ == "__main__":
    main()
