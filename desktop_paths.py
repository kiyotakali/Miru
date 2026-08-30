"""Platform-specific persistent paths for Miru desktop clients."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


def app_support_dir(
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the per-user persistent root used by the desktop client."""
    platform_name = platform_name or sys.platform
    environ = environ if environ is not None else os.environ
    home = Path(home) if home is not None else Path.home()

    if platform_name == "win32":
        local_app_data = environ.get("LOCALAPPDATA") or environ.get("APPDATA")
        if local_app_data:
            return Path(local_app_data) / "Miru"
        return home / "AppData" / "Local" / "Miru"
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "Miru"

    xdg_data_home = environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "Miru"
    return home / ".local" / "share" / "Miru"


def launcher_config_path() -> Path:
    return app_support_dir() / "config.json"


def data_dir() -> Path:
    return app_support_dir() / "data"


def webview_storage_dir() -> Path:
    return app_support_dir() / "webview"
