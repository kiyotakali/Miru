import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "templates" / "index.html"


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"missing JavaScript function: {name}"
    brace = source.find("{", match.end())
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_windows_capture_recovery_never_sends_user_to_macos_permissions():
    html = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _function_source(html, name)
        for name in (
            "_showClientScreenRecordingModal",
            "_permModalEnableWindows",
        )
    )
    harness = r"""
let elements = {};
let window = {
  __MIRU_DESKTOP_PLATFORM__: 'windows',
  pywebview: { api: {} },
  MiruDesktop: { probeScreenCapture: async function() { return { ok: true }; } }
};
let CHARACTER = { name: 'Miru' };
let localStorage = { setItem: function() {} };
let persisted = [];
let healthWatchStarted = false;
let scheduledDelay = null;
let document = {
  body: { appendChild: function(element) { elements[element.id] = element; } },
  createElement: function() {
    return {
      id: '',
      style: {},
      innerHTML: '',
      removed: false,
      remove: function() { this.removed = true; }
    };
  },
  getElementById: function(id) { return elements[id] || null; }
};
function escapeHtml(value) { return String(value); }
function _persistScreenCaptureSettings(payload) { persisted.push(payload); }
function _startSensorHealthWatch() { healthWatchStarted = true; }
function setTimeout(callback, delay) { scheduledDelay = delay; callback(); }
""" + functions + r"""

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function run() {
  _showClientScreenRecordingModal(false, 'disabled');
  let overlay = elements.permModalOverlay;
  assert(!!overlay, 'Windows recovery modal was not rendered');
  assert(overlay.innerHTML.includes('Windows 不需要单独授予屏幕录制权限'), 'Windows explanation missing');
  assert(overlay.innerHTML.includes('重新测试截屏'), 'Windows retry action missing');
  assert(!overlay.innerHTML.includes('打开系统设置'), 'Windows modal exposed macOS settings action');
  assert(!overlay.innerHTML.includes('macOS'), 'Windows modal exposed macOS copy');

  let button = { disabled: false, textContent: '', style: {} };
  let error = { style: {}, textContent: '' };
  overlay = { removed: false, remove: function() { this.removed = true; } };
  elements.permModalEnableBtn = button;
  elements.permModalError = error;
  elements.permModalOverlay = overlay;
  await _permModalEnableWindows();
  assert(button.textContent === '截屏测试成功，已开启', 'success was not visible');
  assert(scheduledDelay === 900 && overlay.removed, 'success modal did not close after confirmation');
  assert(persisted.length === 1 && persisted[0].screenshot_enabled === true, 'enabled state was not persisted');
  assert(healthWatchStarted, 'sensor health watch was not started');
  console.log(JSON.stringify({ ok: true }));
}
run().catch(function(error) {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""
    proc = subprocess.run(
        ["node"],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
