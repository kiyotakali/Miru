"""Tests for user_settings.py — per-user settings via Flask g.user_data_dir."""

import importlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _user_ctx(user_data_dir: str):
    """Push a Flask app + request context with g.user_data_dir set."""
    from flask import Flask, g
    app = Flask(__name__)
    with app.app_context():
        g.user_data_dir = user_data_dir
        yield


def test_defaults_when_empty(monkeypatch, tmp_path):
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        cfg = user_settings.get()
        assert cfg["timezone"] == ""
        assert cfg["auto_screenshot_interval"] == 30
        assert cfg["pet_collapse_delay"] == 4000
        assert cfg["pet_hotkey"]  # OS-dependent default
        # pet_chat_position + pet_toolbar_position were removed — Live2D
        # layout is computed from actual model bounds at runtime, so those
        # static percentages were basically never effective.
        assert "pet_chat_position" not in cfg
        assert "pet_toolbar_position" not in cfg


def test_screenshot_defaults_match_all_clients():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    sensor_source = (ROOT / "sensor.py").read_text(encoding="utf-8")
    service_source = (
        ROOT / "miru-mobile/android/app/src/main/java/com/miru/companion/ScreenCaptureService.java"
    ).read_text(encoding="utf-8")
    activity_source = (
        ROOT / "miru-mobile/android/app/src/main/java/com/miru/companion/MainActivity.java"
    ).read_text(encoding="utf-8")
    profile_source = (
        ROOT / "miru-mobile/android/app/src/main/java/com/miru/companion/MiruProfileStore.java"
    ).read_text(encoding="utf-8")

    assert "CHECK_INTERVAL = 30" in sensor_source
    assert 'get("auto_screenshot_interval", 30)' in app_source
    assert "cfg.auto_screenshot_interval || 30" in html
    assert "默认30秒；低于10秒会显著增加耗电与 API 费用" in html
    assert 'getInt(key + ".capture_interval_s", 30)' in profile_source
    assert "Math.max(5, Math.min(3600, seconds))" in activity_source
    assert "DEFAULT_INTERVAL_S = 30" in service_source
    assert "MAX_INTERVAL_MS = 3_600_000" in service_source
    assert "MiruProfileStore.getCaptureInterval(this)" in service_source
    assert "Math.max(5, Math.min(3600, seconds))" in service_source


def test_update_persists_to_user_dir(monkeypatch, tmp_path):
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        r = user_settings.update({
            "timezone": "Asia/Shanghai",
            "auto_screenshot_interval": 45,
            "pet_hotkey": "cmd+e",
            "pet_collapse_delay": 8000,
        })
        assert r["ok"] is True

        cfg = user_settings.get()
        assert cfg["timezone"] == "Asia/Shanghai"
        assert cfg["auto_screenshot_interval"] == 45
        assert cfg["pet_hotkey"] == "cmd+e"

        # File written to user dir
        with open(os.path.join(user_dir, "user_settings.json")) as f:
            raw = json.load(f)
        assert raw["timezone"] == "Asia/Shanghai"


def test_concurrent_updates_are_serialized_per_user(monkeypatch, tmp_path):
    """Concurrent desktop/native writes must not share one .tmp file."""
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)

    original_save = user_settings._save
    counter_lock = threading.Lock()
    active_saves = 0
    max_active_saves = 0
    errors = []

    def slow_save(data):
        nonlocal active_saves, max_active_saves
        with counter_lock:
            active_saves += 1
            max_active_saves = max(max_active_saves, active_saves)
        try:
            time.sleep(0.02)
            return original_save(data)
        finally:
            with counter_lock:
                active_saves -= 1

    monkeypatch.setattr(user_settings, "_save", slow_save)

    def run_update(payload):
        try:
            with _user_ctx(user_dir):
                result = user_settings.update(payload)
                assert result["ok"] is True
        except Exception as exc:
            errors.append(exc)

    workers = [
        threading.Thread(
            target=run_update,
            args=({"screenshot_enabled": True},),
        ),
        threading.Thread(
            target=run_update,
            args=({"screenshot_perm_guided": True},),
        ),
        threading.Thread(
            target=run_update,
            args=({"auto_screenshot_interval": 45},),
        ),
        threading.Thread(
            target=run_update,
            args=({"timezone": "Asia/Shanghai"},),
        ),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    assert max_active_saves == 1
    with open(os.path.join(user_dir, "user_settings.json"), encoding="utf-8") as f:
        raw = json.load(f)
    assert raw == {
        "screenshot_enabled": True,
        "screenshot_perm_guided": True,
        "auto_screenshot_interval": 45,
        "timezone": "Asia/Shanghai",
    }
    assert not list(Path(user_dir).glob("user_settings.json.*.tmp"))


def test_update_without_context_returns_error(monkeypatch, tmp_path):
    import user_settings
    importlib.reload(user_settings)
    # No Flask context pushed → _config_path returns None
    r = user_settings.update({"timezone": "Asia/Shanghai"})
    assert r["ok"] is False
    assert "no user context" in r["error"]


def test_screenshot_interval_clamped(monkeypatch, tmp_path):
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        # Write raw 1 (below min=5) directly and verify get() clamps
        with open(os.path.join(user_dir, "user_settings.json"), "w") as f:
            json.dump({"auto_screenshot_interval": 1}, f)
        assert user_settings.get()["auto_screenshot_interval"] == 5
        assert user_settings.screenshot_interval() == 5

        # update() also clamps — 2s requested, clamped to 5s floor
        user_settings.update({"auto_screenshot_interval": 2})
        with open(os.path.join(user_dir, "user_settings.json")) as f:
            raw = json.load(f)
        assert raw["auto_screenshot_interval"] == 5

        # Accepts values above the floor
        user_settings.update({"auto_screenshot_interval": 15})
        with open(os.path.join(user_dir, "user_settings.json")) as f:
            raw = json.load(f)
        assert raw["auto_screenshot_interval"] == 15

        # Upper clamp
        user_settings.update({"auto_screenshot_interval": 99999})
        with open(os.path.join(user_dir, "user_settings.json")) as f:
            raw = json.load(f)
        assert raw["auto_screenshot_interval"] == 3600


def test_invalid_timezone_rejected(monkeypatch, tmp_path):
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        r = user_settings.update({"timezone": "Mars/Olympus"})
        assert r["ok"] is False
        assert "Invalid timezone" in r["error"]


def test_user_now_fallback_chain(monkeypatch, tmp_path):
    """user tz → server default_timezone → system local."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import server_config
    import user_settings
    importlib.reload(server_config)
    importlib.reload(user_settings)

    # Set a default on server_config
    admin_dir = tmp_path / "_admin"
    admin_dir.mkdir(exist_ok=True)
    with open(admin_dir / "server_config.json", "w") as f:
        json.dump({"default_timezone": "Asia/Shanghai"}, f)

    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        # User has no timezone — should fall back to server default
        now = user_settings.user_now()
        assert now.tzinfo is not None
        assert "Shanghai" in str(now.tzinfo)

        # Now set user tz — should override
        user_settings.update({"timezone": "America/New_York"})
        now2 = user_settings.user_now()
        assert "New_York" in str(now2.tzinfo)


def test_screenshot_enabled_default_false(monkeypatch, tmp_path):
    """screenshot_enabled defaults to False (user must opt in)."""
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        cfg = user_settings.get()
        assert cfg["screenshot_enabled"] is False


def test_screenshot_enabled_toggle(monkeypatch, tmp_path):
    """screenshot_enabled can be toggled on/off."""
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        # Enable
        r = user_settings.update({"screenshot_enabled": True})
        assert r["ok"] is True
        assert user_settings.get()["screenshot_enabled"] is True

        # Disable
        r = user_settings.update({"screenshot_enabled": False})
        assert r["ok"] is True
        assert user_settings.get()["screenshot_enabled"] is False

        # Persisted in file
        with open(os.path.join(user_dir, "user_settings.json")) as f:
            raw = json.load(f)
        assert raw["screenshot_enabled"] is False


def test_sensor_user_enabled_flag():
    """ScreenSensor.set_enabled controls tick() behavior."""
    from datetime import datetime, timedelta

    from sensor import ScreenSensor
    s = ScreenSensor(backend_url="http://localhost:5001", device_id="test")
    assert s.is_enabled() is False

    s._capture_disabled = True
    s._capture_disabled_until = datetime.now() + timedelta(minutes=5)
    s._capture_fail_count = 3
    s.set_enabled(True)
    assert s.is_enabled() is True
    # Enabling also resets capture backoff
    assert s._capture_disabled is False
    assert s._capture_disabled_until is None
    assert s._capture_fail_count == 0

    s.set_enabled(False)
    assert s.is_enabled() is False


def test_sensor_logs_capture_failure_reason(capsys):
    """Windows capture failures remain diagnosable after the next success."""
    from sensor import ScreenSensor

    sensor = ScreenSensor(backend_url="http://localhost:5001", device_id="test")
    sensor._user_enabled = True
    sensor._last_capture_error = "no Windows monitor found"
    sensor._capture_screenshot = lambda: None

    sensor.tick()

    output = capsys.readouterr().out
    assert "Capture attempt failed (1/3): no Windows monitor found" in output


def test_sensor_account_switch_reloads_local_enabled(monkeypatch):
    """A reused sensor singleton must not inherit the previous account toggle."""
    from sensor import ScreenSensor
    from PIL import Image

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"settings": {
                "screenshot_enabled": False,
                "auto_screenshot_interval": 60,
            }}

    def fake_get(*_args, **_kwargs):
        return Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    s = ScreenSensor(
        backend_url="http://old.example:5001",
        device_id="old-device",
        auth_token="old-token",
    )
    s.set_enabled(True)
    assert s.is_enabled() is True
    assert s._screen_changed(Image.new("RGB", (200, 150), color="white")) is True
    assert s._screen_changed(Image.new("RGB", (200, 150), color="white")) is False

    s.update_config(
        backend_url="http://new.example:5001",
        device_id="new-device",
        auth_token="new-token",
    )

    assert s.is_enabled() is False
    assert s._last_thumbnail is None
    assert s._screen_changed(Image.new("RGB", (200, 150), color="white")) is True


def test_sensor_enable_wakes_loop_and_resets_change_baseline():
    from sensor import ScreenSensor
    from PIL import Image

    s = ScreenSensor(backend_url="http://localhost:5001", device_id="test")
    frame = Image.new("RGB", (200, 150), color="white")
    assert s._screen_changed(frame) is True
    assert s._screen_changed(frame.copy()) is False

    s.set_enabled(True)

    assert s._wake_event.is_set()
    assert s._last_thumbnail is None
    assert s._screen_changed(frame.copy()) is True


def test_settings_screenshot_toggle_uses_unified_persist_path():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r"ssCb\.addEventListener\('change', function\(\) \{(?P<body>.*?)\n\s+\}\);",
        html,
        re.S,
    )
    assert match, "settings screenshot toggle handler not found"
    body = match.group("body")
    assert "_persistScreenCaptureSettings({ screenshot_enabled: on });" in body
    assert "body: JSON.stringify({ screenshot_enabled: on })" not in body


def test_screenshot_stats_aggregation(monkeypatch, tmp_path):
    """get_screenshot_stats aggregates passed ScreenSemanticGate rows."""
    import storage
    importlib.reload(storage)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)

    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    log = {
        yesterday: [
            {"t": f"{yesterday}T10:00:00", "d": "desktop_abc"},
            {"t": f"{yesterday}T11:00:00", "d": "desktop_abc"},
            {"t": f"{yesterday}T12:00:00", "d": "phone_xyz"},
        ],
        today: [
            {"t": f"{today}T09:00:00", "d": "desktop_abc"},
            {"t": f"{today}T14:30:00", "d": "phone_xyz"},
            {"t": f"{today}T15:00:00", "d": "phone_xyz"},
        ],
    }
    log_path = os.path.join(user_dir, "screenshot_log.json")
    with open(log_path, "w") as f:
        json.dump(log, f)

    gate_log = {
        yesterday: [
            {"t": f"{yesterday}T10:00:00", "d": "desktop_abc", "passed_gate": True},
            {"t": f"{yesterday}T11:00:00", "d": "desktop_abc", "passed_gate": False},
            {"t": f"{yesterday}T12:00:00", "d": "phone_xyz", "passed_gate": True},
        ],
        today: [
            {"t": f"{today}T09:00:00", "d": "desktop_abc", "passed_gate": True},
            {"t": f"{today}T14:30:00", "d": "phone_xyz", "passed_gate": False},
            {"t": f"{today}T15:00:00", "d": "phone_xyz", "passed_gate": True},
        ],
    }
    gate_path = os.path.join(user_dir, "screen_semantic_gate_log.json")
    with open(gate_path, "w") as f:
        json.dump(gate_log, f)

    with _user_ctx(user_dir):
        stats = storage.get_screenshot_stats()

    assert stats["desktop_abc"]["total"] == 2
    assert stats["desktop_abc"]["today"] == 1
    assert stats["desktop_abc"]["last_time"] == f"{today}T09:00:00"

    assert stats["phone_xyz"]["total"] == 2
    assert stats["phone_xyz"]["today"] == 1
    assert stats["phone_xyz"]["last_time"] == f"{today}T15:00:00"


def test_screenshot_stats_empty(monkeypatch, tmp_path):
    """get_screenshot_stats returns empty dict when no log exists."""
    import storage
    importlib.reload(storage)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        stats = storage.get_screenshot_stats()
    assert stats == {}


def test_append_screenshot_log_with_captured_at(monkeypatch, tmp_path):
    """Regression: append_screenshot_log must NOT raise UnboundLocalError when
    captured_at is provided (bug: `now` was only assigned inside the fallback
    branch but still referenced by the prune step).
    """
    import storage
    importlib.reload(storage)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        captured_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        # This call path (captured_at provided) used to silently fail with
        # `UnboundLocalError: local variable 'now' referenced before assignment`
        storage.append_screenshot_log("dev_abc", captured_at=f"{captured_day}T15:30:00")
        storage.append_screenshot_log("dev_abc", captured_at=f"{captured_day}T15:31:00")
        storage.append_screenshot_log("dev_xyz")  # also verify no-captured_at branch still works

        log = storage.load_screenshot_log()
        day = captured_day
        assert day in log, f"expected {day} key, got {list(log.keys())}"
        entries = log[day]
        assert len(entries) >= 2
        # Verify captured_at was honored, not overwritten by now()
        assert any(e["t"] == f"{captured_day}T15:30:00" and e["d"] == "dev_abc" for e in entries)
        assert any(e["t"] == f"{captured_day}T15:31:00" and e["d"] == "dev_abc" for e in entries)
        # Stats deliberately count only screenshots that passed ScreenSemanticGate.
        stats = storage.get_screenshot_stats()
        assert stats == {}
        storage.append_screen_semantic_gate_log(
            observation="这张截图有新的长期记忆价值",
            significance=3,
            should_continue=True,
            device_id="dev_abc",
            captured_at=f"{captured_day}T15:32:00",
        )
        storage.append_screen_semantic_gate_log(
            observation="这张截图只是重复 UI 切换",
            significance=3,
            should_continue=False,
            device_id="dev_xyz",
        )
        stats = storage.get_screenshot_stats()
        assert stats["dev_abc"]["total"] == 1
        assert "dev_xyz" not in stats


def test_airi_base_url_dropped(monkeypatch, tmp_path):
    """Legacy `airi_base_url` field should no longer be readable/writable via get/update."""
    import user_settings
    importlib.reload(user_settings)
    user_dir = str(tmp_path / "u_test")
    os.makedirs(user_dir, exist_ok=True)
    with _user_ctx(user_dir):
        # Writing it silently drops the field
        user_settings.update({"airi_base_url": "http://example.com"})
        cfg = user_settings.get()
        assert "airi_base_url" not in cfg
