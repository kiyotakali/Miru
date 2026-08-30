from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dmg_wkwebview_file_input_has_native_open_panel_delegate():
    launcher = (ROOT / "miru_launcher.py").read_text(encoding="utf-8")

    assert "NSOpenPanel" in launcher
    assert "webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_" in launcher
    assert "setUIDelegate_(controller)" in launcher


def test_self_server_image_file_input_does_not_restrict_archive_extensions():
    html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")

    # WKWebView + macOS file picker may disable .tar.gz/.tgz when accept
    # contains MIME/compound-extension filters. The backend already validates
    # extension and size, so the first-run wizard should let users choose the
    # downloaded Miru service package freely.
    assert '<input id="selfImageTar" class="file-input" type="file">' in html
    assert 'id="selfImageTar" class="file-input" type="file" accept=' not in html
