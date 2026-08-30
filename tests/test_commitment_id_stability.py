"""Regression test for commitment cid stability across processes.

Before the fix, `core._parse_commitment_line` used Python's built-in `hash()`
which is seeded per-process (PYTHONHASHSEED=random). Every VPS restart shifted
every commitment's cid, so frontend PUT /api/commitments/{cid} 404'd after
restart and the UI appeared to "revert" completions.

The fix uses hashlib.md5 which is deterministic. This test runs the parser in
a subprocess with a different PYTHONHASHSEED and asserts the cid is identical
— which is exactly what the old code failed.
"""

import os
import subprocess
import sys
import textwrap


SAMPLE_LINE = "- [ ] 写周报 (deadline: 2026-04-25)  [added: 2026-04-22 19:30]"


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_in_subprocess(hashseed: str) -> str:
    """Run _parse_commitment_line in a fresh Python process with PYTHONHASHSEED."""
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {REPO_ROOT!r})
        import core
        item = core._parse_commitment_line({SAMPLE_LINE!r})
        print(item["id"])
    """)
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = hashseed
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    # Some imports print startup banners (e.g. server_config VAPID keygen);
    # the cid is always the last non-empty line we print.
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return lines[-1].strip()


def test_cid_is_stable_across_processes():
    """Same input → same cid, regardless of PYTHONHASHSEED."""
    cid_a = _parse_in_subprocess("0")
    cid_b = _parse_in_subprocess("12345")
    cid_c = _parse_in_subprocess("99999")
    assert cid_a == cid_b == cid_c, (
        f"cid drifted across processes: {cid_a}, {cid_b}, {cid_c} — "
        "hashlib fix is not in place"
    )


def test_cid_format_unchanged():
    """The cid format stays 'c_<ts>_<4hex>' or 'c_<10hex>' so the frontend
    regex / fixtures don't need updating."""
    import core
    item = core._parse_commitment_line(SAMPLE_LINE)
    assert item is not None
    cid = item["id"]
    assert cid.startswith("c_")
    # Either c_<timestamp-digits>_<4 digits> or c_<10 hex chars>
    tail = cid[2:]
    assert "_" in tail or len(tail) == 10
