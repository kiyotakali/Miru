"""Run native X11 bridge checks with a temporary, account-free test page.

An optional --pet-url can exercise a separately prepared Live2D page instead.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pet-url", help="Optional local pet page with separately obtained Live2D assets")
parser.add_argument("--screenshot", type=Path, help="Optional pet-window screenshot on the test display")
args = parser.parse_args()
# Xvfb has no real GPU. Scope SwiftShader to this test process only.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu-compositing --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader"
import linux_pet
import webview
from Xlib import X
from Xlib.ext import shape, xtest

instances = []
original_api = linux_pet.PetApi


class CheckedApi(original_api):
    def __init__(self):
        super().__init__()
        instances.append(self)


linux_pet.PetApi = CheckedApi
original_start = webview.start
outcome = {}


def checked_start(callback, **kwargs):
    def exercise():
        threading.Thread(target=callback, daemon=True).start()
        api = instances[0]
        window = api._window
        try:
            for _ in range(100):
                time.sleep(.3)
                ready = window.evaluate_js("Boolean(window.pywebview && window.pywebview.api.invoke && window._cursorEventsCount >= 5)")
                if ready:
                    break
            else:
                raise AssertionError("native bridge/cursor monitor did not become ready")
            outcome["native_bridge_cursor"] = True
            if args.pet_url:
                assert window.evaluate_js("Boolean(_petModel)"), "Live2D model did not load"
                outcome["live2d_model"] = True
            api._monitoring = False
            api.invoke("resize_airi_window", {"preset": "small"})
            assert window.width == 280, f"width={window.width}"
            api.invoke("move_airi_window", {"x": 50, "y": 60})
            for _ in range(40):
                if api.invoke("get_airi_window_position") == [50, 60]:
                    break
                time.sleep(.05)
            assert api.invoke("get_airi_window_position") == [50, 60], "window move did not settle"
            outcome["resize_move"] = True
            api._pass(True)
            assert len(api._native().shape_get_rectangles(shape.SK.Input).rectangles) == 0
            api._pass(False)
            assert len(api._native().shape_get_rectangles(shape.SK.Input).rectangles) > 0
            outcome["input_passthrough"] = True
            api._register_hotkey("ctrl+alt+m")
            from Xlib import XK
            ctrl = api._display.keysym_to_keycode(XK.string_to_keysym("Control_L"))
            alt = api._display.keysym_to_keycode(XK.string_to_keysym("Alt_L"))
            key = api._hotkey[0]
            for expected in (True, False):
                with api._lock:
                    for code in (ctrl, alt, key):
                        xtest.fake_input(api._display, X.KeyPress, code)
                    for code in (key, alt, ctrl):
                        xtest.fake_input(api._display, X.KeyRelease, code)
                    api._display.sync()
                time.sleep(.5)
                assert api._hidden == expected
            outcome["global_hotkey_hide_show"] = True
            api.invoke("resize_airi_window", {"preset": "medium"})
            api._monitoring = True
            if args.screenshot:
                time.sleep(1)
                from PIL import ImageGrab
                with api._lock:
                    native = api._native()
                    geometry = native.get_geometry()
                    position = api._root.translate_coords(native, 0, 0)
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(bbox=(position.x, position.y,
                                    position.x + geometry.width, position.y + geometry.height)).save(args.screenshot)
            outcome["ok"] = True
        except Exception as error:
            traceback.print_exc()
            outcome.update(ok=False, error=str(error))
        finally:
            (root / "artifacts").mkdir(exist_ok=True)
            (root / "artifacts/linux-qt-smoke.json").write_text(json.dumps(outcome, indent=2))
            print(json.dumps(outcome), flush=True)
            window.destroy()
    original_start(exercise, **kwargs)


webview.start = checked_start
with tempfile.TemporaryDirectory(prefix="miru-qt-smoke-") as data:
    os.environ["XDG_DATA_HOME"] = data
    if args.pet_url:
        os.environ["AIRI_PET_URL"] = args.pet_url
    else:
        page = Path(data) / "bridge.html"
        page.write_text('''<!doctype html><html><body style="background:transparent">
<div style="background:#dfaccb;width:100px;height:100px">Miru bridge test</div>
<script>
window._cursorEventsCount = 0;
window.addEventListener('contextlife://pet-cursor', () => window._cursorEventsCount++);
window.addEventListener('pywebviewready', async () => {
  await window.pywebview.api.invoke('start_airi_pet_monitor', {});
});
</script></body></html>''', encoding="utf-8")
        os.environ["AIRI_PET_URL"] = page.as_uri()
    linux_pet.main()
raise SystemExit(0 if outcome.get("ok") else 1)
