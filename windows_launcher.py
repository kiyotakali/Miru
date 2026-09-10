"""Miru Windows/Linux desktop launcher.

Owns the local Flask runtime, main window, screen sensor and pet sidecar.
Windows uses WebView2/Tauri; Linux uses Qt. Closing the main window stops the
runtime; minimizing leaves it running.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from desktop_paths import (
    app_support_dir,
    data_dir,
    launcher_config_path,
    webview_storage_dir,
)


PORT = 5001
DESKTOP_PLATFORM = "windows"


def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _load_config() -> dict:
    path = launcher_config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _setup_complete_from_config(config: dict) -> bool:
    if "setup_complete" in config:
        return bool(config.get("setup_complete"))
    return bool(config.get("auth_token"))


def _desktop_url(url: str) -> str:
    """Tag a page so its earliest script can enter desktop-client mode."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["desktop"] = "1"
    query["desktop_platform"] = DESKTOP_PLATFORM
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _initial_url(config: dict, cache_buster: int | None = None) -> str:
    server_url = str(config.get("server_url") or "").rstrip("/")
    token = str(config.get("auth_token") or "")
    cache_buster = cache_buster or int(time.time())
    if token and _setup_complete_from_config(config):
        base = server_url or f"http://127.0.0.1:{PORT}"
        return _desktop_url(
            f"{base}/app?token={quote(token)}&v={cache_buster}"
        )
    return _desktop_url(f"http://127.0.0.1:{PORT}/login")


def _no_proxy_json(method: str, path: str, payload: dict | None = None) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with opener.open(request, timeout=5) as response:
        body = response.read()
    return json.loads(body or b"{}")


def _existing_instance_ready() -> bool:
    try:
        config = _no_proxy_json("GET", "/api/client-config")
        if not config.get("client_mode"):
            return False
        _no_proxy_json("POST", "/api/open-webui", {})
        return True
    except Exception:
        return False


def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def _wait_for_flask(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use():
            try:
                return bool(_no_proxy_json("GET", "/api/health").get("ok"))
            except Exception:
                pass
        time.sleep(0.1)
    return False


def _show_error(message: str) -> None:
    print(message, file=sys.stderr)
    if DESKTOP_PLATFORM != "windows":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Miru", 0x10)
    except Exception:
        pass


def _configure_logging() -> None:
    log_dir = app_support_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "miru.log", "a", encoding="utf-8", buffering=1)
    if DESKTOP_PLATFORM == "linux":
        try:
            os.chmod(log_dir / "miru.log", 0o600)
        except OSError:
            pass
    if getattr(sys, "frozen", False) or sys.stdout is None or DESKTOP_PLATFORM == "linux":
        sys.stdout = stream
    # Source runs keep terminal stderr so startup failures remain visible.
    if getattr(sys, "frozen", False) or sys.stderr is None:
        sys.stderr = stream


class MiruDesktopApi:
    """Restricted native bridge exposed to the trusted Miru WebView."""

    def __init__(self, app_module, state: dict):
        self._app = app_module
        self._state = state

    def get_local_settings(self) -> dict:
        try:
            body = _no_proxy_json("GET", "/api/user-settings")
            return {"ok": True, "settings": body.get("settings", body)}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}", "status": exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_local_settings(self, payload: dict | None = None) -> dict:
        try:
            body = _no_proxy_json("POST", "/api/user-settings", payload or {})
            return {"ok": bool(body.get("ok", True)), "settings": body.get("settings", body)}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}", "status": exc.code}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_external_url(self, url: str = "") -> dict:
        value = str(url or "").strip()
        if urlsplit(value).scheme not in {"http", "https"}:
            return {"ok": False, "error": "unsupported URL scheme"}
        try:
            return {"ok": bool(webbrowser.open(value))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_screen_permission(self) -> dict:
        if DESKTOP_PLATFORM == "linux":
            from linux_launcher import x11_session_available
            supported = x11_session_available()
            return {"ok": True, "platform": "linux", "granted": supported,
                    "requires_opt_in": True, "supported": supported}
        # Windows has no macOS-style TCC prompt. This only reports platform
        # capability; pixels are not captured until the user clicks Enable.
        return {
            "ok": True,
            "platform": "windows",
            "granted": True,
            "requires_opt_in": True,
        }

    def probe_screen_capture(self) -> dict:
        try:
            import sensor

            instance = sensor.get_sensor()
            result = instance.probe_capture()
            result["platform"] = DESKTOP_PLATFORM
            return result
        except Exception as exc:
            return {"ok": False, "platform": DESKTOP_PLATFORM, "error": str(exc)}

    def notify_app_ready(self, payload: dict | None = None) -> dict:
        path = str((payload or {}).get("path") or "")
        if path and "/app" not in path:
            return {"ok": False, "error": "not on app page"}
        self._state["app_ready"] = True
        return {"ok": True}


def _stop_runtime(app_module, state: dict) -> None:
    lock = state["shutdown_lock"]
    with lock:
        if state.get("shutting_down"):
            return
        state["shutting_down"] = True

    try:
        with app_module._client_runtime_lock:
            app_module._client_runtime_cancel_event.set()
            config = dict(app_module._client_mode_config or {})
        app_module._teardown_client_runtime(config)
    except Exception as exc:
        print(f"[DesktopLauncher] client runtime stop failed: {exc}")
    try:
        app_module._kill_pet()
    except Exception as exc:
        print(f"[DesktopLauncher] pet stop failed: {exc}")


def _webview_bootstrap_ready(window) -> bool:
    """Return true once the visible /app finished its own data bootstrap."""
    try:
        current = str(window.get_current_url() or "")
        if urlsplit(current).path != "/app":
            return False
        return bool(
            window.evaluate_js(
                "Boolean(window._bootstrapComplete === true "
                "&& !document.getElementById('bootSplash'))"
            )
        )
    except Exception:
        return False


def _runtime_coordinator(window, app_module, state: dict) -> None:
    while not state.get("shutting_down"):
        try:
            if app_module._open_webui_requested.is_set():
                app_module._open_webui_requested.clear()
                window.show()
                window.restore()

            if app_module._pet_should_hide_event.is_set():
                app_module._pet_should_hide_event.clear()
                app_module._pet_ready_event.clear()
                app_module._kill_pet()
                state["pet_launched"] = False
                state["app_ready"] = False
                window.load_url(_desktop_url(f"http://127.0.0.1:{PORT}/login"))
                window.show()
                window.restore()

            wants_pet = (
                app_module._pet_ready_event.is_set()
                or app_module._launch_pet_requested.is_set()
            )
            # A remote server can finish /app before pywebview exposes the
            # native bridge, so the page's one-shot app-ready callback may be
            # missed. Confirm bootstrap from the owning launcher as a fallback.
            if wants_pet and not state.get("app_ready"):
                state["app_ready"] = _webview_bootstrap_ready(window)
            if wants_pet and state.get("app_ready") and not state.get("pet_launched"):
                app_module._launch_pet_requested.clear()
                app_module._kill_pet()
                app_module._launch_full_stack()
                state["pet_launched"] = True
        except Exception as exc:
            print(f"[DesktopLauncher] coordinator warning: {exc}")
        time.sleep(0.25)


def main(platform_name: str = "windows") -> int:
    global DESKTOP_PLATFORM
    DESKTOP_PLATFORM = platform_name
    if platform_name == "windows":
        if sys.platform != "win32":
            raise RuntimeError("windows_launcher.py must run on Windows")
        from windows.platform import set_process_dpi_awareness
        set_process_dpi_awareness()
    elif platform_name != "linux" or not sys.platform.startswith("linux"):
        raise RuntimeError("unsupported desktop platform")
    _configure_logging()

    if _existing_instance_ready():
        return 0
    if _port_in_use():
        _show_error("Port 5001 is already in use. Close the other program and start Miru again.")
        return 1

    root = _bundle_dir()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    support = app_support_dir()
    support.mkdir(parents=True, exist_ok=True)
    persistent_data = data_dir()
    persistent_data.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(persistent_data)

    import app as app_module

    config = _load_config()
    server_url = str(config.get("server_url") or "")
    auth_token = str(config.get("auth_token") or "")
    state = {
        "app_ready": False,
        "pet_launched": False,
        "shutting_down": False,
        "shutdown_lock": threading.Lock(),
    }

    backend_thread = threading.Thread(
        target=app_module.run_client_mode,
        args=(server_url, auth_token, PORT),
        daemon=True,
        name="miru-local-flask",
    )
    backend_thread.start()
    if not _wait_for_flask():
        _show_error(f"Miru local service did not start. See {support / 'logs' / 'miru.log'}.")
        return 1

    import webview

    api = MiruDesktopApi(app_module, state)
    window = webview.create_window(
        "Miru",
        _initial_url(config),
        js_api=api,
        width=1200,
        height=820,
        min_size=(920, 640),
        resizable=True,
        text_select=True,
        background_color="#FFF9F2",
    )

    window.events.closing += lambda: _stop_runtime(app_module, state)

    def on_loaded():
        current = window.get_current_url() or ""
        state["app_ready"] = False if "/app" not in current else state["app_ready"]

    window.events.loaded += on_loaded
    threading.Thread(
        target=_runtime_coordinator,
        args=(window, app_module, state),
        daemon=True,
        name="miru-runtime-coordinator",
    ).start()

    storage = webview_storage_dir()
    storage.mkdir(parents=True, exist_ok=True)
    icon = root / "src-tauri" / "icons" / ("128x128.png" if platform_name == "linux" else "icon.ico")
    debug_port = str(os.environ.get("MIRU_WEBVIEW_DEBUG_PORT") or "").strip()
    if debug_port:
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(debug_port)
    try:
        webview.start(
            gui="qt" if platform_name == "linux" else "edgechromium",
            private_mode=False,
            storage_path=str(storage),
            icon=str(icon) if icon.exists() else None,
        )
    finally:
        _stop_runtime(app_module, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
