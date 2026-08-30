"""Device list dedupe + 30-day cleanup tests.

The two rules under test (see device_manager._dedupe_and_cleanup):
  A. Stale: last_seen >30 days ago → dropped permanently
  B. Dedupe: same (name, platform) → keep only latest last_seen

Both are critical for the V2415A duplicate bug: a Vivo phone whose APK
LocalStorage was cleared (`pm clear` / Vivo system cleanup) gets a fresh
device_id but the same name/platform — without dedupe the user sees
multiple "Vivo V2415A" entries, one online and others stale.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import device_manager


def _ts(days_ago: float) -> str:
    """Build an ISO timestamp `days_ago` days before now."""
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _device(device_id: str, name: str, platform: str, days_ago: float) -> dict:
    return {
        "device_id": device_id,
        "name": name,
        "type": "phone" if "android" in platform or "ios" in platform else "desktop",
        "platform": platform,
        "registered_at": _ts(days_ago + 1),
        "last_seen": _ts(days_ago),
    }


class DedupeTests(unittest.TestCase):
    """Pure logic tests against _dedupe_and_cleanup — no filesystem."""

    def test_empty_list_is_passthrough(self):
        cleaned, changed, merge_map = device_manager._dedupe_and_cleanup([])
        self.assertEqual(cleaned, [])
        self.assertFalse(changed)
        self.assertEqual(merge_map, {})

    def test_single_fresh_device_unchanged(self):
        d = [_device("dev_1", "Vivo V2415A", "android", days_ago=0)]
        cleaned, changed, _merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertFalse(changed)

    def test_drops_devices_older_than_30_days(self):
        """Rule A: a 35-day-old device is dropped."""
        d = [
            _device("dev_old", "Old Phone", "android", days_ago=35),
            _device("dev_new", "New Phone", "android", days_ago=1),
        ]
        cleaned, changed, _merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["device_id"], "dev_new")
        self.assertTrue(changed)

    def test_keeps_devices_at_29_days(self):
        """Boundary: 29 days old is still considered fresh."""
        d = [_device("dev_x", "Phone", "android", days_ago=29)]
        cleaned, _, _m = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)

    def test_dedupe_same_name_platform_keeps_latest(self):
        """Rule B: 3 V2415A entries → only the most-recent kept."""
        d = [
            _device("dev_old1", "Vivo V2415A", "android", days_ago=10),
            _device("dev_old2", "Vivo V2415A", "android", days_ago=5),
            _device("dev_new",  "Vivo V2415A", "android", days_ago=0.1),
        ]
        cleaned, changed, _merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["device_id"], "dev_new")
        self.assertTrue(changed)

    def test_dedupe_today_duplicates_keeps_latest(self):
        """The actual V2415A bug from production: two entries today,
        both within 30d, dedupe must still pick the most-recent."""
        d = [
            _device("dev_morning", "Vivo V2415A", "android", days_ago=0.1),
            _device("dev_afternoon","Vivo V2415A", "android", days_ago=0.05),
        ]
        cleaned, _, _m = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["device_id"], "dev_afternoon")

    def test_different_platforms_not_merged(self):
        """Same name on Android vs iOS shouldn't merge."""
        d = [
            _device("dev_a", "Phone", "android", days_ago=1),
            _device("dev_i", "Phone", "ios", days_ago=2),
        ]
        cleaned, _, _m = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 2)

    def test_different_names_not_merged(self):
        """MacBook Air and MacBook Pro stay separate."""
        d = [
            _device("dev_a", "MacBook Air", "darwin", days_ago=1),
            _device("dev_p", "MacBook Pro", "darwin", days_ago=2),
        ]
        cleaned, _, _m = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 2)

    def test_combined_stale_and_dedupe(self):
        """Real production scenario: 4 devices, mix of stale + dupes.

        Models the actual VPS state seen earlier:
          #1 dev_f999 V2415A 19 days ago (stale)
          #2 dev_9b7a V2415A today morning
          #3 local_24 Macbook Air today
          #4 dev_db9d V2415A today afternoon (latest)

        Expected: 2 final devices (#4 V2415A + #3 Macbook Air).
        """
        d = [
            _device("dev_f999", "Vivo V2415A", "android", days_ago=19),
            _device("dev_9b7a", "Vivo V2415A", "android", days_ago=0.08),
            _device("local_24","Macbook Air", "darwin",  days_ago=0.01),
            _device("dev_db9d","Vivo V2415A", "android", days_ago=0.001),
        ]
        cleaned, changed, _merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertTrue(changed)
        self.assertEqual(len(cleaned), 2)
        # The V2415A survivor must be the latest one (dev_db9d)
        v_kept = [x for x in cleaned if x["name"] == "Vivo V2415A"][0]
        self.assertEqual(v_kept["device_id"], "dev_db9d")

    def test_missing_last_seen_kept_not_dropped(self):
        """Defensive: malformed records (no last_seen) shouldn't be silently lost."""
        d = [{"device_id": "weird", "name": "X", "platform": "linux"}]
        cleaned, _, _m = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)


class FilesystemRoundtripTests(unittest.TestCase):
    """Verify get_devices() persists cleanup back to devices.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_data_dir = device_manager._data_dir
        device_manager._data_dir = lambda: self.tmp

    def tearDown(self):
        device_manager._data_dir = self._orig_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, devices: list[dict]):
        path = os.path.join(self.tmp, "devices.json")
        with open(path, "w") as f:
            json.dump(devices, f, default=str)

    def _read(self) -> list[dict]:
        path = os.path.join(self.tmp, "devices.json")
        with open(path) as f:
            return json.load(f)

    def test_get_devices_writes_back_dedupe(self):
        """If on-disk list has dupes, get_devices() should clean + persist."""
        self._write([
            _device("dev_a", "V2415A", "android", days_ago=5),
            _device("dev_b", "V2415A", "android", days_ago=1),
            _device("dev_c", "V2415A", "android", days_ago=40),  # stale
        ])
        result = device_manager.get_devices()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["device_id"], "dev_b")

        # Persisted: subsequent reads should already see only 1 entry
        on_disk = self._read()
        self.assertEqual(len(on_disk), 1)

    def test_get_devices_no_writeback_when_clean(self):
        """If on-disk list is already clean, get_devices() reads but doesn't write."""
        self._write([_device("dev_x", "X", "linux", days_ago=1)])
        original_mtime = os.path.getmtime(
            os.path.join(self.tmp, "devices.json"))

        # Sleep tiny amount so mtime would be detectably different if rewritten
        import time as _t
        _t.sleep(0.05)
        device_manager.get_devices()
        new_mtime = os.path.getmtime(
            os.path.join(self.tmp, "devices.json"))
        self.assertEqual(original_mtime, new_mtime,
                         "clean list should not trigger a rewrite")

    def test_get_online_filters_after_dedupe(self):
        """get_online_devices() must operate on the deduped result."""
        self._write([
            _device("dev_old", "V2415A", "android", days_ago=1),     # offline (>120s)
            _device("dev_new", "V2415A", "android", days_ago=0.0001), # ~9s ago — online
        ])
        online = device_manager.get_online_devices(timeout_seconds=120)
        # Both happened to be V2415A → after dedupe only dev_new remains,
        # and that one is online.
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["device_id"], "dev_new")


class MergeMapTests(unittest.TestCase):
    """The merge_map drives screenshot_log rewrites. Verify it points
    losers → winner correctly so dev_9b7a8146 (V2415A morning) gets
    folded into dev_db9dc5b9 (V2415A afternoon) like in production."""

    def test_merge_map_points_losers_to_winner(self):
        d = [
            _device("dev_morning", "Vivo V2415A", "android", days_ago=0.1),
            _device("dev_afternoon","Vivo V2415A", "android", days_ago=0.05),
            _device("dev_old",     "Vivo V2415A", "android", days_ago=10),
        ]
        cleaned, _, merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["device_id"], "dev_afternoon")
        # Both losers must map to the winner — historical screenshots get
        # rewritten on disk via _rewrite_screenshot_log_device_ids.
        self.assertEqual(merge_map.get("dev_morning"), "dev_afternoon")
        self.assertEqual(merge_map.get("dev_old"),     "dev_afternoon")

    def test_merge_map_empty_when_no_dupes(self):
        d = [
            _device("dev_a", "Phone", "android", days_ago=1),
            _device("dev_b", "Phone", "ios",     days_ago=1),
        ]
        _, _, merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(merge_map, {})

    def test_stale_device_does_not_appear_in_merge_map(self):
        """A device dropped by rule A (>30d) is NOT in merge_map — its
        screenshot history was already aged out, no need to rewrite."""
        d = [
            _device("dev_old",   "Phone", "android", days_ago=40),  # stale → dropped
            _device("dev_fresh", "Phone", "android", days_ago=1),   # kept
        ]
        cleaned, _, merge_map = device_manager._dedupe_and_cleanup(d)
        self.assertEqual(len(cleaned), 1)
        self.assertNotIn("dev_old", merge_map)


class ScreenshotLogRewriteTests(unittest.TestCase):
    """Integration test for the dedupe → rewrite chain. Without rewriting,
    the user sees stale device_ids ("dev_9b7a8146") in the screenshot
    stats UI; this is the bug we're fixing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Patch storage.screenshot_log_path to point inside our tmp dir
        import storage
        self._orig_storage_path = storage.screenshot_log_path
        storage.screenshot_log_path = lambda: os.path.join(self.tmp, "screenshot_log.json")

    def tearDown(self):
        import storage
        storage.screenshot_log_path = self._orig_storage_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_log(self, data):
        with open(os.path.join(self.tmp, "screenshot_log.json"), "w") as f:
            json.dump(data, f)

    def _read_log(self):
        with open(os.path.join(self.tmp, "screenshot_log.json")) as f:
            return json.load(f)

    def test_rewrites_old_device_ids_to_new(self):
        """The classic V2415A scenario: 5 entries, mix of old/new ids."""
        self._write_log({
            "2026-04-13": [
                {"d": "dev_old1", "t": "11:00"},
                {"d": "dev_old1", "t": "11:30"},
            ],
            "2026-05-02": [
                {"d": "dev_old2", "t": "12:55"},
                {"d": "dev_new",  "t": "14:38"},
                {"d": "dev_new",  "t": "14:40"},
            ],
        })
        merge_map = {"dev_old1": "dev_new", "dev_old2": "dev_new"}
        count = device_manager._rewrite_screenshot_log_device_ids(merge_map)
        self.assertEqual(count, 3)
        result = self._read_log()
        # All entries now point at dev_new
        for date_key, entries in result.items():
            for entry in entries:
                self.assertEqual(entry["d"], "dev_new")

    def test_empty_merge_map_is_noop(self):
        self._write_log({"2026-05-02": [{"d": "dev_x", "t": "1"}]})
        count = device_manager._rewrite_screenshot_log_device_ids({})
        self.assertEqual(count, 0)
        # File unchanged
        self.assertEqual(self._read_log(), {"2026-05-02": [{"d": "dev_x", "t": "1"}]})

    def test_missing_log_file_is_safe(self):
        # No screenshot_log.json on disk
        count = device_manager._rewrite_screenshot_log_device_ids({"a": "b"})
        self.assertEqual(count, 0)


class CascadeRewriteTests(unittest.TestCase):
    """The dedupe cascade walks every data file in
    _DEVICE_ID_CASCADE_FILES. This regression test pins that:
      - chat_history.json with `device_id` key gets rewritten
      - emotion_log.json with `d` key gets rewritten
      - missing files don't blow up
    Adding a new data file using device_id as a foreign key MUST be
    accompanied by a similar test, otherwise old entries become orphans
    next time the user clears LocalStorage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import storage
        self._orig = (
            storage.screenshot_log_path,
            storage.chat_history_path,
            storage.emotion_log_path,
        )
        storage.screenshot_log_path = lambda: os.path.join(self.tmp, "screenshot_log.json")
        storage.chat_history_path   = lambda: os.path.join(self.tmp, "chat_history.json")
        storage.emotion_log_path    = lambda: os.path.join(self.tmp, "emotion_log.json")

    def tearDown(self):
        import storage
        storage.screenshot_log_path = self._orig[0]
        storage.chat_history_path   = self._orig[1]
        storage.emotion_log_path    = self._orig[2]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, fname, data):
        with open(os.path.join(self.tmp, fname), "w") as f:
            json.dump(data, f)

    def _read(self, fname):
        with open(os.path.join(self.tmp, fname)) as f:
            return json.load(f)

    def test_cascade_rewrites_all_three_files(self):
        # screenshot_log: {date: [{d, t}]}
        self._write("screenshot_log.json", {
            "2026-05-01": [{"d": "old1", "t": "10:00"}],
        })
        # chat_history: list of {device_id, role, ...}
        self._write("chat_history.json", [
            {"device_id": "old1", "role": "user", "text": "hi"},
            {"device_id": "old2", "role": "user", "text": "hey"},
            {"device_id": "new",  "role": "user", "text": "ok"},
        ])
        # emotion_log: {date: [{d, mood}]} (compact form)
        self._write("emotion_log.json", {
            "2026-05-01": [
                {"d": "old1", "mood": "calm"},
                {"d": "old2", "mood": "tired"},
            ],
        })

        merge_map = {"old1": "new", "old2": "new"}
        summary = device_manager._cascade_device_id_merge(merge_map)

        self.assertEqual(summary, {
            "screenshot_log.json": 1,
            "chat_history.json":   2,
            "emotion_log.json":    2,
        })

        # Verify all files rewrote — no orphan device_ids remain
        self.assertEqual(self._read("screenshot_log.json"),
                         {"2026-05-01": [{"d": "new", "t": "10:00"}]})
        chat = self._read("chat_history.json")
        self.assertTrue(all(e["device_id"] == "new" for e in chat))
        emo = self._read("emotion_log.json")
        self.assertTrue(all(e["d"] == "new" for e in emo["2026-05-01"]))

    def test_cascade_skips_missing_files(self):
        # Only one of the three exists; cascade shouldn't fail
        self._write("emotion_log.json", {"2026-05-01": [{"d": "old", "mood": "x"}]})
        summary = device_manager._cascade_device_id_merge({"old": "new"})
        self.assertEqual(summary, {"emotion_log.json": 1})

    def test_cascade_empty_merge_map_is_noop(self):
        self._write("chat_history.json", [{"device_id": "x"}])
        summary = device_manager._cascade_device_id_merge({})
        self.assertEqual(summary, {})


if __name__ == "__main__":
    unittest.main()
