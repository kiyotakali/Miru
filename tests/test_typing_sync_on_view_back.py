"""Regression tests for the typing-indicator sync on view re-entry (Fix C+D).

Two issues being guarded:

  Bug C: User sends message → typing indicator shows → switches to Memory/
         DDL/etc. → 90s safety timeout fires while away → user comes back →
         indicator GONE but backend still generating. Symptom: "no typing,
         no reply, looks frozen". Fix: showCompanionChat() now calls
         _syncTypingFromBackend() which polls /api/chat/typing once and
         re-shows the indicator if backend is still typing.

  Bug D: 30s safety timeout was too short for the user's slow API. Bumped
         to 90s (matches the LLM client request timeout in prompt._get_client).

Static + Node behavioural checks against the real index.html.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

INDEX = Path(__file__).parent.parent / "templates" / "index.html"


def _node_available() -> bool:
    return shutil.which("node") is not None


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def test_typing_safety_timeout_is_90s():
    src = INDEX.read_text(encoding="utf-8")
    # Either via the named constant or a literal 90000 setTimeout argument
    assert "_TYPING_SAFETY_TIMEOUT_MS = 90000" in src, (
        "Bug D regression: typing safety timeout should be 90000ms (was 30000ms). "
        "Slow APIs need >30s before declaring the indicator stuck."
    )
    # The setTimeout call must use the constant (not a hardcoded 30000)
    m = re.search(
        r"function _showTypingIndicator\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "_showTypingIndicator missing"
    body = m.group(1)
    assert "30000" not in body, (
        "Bug D regression: 30000ms hardcode leaked back into _showTypingIndicator"
    )


def test_sync_typing_helper_exists():
    src = INDEX.read_text(encoding="utf-8")
    assert "function _syncTypingFromBackend(" in src, (
        "Bug C regression: _syncTypingFromBackend helper is missing — the "
        "frontend can no longer self-heal a missed typing_start after view "
        "re-entry."
    )
    # Must hit the right endpoint
    m = re.search(
        r"function _syncTypingFromBackend\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "_syncTypingFromBackend pattern not found"
    body = m.group(1)
    assert "/api/chat/typing" in body, (
        "_syncTypingFromBackend should query /api/chat/typing for the "
        "authoritative backend state."
    )
    # Must call both show and hide depending on the response so the sync is
    # bidirectional (don't only re-show — must also clear stale indicator).
    assert "_showTypingIndicator" in body, (
        "_syncTypingFromBackend must be able to re-show the indicator."
    )
    assert "_hideTypingIndicator" in body, (
        "_syncTypingFromBackend must be able to clear a stale indicator."
    )


def test_show_companion_chat_calls_sync():
    """The whole point of Fix C is that view re-entry triggers the sync.
    If showCompanionChat doesn't call _syncTypingFromBackend, the bug
    persists."""
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function showCompanionChat\([^)]*\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "showCompanionChat function not found"
    body = m.group(1)
    assert "_syncTypingFromBackend(" in body, (
        "Bug C regression: showCompanionChat must call _syncTypingFromBackend "
        "on view re-entry, otherwise a missed typing_start while the user was "
        "on another view leaves the indicator hidden until the next SSE event."
    )


# ---------------------------------------------------------------------------
# Behavioural — Node
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_sync_helper_behaviour():
    """Drive _syncTypingFromBackend with a stubbed fetch returning each
    of the four (typing × shown) state combinations; assert the right
    show/hide is called."""
    src = INDEX.read_text(encoding="utf-8")

    def grab(pattern):
        m = re.search(pattern, src, re.DOTALL)
        assert m, f"pattern not found: {pattern}"
        return m.group(0)

    fn_sync = grab(r"function _syncTypingFromBackend\(\)\s*\{[\s\S]*?\n\}")

    harness = textwrap.dedent(r"""
    var _calls = [];
    var _showTypingIndicator = function() { _calls.push('show'); };
    var _hideTypingIndicator = function() { _calls.push('hide'); };
    var _typingIndicatorShown = false;
    var _backendSays = false;

    // Stub fetch — returns a Promise that resolves to {ok:true, json:()=>{typing:_backendSays}}
    global.fetch = function(url) {
      return Promise.resolve({
        ok: true,
        json: function() { return Promise.resolve({ typing: _backendSays }); },
      });
    };

    """) + fn_sync + textwrap.dedent(r"""

    async function scenario(label, backendTyping, frontendShown) {
      _calls = [];
      _backendSays = backendTyping;
      _typingIndicatorShown = frontendShown;
      _syncTypingFromBackend();
      // Flush all pending promise microtasks. setImmediate sits AFTER the
      // microtask queue, so awaiting a Promise wrapped in setImmediate
      // ensures the fetch().then().then() chain has fully run.
      await new Promise(function(r) { setImmediate(r); });
      await new Promise(function(r) { setImmediate(r); });
      return { label: label, calls: _calls.slice() };
    }

    (async () => {
      var results = [];
      results.push(await scenario('backend_typing+frontend_hidden', true, false));
      results.push(await scenario('backend_idle+frontend_shown', false, true));
      results.push(await scenario('backend_typing+frontend_shown', true, true));
      results.push(await scenario('backend_idle+frontend_hidden', false, false));
      console.log(JSON.stringify(results));
    })();
    """)

    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True, timeout=10, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT: " + proc.stdout + "\nSTDERR: " + proc.stderr
        )
    by = {r["label"]: r["calls"] for r in json.loads(proc.stdout.strip())}

    # Backend typing + frontend hidden → must SHOW (the Bug C scenario)
    assert by["backend_typing+frontend_hidden"] == ["show"], (
        f"Bug C regression: expected ['show'], got {by['backend_typing+frontend_hidden']!r}. "
        f"This is the exact case where user comes back from another view to "
        f"find no typing indicator even though backend is still generating."
    )
    # Backend idle + frontend shown → must HIDE (clear stale indicator)
    assert by["backend_idle+frontend_shown"] == ["hide"], (
        f"expected ['hide'], got {by['backend_idle+frontend_shown']!r}"
    )
    # Both consistent → no-op (don't disturb correct state)
    assert by["backend_typing+frontend_shown"] == [], (
        f"sync must be a no-op when state matches; got {by['backend_typing+frontend_shown']!r}"
    )
    assert by["backend_idle+frontend_hidden"] == [], (
        f"sync must be a no-op when state matches; got {by['backend_idle+frontend_hidden']!r}"
    )


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_safety_timeout_is_90s_in_practice():
    """Pull the real _showTypingIndicator and verify the setTimeout argument
    is 90000ms (not 30000ms or any other regression)."""
    src = INDEX.read_text(encoding="utf-8")

    fn_show = re.search(
        r"function _showTypingIndicator\(\)\s*\{[\s\S]*?\n\}",
        src,
    )
    assert fn_show, "_showTypingIndicator missing"

    harness = textwrap.dedent(r"""
    var _scheduledDelay = null;
    global.setTimeout = function(fn, delay) {
      _scheduledDelay = delay;
      return 1;
    };
    global.clearTimeout = function() {};
    var _typingIndicatorShown = false;
    var _typingIndicatorTimeout = null;
    // _hideTypingIndicator must be declared *before* _showTypingIndicator
    // because _showTypingIndicator references it (as the setTimeout callback).
    var _hideTypingIndicator = function() {};
    var _pushHeaderOverride = function() {};
    global.window = global;
    global.CHARACTER = { name: 'Miru' };
    """) + "\n" + grab_constant(src, '_TYPING_SAFETY_TIMEOUT_MS') + "\n" + fn_show.group(0) + textwrap.dedent(r"""

    _showTypingIndicator();
    console.log(JSON.stringify({ delay: _scheduledDelay }));
    """)

    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True, timeout=10, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT: " + proc.stdout + "\nSTDERR: " + proc.stderr
        )
    out = json.loads(proc.stdout.strip())
    assert out["delay"] == 90000, (
        f"Bug D regression: setTimeout delay is {out['delay']!r}, expected 90000ms"
    )


def grab_constant(src, name):
    """Extract `var NAME = VALUE;` line from src as a string."""
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*[^;]+;", src)
    assert m, f"constant {name} not found in src"
    return m.group(0)
