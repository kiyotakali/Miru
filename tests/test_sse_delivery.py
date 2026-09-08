import json
from pathlib import Path
import queue
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import sse


def test_idle_stream_heartbeat_reaches_eventsource_onmessage(monkeypatch):
    clock = [0]
    monkeypatch.setattr(sse, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class IdleQueue:
        def get(self, timeout):
            clock[0] += timeout
            raise queue.Empty

    stream = sse.stream_generator(IdleQueue())
    try:
        assert next(stream).startswith("event: connected\n")
        heartbeat = next(stream)
        # Comments are invisible to EventSource; a default data event invokes
        # the existing onmessage handler that resets the browser watchdog.
        assert heartbeat.startswith("data: ")
        assert json.loads(heartbeat.removeprefix("data: ").strip()) == {}
    finally:
        stream.close()


def test_heartbeat_is_not_starved_by_named_events(monkeypatch):
    clock = [0]
    monkeypatch.setattr(sse, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class BusyQueue:
        def get(self, timeout):
            clock[0] += 10
            return 'event: chat_message\ndata: {}\n\n'

    stream = sse.stream_generator(BusyQueue())
    try:
        assert next(stream).startswith("event: connected\n")
        for _ in range(3):
            assert next(stream).startswith("event: chat_message\n")
        assert next(stream) == "data: {}\n\n"
    finally:
        stream.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("role,scroll_top,expected_scroll", [
    ("assistant", 600, True),
    ("user", 600, True),
    ("assistant", 100, False),
])
def test_live_message_preserves_scroll_intent(role, scroll_top, expected_scroll):
    source = (Path(__file__).resolve().parents[1] / "templates/index.html").read_text(encoding="utf-8")
    start = source.index("function _sseAppendMessage(msg) {")
    end = source.index("\nfunction _getDeviceId()", start)
    harness = """
const assert = require('assert');
const companionChatMessages = {scrollHeight: 1000, scrollTop: SCROLL_TOP, clientHeight: 400};
const emptyCompanionState = {style: {}};
let scrolled = false, badge = false;
function addCompanionReplyBubble() { companionChatMessages.scrollHeight += 500; }
function addCompanionUserBubble() { companionChatMessages.scrollHeight += 500; }
function scrollCompanionChatToBottom() { scrolled = true; }
function _showCompanionNewMsgBadge(value) { badge = value; }
function _assetUrl(value) { return value; }
""".replace("SCROLL_TOP", str(scroll_top))
    # Append the production function unchanged after configuring the fixture.
    harness += source[start:end]
    harness += "\n_sseAppendMessage(" + json.dumps({"role": role, "text": "long reply"}) + ");\n"
    harness += "assert.strictEqual(scrolled, " + json.dumps(expected_scroll) + ");\n"
    harness += "assert.strictEqual(badge, " + json.dumps(not expected_scroll) + ");\n"
    result = subprocess.run(["node"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
