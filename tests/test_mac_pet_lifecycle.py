from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_mac_window_close_hard_quits_instead_of_backgrounding():
    launcher = (ROOT / "miru_launcher.py").read_text(encoding="utf-8")
    match = re.search(
        r"def windowShouldClose_\(self, sender\):(?P<body>.*?)(?:\n\s+# WKNavigationDelegate:)",
        launcher,
        re.S,
    )
    assert match, "windowShouldClose_ body not found"
    body = match.group("body")
    assert "_hard_quit()" in body
    assert "NSAlert" not in body
    assert "留在后台" not in body
    assert "orderOut_" not in body


def test_kill_pet_removes_singleton_lock():
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(
        r"def _kill_pet\(\):(?P<body>.*?)(?:\n\ndef _find_tauri_binary)",
        app_py,
        re.S,
    )
    assert match, "_kill_pet body not found"
    body = match.group("body")
    assert 'pet_lock_file = pid_dir / "data" / ".pet.lock"' in body
    assert "pet_lock_file" in re.search(r"for f in \((?P<files>.*?)\):", body, re.S).group("files")


def test_hotkey_can_restore_missing_pet_window():
    rust = (ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "fn pet_url_with_cache_buster()" in rust
    match = re.search(
        r"fn toggle_pet_visibility\(app: &AppHandle\) \{(?P<body>.*?)(?:\n\}\n\n#\[cfg\(test\)\])",
        rust,
        re.S,
    )
    assert match, "toggle_pet_visibility body not found"
    body = match.group("body")
    assert "app.get_webview_window(\"airi-pet\")" in body
    assert "open_airi_window(app.clone(), pet_url)" in body
    assert "hotkey restored missing window" in body
    assert "orderFrontRegardless" in body


def test_pending_first_run_setup_does_not_open_app_or_sync_models():
    launcher = (ROOT / "miru_launcher.py").read_text(encoding="utf-8")

    assert "setup_complete = _setup_complete_from_config(cfg)" in launcher
    assert "def _setup_complete_from_config(cfg: dict) -> bool:" in launcher
    assert 'if "setup_complete" in cfg:' in launcher
    assert 'return bool(cfg.get("auth_token"))' in launcher
    assert "def _webview_target_from_config(cfg: dict, cache_buster: int) -> str:" in launcher
    assert "if auth_token and _setup_complete_from_config(cfg):" in launcher
    assert "initial_url = _webview_target_from_config(cfg, _cache_buster)" in launcher
    assert "target = _webview_target_from_config(latest_cfg, int(time.time()))" in launcher
    assert "if server_url and auth_token and setup_complete:" in launcher


def test_legacy_launcher_config_with_token_is_returning_account():
    import miru_launcher

    assert miru_launcher._setup_complete_from_config({"auth_token": "tok"}) is True
    assert miru_launcher._setup_complete_from_config({
        "auth_token": "tok",
        "setup_complete": False,
    }) is False
    assert miru_launcher._setup_complete_from_config({}) is False


def test_visible_window_keeps_pending_remote_setup_on_local_login():
    import miru_launcher

    pending = {
        "auth_token": "pending-token",
        "server_url": "http://8.8.8.8:5002",
        "setup_complete": False,
    }
    complete = {**pending, "setup_complete": True}

    assert miru_launcher._webview_target_from_config(pending, 123) == (
        "http://localhost:5001/login"
    )
    assert miru_launcher._webview_target_from_config(complete, 123) == (
        "http://8.8.8.8:5002/app?token=pending-token&v=123"
    )


def test_pet_waits_until_app_webview_finished_loading():
    launcher = (ROOT / "miru_launcher.py").read_text(encoding="utf-8")
    index_html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert '"webui_app_loaded": False' in launcher
    assert '"webui_app_ready": False' in launcher
    assert "def _webview_is_on_app_page(self, webview):" in launcher
    assert "return \"/app\" in value" in launcher
    assert "def _mark_app_ready(self, payload):" in launcher
    assert "notifyAppReady:function(p){return call('app_ready',p||{});}" in launcher
    assert "def _pet_launch_allowed(self):" in launcher
    assert "[Launcher] Pet ready; waiting for /app bootstrap" in launcher
    assert "window.MiruDesktop.notifyAppReady" in index_html
    assert "phase: 'bootstrap_complete'" in index_html

    finish_body = re.search(
        r"def webView_didFinishNavigation_\(self, webview, navigation\):(?P<body>.*?)(?:\n\s+# WKUIDelegate:)",
        launcher,
        re.S,
    )
    assert finish_body, "webView_didFinishNavigation_ body not found"
    assert "_state[\"webui_app_loaded\"] = self._webview_is_on_app_page(webview)" in finish_body.group("body")

    assert '_state["webui_app_loaded"] and _state["webui_app_ready"]' in launcher

    periodic_body = re.search(
        r"def periodicCheck_\(self, timer\):(?P<body>.*?)(?:\n\s+controller = _MiruController)",
        launcher,
        re.S,
    )
    assert periodic_body, "periodicCheck_ body not found"
    body = periodic_body.group("body")
    assert "if self._pet_launch_allowed():" in body
    assert "_ensure_pet_launched()" in body
