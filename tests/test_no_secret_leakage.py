"""Repo-level secret-leakage tests.

Two layers:

  1. Black-box: run scripts/scan_secrets.sh against the source tree and
     require exit 0. This is the same scanner used in build_mac.sh and
     APK build, so passing here = passing release gate.

     2. Targeted: check specific files / patterns to lock invariants the
     scanner can't easily express (e.g. "private-server clients must not
     fall back to the old production discovery URL").
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_SCRIPT = REPO_ROOT / "scripts" / "scan_secrets.sh"


def test_docker_context_excludes_maintainer_and_desktop_only_files():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        "docs/",
        "AGENTS.md",
        "CLAUDE.md",
        "deploy/update_vps.sh",
        "deploy/upload_release.sh",
        "scripts/release_real_api_smoke.py",
        "windows/",
        "windows_launcher.py",
        "packaging/",
        "artifacts/",
        "**/__pycache__/",
        "**/*.pyc",
    } <= patterns


class ScanScriptTests(unittest.TestCase):
    """End-to-end test: the live scanner must come back clean on the
    current source tree. If you intentionally introduce a new
    operator-only string (e.g. a new deploy script), add it to the
    scanner's allowlist; do NOT skip this test."""

    @unittest.skipUnless(SCAN_SCRIPT.exists(), "scan_secrets.sh missing")
    @unittest.skipIf(os.name == "nt", "bash release scanner is validated on macOS/Linux")
    def test_scan_script_returns_clean_on_source(self):
        result = subprocess.run(
            ["bash", str(SCAN_SCRIPT), str(REPO_ROOT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            self.fail(
                "scan_secrets.sh detected violations in source tree:\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )


class ClientCodeStringInvariants(unittest.TestCase):
    """Private-server v1 clients must route through the invitation code.

    The server segment in ``MIRU-XXXXXXXXXX-YYYYYY`` is the source of truth.
    Client login paths should not silently fall back to mirulife.top or the
    old discovery resolver.
    """

    def _grep_count(self, path: str, pattern: str) -> int:
        full = REPO_ROOT / path
        if not full.exists():
            return 0
        text = full.read_text(encoding="utf-8", errors="ignore")
        return len(re.findall(pattern, text))

    def test_legacy_discovery_module_is_fallback_only(self):
        """The legacy discovery module may exist for old experiments, but it
        must not depend on the retired discover.mirulife.top endpoint."""
        path = REPO_ROOT / "discovery.py"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("BUNDLED_FALLBACK_URL", text)
        self.assertNotIn("DISCOVERY_URL", text)
        self.assertNotIn("discover.mirulife.top", text)
        self.assertNotIn("lookup.json", text)

    def test_no_hardcoded_mirulife_in_python_client_paths(self):
        """Python client entry points must not hardcode the old hosted server."""
        forbidden = re.compile(r'["\']https://mirulife\.top["\']')
        client_paths = [
            "miru_launcher.py",
            "client.py",
            "client_app.py",
        ]
        for rel in client_paths:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            matches = forbidden.findall(text)
            self.assertEqual(
                matches, [],
                f"{rel}: found hardcoded mirulife.top literal — private-server "
                f"clients must route through the invitation server segment. "
                f"Matches: {matches}"
            )

    def test_app_py_client_mode_uses_invitation_server_segment(self):
        """app.py client-mode login decodes the full invitation code and
        stores the returned private server URL. It must not call discovery."""
        text = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn(
            '_DEFAULT_CLIENT_SERVER_URL = "https://mirulife.top"', text,
            "app.py still hardcodes the old production server"
        )
        self.assertIn("server_url_from_invitation_code", text)
        self.assertNotIn("_default_client_server_url", text)
        self.assertNotIn("resolve_server_url", text)
        self.assertNotIn("from discovery import", text)
        forbidden = re.compile(r'["\']https://mirulife\.top["\']')
        matches = forbidden.findall(text)
        self.assertEqual(
            matches, [],
            f"app.py: hardcoded mirulife.top literal regression — "
            f"client login must use the invitation server segment. "
            f"Matches: {matches}"
        )

    def test_apk_index_html_uses_invitation_server_segment(self):
        """APK login must use the server segment in the long invitation code.

        Private-server release login should not fall back to a bundled
        production domain or discovery resolver.
        """
        path = REPO_ROOT / "miru-mobile" / "www" / "index.html"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("decodeServerSegment", text)
        self.assertIn("OBFUSCATION_MASK", text)
        self.assertIn("'http://' + ip + ':' + port", text)
        self.assertNotIn("BUNDLED_FALLBACK_URL", text)
        self.assertNotIn("DISCOVERY_URL", text)
        self.assertNotIn("DEFAULT_SERVER_URL", text)
        self.assertNotIn("_resolveDefaultServerUrl", text)


class PrivacyHtmlTests(unittest.TestCase):
    """The old proxy hostname was removed; verify the privacy notice
    reads sensibly without it."""

    def test_qingyuntop_removed_from_privacy(self):
        path = REPO_ROOT / "templates" / "privacy.html"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("qingyuntop", text,
                         "Old LLM proxy hostname must be removed from privacy notice")
        # Must still mention LLM usage in some generic way (transparency):
        self.assertTrue(
            "LLM" in text or "VLM" in text or "大模型" in text,
            "Privacy notice should still describe LLM / VLM usage generically"
        )


if __name__ == "__main__":
    unittest.main()
