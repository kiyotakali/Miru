#!/usr/bin/env python3
"""Set the target web URL for the Tauri cross-platform shell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "tauri-shell" / "web" / "config.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Update shell web_url in tauri-shell/web/config.json")
    parser.add_argument("--url", required=True, help="Target URL, e.g. http://127.0.0.1:5001")
    parser.add_argument(
        "--fallback",
        action="append",
        default=[],
        help="Optional fallback URL (can be passed multiple times)",
    )
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        raise SystemExit(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    data["web_url"] = args.url
    if args.fallback:
        data["fallback_urls"] = args.fallback
    else:
        data.setdefault("fallback_urls", [])
    data.setdefault("replace_history", True)

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[shell-url] web_url set to: {data['web_url']}")
    if data.get("fallback_urls"):
        print(f"[shell-url] fallback_urls: {data['fallback_urls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
