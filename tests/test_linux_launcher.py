from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import ast
import sys
from types import SimpleNamespace

import pytest

import linux_launcher
import windows_launcher


def test_linux_url_retains_auth_and_platform(monkeypatch):
    monkeypatch.setattr(windows_launcher, "DESKTOP_PLATFORM", "linux")
    url = windows_launcher._initial_url({"auth_token": "test-token", "setup_complete": True})
    query = parse_qs(urlsplit(url).query)
    assert query["desktop_platform"] == ["linux"]
    assert query["token"] == ["test-token"]


def test_linux_capture_capability_is_not_consent(monkeypatch):
    monkeypatch.setattr(windows_launcher, "DESKTOP_PLATFORM", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    api = windows_launcher.MiruDesktopApi(None, {})
    assert api.check_screen_permission() == {
        "ok": True, "platform": "linux", "granted": True,
        "requires_opt_in": True, "supported": True,
    }
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert api.check_screen_permission()["supported"] is False


def test_linux_launcher_rejects_ssh_without_display(monkeypatch):
    monkeypatch.setattr(linux_launcher.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert linux_launcher.main() == 1


@pytest.mark.parametrize("session", ["wayland", "Wayland", "WAYLAND"])
def test_linux_launcher_rejects_wayland(monkeypatch, session):
    monkeypatch.setattr(linux_launcher.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    assert linux_launcher.main() == 1


def test_linux_launcher_delegates_to_shared_lifecycle(monkeypatch):
    monkeypatch.setattr(linux_launcher.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("MIRU_DESKTOP_PLATFORM", raising=False)
    monkeypatch.delenv("QT_API", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    calls = []
    def delegate(**kw):
        assert linux_launcher.os.environ["MIRU_DESKTOP_PLATFORM"] == "linux"
        calls.append(kw)
        return 7
    monkeypatch.setattr(windows_launcher, "main", delegate)
    assert linux_launcher.main() == 7
    assert calls == [{"platform_name": "linux"}]


def _app_namespace(name, platform="linux", **bindings):
    # Test lifecycle boundaries without starting the Flask/model runtime.
    import os
    source = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {name, "_is_linux_desktop"}]
    for function in functions:
        function.decorator_list = []
    namespace = {"os": os, "sys": SimpleNamespace(platform=platform, executable="test-python"),
                 "__file__": str(source), "_find_tauri_binary": lambda: "tauri-pet"}
    namespace.update(bindings)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_pet_dispatch_preserves_auth_and_non_linux_command(monkeypatch, capsys, platform):
    import subprocess
    namespace = _app_namespace("_launch_pet_tauri", platform)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)) or "child")
    original_env = {"PATH": "test-path", "MIRU_DESKTOP_PLATFORM": "linux"}
    url = "http://127.0.0.1:5001/pet?token=test-private-token#anchor"
    assert namespace["_launch_pet_tauri"](url, "ctrl+alt+m", original_env, "test-root") == "child"
    command, options = calls[0]
    assert original_env == {"PATH": "test-path", "MIRU_DESKTOP_PLATFORM": "linux"}
    assert options["env"]["AIRI_PET_HOTKEY"] == "ctrl+alt+m"
    assert options["cwd"] == "test-root"
    if platform == "linux":
        assert command == ["test-python", "-s", str(Path(namespace["__file__"]).parent / "linux_pet.py")]
        assert options["env"]["AIRI_PET_PARENT_PID"] == str(namespace["os"].getpid())
        parsed = urlsplit(options["env"]["AIRI_PET_URL"])
        assert parse_qs(parsed.query) == {"token": ["test-private-token"], "desktop_platform": ["linux"]}
        assert parsed.fragment == "anchor"
    else:
        assert command == ["tauri-pet"]
        assert options["env"]["AIRI_PET_URL"] == url
    assert "test-private-token" not in capsys.readouterr().out


def test_linux_server_does_not_start_qt_pet(monkeypatch):
    import subprocess
    namespace = _app_namespace("_launch_pet_tauri")
    namespace["_find_tauri_binary"] = lambda: None
    def unexpected_spawn(*args, **kwargs):
        pytest.fail("server launched a Qt desktop process")
    monkeypatch.setattr(subprocess, "Popen", unexpected_spawn)
    assert namespace["_launch_pet_tauri"]("http://127.0.0.1:5001/pet", "ctrl+alt+m",
                                           {"PATH": "test-path"}, "test-root") is None


def test_xwayland_without_session_type_is_rejected(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert linux_launcher.x11_session_available() is False
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert linux_launcher.x11_session_available() is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process cleanup")
def test_linux_shutdown_ignores_stale_pid_files(monkeypatch, tmp_path):
    import os
    namespace = _app_namespace("_kill_pet", _pet_process=None, _pet_pid_dir=lambda: tmp_path)
    monkeypatch.setenv("MIRU_DESKTOP_PLATFORM", "linux")
    (tmp_path / ".pet.pid").write_text("12345")
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("signalled an unverified persisted PID"))
    namespace["_kill_pet"]()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process cleanup")
def test_linux_exit_api_stops_owned_children_without_signalling_parent(monkeypatch, tmp_path):
    import os
    import threading
    import time
    calls = []
    class Child:
        stopped = False
        def poll(self):
            return 0 if self.stopped else None
        def terminate(self):
            self.stopped = True
            calls.append("owned-child")
    namespace = _app_namespace("api_shutdown", _kill_pet=lambda: calls.append("pet"),
                               _pet_pid_dir=lambda: tmp_path, _pet_children_list=[Child()],
                               jsonify=lambda data: data)
    monkeypatch.setenv("MIRU_DESKTOP_PLATFORM", "linux")
    (tmp_path / ".child_pids").write_text("12345")
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("signalled an unrelated PID"))
    monkeypatch.setattr(os, "getppid", lambda: pytest.fail("looked up the launching shell to kill"))
    def exit_current(code):
        raise SystemExit(code)
    monkeypatch.setattr(os, "_exit", exit_current)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))
    with pytest.raises(SystemExit) as exit_info:
        namespace["api_shutdown"]()
    assert exit_info.value.code == 0
    assert calls == ["pet", "owned-child"]
