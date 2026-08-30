"""Static + behavioural tests for the chat-header refactor (#260 + redesign).

Iteration 2 (this file): the WeChat-style chat-view-header that was added in the
first refactor turned out to be visually noisy. It got deleted along with the
old "Miru ♡ 第 N 天" days-together banner. The new layout is:

  Desktop  — sidebar brand block "<h1>Miru</h1> <span>Your AI Companion</span>"
             gets a status row underneath:
               #brandStatusLeft   typing/transient (pink italic, fades)
               #brandStatusRight  steady ♡ 第 N 天

  Mobile   — single subtitle slot #mobileHeaderSubtitle shows the override
             (typing) when active, otherwise the steady ♡ 第 N 天.

The subtitle is driven by a two-layer API:
  _setHeaderDefault(text)     — steady layer (relationship)
  _pushHeaderOverride(text)   — transient layer (typing)
  _popHeaderOverride()        — clears transient, default re-emerges

Plus invariants kept from the first refactor:
  - the old pink .chat-typing-bar is gone (bug 1)
  - settings mobile-tab button bypasses _mobileTab (bug 2)
  - _mobileTab defensively short-circuits on 'settings' and preserves the
    connection dot
  - days_together is rendered 1-based (开始使用 = 第 1 天)

Tests exercise the real index.html via Node + DOM stubs.
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
# Static invariants
# ---------------------------------------------------------------------------

def test_old_pink_typing_bar_removed():
    src = INDEX.read_text(encoding="utf-8")
    assert "chat-typing-bar" not in src, (
        "Bug 1 regression: the old pink sticky '.chat-typing-bar' element "
        "should be removed; typing indication moves to the brand status row."
    )
    assert 'id="typingBar"' not in src, (
        "Bug 1 regression: typingBar id leftover — likely an incomplete refactor."
    )


def test_chat_view_header_deleted():
    """The white in-page chat-view-header from the first refactor was ugly
    and has been removed; brand status row replaces it."""
    src = INDEX.read_text(encoding="utf-8")
    for forbidden in (
        'id="chatViewHeader"',
        'id="chatViewHeaderName"',
        'id="chatViewHeaderSubtitle"',
        'id="chatViewHeaderAvatar"',
        ".chat-view-header ",
        ".chat-view-header-avatar",
        ".chat-view-header-text",
        ".chat-view-header-name",
        ".chat-view-header-subtitle",
    ):
        assert forbidden not in src, (
            f"chat-view-header redesign regression: leftover {forbidden!r} in index.html"
        )


def test_days_together_banner_deleted():
    """The standalone days-together banner is gone — its content moved to
    brandStatusRight (desktop) and mobileHeaderSubtitle default (mobile)."""
    src = INDEX.read_text(encoding="utf-8")
    for forbidden in (
        'id="daysTogether"',
        'id="daysTogetherText"',
        ".days-together-banner",
    ):
        assert forbidden not in src, (
            f"days-together redesign regression: leftover {forbidden!r}"
        )


def test_brand_status_row_exists():
    src = INDEX.read_text(encoding="utf-8")
    assert 'id="brandStatusLeft"' in src, (
        "brandStatusLeft slot missing — typing indicator has no home on desktop."
    )
    assert 'id="brandStatusRight"' in src, (
        "brandStatusRight slot missing — inline days indicator has no home."
    )
    assert 'id="brandDaysNum"' in src, (
        "brandDaysNum slot missing — the highlighted day number lives there."
    )
    assert 'class="brand-block"' in src, "brand-block wrapper missing"
    assert 'class="brand-status"' in src, "brand-status row missing"
    assert 'class="brand-days"' in src, (
        "brand-days inline span missing — should be inside brand-line."
    )


def test_brand_days_uses_english_labels_no_heart():
    """Format is "The N Day" (English, no ♡). Both label spans must be
    present and the heart span must be gone."""
    src = INDEX.read_text(encoding="utf-8")
    # English labels in the inline indicator
    m = re.search(
        r'<span class="brand-days"[^>]*>([\s\S]*?)</span>\s*</div>',
        src,
    )
    assert m, "brand-days <span> block not found"
    block = m.group(1)
    assert ">The<" in block, (
        "brand-days prefix must be the literal word 'The' — user explicitly "
        "wanted English labels, not 第."
    )
    assert ">Day<" in block, (
        "brand-days suffix must be the literal word 'Day' — user explicitly "
        "wanted English labels, not 天."
    )
    # Heart character must NOT appear inside the indicator
    assert "brand-days-heart" not in src, (
        "the .brand-days-heart span was deleted; rebuild left a leftover."
    )
    assert "♡" not in block and "♥" not in block and "♡" not in block, (
        "no heart glyph allowed inside brand-days — user asked for it removed."
    )


def test_mobile_subtitle_has_structured_slots():
    """Mobile must mirror desktop's structured spans (prefix/num/suffix)
    so the digit can pick up the serif/gradient styling. A flat
    textContent string can't be partially restyled."""
    src = INDEX.read_text(encoding="utf-8")
    for slot in (
        'id="mobileHeaderTyping"',
        'id="mobileHeaderDays"',
        'id="mobileHeaderDaysNum"',
    ):
        assert slot in src, (
            f"mobile structured slot missing: {slot}. Without separate "
            f"spans the day number can't be styled differently from the "
            f"surrounding 'The'/'Day' labels."
        )
    # The literal English labels must appear in the mobile structure —
    # check the dedicated prefix/suffix span markup directly.
    assert '<span class="m-days-prefix">The</span>' in src, (
        "mobile .m-days-prefix must contain literal 'The'"
    )
    assert '<span class="m-days-suffix">Day</span>' in src, (
        "mobile .m-days-suffix must contain literal 'Day'"
    )


def test_mobile_days_num_has_styled_typography():
    """The mobile day-number must NOT inherit the muted 11px sans-serif
    of the surrounding subtitle — it should pick up serif + gradient
    styling parallel to desktop."""
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(r"\.m-days-num\s*\{([^}]*)\}", src)
    assert m, ".m-days-num CSS rule missing — the mobile number won't be styled"
    rule = m.group(1)
    assert "serif" in rule.lower(), (
        ".m-days-num must use a serif font-family (matching desktop), "
        "otherwise the number is visually identical to the surrounding text."
    )
    assert "linear-gradient" in rule, (
        ".m-days-num must apply the pink→violet gradient via "
        "background-clip:text, otherwise the number stays flat gray."
    )
    assert "background-clip" in rule or "-webkit-background-clip" in rule, (
        ".m-days-num gradient won't fill the glyphs without background-clip:text"
    )


def test_mobile_header_subtitle_exists():
    src = INDEX.read_text(encoding="utf-8")
    assert 'id="mobileHeaderSubtitle"' in src, (
        "mobile-header subtitle slot missing — typing/days have no home on mobile."
    )


def test_two_layer_subtitle_api_exists():
    src = INDEX.read_text(encoding="utf-8")
    for fn in (
        "function _setHeaderDays(",
        "function _pushHeaderOverride(",
        "function _popHeaderOverride(",
        "function _applyHeaderSubtitle(",
    ):
        assert fn in src, f"two-layer subtitle helper missing: {fn}"


def test_typing_indicator_uses_override_layer():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function _showTypingIndicator\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "_showTypingIndicator missing"
    body = m.group(1)
    assert "_pushHeaderOverride(" in body, (
        "Bug 1 regression: _showTypingIndicator must push onto the override "
        "layer so the steady days-together default re-emerges when typing stops."
    )
    assert "typingBar" not in body, "_showTypingIndicator still references typingBar"

    m2 = re.search(
        r"function _hideTypingIndicator\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m2, "_hideTypingIndicator missing"
    body2 = m2.group(1)
    assert "_popHeaderOverride(" in body2, (
        "_hideTypingIndicator must pop the override so the days-together "
        "default snaps back."
    )


def test_show_typing_indicator_has_safety_timeout():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function _showTypingIndicator\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    body = m.group(1)
    assert "setTimeout(_hideTypingIndicator" in body, (
        "_showTypingIndicator must arm a safety timeout so the override "
        "auto-clears even if SSE typing_stop never arrives."
    )


def test_apply_relationship_uses_one_based_counting():
    """Day 1 (开始使用) must show '第 1 天', not '第 0 天'."""
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function _applyRelationshipData\(data\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "_applyRelationshipData missing"
    body = m.group(1)
    # +1 conversion must be present
    assert "rawDays + 1" in body or "+ 1" in body, (
        "_applyRelationshipData should render days_together + 1 (1-based)."
    )
    # day-number sink: _setHeaderDays
    assert "_setHeaderDays(" in body, (
        "_applyRelationshipData should pipe the rendered number into "
        "_setHeaderDays so both desktop and mobile slots stay in sync."
    )
    # must guard on first_meet_date
    assert "first_meet_date" in body, (
        "_applyRelationshipData should bail out when first_meet_date is "
        "absent — otherwise we'd show '第 1 天' before relationship init."
    )


def test_settings_button_bypasses_mobile_tab():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r'<button data-tab="settings"\s+onclick="([^"]+)"',
        src,
    )
    assert m, "settings tab button not found"
    handler = m.group(1)
    assert handler != "_mobileTab('settings')", (
        "Bug 2 regression: settings tab button calling _mobileTab again."
    )
    assert "openSettingsModal" in handler.lower() or "_openMobileSettings" in handler, (
        f"settings button onclick is {handler!r}; expected a settings-modal opener"
    )


def test_mobile_tab_redirects_settings_safely():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function _mobileTab\(tab\)\s*\{(.*?)\n\}\s*\n",
        src, re.DOTALL,
    )
    assert m, "_mobileTab function not found"
    body = m.group(1)
    assert "tab === 'settings'" in body, (
        "_mobileTab should defensively short-circuit on 'settings'."
    )
    titles_match = re.search(r"var\s+titles\s*=\s*\{([^}]*)\}", body)
    assert titles_match, "titles map not found"
    titles_block = titles_match.group(1)
    assert "settings" not in titles_block, (
        "Bug 2 regression: 'settings' is back in the titles map of _mobileTab."
    )


def test_mobile_header_preserves_connection_dot():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r"function _mobileTab\(tab\)\s*\{(.*?)\n\}\s*\n",
        src, re.DOTALL,
    )
    body = m.group(1)
    assert "connectionDot" in body, (
        "_mobileTab regression: must keep the connection dot child of "
        "mobileHeaderName when retitling."
    )


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_subtitle_two_layer_round_trip():
    """Drive the real two-layer subtitle API in Node, mirroring the
    structured DOM both desktop and mobile use:

      desktop  : brandStatusLeft (typing)  +  brandStatusRight container
                                              wrapping brandDaysNum
      mobile   : mobileHeaderSubtitle wrapper
                   ├─ mobileHeaderTyping (typing)
                   └─ mobileHeaderDays container wrapping mobileHeaderDaysNum

    Each container has style.display toggled; each text slot has textContent
    set. We capture both axes per snapshot.
    """
    src = INDEX.read_text(encoding="utf-8")

    def grab(pattern):
        m = re.search(pattern, src, re.DOTALL)
        assert m, f"pattern not found: {pattern}"
        return m.group(0)

    fn_apply = grab(r"function _applyHeaderSubtitle\(\)\s*\{[\s\S]*?\n\}")
    fn_setDays = grab(r"function _setHeaderDays\(days\)\s*\{[\s\S]*?\n\}")
    fn_push = grab(r"function _pushHeaderOverride\(text\)\s*\{[\s\S]*?\n\}")
    fn_pop = grab(r"function _popHeaderOverride\(\)\s*\{[\s\S]*?\n\}")
    fn_show = grab(r"function _showTypingIndicator\(\)\s*\{[\s\S]*?\n\}")
    fn_hide = grab(r"function _hideTypingIndicator\(\)\s*\{[\s\S]*?\n\}")

    harness = textwrap.dedent(r"""
    // Initial display values mirror the inline style attributes in
    // index.html: brandStatusRight, mobileHeaderDays, mobileHeaderSubtitle
    // all start hidden until _applyHeaderSubtitle decides otherwise.
    var _c = {
      left: '', num: '', rightDisp: 'none',
      mTyping: '', mNum: '', mDaysDisp: 'none', mWrapDisp: 'none',
    };
    function makeText(field) {
      return {
        get textContent() { return _c[field]; },
        set textContent(v) { _c[field] = v; },
      };
    }
    function makeContainer(field) {
      return {
        style: {
          set display(v) { _c[field] = v; },
          get display() { return _c[field]; },
        },
      };
    }
    var _elements = {
      brandStatusLeft: makeText('left'),
      brandDaysNum: makeText('num'),
      brandStatusRight: makeContainer('rightDisp'),
      mobileHeaderSubtitle: makeContainer('mWrapDisp'),
      mobileHeaderTyping: makeText('mTyping'),
      mobileHeaderDays: makeContainer('mDaysDisp'),
      mobileHeaderDaysNum: makeText('mNum'),
    };
    global.document = {
      getElementById: function(id) { return _elements[id] || null; },
    };
    global.window = global;
    global.CHARACTER = { name: 'TestChar' };
    var _typingIndicatorShown = false;
    var _typingIndicatorTimeout = null;
    var _TYPING_SAFETY_TIMEOUT_MS = 90000;
    var _headerDays = null;
    var _headerOverride = null;
    """) + "\n" + fn_apply + "\n" + fn_setDays + "\n" + fn_push + "\n" + fn_pop + "\n" + fn_show + "\n" + fn_hide + textwrap.dedent(r"""

    var snapshots = [];
    function snap(label) { snapshots.push(Object.assign({label: label}, _c)); }

    snap('initial');
    _setHeaderDays(42);
    snap('after_days');
    _showTypingIndicator();
    snap('after_show');
    _showTypingIndicator();
    snap('after_show_idempotent');
    _hideTypingIndicator();
    snap('after_hide');
    _setHeaderDays(null);
    snap('after_clear_days');
    console.log(JSON.stringify(snapshots));
    """)

    proc = subprocess.run(
        ["node", "-e", harness],
        capture_output=True, timeout=10, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "node failed:\nSTDOUT: " + proc.stdout + "\nSTDERR: " + proc.stderr
        )
    snaps = json.loads(proc.stdout.strip())
    by = {s["label"]: s for s in snaps}

    # Initial — everything hidden / empty
    s = by["initial"]
    assert s["left"] == "" and s["num"] == "" and s["rightDisp"] == "none"
    assert s["mTyping"] == "" and s["mNum"] == ""
    assert s["mDaysDisp"] == "none" and s["mWrapDisp"] == "none"

    # Days = 42 — desktop number visible, mobile days span visible with same
    # number, mobile wrapper visible, typing slots empty
    s = by["after_days"]
    assert s["num"] == "42" and s["rightDisp"] == ""
    assert s["mNum"] == "42", (
        f"mobile day-number slot must mirror desktop's '42', got {s['mNum']!r}"
    )
    assert s["mDaysDisp"] == "" and s["mWrapDisp"] == ""
    assert s["left"] == "" and s["mTyping"] == ""

    # Show typing — desktop typing fills left, mobile typing fills its slot,
    # mobile days indicator hides (single mobile line can't show both)
    s = by["after_show"]
    assert "TestChar" in s["left"]
    assert "TestChar" in s["mTyping"]
    assert s["mDaysDisp"] == "none", (
        "mobile days span must hide while typing — they share one line."
    )
    # Days number itself unchanged (still 42 in DOM, just hidden)
    assert s["num"] == "42" and s["mNum"] == "42"
    # Desktop days container stays visible — separate row from typing on desktop
    assert s["rightDisp"] == ""
    # Mobile wrapper still visible (it's hosting the typing text)
    assert s["mWrapDisp"] == ""

    # Idempotent
    for k in ("left", "num", "rightDisp", "mTyping", "mNum",
             "mDaysDisp", "mWrapDisp"):
        assert by["after_show_idempotent"][k] == by["after_show"][k]

    # Hide — typing clears, mobile days re-emerges
    s = by["after_hide"]
    assert s["left"] == "" and s["mTyping"] == ""
    assert s["mDaysDisp"] == "" and s["mNum"] == "42"
    assert s["num"] == "42" and s["rightDisp"] == ""

    # Clear days — both desktop and mobile day containers hide; mobile
    # wrapper hides since neither layer has content
    s = by["after_clear_days"]
    assert s["num"] == "" and s["rightDisp"] == "none"
    assert s["mNum"] == "" and s["mDaysDisp"] == "none"
    assert s["mWrapDisp"] == "none", (
        "mobile wrapper must hide when both typing and days are empty, "
        "otherwise an empty subtitle line takes vertical space and the "
        "name no longer sits centered."
    )


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_apply_relationship_one_based_in_node():
    """Drive _applyRelationshipData and assert day-1 counting end-to-end
    using the real structured DOM stubs."""
    src = INDEX.read_text(encoding="utf-8")

    def grab(pattern):
        m = re.search(pattern, src, re.DOTALL)
        assert m, f"pattern not found: {pattern}"
        return m.group(0)

    fn_apply_sub = grab(r"function _applyHeaderSubtitle\(\)\s*\{[\s\S]*?\n\}")
    fn_setDays = grab(r"function _setHeaderDays\(days\)\s*\{[\s\S]*?\n\}")
    fn_relationship = grab(
        r"function _applyRelationshipData\(data\)\s*\{[\s\S]*?\n\}"
    )

    harness = textwrap.dedent(r"""
    var _c = {
      left: '', num: '', rightDisp: 'none',
      mTyping: '', mNum: '', mDaysDisp: 'none', mWrapDisp: 'none',
    };
    function makeText(f) { return {
      get textContent() { return _c[f]; }, set textContent(v) { _c[f] = v; }
    }; }
    function makeContainer(f) { return { style: {
      get display() { return _c[f]; }, set display(v) { _c[f] = v; }
    }}; }
    var _elements = {
      brandStatusLeft: makeText('left'),
      brandDaysNum: makeText('num'),
      brandStatusRight: makeContainer('rightDisp'),
      mobileHeaderSubtitle: makeContainer('mWrapDisp'),
      mobileHeaderTyping: makeText('mTyping'),
      mobileHeaderDays: makeContainer('mDaysDisp'),
      mobileHeaderDaysNum: makeText('mNum'),
    };
    global.document = {
      getElementById: function(id) { return _elements[id] || null; },
    };
    global.window = global;
    global.CHARACTER = { name: 'TestChar' };
    var _headerDays = null;
    var _headerOverride = null;
    """) + "\n" + fn_apply_sub + "\n" + fn_setDays + "\n" + fn_relationship + textwrap.dedent(r"""

    var results = {};

    function snap(label) {
      results[label] = Object.assign({}, _c);
    }

    // Brand-new user — first_meet_date set today, days_together=0 → '第 1 天'
    _applyRelationshipData({ first_meet_date: '2026-04-28', days_together: 0 });
    snap('day1');

    // Returning user — days_together=10 → '第 11 天'
    _applyRelationshipData({ first_meet_date: '2026-04-18', days_together: 10 });
    snap('day11');

    // Missing first_meet_date — must NOT show '第 1 天' (brand-new account
    // before relationship init)
    _applyRelationshipData({ days_together: 0 });
    snap('unknown');

    // Empty payload — same: don't fabricate
    _applyRelationshipData({});
    snap('empty');

    console.log(JSON.stringify(results));
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

    assert out["day1"]["num"] == "1", (
        f"day-1 desktop number should be '1', got {out['day1']['num']!r}"
    )
    assert out["day1"]["mNum"] == "1", (
        f"day-1 mobile number should be '1', got {out['day1']['mNum']!r}"
    )
    assert out["day1"]["mDaysDisp"] == "" and out["day1"]["mWrapDisp"] == ""

    assert out["day11"]["num"] == "11" and out["day11"]["mNum"] == "11", (
        f"day-11 user (raw 10) should see '11' on both desktop and mobile, "
        f"got desktop={out['day11']['num']!r} mobile={out['day11']['mNum']!r}"
    )

    # Missing first_meet_date — both day containers hidden, both num slots
    # empty, mobile wrapper hidden so no empty subtitle line shows
    assert out["unknown"]["num"] == "" and out["unknown"]["mNum"] == ""
    assert out["unknown"]["rightDisp"] == "none"
    assert out["unknown"]["mDaysDisp"] == "none"
    assert out["unknown"]["mWrapDisp"] == "none", (
        "missing first_meet_date should hide the mobile subtitle wrapper "
        "entirely — leaving it visible would push the centered name off-axis."
    )

    assert out["empty"]["num"] == ""
    assert out["empty"]["rightDisp"] == "none"
    assert out["empty"]["mWrapDisp"] == "none"


@pytest.mark.skipif(not _node_available(), reason="node not available")
def test_close_settings_no_longer_strands_header():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(
        r'<button data-tab="settings"\s+onclick="([^"]+)"',
        src,
    )
    handler = m.group(1)
    assert handler != "_mobileTab('settings')", (
        "settings click handler regression: bug 2 root cause"
    )
