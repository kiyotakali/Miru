from pathlib import Path
import sys

from PIL import Image

import desktop_paths
from sensor import ScreenSensor
from windows import platform as windows_platform


def test_windows_app_support_uses_local_app_data():
    local_app_data = r"C:\Users\tester\AppData\Local"
    path = desktop_paths.app_support_dir(
        platform_name="win32",
        environ={"LOCALAPPDATA": local_app_data},
        home=Path(r"C:\Users\tester"),
    )
    assert path == Path(local_app_data) / "Miru"


def test_macos_app_support_path_is_unchanged():
    path = desktop_paths.app_support_dir(
        platform_name="darwin",
        environ={},
        home=Path("/Users/tester"),
    )
    assert path == Path("/Users/tester/Library/Application Support/Miru")


def test_cursor_monitor_selection_handles_negative_coordinates():
    monitors = [
        windows_platform.MonitorBounds(1, 0, 0, 1920, 1080, r"\\.\DISPLAY1"),
        windows_platform.MonitorBounds(2, -1280, 0, 0, 1024, r"\\.\DISPLAY2"),
    ]

    assert windows_platform._select_monitor_for_point(monitors, 100, 100).index == 1
    assert windows_platform._select_monitor_for_point(monitors, -500, 400).index == 2
    assert windows_platform._select_monitor_for_point(monitors, -1500, 400).index == 2


def test_windows_capture_uses_cursor_monitor_bbox(monkeypatch):
    monitor = windows_platform.MonitorBounds(
        2, -1280, 0, 0, 1024, r"\\.\DISPLAY2"
    )
    calls = []

    monkeypatch.setattr(windows_platform, "get_cursor_monitor", lambda: monitor)

    def fake_grab(*, bbox, all_screens):
        calls.append((bbox, all_screens))
        return Image.new("RGB", (1280, 1024), "white")

    monkeypatch.setattr("PIL.ImageGrab.grab", fake_grab)

    sensor = ScreenSensor()
    image = sensor._capture_windows()

    assert image is not None
    assert image.size == (1280, 1024)
    assert calls == [((-1280, 0, 0, 1024), True)]
    assert sensor._last_capture_display_index == 2
    assert sensor._last_capture_display_name == r"\\.\DISPLAY2"
    assert sensor._last_capture_error == ""


def test_capture_probe_never_uploads(monkeypatch):
    sensor = ScreenSensor()
    monkeypatch.setattr(
        sensor,
        "_capture_screenshot",
        lambda: Image.new("RGB", (640, 480), "black"),
    )
    monkeypatch.setattr(
        sensor,
        "_post_screenshot",
        lambda _payload: (_ for _ in ()).throw(AssertionError("must not upload")),
    )

    assert sensor.probe_capture() == {
        "ok": True,
        "width": 640,
        "height": 480,
        "display_index": None,
        "display_name": "",
    }


def test_windows_client_device_name_fallback(monkeypatch):
    import app as app_mod

    saved = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("platform.node", lambda: "")
    monkeypatch.setattr(app_mod, "_save_launcher_config", lambda cfg: saved.append(dict(cfg)))
    monkeypatch.setattr(app_mod, "_replace_client_runtime_config", lambda _cfg: (1, None))
    monkeypatch.setattr(app_mod, "_deferred_client_setup_trigger", None)

    app_mod._persist_client_config_and_start(
        "http://127.0.0.1:5001",
        "test-token",
        user_id="u_test",
        mode="local",
        local_only=True,
        setup_complete=False,
    )

    assert saved[0]["device_name"] == "Windows PC"
