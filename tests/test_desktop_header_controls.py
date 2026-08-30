from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "templates" / "index.html"


def _index() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _css_block(html: str, selector: str) -> str:
    start = html.index(selector)
    body_start = html.index("{", start) + 1
    body_end = html.index("}", body_start)
    return html[body_start:body_end]


def test_desktop_header_exit_and_refresh_are_not_visible_actions():
    html = _index()

    assert '<button class="header-refresh-btn" id="refreshBtn"' in html
    assert '<button class="header-exit-btn" id="exitBtn"' in html

    refresh_css = _css_block(html, ".header-refresh-btn")
    exit_css = _css_block(html, ".header-exit-btn")
    assert "display: none" in refresh_css
    assert "display: none" in exit_css


def test_refresh_all_tolerates_hidden_or_removed_button():
    html = _index()

    assert "if (btn) btn.classList.add('spinning');" in html
    assert "if (btn) setTimeout(function() { btn.classList.remove('spinning'); }, 800);" in html
