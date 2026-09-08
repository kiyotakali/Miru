"""X11 desktop pet using Qt/Chromium and the existing Miru pet page."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from Xlib import X, XK, display, protocol
from Xlib.ext import shape

from desktop_paths import app_support_dir


def _parse_hotkey(text):
    spec = str(text).lower()
    if spec.endswith("++"):
        spec = spec[:-1] + "plus"
    parts = spec.split("+")
    modifiers = {"ctrl": X.ControlMask, "control": X.ControlMask,
                 "alt": X.Mod1Mask, "option": X.Mod1Mask,
                 "shift": X.ShiftMask, "super": X.Mod4Mask, "meta": X.Mod4Mask,
                 "cmd": X.Mod4Mask, "command": X.Mod4Mask, "win": X.Mod4Mask}
    mask = 0
    for part in parts[:-1]:
        part = part.strip()
        if part not in modifiers:
            raise ValueError("Unsupported hotkey modifier: " + part)
        mask |= modifiers[part]
    if not mask:
        raise ValueError("A hotkey needs at least one modifier")
    names = {"enter": "Return", "return": "Return", "escape": "Escape", "esc": "Escape",
             "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
             "tab": "Tab", "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
             "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
             " ": "space", "space": "space"}
    key = parts[-1] if parts[-1] == " " else parts[-1].strip()
    key = names.get(key, key.upper() if key.startswith("f") and key[1:].isdigit() else key)
    keysym = ord(key) if len(key) == 1 else XK.string_to_keysym(key)
    if not keysym:
        raise ValueError("Unsupported hotkey key")
    return mask, keysym


def _cursor_payload(state, logical_width, logical_height, pass_through):
    cx, cy, x, y, width, height = state
    scale_x, scale_y = width / logical_width, height / logical_height
    return {"cursor": {"x": cx / scale_x, "y": cy / scale_y},
            "window": {"x": x / scale_x, "y": y / scale_y,
                       "width": logical_width, "height": logical_height},
            "local": {"x": (cx - x) / scale_x, "y": (cy - y) / scale_y},
            "insideWindow": x <= cx < x + width and y <= cy < y + height,
            "coordinateSpace": "logical", "passThrough": pass_through}


def _watch_parent(parent_pid, stopped):
    # Popen is called from a short-lived thread, so PR_SET_PDEATHSIG would
    # fire when that thread exits. Observe the owning process instead.
    while not stopped.is_set():
        if os.getppid() != parent_pid:
            os._exit(0)
        stopped.wait(.5)


class PetApi:
    def __init__(self):
        self._window = None
        self._display = display.Display()
        self._root = self._display.screen().root
        self._lock = threading.RLock()
        self._native_window = None
        self._stopped = threading.Event()
        self._monitoring = False
        self._hidden = False
        self._pass_through = False
        self._drag = None
        self._hotkey = None

    def _native(self):
        if self._native_window is not None:
            return self._native_window
        atom = self._display.intern_atom("_NET_CLIENT_LIST")
        clients = self._root.get_full_property(atom, X.AnyPropertyType)
        ids = clients.value if clients is not None else [w.id for w in self._root.query_tree().children]
        for wid in ids:
            window = self._display.create_resource_object("window", int(wid))
            pid = window.get_full_property(self._display.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            if pid is not None and int(pid.value[0]) == os.getpid() and window.get_wm_name() == "ContextLife AIRI Pet":
                self._native_window = window
                self._root.send_event(protocol.event.ClientMessage(
                    window=window, client_type=self._display.intern_atom("_NET_WM_STATE"),
                    data=(32, [1, self._display.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
                               self._display.intern_atom("_NET_WM_STATE_SKIP_PAGER"), 1, 0])),
                    event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                self._display.flush()
                return window
        return None

    def _pass(self, enabled):
        with self._lock:
            native = self._native()
            if native is None:
                return
            geometry = native.get_geometry()
            native.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0,
                                    [] if enabled else [(0, 0, geometry.width, geometry.height)])
            self._display.sync()
            self._pass_through = bool(enabled)

    def _register_hotkey(self, text):
        mask, keysym = _parse_hotkey(text)
        with self._lock:
            code = self._display.keysym_to_keycode(keysym)
            if not code:
                raise ValueError("Hotkey is not available in the current keyboard layout")
            if self._hotkey == (code, mask):
                return text
            errors = []
            for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                self._root.grab_key(code, mask | extra, False, X.GrabModeAsync, X.GrabModeAsync,
                                   onerror=lambda error, request: errors.append(error))
            self._display.sync()
            if errors:
                for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                    self._root.ungrab_key(code, mask | extra)
                self._display.sync()
                raise RuntimeError("Hotkey is already used by another application")
            if self._hotkey:
                old_code, old_mask = self._hotkey
                for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                    self._root.ungrab_key(old_code, old_mask | extra)
            self._hotkey = (code, mask)
        return text

    def _snap(self, anchor="bottom-right"):
        import webview
        screens = webview.screens
        screen = next((s for s in screens if s.x <= self._window.x < s.x + s.width
                       and s.y <= self._window.y < s.y + s.height), screens[0])
        width, height = self._window.width, self._window.height
        x = screen.x + screen.width - width - 26
        if anchor == "bottom-left":
            x = screen.x + 26
        elif anchor == "bottom-center":
            x = screen.x + (screen.width - width) / 2
        self._window.move(max(screen.x, int(x)), max(screen.y, int(screen.y + screen.height - height - 26)))

    def invoke(self, command, args=None):
        args = args or {}
        if command == "start_airi_pet_monitor":
            self._monitoring = True
        elif command == "stop_airi_pet_monitor":
            self._monitoring = False
            self._pass(False)
        elif command == "set_airi_window_pass_through":
            self._pass(bool(args.get("enabled")))
        elif command == "get_airi_window_position":
            return [self._window.x, self._window.y]
        elif command == "move_airi_window":
            self._window.move(int(args["x"]), int(args["y"]))
        elif command == "start_airi_window_drag":
            self._pass(False)
            x, y = self._window.x, self._window.y
            with self._lock:
                pointer = self._root.query_pointer()
                native = self._native()
                scale = native.get_geometry().width / self._window.width if native else 1
                self._drag = (pointer.root_x, pointer.root_y, x, y, scale)
        elif command == "update_airi_window_drag":
            with self._lock:
                pointer = self._root.query_pointer()
                drag = self._drag
            if drag:
                cx, cy, x, y, scale = drag
                self._window.move(int(x + (pointer.root_x - cx) / scale), int(y + (pointer.root_y - cy) / scale))
        elif command == "end_airi_window_drag":
            self._drag = None
        elif command == "resize_airi_window":
            width = args.get("width", {"small": 280, "large": 560}.get(args.get("preset"), 420))
            width = max(160, min(900, int(width)))
            height = max(290, min(1630, int(args.get("height", width * 760 / 420))))
            self._window.resize(width, height)
            deadline = time.monotonic() + 2
            while (self._window.width, self._window.height) != (width, height):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Qt window resize did not finish")
                time.sleep(.01)
            self._pass(False)
            self._snap(args.get("anchor") or "bottom-right")
            return [width, height]
        elif command == "scale_airi_window":
            width = max(160, min(900, round(self._window.width * float(args["factor"]))))
            return self.invoke("resize_airi_window", {"width": width})
        elif command == "snap_airi_window":
            self._snap(args.get("anchor") or "bottom-right")
        elif command == "hide_airi_window":
            self._pass(False)
            self._hidden = True
            self._window.hide()
        elif command == "close_airi_window":
            self._window.destroy()
        elif command == "elevate_airi_window":
            return None
        elif command == "update_global_hotkey":
            return self._register_hotkey(args["hotkey"])
        elif command == "open_external_url":
            url = str(args.get("url", ""))
            if urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError("Unsupported URL scheme")
            webbrowser.open(url)
        else:
            raise ValueError("Unsupported pet command: " + command)

    def _run(self):
        try:
            self._register_hotkey(os.environ.get("AIRI_PET_HOTKEY", "ctrl+alt+m"))
        except Exception as error:
            print(f"[LinuxPet] {error}", flush=True)
        previous = None
        samples = 0
        last_emit = 0.0
        while not self._stopped.wait(.048):
            try:
                toggle = False
                with self._lock:
                    while self._display.pending_events():
                        event = self._display.next_event()
                        if event.type == X.KeyPress and self._hotkey and event.detail == self._hotkey[0]:
                            toggle = True
                if toggle:
                    self._pass(False)
                    self._hidden = not self._hidden
                    self._window.hide() if self._hidden else self._window.show()
                if not self._monitoring or self._hidden:
                    continue
                with self._lock:
                    native = self._native()
                    if native is None:
                        continue
                    geometry = native.get_geometry()
                    position = self._root.translate_coords(native, 0, 0)
                    cursor = self._root.query_pointer()
                state = (cursor.root_x, cursor.root_y, position.x, position.y, geometry.width, geometry.height)
                now = time.monotonic()
                if state == previous and samples >= 5 and now - last_emit < .25:
                    continue
                previous = state
                samples += 1
                last_emit = now
                payload = _cursor_payload(state, self._window.width, self._window.height, self._pass_through)
                self._window.evaluate_js("window.dispatchEvent(new CustomEvent('contextlife://pet-cursor', {detail:"
                                        + json.dumps(payload) + "}))")
            except Exception as error:
                if self._stopped.is_set():
                    break
                print(f"[LinuxPet] monitor retry: {error}", flush=True)
                with self._lock:
                    self._native_window = None
                previous = None
                self._stopped.wait(.5)


def main():
    expected_parent = os.environ.get("AIRI_PET_PARENT_PID")
    if expected_parent:
        expected_parent = int(expected_parent)
        if expected_parent != os.getppid():
            return 0
    os.environ["QT_API"] = "pyside6"
    # Avoid NVIDIA DMA-BUF texture import failures; WebGL remains enabled.
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-gpu-compositing" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --disable-gpu-compositing").strip()
    import webview

    support = app_support_dir()
    support.mkdir(parents=True, exist_ok=True)
    singleton = open(support / "qt-pet.lock", "a+")
    try:
        fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        singleton.close()
        return 0
    api = PetApi()
    if expected_parent:
        threading.Thread(target=_watch_parent, args=(expected_parent, api._stopped), daemon=True).start()
    api._window = webview.create_window(
        "ContextLife AIRI Pet", os.environ.get("AIRI_PET_URL", "http://127.0.0.1:5001/pet?desktop_platform=linux"),
        js_api=api, width=420, height=760, min_size=(160, 290),
        frameless=True, transparent=True, on_top=True, easy_drag=False)
    api._window.events.closed += api._stopped.set
    storage = support / "pet-webview"
    storage.mkdir(exist_ok=True)
    try:
        webview.start(api._run, gui="qt", private_mode=False, storage_path=str(storage))
    finally:
        api._stopped.set()
        with api._lock:
            api._display.close()
        singleton.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
