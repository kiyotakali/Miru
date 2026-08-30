from pathlib import Path
import os
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="macOS acceptance launcher is validated on macOS",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "launch_mac_miru_for_acceptance.sh"


def test_acceptance_launcher_is_syntax_valid_and_never_opens_new_instance():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'open "$APP_PATH"' in text
    assert "open -n" not in text
    assert "osascript" in text
    assert "pgrep -f" in text
