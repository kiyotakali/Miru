"""Windows-specific helpers for Miru's desktop launcher and sensor."""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorBounds:
    index: int
    left: int
    top: int
    right: int
    bottom: int
    device_name: str = ""

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def set_process_dpi_awareness() -> bool:
    """Enable physical-pixel coordinates before any Windows UI is created."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE. Windows 8.1+.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, -2147024891):
            return True
    except Exception:
        pass
    try:
        import ctypes

        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


def _select_monitor_for_point(
    monitors: list[MonitorBounds], x: int, y: int
) -> MonitorBounds | None:
    """Select the containing monitor, or the nearest monitor as fallback."""
    if not monitors:
        return None
    for monitor in monitors:
        if monitor.left <= x < monitor.right and monitor.top <= y < monitor.bottom:
            return monitor

    def distance_squared(monitor: MonitorBounds) -> int:
        nearest_x = min(max(x, monitor.left), monitor.right - 1)
        nearest_y = min(max(y, monitor.top), monitor.bottom - 1)
        return (x - nearest_x) ** 2 + (y - nearest_y) ** 2

    return min(monitors, key=distance_squared)


def _enumerate_monitors() -> list[MonitorBounds]:
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[MonitorBounds] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def callback(handle, _hdc, rect_ptr, _data):
        rect = rect_ptr.contents
        device_name = ""
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            device_name = str(info.szDevice)
        monitors.append(
            MonitorBounds(
                index=len(monitors) + 1,
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                device_name=device_name,
            )
        )
        return True

    callback_ref = callback_type(callback)
    if not ctypes.windll.user32.EnumDisplayMonitors(
        None, None, callback_ref, 0
    ):
        return []
    return monitors


def get_cursor_monitor() -> MonitorBounds | None:
    """Return physical-pixel bounds for the monitor under the cursor."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return None
    return _select_monitor_for_point(_enumerate_monitors(), point.x, point.y)


def _tasklist_image_name(pid: int) -> str:
    if os.name != "nt" or pid <= 0:
        return ""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for row in csv.reader(io.StringIO(output)):
        if len(row) >= 2 and row[1].replace(",", "").strip() == str(pid):
            return row[0].strip()
    return ""


def is_pet_process_alive(pid: int) -> bool:
    """Return true only when PID belongs to Miru's packaged or dev pet."""
    image_name = _tasklist_image_name(pid).lower()
    return image_name in {"miru-pet.exe", "app.exe"}


def taskkill_process_tree(pid: int) -> None:
    """Force-kill a Windows process tree and swallow taskkill errors."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.call(
        ["taskkill", "/PID", str(pid), "/F", "/T"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

