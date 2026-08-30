"""Static invariants for templates/pet.html — the desktop pet's WKWebView UI.

This file is independent from index.html (the main webview). Pet uses its own
`petSend` function and polls /api/chat/typing instead of subscribing to SSE
typing_start. The bug we're guarding against:

  Without an optimistic _backendTyping=true, _showUserCaption's 600ms timer
  fires _updateCaption(), which sees _backendTyping=false and falls back to
  _latestMessage (the PREVIOUS assistant reply). Result: send → user msg →
  flash of stale reply → finally "thinking..." once the 1.5–5s typing poll
  catches up. User-perceived gap = up to 5 seconds.

If a future refactor accidentally removes the optimistic flag or drops the
error-path resets, these tests fail loudly and explain why.
"""
import os
import re

PET = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "templates", "pet.html",
)


def _read():
    with open(PET, "r", encoding="utf-8") as f:
        return f.read()


def _petsend_body():
    src = _read()
    m = re.search(
        r"async function petSend\(\)\s*\{(.*?)\n\}\n",
        src, re.DOTALL,
    )
    assert m, "petSend function not found in pet.html"
    return m.group(1)


def test_petsend_sets_optimistic_thinking_before_fetch():
    """_backendTyping must flip to true BEFORE the fetch() call so that
    _showUserCaption's 600ms timer transitions to 'thinking…' instead of
    flashing the stale _latestMessage."""
    body = _petsend_body()
    optimistic_pos = body.find("_backendTyping = true")
    fetch_pos = body.find("fetch('/api/chat'")
    assert optimistic_pos != -1, \
        "Pet thinking-gap regression: missing optimistic _backendTyping=true. " \
        "Without it the caption flashes the previous assistant reply for up " \
        "to 5 seconds before polling catches up."
    assert fetch_pos != -1, "fetch('/api/chat') missing"
    assert optimistic_pos < fetch_pos, \
        "Pet thinking-gap regression: _backendTyping=true must run BEFORE fetch, " \
        "otherwise the 600ms _showUserCaption timer races the network request."


def test_petsend_resets_thinking_on_http_error():
    """If the request returns !ok, _backendTyping must be reset so the
    error caption replaces the thinking dots (not stack on top)."""
    body = _petsend_body()
    # Locate the !res.ok block
    m = re.search(r"if \(!res\.ok\)\s*\{(.*?)\}", body, re.DOTALL)
    assert m, "!res.ok handler not found"
    block = m.group(1)
    assert "_backendTyping = false" in block, \
        "Pet error-path regression: !res.ok branch must reset _backendTyping " \
        "to false, otherwise thinking dots persist forever after a server error."


def test_petsend_resets_thinking_on_network_error():
    """catch(e) block must also reset _backendTyping. Counts the resets in
    the function — there must be at least 2 (one for !res.ok, one for catch)."""
    body = _petsend_body()
    reset_count = body.count("_backendTyping = false")
    assert reset_count >= 2, \
        f"Pet network-error regression: expected at least 2 '_backendTyping = " \
        f"false' resets in petSend (one per error path: !res.ok + catch), " \
        f"found {reset_count}. Without the catch-block reset a network failure " \
        f"leaves the dots animating forever."


def test_petsend_attaches_x_device_id_header():
    """Same Phase 2 invariant as index.html: every send must tag itself with
    X-Device-Id so multi-device de-dup can identify echo broadcasts."""
    body = _petsend_body()
    assert "'X-Device-Id'" in body, \
        "Pet Phase 2 regression: petSend must include X-Device-Id header so " \
        "the SSE chat_message echo can be filtered out by other devices."
    assert "_petDeviceId()" in body, \
        "Pet Phase 2 regression: device id should come from the stable " \
        "_petDeviceId() helper, not be hardcoded or omitted."


def test_pet_device_id_helper_is_stable():
    """_petDeviceId() must hash and cache; calling twice returns the same id."""
    src = _read()
    m = re.search(r"function _petDeviceId\(\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "_petDeviceId helper not found"
    body = m.group(1)
    # Cache via localStorage
    assert "localStorage.getItem" in body and "localStorage.setItem" in body, \
        "_petDeviceId must persist its id in localStorage for cross-launch stability"
    assert "miru_pet_device_id" in body, \
        "_petDeviceId should use a pet-specific localStorage key (not collide " \
        "with the main webview's miru_device_id)."


def test_update_caption_branches_on_backend_typing():
    """Sanity: _updateCaption must still prefer _backendTyping over _latestMessage.
    Otherwise our optimistic flip wouldn't actually show 'thinking...'."""
    src = _read()
    m = re.search(
        r"function _updateCaption\(\)\s*\{(.*?)\n\}",
        src, re.DOTALL,
    )
    assert m, "_updateCaption function not found"
    body = m.group(1)
    backend_pos = body.find("_backendTyping")
    latest_pos = body.find("_latestMessage")
    assert backend_pos != -1 and latest_pos != -1, \
        "_updateCaption should reference both _backendTyping and _latestMessage"
    assert backend_pos < latest_pos, \
        "_updateCaption regression: must check _backendTyping FIRST (thinking " \
        "takes priority over showing the stale _latestMessage)."


if __name__ == "__main__":
    test_petsend_sets_optimistic_thinking_before_fetch()
    test_petsend_resets_thinking_on_http_error()
    test_petsend_resets_thinking_on_network_error()
    test_petsend_attaches_x_device_id_header()
    test_pet_device_id_helper_is_stable()
    test_update_caption_branches_on_backend_typing()
    print("ALL PASS")
