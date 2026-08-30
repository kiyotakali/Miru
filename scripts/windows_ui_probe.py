"""Drive and inspect Miru windows from the logged-in Windows desktop.

This acceptance helper must run in the interactive user session. It writes a
small JSON result so an SSH-launched scheduled task can collect the outcome.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_RESTORE = 0xF120
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_M = 0x4D


def _process_image(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _miru_windows() -> list[dict]:
    user32 = ctypes.windll.user32
    rows: list[dict] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _process_image(int(pid.value))
        basename = os.path.basename(image).lower()
        if basename not in {"miru.exe", "miru-pet.exe"}:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows.append(
            {
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "image": basename,
                "title": title.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "minimized": bool(user32.IsIconic(hwnd)),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
            }
        )
        return True

    callback_ref = callback_type(callback)
    user32.EnumWindows(callback_ref, 0)
    return rows


def _main_window(rows: list[dict]) -> dict:
    candidates = [row for row in rows if row["image"] == "miru.exe"]
    if not candidates:
        raise RuntimeError("Miru main window was not found")
    return next((row for row in candidates if row["visible"]), candidates[0])


def _send_hotkey() -> None:
    user32 = ctypes.windll.user32
    for key in (VK_CONTROL, VK_MENU, VK_M):
        user32.keybd_event(key, 0, 0, 0)
    for key in (VK_M, VK_MENU, VK_CONTROL):
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def _save_screenshot(path: Path) -> None:
    from PIL import ImageGrab

    path.parent.mkdir(parents=True, exist_ok=True)
    image = ImageGrab.grab(all_screens=True)
    image.save(path, format="PNG")


def _click(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    if not user32.SetCursorPos(x, y):
        raise RuntimeError(f"Could not move the pointer to ({x}, {y})")
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def run(
    action: str,
    screenshot_path: Path | None,
    click_x: int | None = None,
    click_y: int | None = None,
    second_x: int | None = None,
    second_y: int | None = None,
) -> dict:
    user32 = ctypes.windll.user32
    before = _miru_windows()
    if action == "minimize":
        user32.SendMessageW(
            _main_window(before)["hwnd"], WM_SYSCOMMAND, SC_MINIMIZE, 0
        )
    elif action == "restore":
        main = _main_window(before)
        user32.SendMessageW(main["hwnd"], WM_SYSCOMMAND, SC_RESTORE, 0)
        user32.SetForegroundWindow(main["hwnd"])
    elif action == "close":
        user32.PostMessageW(_main_window(before)["hwnd"], WM_CLOSE, 0, 0)
    elif action == "hotkey":
        _send_hotkey()
    elif action == "screenshot":
        if screenshot_path is None:
            raise RuntimeError("--screenshot-path is required")
        _save_screenshot(screenshot_path)
    elif action == "click":
        if click_x is None or click_y is None:
            raise RuntimeError("--x and --y are required")
        _click(click_x, click_y)
        if second_x is not None or second_y is not None:
            if second_x is None or second_y is None:
                raise RuntimeError("--second-x and --second-y must be used together")
            time.sleep(0.35)
            _click(second_x, second_y)
        if screenshot_path is not None:
            time.sleep(0.25)
            _save_screenshot(screenshot_path)
    elif action != "status":
        raise RuntimeError(f"Unsupported action: {action}")
    time.sleep(1.0)
    return {
        "ok": True,
        "action": action,
        "session_id": ctypes.windll.kernel32.WTSGetActiveConsoleSessionId(),
        "before": before,
        "after": _miru_windows(),
        "screenshot_path": str(screenshot_path) if screenshot_path else "",
        "click": (
            [[click_x, click_y], [second_x, second_y]]
            if action == "click" and second_x is not None
            else [click_x, click_y] if action == "click" else None
        ),
    }


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("windows_ui_probe.py must run on Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=(
            "status", "minimize", "restore", "close", "hotkey", "screenshot", "click"
        ),
        required=True,
    )
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--screenshot-path", type=Path)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--second-x", type=int)
    parser.add_argument("--second-y", type=int)
    args = parser.parse_args()
    try:
        result = run(
            args.action,
            args.screenshot_path,
            args.x,
            args.y,
            args.second_x,
            args.second_y,
        )
    except Exception as exc:
        result = {"ok": False, "action": args.action, "error": str(exc)}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
