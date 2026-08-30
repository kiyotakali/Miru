"""Tests for the retired discovery resolver.

Current DMG/APK releases do not use central discovery. The remaining Python
module is a compatibility shim for old client entry points: cached URL first,
otherwise immediate bundled fallback, with no network call.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import discovery


class ResolveServerURLTests(unittest.TestCase):
    def test_cached_url_returned_directly(self):
        with patch("urllib.request.urlopen") as mock:
            url = discovery.resolve_server_url(cached="https://my-vps.example.com")
        self.assertEqual(url, "https://my-vps.example.com")
        self.assertFalse(mock.called, "cached path must not call any network endpoint")

    def test_cached_url_trailing_slash_stripped(self):
        url = discovery.resolve_server_url(cached="https://my-vps.example.com/")
        self.assertEqual(url, "https://my-vps.example.com")

    def test_no_cached_url_uses_bundled_fallback_without_network(self):
        with patch("urllib.request.urlopen") as mock:
            url = discovery.resolve_server_url(cached=None)
        self.assertEqual(url, discovery.BUNDLED_FALLBACK_URL.rstrip("/"))
        self.assertFalse(mock.called, "retired discovery shim must not call network")

    def test_force_refresh_uses_fallback_without_network(self):
        with patch("urllib.request.urlopen") as mock:
            url = discovery.resolve_server_url(
                cached="https://stale-vps.example.com",
                force_refresh=True,
            )
        self.assertEqual(url, discovery.BUNDLED_FALLBACK_URL.rstrip("/"))
        self.assertFalse(mock.called, "force_refresh no longer calls discovery")

    def test_bundled_fallback_is_https(self):
        self.assertTrue(discovery.BUNDLED_FALLBACK_URL.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
