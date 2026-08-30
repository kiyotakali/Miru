#!/usr/bin/env python3
"""Run Miru's Windows screen-capture path in the interactive desktop session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--save-preview", type=Path)
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("windows_desktop_probe.py must run on Windows")

    from sensor import ScreenSensor
    from windows.platform import (
        _enumerate_monitors,
        get_cursor_monitor,
        set_process_dpi_awareness,
    )

    set_process_dpi_awareness()
    monitors = _enumerate_monitors()
    cursor_monitor = get_cursor_monitor()
    sensor = ScreenSensor()
    image = sensor._capture_screenshot()

    result = {
        "ok": image is not None,
        "monitors": [
            {
                "index": monitor.index,
                "bbox": monitor.bbox,
                "device_name": monitor.device_name,
            }
            for monitor in monitors
        ],
        "cursor_monitor": cursor_monitor.index if cursor_monitor else None,
        "capture_size": list(image.size) if image is not None else None,
        "capture_display_index": sensor._last_capture_display_index,
        "capture_display_name": sensor._last_capture_display_name,
        "error": sensor._last_capture_error,
    }

    if image is not None and args.save_preview:
        args.save_preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.save_preview, format="PNG")
        result["preview"] = str(args.save_preview)

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
