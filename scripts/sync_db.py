#!/usr/bin/env python3
"""Run optional BYODB sync from command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sync_backend import get_sync_backend


def main() -> int:
    parser = argparse.ArgumentParser(description="ContextLife DB sync helper")
    parser.add_argument(
        "--mode",
        default="bidirectional",
        choices=["push", "pull", "bidirectional"],
        help="Sync direction",
    )
    parser.add_argument(
        "--no-uploads",
        action="store_true",
        help="Skip uploads sync",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only print sync backend status",
    )
    args = parser.parse_args()

    backend = get_sync_backend()

    if args.status:
        print(json.dumps(backend.status(), ensure_ascii=False, indent=2))
        return 0

    result = backend.run(mode=args.mode, include_uploads=not args.no_uploads)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
