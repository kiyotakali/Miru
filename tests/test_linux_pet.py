"""Linux native boundary regressions; no display or Qt application needed."""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only native pet", allow_module_level=True)
pytest.importorskip("Xlib")
from Xlib import X, XK
import linux_pet


@pytest.mark.parametrize("spec,mask,key", [
    ("ctrl+alt+m", X.ControlMask | X.Mod1Mask, "m"),
    ("cmd+enter", X.Mod4Mask, "Return"),
    ("command+arrowup", X.Mod4Mask, "Up"),
    ("win+pageup", X.Mod4Mask, "Page_Up"),
    ("ctrl+ ", X.ControlMask, "space"),
    ("ctrl+shift++", X.ControlMask | X.ShiftMask, "plus"),
    ("ctrl+f12", X.ControlMask, "F12"),
    ("ctrl+.", X.ControlMask, "period"),
])
def test_recorded_hotkeys_map_to_x11(spec, mask, key):
    assert linux_pet._parse_hotkey(spec) == (mask, XK.string_to_keysym(key))


@pytest.mark.parametrize("spec", ["m", "enter", "ctrl+", "unknown+m", "ctrl+not-a-key"])
def test_invalid_or_bare_hotkeys_do_not_grab_input(spec):
    with pytest.raises(ValueError):
        linux_pet._parse_hotkey(spec)


@pytest.mark.parametrize("scale", [1, 1.25, 1.5, 2])
def test_fractional_scale_cursor_coordinates(scale):
    # Cursor is 100x150 logical pixels inside a window at an arbitrary origin.
    state = (300 + 100 * scale, 200 + 150 * scale, 300, 200, 420 * scale, 760 * scale)
    payload = linux_pet._cursor_payload(state, 420, 760, True)
    assert payload["coordinateSpace"] == "logical"
    assert payload["local"] == {"x": 100, "y": 150}
    assert payload["window"]["width"] == 420
    assert payload["insideWindow"] is True
    assert payload["passThrough"] is True


def test_monitor_recovers_after_transient_bridge_failure():
    api = linux_pet.PetApi.__new__(linux_pet.PetApi)
    api._stopped = threading.Event()
    api._lock = threading.RLock()
    api._monitoring, api._hidden, api._pass_through = True, False, False
    api._hotkey, api._native_window = None, None
    api._register_hotkey = lambda text: None
    api._display = SimpleNamespace(pending_events=lambda: 0)
    api._root = SimpleNamespace(
        translate_coords=lambda *a: SimpleNamespace(x=300, y=200),
        query_pointer=lambda: SimpleNamespace(root_x=425, root_y=387.5))
    api._native = lambda: SimpleNamespace(get_geometry=lambda: SimpleNamespace(width=525, height=950))
    calls = []
    def evaluate(script):
        calls.append(script)
        if len(calls) == 1:
            raise RuntimeError("temporary bridge failure")
        api._stopped.set()
    api._window = SimpleNamespace(width=420, height=760, evaluate_js=evaluate)
    api._run()
    assert len(calls) == 2
    payload = json.loads(calls[-1].split("{detail:", 1)[1][:-3])
    assert payload["local"] == {"x": 100, "y": 150}


def test_parent_watcher_exits_only_after_reparenting(monkeypatch):
    stopped = threading.Event()
    parents = iter([123, 123, 1])
    monkeypatch.setattr(linux_pet.os, "getppid", lambda: next(parents))
    def exit_child(code):
        raise SystemExit(code)
    monkeypatch.setattr(linux_pet.os, "_exit", exit_child)
    monkeypatch.setattr(stopped, "wait", lambda timeout: None)
    with pytest.raises(SystemExit) as exit_info:
        linux_pet._watch_parent(123, stopped)
    assert exit_info.value.code == 0
