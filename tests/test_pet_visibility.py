import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PET_HTML = ROOT / "templates" / "pet.html"
INDEX_HTML = ROOT / "templates" / "index.html"
TAURI_LIB = ROOT / "src-tauri" / "src" / "lib.rs"


def test_tauri_pet_bridge_does_not_auto_hide_on_main_ui_heartbeat():
    html = PET_HTML.read_text(encoding="utf-8")
    m = re.search(r"function _initPetBridge\(invoke\) \{([\s\S]*?)\n\}", html)
    assert m, "_initPetBridge missing"
    body = m.group(1)

    assert "start_airi_pet_monitor" in body
    assert "_startUiActivePolling()" not in body


def test_ui_active_heartbeat_stays_disabled_in_dmg_client_mode():
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"\(function startUiHeartbeat\(\) \{([\s\S]*?)\n\}\)\(\);", html)
    assert m, "startUiHeartbeat IIFE missing"
    body = m.group(1)

    assert "window.__MIRU_CLIENT_MODE__" in body
    assert "return;  // Design C: pet is always-on" in body


def test_native_pet_hotkey_uses_real_window_visibility_as_source_of_truth():
    rust = TAURI_LIB.read_text(encoding="utf-8")
    assert "fn should_show_pet_for_toggle(remembered_hidden: bool, actual_visible: bool) -> bool" in rust
    assert "remembered_hidden || !actual_visible" in rust

    m = re.search(r"fn toggle_pet_visibility\(app: &AppHandle\) \{([\s\S]*?)\n\}", rust)
    assert m, "toggle_pet_visibility missing"
    body = m.group(1)

    assert "let mut actual_visible = window.is_visible().unwrap_or(false);" in body
    assert "msg_send![panel, isVisible]" in body
    assert "actual_visible = panel_visible;" in body
    assert "let should_show = should_show_pet_for_toggle(remembered_hidden, actual_visible);" in body
    assert "if should_show" in body


def test_native_pet_snap_show_synchronizes_hidden_state():
    rust = TAURI_LIB.read_text(encoding="utf-8")
    m = re.search(r"fn snap_airi_window\(window: Window, anchor: Option<String>\) -> Result<\(\), String> \{([\s\S]*?)\n\}", rust)
    assert m, "snap_airi_window missing"
    body = m.group(1)

    assert "window.show()" in body
    assert ".state::<PetVisibilityState>()" in body
    assert ".hidden" in body
    assert ".store(false, Ordering::Relaxed)" in body
