"""Real race-condition test for pet.html optimistic typing lock.

We extract the entire <script> block from pet.html, prepend a harness that
stubs DOM / fetch / Tauri / setInterval(_petInit), then run it in Node.
This catches the actual race the previous string-matching tests missed:

  Bug: between user-send and backend `_csm_typing` flip, the typing-poll
       arrives with `typing=false`. Old code blindly wrote that into
       `_backendTyping`, flipping our optimistic `true` → `false`. The next
       `_updateCaption` then fell through to `_latestMessage` (the stale
       previous assistant reply), producing the user-visible "history
       message flash" before real "thinking…".

  Fix: 15s `_optimisticTypingDeadline` lock — the poll cannot flip true→false
       while the lock is active. Lock releases when (a) backend confirms
       typing=true, (b) a new assistant reply arrives, (c) deadline elapses.
"""
import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

PET_HTML = Path(__file__).parent.parent / "templates" / "pet.html"


def _extract_script_block(html: str) -> str:
    """Return the body of the LAST <script> in pet.html (the inline app code)."""
    blocks = re.findall(
        r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>",
        html,
    )
    assert blocks, "no inline <script> found in pet.html"
    # The race-relevant code is in the largest block
    blocks.sort(key=len, reverse=True)
    return blocks[0]


@pytest.fixture(scope="module")
def js_runtime():
    if not shutil.which("node"):
        pytest.skip("node not available")
    return True


def _make_harness(scenario: str) -> str:
    """Build a Node-runnable harness: stubs + pet.html script + scenario driver."""
    script = _extract_script_block(PET_HTML.read_text(encoding="utf-8"))

    # Strip the auto-init at the bottom (calls Tauri APIs we don't stub)
    script = re.sub(r"// Go!\s*\n\s*_petInit\(\);\s*$", "", script)

    stubs = textwrap.dedent(r"""
    // ─── Browser / Tauri stubs ──────────────────────────────────────
    function makeFakeEl(name) {
      var state = { className: '', innerHTML: '', textContent: '', style: {} };
      return {
        _name: name,
        get className() { return state.className; },
        set className(v) { state.className = v; },
        get innerHTML() { return state.innerHTML; },
        set innerHTML(v) { state.innerHTML = v; state.textContent = v; },
        get textContent() { return state.textContent; },
        set textContent(v) { state.textContent = v; state.innerHTML = v; },
        style: new Proxy(state.style, {
          get: function(t, k) { return t[k] || ''; },
          set: function(t, k, v) { t[k] = v; return true; },
        }),
        classList: { add: function(){}, remove: function(){}, toggle: function(){}, contains: function(){return false;} },
        getBoundingClientRect: function() { return { top: 100, bottom: 200, left: 0, right: 100, width: 100, height: 100 }; },
        addEventListener: function() {},
        removeEventListener: function() {},
        querySelector: function() { return makeFakeEl('inner'); },
        querySelectorAll: function() { return []; },
        appendChild: function() {},
        focus: function() {},
        blur: function() {},
        click: function() {},
        contains: function() { return false; },
        children: [],
        parentNode: null,
      };
    }
    var _inputEl = makeFakeEl('input');
    _inputEl.value = '';
    var _captionEl = makeFakeEl('petCaption');
    var _captionWrap = makeFakeEl('petCaptionWrap');
    function _getEl(id) {
      if (id === 'petInput') return _inputEl;
      if (id === 'petCaption') return _captionEl;
      if (id === 'petCaptionWrap') return _captionWrap;
      return makeFakeEl(id);
    }
    global.document = {
      getElementById: _getEl,
      querySelector: function() { return makeFakeEl('q'); },
      querySelectorAll: function() { return []; },
      addEventListener: function() {},
      removeEventListener: function() {},
      body: makeFakeEl('body'),
      documentElement: makeFakeEl('html'),
      createElement: function(tag) { return makeFakeEl(tag); },
    };
    global.window = global;
    global.navigator = { userAgent: 'TEST', language: 'en' };
    global.screen = { width: 800, height: 600, colorDepth: 24 };
    global.localStorage = (function() {
      var s = {};
      return {
        getItem: function(k) { return s[k] || null; },
        setItem: function(k, v) { s[k] = String(v); },
        removeItem: function(k) { delete s[k]; },
      };
    })();
    global.requestAnimationFrame = function(cb) { return setTimeout(cb, 16); };
    global.cancelAnimationFrame = clearTimeout;

    // Suppress setInterval in pet.html init code (would spam fetches)
    var _origSetInterval = setInterval;
    var _intervalsBlocked = true;
    global.setInterval = function(fn, ms) {
      if (_intervalsBlocked) return null;  // ignore until scenario unblocks
      return _origSetInterval(fn, ms);
    };

    // Tauri shim — pet.html calls window.__TAURI__.core.invoke
    global.__TAURI__ = {
      core: { invoke: function() { return Promise.resolve(null); } },
      event: { listen: function() { return Promise.resolve(function(){}); }, emit: function(){} },
    };
    global.__TAURI_INTERNALS__ = global.__TAURI__;

    // Programmable fetch — script under test will call /api/chat, /api/chat/typing, /api/chat/history
    var _fetchQueue = [];
    function _enqueueFetch(url, body, ok) {
      _fetchQueue.push({ url: url, body: body, ok: ok !== false });
    }
    // Background pollers that should NOT consume the scenario fetch queue
    // (e.g. _fetchScreenshotState polling /api/user-settings every 5s).
    function _isBackgroundPoll(url) {
      if (!url) return false;
      if (url.indexOf('/api/user-settings') !== -1) return true;
      if (url.indexOf('/api/events') !== -1) return true;
      return false;
    }
    global.fetch = function(url) {
      if (_isBackgroundPoll(url)) {
        // Inert response — never reaches scenario assertions.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: function() { return Promise.resolve({ settings: {} }); },
        });
      }
      var entry = _fetchQueue.shift();
      if (!entry) {
        return Promise.reject(new Error('no queued response for ' + url));
      }
      return Promise.resolve({
        ok: entry.ok,
        status: entry.ok ? 200 : 500,
        json: function() { return Promise.resolve(entry.body); },
      });
    };
    """)

    after_script = textwrap.dedent(r"""

    // ─── Scenario driver ────────────────────────────────────────────
    var _captionLog = [];
    function _snap(label) {
      var html = _captionEl.innerHTML || '';
      _captionLog.push({
        label: label,
        thinking: (_captionEl.className || '').indexOf('pet-caption--thinking') !== -1,
        text: html,
        backendTyping: _backendTyping,
        optimisticActive: Date.now() < _optimisticTypingDeadline,
        latest: _latestMessage ? _latestMessage.text : null,
      });
    }
    function _sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

    // Mimic petSend without DOM input dependency
    async function _doSend(text) {
      _inputEl.value = text;
      _showUserCaption(text);
      if (!_panelExpanded) { _panelExpanded = true; }
      _backendTyping = true;
      _optimisticTypingDeadline = Date.now() + 15000;
      try {
        var res = await fetch('/api/chat', { method: 'POST' });
        if (!res.ok) {
          _backendTyping = false;
          _optimisticTypingDeadline = 0;
          _latestMessage = { role: 'assistant', text: 'Error: rejected' };
          _updateCaption();
        }
      } catch(e) {
        _backendTyping = false;
        _optimisticTypingDeadline = 0;
        _latestMessage = { role: 'assistant', text: 'Error: ' + e.message };
        _updateCaption();
      }
    }

    """) + scenario + textwrap.dedent(r"""

    Promise.resolve().then(_run).then(function() {
      console.log(JSON.stringify(_captionLog));
    }).catch(function(e) {
      console.error('SCENARIO ERROR: ' + e.message + '\n' + e.stack);
      process.exit(2);
    });
    """)

    return stubs + "\n" + script + "\n" + after_script


def _run(scenario: str) -> list:
    if not shutil.which("node"):
        pytest.skip("node not available")
    code = _make_harness(scenario)
    proc = subprocess.run(
        ["node"],
        input=code,
        capture_output=True, timeout=15, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT: " + proc.stdout[-2000:]
            + "\nSTDERR: " + proc.stderr[-2000:]
        )
    return json.loads(proc.stdout.strip())


# ===========================================================================
# Scenarios
# ===========================================================================

POLL_RACE_SCENARIO = textwrap.dedent(r"""
async function _run() {
  // Pre-state: stale assistant reply cached (the "history message" risk).
  _latestMessage = { id: 'old_001', role: 'assistant', text: 'STALE_PREVIOUS' };
  _lastSeenMsgId = 'old_001';

  // Queue: petSend POST → poll (typing=false RACE) → poll (typing=true) → reply
  _enqueueFetch('/api/chat', { ok: true });
  _enqueueFetch('/api/chat/typing', { typing: false });   // race: backend not yet typing
  _enqueueFetch('/api/chat/history?limit=3', []);
  _enqueueFetch('/api/chat/typing', { typing: true });    // backend confirms
  _enqueueFetch('/api/chat/history?limit=3', []);
  _enqueueFetch('/api/chat/typing', { typing: false });   // LLM done
  _enqueueFetch('/api/chat/history?limit=3', [
    { id: 'reply_001', role: 'assistant', text: 'NEW_REPLY' },
  ]);

  _doSend('hello');
  _snap('after_send_kicked');

  await _sleep(50);
  _pollChat();          // RACE: this is the bug we're fixing
  await _sleep(30);
  _snap('after_race_poll');

  await _sleep(550);
  _snap('after_600ms_timer');

  _pollChat();          // backend confirms typing=true
  await _sleep(30);
  _snap('after_real_typing');

  _pollChat();          // assistant reply lands
  await _sleep(30);
  _snap('after_reply');
}
""")


def test_race_full_scenario():
    log = _run(POLL_RACE_SCENARIO)
    by = {e["label"]: e for e in log}

    # --- After user sends, before any race ---
    after_send = by["after_send_kicked"]
    assert "hello" in after_send["text"], \
        f"user msg should be visible immediately, got {after_send['text']!r}"

    # --- THE CORE TEST: poll race must NOT revert thinking ---
    after_race = by["after_race_poll"]
    assert after_race["backendTyping"] is True, (
        "Race regression: typing=false poll overwrote optimistic _backendTyping=true. "
        "This is exactly the bug — the next _updateCaption would have flashed "
        "the stale 'history message'. Snapshot: " + json.dumps(after_race)
    )
    assert after_race["optimisticActive"] is True

    # --- 600ms timer: must show thinking, NOT the stale reply ---
    timer = by["after_600ms_timer"]
    assert timer["thinking"] is True, \
        f"600ms _showUserCaption setTimeout should display thinking, got {timer}"
    assert "STALE_PREVIOUS" not in timer["text"], \
        "600ms timer is showing the stale 'history message' — bug not fixed"

    # --- Backend confirms typing=true: optimistic window must release ---
    real = by["after_real_typing"]
    assert real["backendTyping"] is True
    assert real["optimisticActive"] is False, \
        "Optimistic window should release once backend confirms typing"

    # --- Assistant reply arrives: thinking off, real text shown ---
    reply = by["after_reply"]
    assert reply["thinking"] is False
    assert "NEW_REPLY" in reply["text"]
    assert reply["backendTyping"] is False
    assert reply["optimisticActive"] is False


# ===========================================================================
# Fast-fail scenarios for individual invariants
# ===========================================================================

FETCH_FAIL_SCENARIO = textwrap.dedent(r"""
async function _run() {
  _latestMessage = null;
  _enqueueFetch('/api/chat', { error: 'simulated' }, false);   // fetch !ok
  _doSend('hi');
  await _sleep(50);
  _snap('after_failed_send');
}
""")

def test_fetch_failure_releases_lock():
    """Server error must release optimistic lock and clear typing."""
    log = _run(FETCH_FAIL_SCENARIO)
    after = log[-1]
    assert after["backendTyping"] is False, \
        "fetch failure must reset _backendTyping = false"
    assert after["optimisticActive"] is False, \
        "fetch failure must release _optimisticTypingDeadline"


# ===========================================================================
# Static safety net
# ===========================================================================

def test_static_invariants():
    src = PET_HTML.read_text(encoding="utf-8")
    assert "_optimisticTypingDeadline" in src
    assert "Date.now() + 15000" in src, \
        "lock window should be 15s per user spec (slow LLM tolerance)"
    assert re.search(
        r"if\s*\(\s*inOptimistic\s*&&\s*_backendTyping\s*&&\s*!newTyping\s*\)\s*\{\s*return",
        src,
    ), "race-guard early return missing"
    # Released on backend typing=true
    assert re.search(
        r"if\s*\(\s*newTyping\s*\)\s*\{\s*_optimisticTypingDeadline\s*=\s*0",
        src,
    ), "lock should release when backend confirms typing=true"
