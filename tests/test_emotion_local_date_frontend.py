import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "templates" / "index.html"


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", source)
    assert match, f"missing JavaScript function: {name}"
    brace = source.find("{", match.end())
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_local_date_key_uses_device_calendar_day_not_utc_day():
    html = INDEX_HTML.read_text(encoding="utf-8")
    helper = _function_source(html, "_miruLocalDateKey")
    harness = (
        helper
        + "\nconst result = _miruLocalDateKey(new Date('2026-08-26T16:30:00Z'));"
        + "\nconsole.log(JSON.stringify({result}));"
    )
    env = dict(os.environ, TZ="Asia/Shanghai")
    proc = subprocess.run(
        ["node"],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"result": "2026-08-27"}


def test_emotion_views_and_cache_use_the_same_local_date_key():
    html = INDEX_HTML.read_text(encoding="utf-8")
    calendar = _function_source(html, "_buildEmotionCalendarHtml")
    curve = _function_source(html, "buildEmotionCurveHtml")

    assert "var today = _miruLocalDateKey();" in calendar
    assert "const todayStr = _miruLocalDateKey();" in curve
    assert "'emotion_' + _miruLocalDateKey()" in html
    assert "'emotion_month_' + _miruLocalDateKey().slice(0,7)" in html
    assert "new Date().toISOString().slice(0, 10)" not in calendar
    assert "new Date().toISOString().slice(0, 10)" not in curve
