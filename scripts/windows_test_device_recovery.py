"""Acceptance check for native Windows local-device self-recovery."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_paths import launcher_config_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=75.0)
    args = parser.parse_args()

    config = json.loads(launcher_config_path().read_text(encoding="utf-8"))
    token = str(config.get("auth_token") or "")
    if not token:
        raise RuntimeError("The installed Miru client is not logged in")
    headers = {"Authorization": f"Bearer {token}"}
    base_url = "http://127.0.0.1:5001"

    def devices() -> list[dict]:
        response = requests.get(
            f"{base_url}/api/device/list", headers=headers, timeout=5
        )
        response.raise_for_status()
        return response.json()

    before = devices()
    desktop = next(
        (
            item
            for item in before
            if item.get("type") == "desktop"
            and item.get("platform") in {"win32", "windows"}
        ),
        None,
    )
    if desktop is None:
        raise RuntimeError("No registered Windows desktop was found")
    device_id = desktop["device_id"]
    before_ids = {item.get("device_id") for item in before}

    deleted = requests.delete(
        f"{base_url}/api/device/{device_id}", headers=headers, timeout=5
    )
    deleted.raise_for_status()
    absent_immediately = device_id not in {
        item.get("device_id") for item in devices()
    }

    deadline = time.monotonic() + args.timeout
    restored_after = None
    after: list[dict] = []
    while time.monotonic() < deadline:
        after = devices()
        matching = [item for item in after if item.get("device_id") == device_id]
        if len(matching) == 1:
            restored_after = args.timeout - max(0.0, deadline - time.monotonic())
            break
        time.sleep(2)

    after_ids = {item.get("device_id") for item in after}
    result = {
        "ok": restored_after is not None and absent_immediately,
        "device_id": device_id,
        "absent_immediately": absent_immediately,
        "restored_same_id": restored_after is not None,
        "restored_after_seconds": (
            round(restored_after, 1) if restored_after is not None else None
        ),
        "duplicate_count": sum(
            item.get("device_id") == device_id for item in after
        ),
        "unexpected_new_ids": sorted(after_ids - before_ids),
        "before_count": len(before),
        "after_count": len(after),
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] and result["duplicate_count"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
