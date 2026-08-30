"""Legacy backend URL resolver.

Miru's current private-server release no longer uses a central discovery
endpoint. DMG/APK login parses the server segment inside the long invitation
code and connects directly to that private server.

This module remains only for old Python client entry points that still import
``resolve_server_url``. It intentionally performs no network request. If a
cached server URL exists, return it; otherwise fall back immediately to the
old bundled hosted URL.
"""
from __future__ import annotations

from typing import Optional

# Last-resort fallback for legacy clients that do not have a cached URL.
BUNDLED_FALLBACK_URL = "https://mirulife.top"


def resolve_server_url(
    cached: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> str:
    """Return the backend URL a legacy client should connect to.

    Args:
        cached: A previously-stored backend URL (e.g. from on-disk config).
                If non-empty and `force_refresh` is False, returned as-is.
        force_refresh: Legacy compatibility flag. Since the discovery endpoint
                       is retired, this now only means "ignore cached and use
                       the bundled fallback".

    Returns:
        A backend URL string (always non-empty; falls back to bundled
        default if everything else fails).
    """
    if cached and not force_refresh:
        return cached.rstrip("/")
    return BUNDLED_FALLBACK_URL.rstrip("/")
