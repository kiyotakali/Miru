import threading
from pathlib import Path

import windows_launcher


def test_desktop_url_preserves_token_and_tags_windows_client():
    url = windows_launcher._desktop_url(
        "https://example.test/app?token=secret&v=7"
    )
    assert url.startswith("https://example.test/app?")
    assert "token=secret" in url
    assert "v=7" in url
    assert "desktop=1" in url
    assert "desktop_platform=windows" in url


def test_initial_url_for_fresh_user_is_local_desktop_login():
    assert windows_launcher._initial_url({}, cache_buster=123) == (
        "http://127.0.0.1:5001/login?desktop=1&desktop_platform=windows"
    )


def test_initial_url_for_local_returning_user_stays_local():
    url = windows_launcher._initial_url(
        {
            "mode": "local",
            "local_only": True,
            "server_url": "http://127.0.0.1:5001",
            "auth_token": "local-token",
            "setup_complete": True,
        },
        cache_buster=123,
    )
    assert url.startswith("http://127.0.0.1:5001/app?")
    assert "token=local-token" in url
    assert "desktop=1" in url


def test_initial_url_for_remote_returning_user_uses_server():
    url = windows_launcher._initial_url(
        {
            "mode": "remote",
            "server_url": "https://miru.example.test/",
            "auth_token": "remote-token",
            "setup_complete": True,
        },
        cache_buster=123,
    )
    assert url.startswith("https://miru.example.test/app?")
    assert "token=remote-token" in url


def test_windows_permission_check_does_not_capture(monkeypatch):
    class App:
        pass

    api = windows_launcher.MiruDesktopApi(App(), {})
    monkeypatch.setattr(
        "sensor.get_sensor",
        lambda: (_ for _ in ()).throw(AssertionError("must not capture")),
    )

    assert api.check_screen_permission() == {
        "ok": True,
        "platform": "windows",
        "granted": True,
        "requires_opt_in": True,
    }


def test_stop_runtime_cancels_session_and_stops_owned_services():
    calls = []

    class App:
        _client_runtime_lock = threading.Lock()
        _client_runtime_cancel_event = threading.Event()
        _client_mode_config = {"mode": "local", "user_id": "u_local"}

        @staticmethod
        def _teardown_client_runtime(config):
            calls.append(("teardown", config))

        @staticmethod
        def _kill_pet():
            calls.append(("pet", None))

    state = {
        "shutdown_lock": threading.Lock(),
        "shutting_down": False,
    }
    windows_launcher._stop_runtime(App, state)
    windows_launcher._stop_runtime(App, state)

    assert App._client_runtime_cancel_event.is_set()
    assert calls == [
        ("teardown", {"mode": "local", "user_id": "u_local"}),
        ("pet", None),
    ]


def test_frontend_preserves_windows_client_tag_and_requires_opt_in():
    login = (Path(__file__).parents[1] / "templates" / "login.html").read_text(
        encoding="utf-8"
    )
    index = (Path(__file__).parents[1] / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "withDesktopClientParams" in login
    assert "window.__MIRU_DESKTOP_PLATFORM__" in index
    assert "probeScreenCapture" in index
    assert "Capability is not consent" in index
    assert "_permModalDismissWithoutEnable" in index


def test_webview_bootstrap_ready_requires_completed_app_page():
    class Window:
        def __init__(self, url, result):
            self.url = url
            self.result = result
            self.expressions = []

        def get_current_url(self):
            return self.url

        def evaluate_js(self, expression):
            self.expressions.append(expression)
            return self.result

    login = Window("http://127.0.0.1:5001/login", True)
    pending = Window("https://miru.example.test/app?desktop=1", False)
    ready = Window("https://miru.example.test/app?desktop=1", True)

    assert windows_launcher._webview_bootstrap_ready(login) is False
    assert login.expressions == []
    assert windows_launcher._webview_bootstrap_ready(pending) is False
    assert windows_launcher._webview_bootstrap_ready(ready) is True
    assert "_bootstrapComplete" in ready.expressions[0]
    assert "bootSplash" in ready.expressions[0]


def test_windows_build_script_uses_real_python_and_fails_on_native_errors():
    script = (Path(__file__).parents[1] / "build_windows.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Programs\\Python\\Python312\\python.exe' in script
    assert script.count("if ($LASTEXITCODE -ne 0)") >= 7
    assert "Windows pet tests failed" in script
    assert "Windows pet build failed" in script
    assert "PyInstaller failed" in script
    assert "Inno Setup failed" in script
