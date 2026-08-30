import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "templates" / "index.html"


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"missing JavaScript function: {name}"
    brace = source.find("{", match.end())
    assert brace >= 0
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace, len(source)):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            block_comment = True
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
def test_journal_detail_back_preserves_the_list_parent():
    html = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _function_source(html, name)
        for name in (
            "showDetailView",
            "handleDetailBack",
            "showMiruJournalList",
            "navMiruJournal",
            "viewJournalDetail",
        )
    )

    harness = r"""
let currentView = 'chat';
let detailBackTarget = 'chat';
let journalListBackTarget = 'chat';
let _journalEntriesCache = [];
let _journalCurrentDate = null;
let _emotionCalMonth = '';
let _isMobile = false;
let detailBackBtn = { textContent: '' };
let companionChatView = { style: {} };
let detailView = { style: {}, innerHTML: '' };
let detailContent = { innerHTML: '' };
let calls = [];
let document = { getElementById: function() { return null; } };
function setSidebarActive(value) { calls.push('sidebar:' + value); }
function _nextNavGen() {}
function escapeHtml(value) { return String(value); }
function buildJournalListHtml() { return '<journal-list>'; }
function buildJournalBookHtml() { return '<journal-book>'; }
function cachedFetch(url, key, callback) {
  if (url === '/api/journal') {
    callback([{ date: '2000-01-01' }, { date: '1999-12-31' }]);
  } else {
    callback({ date: url.split('/').pop(), narrative: 'entry' });
  }
}
function navMemoryBrowser() { calls.push('memory'); }
function navEmotionCurve(month) { calls.push('emotion:' + month); }
function showCompanionChat() { calls.push('chat'); }
""" + functions + r"""

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function run() {
  await navMiruJournal('memoryBrowser');
  assert(journalListBackTarget === 'memoryBrowser', 'memory parent was not recorded');
  assert(detailBackTarget === 'memoryBrowser', 'journal list did not point back to memory');
  await viewJournalDetail('2000-01-01');
  assert(detailBackTarget === 'miruJournalList', 'detail did not point back to list');
  assert(journalListBackTarget === 'memoryBrowser', 'detail changed the list parent');
  await handleDetailBack();
  assert(detailBackTarget === 'memoryBrowser', 'detail -> list lost memory parent');
  await handleDetailBack();
  assert(calls[calls.length - 1] === 'memory', 'list did not return to memory');

  await navMiruJournal();
  assert(journalListBackTarget === 'chat', 'direct entry did not establish chat parent');
  await viewJournalDetail('1999-12-31');
  await handleDetailBack();
  assert(detailBackTarget === 'chat', 'detail -> list lost chat parent');
  await handleDetailBack();
  assert(calls[calls.length - 1] === 'chat', 'direct journal did not return to chat');

  await navMiruJournal('memoryBrowser');
  await viewJournalDetail('2000-01-01');
  await viewJournalDetail('1999-12-31');
  assert(journalListBackTarget === 'memoryBrowser', 'prev/next detail changed parent');
  console.log(JSON.stringify({ ok: true, calls: calls }));
}
run().catch(function(error) {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""

    proc = subprocess.run(
        ["node"],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_journal_detail_back_uses_preserving_renderer():
    html = INDEX_HTML.read_text(encoding="utf-8")
    body = _function_source(html, "handleDetailBack")
    assert "return showMiruJournalList()" in body
    assert "navMiruJournal()" not in body
